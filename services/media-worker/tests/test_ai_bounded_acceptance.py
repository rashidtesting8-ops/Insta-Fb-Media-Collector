"""Exercise the acceptance limits with local pixels and an injected transport."""

import io
import json
import base64

import pytest
import requests
from PIL import Image

from app.ai.payload import convert_for_payload
from app.ai.pipeline import run_ai_curation
from app.ai.provider import (
    AIProviderConfig,
    BudgetExceededError,
    ImagePayload,
    MockVisionProvider,
    OpenAICompatibleProvider,
    UsageTracker,
)
from tests.test_ai_provider_http import _ok_response
from tests.test_ai_visual_director import _img, _item, _setup_job


def config(**overrides):
    return AIProviderConfig.from_env({
        "AI_BOUNDED_ACCEPTANCE": "1",
        "AI_PROVIDER": "openai_compatible",
        "AI_MODEL": "qwen/qwen3.8-27b",
        "AI_ENDPOINT": "https://api.groq.com/openai/v1/chat/completions",
        "AI_API_KEY": "fixture-only",
        "AI_PRICE_IN_PER_1K": "0.0008",
        "AI_PRICE_OUT_PER_1K": "0.004",
        "AI_MIN_REQUEST_SPACING_S": "0",
        **overrides,
    })


def image(size=128):
    with io.BytesIO() as buffer:
        Image.new("RGB", (size, size)).save(buffer, format="JPEG")
        return ImagePayload("authorized-fixture", "image/jpeg", buffer.getvalue())


def test_caps_and_text_only_followup():
    cfg = config(AI_MAX_RETRIES="4", AI_MAX_REQUESTS_PER_JOB="40", AI_MAX_COST_USD_PER_JOB="2")
    calls = []
    provider = OpenAICompatibleProvider(cfg, transport=lambda request: calls.append(request["body"]) or _ok_response())
    tracker = UsageTracker(cfg)
    provider.complete("MODE A", [image()], tracker)
    provider.complete("MODE B", [], tracker)
    with pytest.raises(BudgetExceededError, match="2/2"):
        provider.complete("third request", [], tracker)
    assert [body["max_completion_tokens"] for body in calls] == [1200, 1800]
    assert {body["reasoning_effort"] for body in calls} == {"none"}
    assert [len(body["messages"][0]["content"]) for body in calls] == [2, 1]
    assert cfg.max_retries == 0 and cfg.max_cost_usd_per_job == 0.25
    assert tracker.usage.requests == 2
    assert tracker.usage.estimated_cost_usd == pytest.approx(0.000832)


def test_no_retry_and_failed_attempt_retains_budget():
    calls = []
    cfg = config()
    tracker = UsageTracker(cfg)

    def fail(request):
        calls.append(request)
        raise requests.Timeout("fixture timeout")

    provider = OpenAICompatibleProvider(cfg, transport=fail)
    with pytest.raises(RuntimeError, match="timeout"):
        provider.complete("MODE A", [image()], tracker)
    assert len(calls) == tracker.usage.requests == 1
    assert tracker.usage.retries == 0
    assert tracker.usage.estimated_cost_usd > 0.10


def test_cost_reservation_includes_possible_output_tokens():
    cfg = config(AI_MAX_COST_USD_PER_JOB="0.107", AI_PRICE_OUT_PER_1K="0.001")
    tracker = UsageTracker(cfg)
    provider = OpenAICompatibleProvider(cfg, transport=lambda _: pytest.fail("must not send"))
    with pytest.raises(BudgetExceededError, match="Worst-case"):
        provider.complete("MODE A", [image()], tracker)
    assert tracker.usage.requests == 0


def test_missing_usage_retains_reservation_and_fails_closed():
    cfg = config()
    tracker = UsageTracker(cfg)
    reply = _ok_response()
    del reply["usage"]
    provider = OpenAICompatibleProvider(cfg, transport=lambda _: reply)
    with pytest.raises(RuntimeError, match="provider token usage"):
        provider.complete("MODE A", [image()], tracker)
    assert tracker.usage.requests == 1
    assert tracker.usage.estimated_cost_usd > 0.10


def test_prepare_and_enforce_jpeg_limits(tmp_path):
    cfg = config(AI_MAX_IMAGE_DIM="512", AI_MAX_IMAGE_BYTES="300000")
    path = tmp_path / "source.jpg"
    path.write_bytes(image(512).data)
    data, mime = convert_for_payload(path, max_dim=cfg.max_image_dim, max_bytes=cfg.max_image_bytes)
    assert mime == "image/jpeg" and len(data) <= 100_000
    with Image.open(io.BytesIO(data)) as prepared:
        assert prepared.size == (256, 256)
    with pytest.raises(ValueError, match="byte cap"):
        convert_for_payload(path, max_dim=256, max_bytes=1)
    provider = OpenAICompatibleProvider(cfg, transport=lambda _: pytest.fail("must not send"))
    tracker = UsageTracker(cfg)
    with pytest.raises(ValueError, match="dimension cap"):
        provider.complete("MODE A", [image(257)], tracker)
    with pytest.raises(ValueError, match="byte cap"):
        provider.complete("MODE A", [ImagePayload("fixture", mime, b"X" * 100_001)], tracker)


@pytest.mark.parametrize("response", ["{}", '{"mode":"ANALYZE_BATCH","items":[]}'])
def test_pipeline_stops_on_invalid_or_empty_mode_a(tmp_path, response):
    source = tmp_path / "fixture.png"
    blob = _img(source)
    job = _setup_job(tmp_path, [_item("web_0001_fixture", "fixture.png", blob=blob)])
    (job / "fixture.png").write_bytes(blob)
    cfg = config()
    calls = []

    def transport(request):
        calls.append(request)
        reply = _ok_response()
        reply["choices"][0]["message"]["content"] = response
        return reply

    provider = OpenAICompatibleProvider(cfg, transport=transport)
    result = run_ai_curation(job, "job_ai", config=cfg, provider=provider)
    assert len(calls) == result.usage["requests"] == 1
    assert result.status == "failed"
    assert not (job / "curation.json").exists()


def test_pipeline_validates_and_keeps_rights_flag_with_fixture_transport(tmp_path):
    blob = _img(tmp_path / "fixture.png")
    asset_id = "web_0001_fixture"
    job = _setup_job(tmp_path, [_item(asset_id, "fixture.png", blob=blob)])
    (job / "fixture.png").write_bytes(blob)
    cfg = config()
    mock = MockVisionProvider()
    fixture_tracker = UsageTracker(AIProviderConfig(provider="mock"))
    requests_sent = []
    wire_sizes = []

    def transport(request):
        content = request["body"]["messages"][0]["content"]
        requests_sent.append(content)
        wire_sizes.append(len(json.dumps(request["body"]).encode("utf8")))
        payloads = [
            ImagePayload(asset_id, "image/jpeg", base64.b64decode(part["image_url"]["url"].split(",", 1)[1]))
            for part in content[1:]
        ]
        fixture = mock.complete(content[0]["text"], payloads, fixture_tracker)
        reply = _ok_response()
        reply["choices"][0]["message"]["content"] = fixture.text
        return reply

    provider = OpenAICompatibleProvider(cfg, transport=transport)
    result = run_ai_curation(job, "job_ai", config=cfg, provider=provider)
    assert result.status == "completed"
    assert [len(content) for content in requests_sent] == [2, 1]
    assert wire_sizes[1] < 4500  # Full MODE A evidence and the shared directive exceeded this.
    [mode_b_text] = requests_sent[1]
    assert mode_b_text["type"] == "text"
    assert "ANALYZE_BATCH" not in mode_b_text["text"]
    assert "score_breakdown" not in mode_b_text["text"]
    assert '"rights_status":"unverified"' in mode_b_text["text"]
    assert '"verification_status":"unverified"' in mode_b_text["text"]
    assert '"visual_weaknesses":' in mode_b_text["text"]
    assert result.usage["requests"] == 2
    saved = json.loads((job / "curation.json").read_text())
    assert saved["curation"]["selected_asset_ids"] == [asset_id]
    assert saved["curation"]["rights_review_required_ids"] == [asset_id]


def test_provider_error_details_are_structured_and_redacted():
    cfg = config()
    response = requests.Response()
    response.status_code = 429
    response.headers.update({
        "Retry-After": "7",
        "X-RateLimit-Limit-Tokens": "8000",
        "X-RateLimit-Remaining-Tokens": "50",
        "X-RateLimit-Reset-Tokens": "1m2.5s",
        "X-RateLimit-Remaining-Requests": cfg.api_key,
        "Set-Cookie": "private-value",
    })
    response._content = json.dumps({"error": {
        "code": "request_too_large",
        "type": "tokens",
        "message": f"Request too large for org_private12345. Bearer {cfg.api_key}",
    }, "debug": "raw-payload"}).encode()

    def fail(_):
        response.raise_for_status()

    tracker = UsageTracker(cfg)
    provider = OpenAICompatibleProvider(cfg, transport=fail)
    with pytest.raises(RuntimeError, match="request_too_large") as error:
        provider.complete("MODE A", [image()], tracker)
    usage = tracker.as_dict()
    assert usage["requests"] == 1
    assert usage["provider_error"] == {
        "http_status": 429, "code": "request_too_large", "type": "tokens",
        "message": "Request too large for [REDACTED]. Bearer [REDACTED]",
    }
    assert usage["rate_limit_snapshot"] == {
        "retry-after": "7", "x-ratelimit-limit-tokens": "8000",
        "x-ratelimit-remaining-tokens": "50", "x-ratelimit-reset-tokens": "1m2.5s",
    }
    for private in (cfg.api_key, "org_private12345", "raw-payload", "private-value"):
        assert private not in json.dumps(usage) + str(error.value)
