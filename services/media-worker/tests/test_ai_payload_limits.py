"""Offline tests for multimodal payload limits, preflight size checks and
provider rate-limit handling (no network, no credentials, no inference)."""

from __future__ import annotations

import base64
import io
import json
import random

import pytest

pytestmark = pytest.mark.usefixtures("isolated_provider_unit")
import requests
from PIL import Image

from app.ai.payload import convert_for_payload
from app.ai.provider import (
    AIProviderConfig,
    ImagePayload,
    OpenAICompatibleProvider,
    RateLimitedError,
    RequestTooLargeError,
    UsageTracker,
)


def _config(**overrides):
    values = dict(
        provider="openai_compatible",
        model="vision-model",
        endpoint="https://api.example.test/v1/chat/completions",
        api_key="fake-key-for-tests",
        timeout_s=20,
        max_retries=1,
        max_image_dim=512,
        max_image_bytes=300_000,
        max_request_bytes=1_000_000,
        min_request_spacing_s=0.0,
    )
    values.update(overrides)
    return AIProviderConfig(**values)


def _provider(transport, *, cfg=None, sleeps=None):
    config = cfg or _config()
    return OpenAICompatibleProvider(
        config, sleep=(sleeps.append if sleeps is not None else lambda _: None), transport=transport
    )


def _noisy_png(path, size=(2000, 1500)):
    im = Image.new("RGB", size)
    px = im.load()
    rng = random.Random(7)
    for y in range(size[1]):
        for x in range(size[0]):
            if x % 7 == 0 and y % 7 == 0:
                px[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    im.save(path, format="PNG")


def test_512px_resize_and_jpeg_size_target(tmp_path):
    src = tmp_path / "noisy.png"
    _noisy_png(src)
    data, mime = convert_for_payload(src, max_dim=512, max_bytes=300_000)
    assert mime == "image/jpeg"
    assert len(data) <= 300_000
    with Image.open(io.BytesIO(data)) as converted:
        assert max(converted.size) <= 512
    with Image.open(src) as original:
        assert original.size == (2000, 1500)  # original untouched


def test_orientation_applied_on_copy_not_original(tmp_path):
    src = tmp_path / "rotated.jpg"
    im = Image.new("RGB", (600, 300), (200, 60, 60))
    exif = im.getexif()
    exif[274] = 6  # rotate 90
    im.save(src, format="JPEG", exif=exif)
    data, _ = convert_for_payload(src, max_dim=512, max_bytes=300_000)
    with Image.open(io.BytesIO(data)) as converted:
        assert converted.width < converted.height  # upright after orientation
    with Image.open(src) as original:
        assert original.getexif().get(274) == 6  # original EXIF untouched


def test_base64_expansion_counted_in_preflight():
    cfg = _config(max_request_bytes=10_000)
    payload = ImagePayload(asset_id="x", mime="image/png", data=b"a" * 9000)
    calls = {"n": 0}

    def transport(request):
        calls["n"] += 1
        return {"choices": [{"message": {"content": "ok"}}]}

    provider = _provider(transport, cfg=cfg)
    with pytest.raises(RequestTooLargeError, match="exceeds local cap"):
        provider.complete("prompt with json request", [payload], UsageTracker(cfg))
    assert calls["n"] == 0  # rejected locally, no HTTP call


def test_full_request_size_validation_accepts_small_payload():
    cfg = _config(max_request_bytes=1_000_000)
    seen = {}

    def transport(request):
        seen["body"] = request["body"]
        return {"choices": [{"message": {"content": "ok"}}]}

    provider = _provider(transport, cfg=cfg)
    provider.complete("json prompt", [ImagePayload("x", "image/png", b"tiny")], UsageTracker(cfg))
    content = seen["body"]["messages"][0]["content"]
    urls = [c["image_url"]["url"] for c in content if c.get("type") == "image_url"]
    assert urls and urls[0].startswith("data:image/png;base64,")
    assert base64.b64decode(urls[0].split(",", 1)[1]) == b"tiny"


def _http_error(code, body="", headers=None):
    resp = requests.Response()
    resp.status_code = code
    resp._content = body.encode("utf-8")
    resp.headers.update(headers or {})
    return requests.HTTPError(f"HTTP {code}", response=resp)


def test_413_is_never_retried():
    calls = {"n": 0}

    def transport(request):
        calls["n"] += 1
        raise _http_error(413, "payload too large")

    cfg = _config(max_retries=3)
    provider = _provider(transport, cfg=cfg)
    with pytest.raises(RuntimeError, match="HTTP 413"):
        provider.complete("json prompt", [], UsageTracker(cfg))
    assert calls["n"] == 1


def test_429_retry_after_is_honored():
    calls = {"n": 0}
    sleeps: list[float] = []

    def transport(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, "rate limited", {"retry-after": "7", "x-ratelimit-remaining-requests": "0"})
        return {"choices": [{"message": {"content": "ok"}}]}

    cfg = _config(max_retries=1)
    tracker = UsageTracker(cfg)
    provider = _provider(transport, cfg=cfg, sleeps=sleeps)
    result = provider.complete("json prompt", [], tracker)
    assert result.text == "ok"
    assert sleeps == [7.0]
    assert tracker.usage.rate_limit_snapshot["x-ratelimit-remaining-requests"] == "0"


def test_quota_exhaustion_stops_with_rate_limited_error():
    calls = {"n": 0}

    def transport(request):
        calls["n"] += 1
        raise _http_error(429, "daily quota exhausted for this model")

    cfg = _config(max_retries=3)
    provider = _provider(transport, cfg=cfg)
    with pytest.raises(RateLimitedError, match="quota exhausted"):
        provider.complete("json prompt", [], UsageTracker(cfg))
    assert calls["n"] == 1  # no retries against an exhausted quota


def test_request_pacing_spaces_calls():
    sleeps: list[float] = []
    calls = {"n": 0}

    def transport(request):
        calls["n"] += 1
        return {"choices": [{"message": {"content": "ok"}}]}

    cfg = _config(min_request_spacing_s=2.0)
    provider = _provider(transport, cfg=cfg, sleeps=sleeps)
    provider.complete("json prompt", [], UsageTracker(cfg))
    provider.complete("json prompt", [], UsageTracker(cfg))
    # second call is spaced (first sees a huge negative delta -> no sleep)
    assert any(s > 0 for s in sleeps)


def test_config_defaults_are_conservative():
    cfg = AIProviderConfig.from_env(env={})
    assert cfg.max_image_dim == 512
    assert cfg.max_image_bytes == 300_000
    assert cfg.max_request_bytes == 1_000_000
    assert cfg.min_request_spacing_s == 2.0
    assert cfg.batch_size == 3
