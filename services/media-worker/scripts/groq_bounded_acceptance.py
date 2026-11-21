import base64
import hashlib
import io
import json
import sys
from pathlib import Path

import requests
from PIL import Image
from app.ai import pipeline, provider
from app.ai.schemas import ModeAResponse, ModeBResponse


EXPECTED = "2e01cb8ce50473e7901d70777ef7f9578feb358d7642d2e6e78df5442c5cf986"
ASSET = "synthetic_grooming_dog_2e01cb8ce504"
JOB = "groq-bounded-synthetic-corrected"
MODEL = "qwen/qwen3.8-27b"
ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
OUT = Path("/result")
LIVE = sys.argv[1:] == ["--live"]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


require(sys.argv[1:] in (["--preflight"], ["--live"]), "Invalid runner mode")
for module in (pipeline, provider):
    require(Path(module.__file__).resolve().is_relative_to("/workspace/app"), "Source mount not loaded")
pixels = Path("/approved.jpg").read_bytes()
require(hashlib.sha256(pixels).hexdigest() == EXPECTED, "Approved JPEG hash mismatch")
with Image.open(io.BytesIO(pixels)) as image:
    image.load()
    require(image.format == "JPEG" and image.size == (256, 256) and len(pixels) <= 100_000,
            "Image exceeds approval")
key = sys.stdin.read().strip() if LIVE else "offline-placeholder"
require(bool(key), "API key missing")
config = provider.AIProviderConfig(
    provider="openai_compatible", model=MODEL, endpoint=ENDPOINT, api_key=key,
    bounded_acceptance=True, mode_a_max_output_tokens=1200, mode_b_max_output_tokens=1800,
    max_retries=0, max_requests_per_job=2, max_cost_usd_per_job=0.25,
    batch_size=1, max_image_dim=256, max_image_bytes=100_000,
    price_in_per_1k=0.0008, price_out_per_1k=0.004,
)
require(config.bounded_acceptance is True and
        (config.model, config.mode_a_max_output_tokens, config.mode_b_max_output_tokens,
         config.max_retries, config.max_requests_per_job, config.max_cost_usd_per_job)
        == (MODEL, 1200, 1800, 0, 2, 0.25), "Effective configuration mismatch")
print(json.dumps({"source": provider.__file__, "model": config.model,
                  "mode_a_cap": config.mode_a_max_output_tokens,
                  "mode_b_cap": config.mode_b_max_output_tokens, "retries": config.max_retries,
                  "max_requests": config.max_requests_per_job, "usd_ceiling": config.max_cost_usd_per_job,
                  "sha256": EXPECTED}), flush=True)
if not LIVE:
    sys.exit(0)

manifest = {"schema_version": "3.0-unified", "job_id": JOB, "assets": [{
    "asset_id": ASSET, "job_id": JOB, "platform": "local", "type": "image",
    "filename": "authorized-image.jpg", "local_path": "authorized-image.jpg",
    "source_url": "urn:sha256:" + EXPECTED, "source_page": "original local synthetic illustration",
    "mime": "image/jpeg", "bytes": len(pixels), "width": 256, "height": 256,
    "sha256": EXPECTED, "date_status": "unknown", "rights_status": "unverified",
    "extraction_status": "downloaded", "representative": True,
    "quality": {"integrity": "ok", "width": 256, "height": 256, "usable_size": True},
    "preprocessing": {"outcome": "processed"},
}]}
with (OUT / "authorized-image.jpg").open("xb") as file:
    file.write(pixels)
(OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")


def prepared_payload(candidate, *, max_dim, max_bytes):
    require(candidate.asset["asset_id"] == ASSET and candidate.kind == "image", "Unexpected asset")
    require(max_dim == 256 and max_bytes == 100_000 and candidate.payload_files[0].read_bytes() == pixels,
            "Approved image or caps changed")
    return [provider.ImagePayload(ASSET, "image/jpeg", pixels, "original synthetic illustration")]


def references(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if (k == "asset_id" or k.endswith("_asset_id")) and v is not None:
                yield v
            elif k.endswith("_ids") and isinstance(v, list):
                yield from v
            yield from references(v)
    elif isinstance(value, list):
        for v in value:
            yield from references(v)


pipeline.build_image_payloads = prepared_payload  # Use exact approved bytes.
attempts = []
tracker = provider.UsageTracker(config)


def save_attempts():
    (OUT / "attempts.json").write_text(json.dumps(attempts, indent=2), encoding="utf8")


def transport(request):
    stage = len(attempts)
    body = request["body"]
    require(stage < 2 and request["url"] == ENDPOINT and body["model"] == MODEL, "Request scope mismatch")
    require(body["max_completion_tokens"] == (1200, 1800)[stage], "Request cap mismatch")
    content = body["messages"][0]["content"]
    images = [item for item in content if item["type"] == "image_url"]
    require(len(images) == (1, 0)[stage], "Request image count mismatch")
    if images:
        sent = base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1], validate=True)
        require(hashlib.sha256(sent).hexdigest() == EXPECTED, "Request image hash mismatch")
    item = {"mode": "AB"[stage], "http": None, "schema": "not reached"}
    attempts.append(item)
    save_attempts()
    try:
        response = requests.post(ENDPOINT, headers=request["headers"], json=body,
                                 timeout=request["timeout_s"], verify=True, allow_redirects=False)
        item["http"] = response.status_code
        if response.status_code != 200:
            body_text = response.text.lower()
            item["reason"] = (
                "provider request-size limit" if "too large" in body_text or response.status_code == 413 else
                "provider rate/quota limit" if response.status_code == 429 else "provider rejected request"
            )
            raise requests.HTTPError("Groq rejected request", response=response)
        try:
            reply = response.json()
            usage = reply.get("usage") or {}
            item["usage"] = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                             if type(usage.get(k)) is int}
            choice = reply["choices"][0]
            require(choice.get("finish_reason") in (None, "stop"), "Completion did not finish")
            raw = json.loads(pipeline.extract_json_text(choice["message"]["content"]))
            parsed = (ModeAResponse, ModeBResponse)[stage].model_validate(raw, strict=True)
            require(all(ref == ASSET for ref in references(parsed.model_dump())), "Unexpected asset referenced")
            if stage == 0:
                require([(r.asset_id, r.visual_analysis_status) for r in parsed.items] == [(ASSET, "analyzed")],
                        "MODE A did not analyze the authorized asset")
            item["schema"] = "PASS"
            return reply
        except (ValueError, TypeError, KeyError, IndexError, RuntimeError):
            item["schema"] = "FAIL"
            raise RuntimeError("Completion failed strict schema or asset validation") from None
    finally:
        save_attempts()


result = pipeline.run_ai_curation(
    OUT, JOB, config=config, tracker=tracker, provider=provider.OpenAICompatibleProvider(config, transport=transport),
    batch_size=1, max_images=1, max_videos=0,
    business_context={"visual_preferences": "Original synthetic illustration. Do not claim real results or commercial rights."},
)
report = {"result": "FAIL", "attempts": attempts, "request_count": len(attempts),
          "estimate_includes_unsettled_reservations":
              any(a["http"] != 200 or a["schema"] != "PASS" for a in attempts),
          "estimated_cost_usd": result.usage.get("estimated_cost_usd"), "curation": None,
          "input_tokens": result.usage.get("input_tokens"), "output_tokens": result.usage.get("output_tokens"),
          "pipeline_status": result.status}
file = OUT / "curation.json"
if result.status == "completed" and file.is_file() and len(attempts) == 2:
    raw = file.read_text(encoding="utf8")
    doc = json.loads(raw)
    selected = doc["curation"]["selected_asset_ids"]
    if (key not in raw and doc["ai_envelope"]["model"] == MODEL
        and doc["ai_envelope"]["provider"] == "openai_compatible"
        and doc["ai_envelope"]["mock"] is False and doc["curation"]["analysis_coverage"]["mock"] is False
        and [a["asset_id"] for a in doc["mode_a_items"]] == [ASSET]
        and set(selected) <= {ASSET} and set(selected) == set(doc["curation"]["rights_review_required_ids"])):
        report.update(result="PASS", curation="curation.json", selected_asset_ids=selected,
                      rights_review_required_ids=doc["curation"]["rights_review_required_ids"])
for field in ("provider_error", "rate_limit_snapshot"):
    if field in result.usage:
        report[field] = result.usage[field]
(OUT / "acceptance-result.json").write_text(json.dumps(report, indent=2), encoding="utf8")
print(json.dumps(report), flush=True)
