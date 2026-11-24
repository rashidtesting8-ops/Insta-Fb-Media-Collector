"""Unit tests for the `requests`-based AI provider HTTP transport.

All transports are injected: no network, no credentials, no inference.
Covers success, 401/403/429/5xx, timeouts, malformed responses, retry
backoff, TLS verification, and secret scrubbing.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("isolated_provider_unit")
import requests

from app.ai.provider import (
    AIProviderConfig,
    CompletionResult,
    ImagePayload,
    OpenAICompatibleProvider,
    UsageTracker,
    scrub_secrets,
)


def _config(**overrides):
    values = dict(
        provider="openai_compatible",
        model="vision-model",
        endpoint="https://api.example.test/v1/chat/completions",
        api_key="fake-key-for-tests-only",
        timeout_s=30,
        max_retries=2,
    )
    values.update(overrides)
    return AIProviderConfig(**values)


def _provider(transport, *, config=None, sleeps=None):
    cfg = config or _config()
    return OpenAICompatibleProvider(cfg, sleep=(sleeps.append if sleeps is not None else lambda _: None), transport=transport)


def _image():
    return [ImagePayload(asset_id="web_0001", mime="image/png", data=b"\x89PNG test bytes")]


def _http_error(code: int, body: str = "denied") -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = code
    resp._content = body.encode("utf-8")
    resp.url = "https://api.example.test/v1/chat/completions"
    return requests.HTTPError(f"HTTP {code}", response=resp)


def _ok_response():
    return {
        "choices": [{"message": {"content": '{"mode":"ANALYZE_BATCH","items":[]}'}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 80},
    }


def test_http_post_uses_requests_with_tls_verify_and_no_ua_override(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return FakeResponse()

    monkeypatch.setattr("app.ai.provider.requests.post", fake_post)
    result = OpenAICompatibleProvider._http_post(
        {"url": "https://api.groq.com/openai/v1/chat/completions", "headers": {"Authorization": "Bearer fake"},
         "body": {"model": "m"}, "timeout_s": 30}
    )
    assert result == {"ok": True}
    assert captured["verify"] is True
    assert captured["allow_redirects"] is False
    assert captured["timeout"] == 30
    assert captured["json"] == {"model": "m"}
    assert "user-agent" not in {k.lower() for k in captured["headers"]}


def test_complete_success_tracks_usage():
    cfg = _config()
    tracker = UsageTracker(cfg)
    provider = _provider(lambda request: _ok_response(), config=cfg)
    result = provider.complete("prompt", _image(), tracker)
    assert result.text.startswith('{"mode"')
    assert result.usage["input_tokens"] == 120
    assert tracker.usage.requests == 1
    assert tracker.usage.input_tokens == 120


def test_401_and_403_fail_without_retry():
    for code in (401, 403):
        calls = {"n": 0}
        sleeps: list[float] = []

        def transport(request, code=code, calls=calls):
            calls["n"] += 1
            raise _http_error(code, "access denied")

        provider = _provider(transport, sleeps=sleeps)
        with pytest.raises(RuntimeError, match=f"HTTP {code}"):
            provider.complete("prompt", [], UsageTracker(_config()))
        assert calls["n"] == 1
        assert sleeps == []


def test_429_retries_exponentially_then_succeeds():
    calls = {"n": 0}
    sleeps: list[float] = []

    def transport(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _http_error(429, "rate limited")
        return _ok_response()

    provider = _provider(transport, sleeps=sleeps)
    result = provider.complete("p", [], UsageTracker(_config()))
    assert result.text.startswith('{"mode"')
    assert calls["n"] == 3
    # pacing adds spacing sleeps; the 429 backoff values must still appear
    assert 2.0 in sleeps and 4.0 in sleeps


def test_5xx_bounded_retry_then_failure():
    calls = {"n": 0}

    def transport(request):
        calls["n"] += 1
        raise _http_error(503, "upstream unavailable")

    provider = _provider(transport)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        provider.complete("p", [], UsageTracker(_config()))
    assert calls["n"] == 3  # initial + 2 bounded retries


def test_timeout_retries_then_fails():
    calls = {"n": 0}

    def transport(request):
        calls["n"] += 1
        raise requests.Timeout("timed out")

    provider = _provider(transport)
    with pytest.raises(RuntimeError, match="timed out"):
        provider.complete("p", [], UsageTracker(_config()))
    assert calls["n"] == 3


def test_malformed_response_fails_honestly():
    calls = {"n": 0}

    def transport(request):
        calls["n"] += 1
        return {"choices": []}  # malformed: no message content

    provider = _provider(transport)
    with pytest.raises(RuntimeError, match="empty choices"):
        provider.complete("p", [], UsageTracker(_config()))
    assert calls["n"] == 3


def test_credentials_scrubbed_from_errors():
    def transport(request):
        raise ValueError("Authorization: Bearer sk-secret1234567890abcdef failed")

    provider = _provider(transport)
    with pytest.raises(RuntimeError) as exc:
        provider.complete("p", [], UsageTracker(_config()))
    assert "sk-secret1234567890abcdef" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


def test_batch_size_default_is_vision_safe():
    cfg = AIProviderConfig.from_env(env={})
    assert cfg.batch_size == 3  # Groq vision models accept at most 3 images
    custom = AIProviderConfig.from_env(env={"AI_BATCH_SIZE": "2"})
    assert custom.batch_size == 2


def test_scrub_secrets_helper():
    assert "hunter2secretvalue" not in scrub_secrets("api_key=hunter2secretvalue")
    assert "[REDACTED]" in scrub_secrets("api_key=hunter2secretvalue")
