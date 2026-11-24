import argparse
import base64
import hashlib
import io
import json
import sys
from pathlib import Path

import requests
from PIL import Image
from pydantic import BaseModel, ValidationError
from app.ai import pipeline, provider, schemas
from app import ai_execution
from app.ai.schemas import ModeAResponse, ModeBResponse
from app.manifest import atomic_write_json


EXPECTED = "2e01cb8ce50473e7901d70777ef7f9578feb358d7642d2e6e78df5442c5cf986"
ASSET = "synthetic_grooming_dog_2e01cb8ce504"
JOB = "groq-bounded-synthetic-corrected"
MODEL = "qwen/qwen3.8-27b"
ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
OUT = Path("/result")
parser = argparse.ArgumentParser()
modes = parser.add_mutually_exclusive_group(required=True)
modes.add_argument("--preflight", action="store_true")
modes.add_argument("--live", action="store_true")
modes.add_argument("--recover-mode-b", type=Path, metavar="CHECKPOINT")
parser.add_argument("--authorize-live-mode-b", action="store_true")
args = parser.parse_args()
RECOVERY = args.recover_mode_b is not None
LIVE = args.live or RECOVERY


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


require(RECOVERY == args.authorize_live_mode_b, "MODE B recovery requires explicit live authorization")
if LIVE:
    # This historical standalone fixture has no verified submitter or ledger-backed start.
    ai_execution.require_pipeline(OUT, JOB)
    require(OUT.is_dir() and not any(OUT.iterdir()), "Use a fresh empty output directory")
if RECOVERY:
    require(not args.recover_mode_b.resolve().is_relative_to(OUT.resolve()), "Keep prior evidence outside the output directory")
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
    bounded_acceptance=True, mode_a_max_output_tokens=1200, mode_b_max_output_tokens=900,
    max_retries=0, max_requests_per_job=1 if RECOVERY else 2, max_cost_usd_per_job=0.25, mode_b_only=RECOVERY,
    batch_size=1, max_image_dim=256, max_image_bytes=100_000,
    price_in_per_1k=0.0008, price_out_per_1k=0.004,
)
require(config.bounded_acceptance is True and
        (config.model, config.mode_a_max_output_tokens, config.mode_b_max_output_tokens,
         config.max_retries, config.max_requests_per_job, config.max_cost_usd_per_job)
        == (MODEL, 1200, 900, 0, 1 if RECOVERY else 2, 0.25), "Effective configuration mismatch")
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


SCHEMA_FIELDS = {
    field
    for model in vars(schemas).values()
    if isinstance(model, type) and issubclass(model, BaseModel)
    for field in model.model_fields
}


def json_path(parts):
    return "$" + "".join(
        f"[{part}]" if isinstance(part, int)
        else f".{part}" if part in SCHEMA_FIELDS else ".<unrecognized-field>"
        for part in parts
    )


def references(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            field = (*path, key)
            if (key == "asset_id" or key.endswith("_asset_id")) and item is not None:
                yield field, item
            elif key.endswith("_ids") and isinstance(item, list):
                for index, ref in enumerate(item):
                    yield (*field, index), ref
            yield from references(item, field)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from references(item, (*path, index))


class CompletionValidationError(ValueError):
    def __init__(self, path, reason):
        super().__init__(reason)
        self.diagnostic = {"path": path, "reason": reason}


def validate_completion(reply, stage):
    envelope_path = "$.choices[0].message.content"
    try:
        choice = reply["choices"][0]
        finish = choice.get("finish_reason")
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError, AttributeError):
        raise CompletionValidationError(envelope_path, "missing or invalid response envelope") from None
    if finish not in (None, "stop"):
        reason = "truncated at output limit" if finish == "length" else "completion did not finish normally"
        raise CompletionValidationError("$.choices[0].finish_reason", reason)
    if not isinstance(content, str):
        raise CompletionValidationError(envelope_path, "expected text content")
    try:
        raw = json.loads(pipeline.extract_json_text(content))
    except json.JSONDecodeError as exc:
        raise CompletionValidationError("$", f"invalid completion JSON at line {exc.lineno}, column {exc.colno}") from None
    schema = (ModeAResponse, ModeBResponse)[stage]
    parsed = schema.model_validate(raw, strict=True)
    if stage == 1:
        missing = next((field for field in schema.model_fields if field not in raw), None)
        if missing is not None:
            raise CompletionValidationError(json_path((missing,)), "missing required output field")
    for path, ref in references(parsed.model_dump()):
        if ref != ASSET:
            raise CompletionValidationError(json_path(path), "unauthorized asset reference")
    return content, raw, parsed


def checkpoint_digest(record):
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf8")).hexdigest()


def read_mode_a_checkpoint(path):
    record = json.loads(path.read_text(encoding="utf8"))
    digest = record.pop("checkpoint_sha256")
    require(checkpoint_digest(record) == digest, "MODE A checkpoint integrity mismatch")
    require(record["asset_id"] == ASSET and record["image_sha256"] == EXPECTED
            and record["model"] == MODEL and record["mock"] is False
            and record["schema_validation"] == "PASS"
            and record["request"]["mode"] == "A" and record["request"]["http_status"] == 200
            and record["request"]["max_completion_tokens"] == 1200, "MODE A checkpoint scope mismatch")
    analysis = ModeAResponse.model_validate(record["analysis"], strict=True)
    require([(item.asset_id, item.visual_analysis_status) for item in analysis.items] == [(ASSET, "analyzed")],
            "MODE A checkpoint asset mismatch")
    return record

restored = None
if RECOVERY:
    record = read_mode_a_checkpoint(args.recover_mode_b)
    serialized = json.dumps(record, ensure_ascii=False)
    require(key not in serialized and provider.scrub_secrets(serialized) == serialized,
            "Checkpoint contains credential material")
    restored = ModeAResponse.model_validate(record["analysis"], strict=True)
else:
    pipeline.build_image_payloads = prepared_payload  # Use exact approved bytes.
attempts = []
tracker = provider.UsageTracker(config)


def save_attempts():
    (OUT / "attempts.json").write_text(json.dumps(attempts, indent=2), encoding="utf8")


def transport(request):
    stage = 1 if RECOVERY else len(attempts)
    if stage == 1:
        require(not RECOVERY or not attempts, "MODE B recovery already attempted")
        checkpoint_path = args.recover_mode_b if RECOVERY else OUT / "mode-a-validated.json"
        record = read_mode_a_checkpoint(checkpoint_path)
        if RECOVERY:
            require(record["analysis"] == restored.model_dump(mode="json"), "Checkpoint changed after validation")
    body = request["body"]
    require(stage < 2 and request["url"] == ENDPOINT and body["model"] == MODEL, "Request scope mismatch")
    require(body["max_completion_tokens"] == (1200, 900)[stage], "Request cap mismatch")
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
        ai_execution.require_transport()
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
            try:
                reply = response.json()
            except ValueError:
                raise CompletionValidationError("$", "invalid HTTP response JSON") from None
            usage = reply.get("usage") or {}
            item["usage"] = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                             if type(usage.get(k)) is int}
            content, raw, parsed = validate_completion(reply, stage)
            if stage == 0:
                require([(r.asset_id, r.visual_analysis_status) for r in parsed.items] == [(ASSET, "analyzed")],
                        "MODE A did not analyze the authorized asset")
            serialized = json.dumps(raw, ensure_ascii=False)
            if key in serialized or provider.scrub_secrets(serialized) != serialized:
                raise CompletionValidationError("$", "credential material detected in completion")
            if stage == 0:
                # Check the same normalization and asset validation the pipeline will apply.
                normalized, problems, _ = pipeline._parse_mode_a(content, {ASSET})
                require(normalized is not None and not problems, "Pipeline MODE A validation failed")
                require(all(type(usage.get(name)) is int and 0 <= usage[name] <= cap for name, cap in (
                    ("prompt_tokens", provider.GROQ_ACCEPTANCE_INPUT_TOKEN_BOUND),
                    ("completion_tokens", config.mode_a_max_output_tokens),
                )), "Invalid MODE A usage")
                checkpoint = {"asset_id": ASSET, "image_sha256": EXPECTED, "model": MODEL,
                              "schema_validation": "PASS", "mock": False,
                              "request": {"mode": "A", "http_status": 200, "attempt": 1,
                                          "max_completion_tokens": config.mode_a_max_output_tokens,
                                          "usage": item["usage"]},
                              "analysis": normalized.model_dump(mode="json")}
                checkpoint["checkpoint_sha256"] = checkpoint_digest(checkpoint)
                checkpoint_path = OUT / "mode-a-validated.json"
                require(not checkpoint_path.exists(), "MODE A checkpoint already exists")
                with ai_execution.publication():
                    atomic_write_json(checkpoint_path, checkpoint)
            item["schema"] = "PASS"
            return reply
        except CompletionValidationError as exc:
            item["schema"] = "FAIL"
            item["validation_errors"] = [exc.diagnostic]
            raise RuntimeError("Completion failed strict validation") from None
        except ValidationError as exc:
            item["schema"] = "FAIL"
            item["validation_errors"] = [
                {"path": json_path(error["loc"]), "reason": error["type"]}
                for error in exc.errors(include_input=False, include_context=False, include_url=False)[:12]
            ]
            raise RuntimeError("Completion failed schema validation") from None
        except (ValueError, TypeError, KeyError, IndexError, RuntimeError, OSError):
            item["schema"] = "FAIL"
            item["validation_errors"] = [{"path": "$", "reason": "response or checkpoint processing failed"}]
            raise RuntimeError("Completion or checkpoint processing failed") from None
    finally:
        save_attempts()


result = pipeline.run_ai_curation(
    OUT, JOB, config=config, tracker=tracker, provider=provider.OpenAICompatibleProvider(config, transport=transport),
    batch_size=1, max_images=1, max_videos=0, validated_mode_a=restored,
    business_context={"visual_preferences": "Original synthetic illustration. Do not claim real results or commercial rights."},
)
report = {"result": "FAIL", "attempts": attempts, "request_count": len(attempts),
          "estimate_includes_unsettled_reservations":
              any(a["http"] != 200 or a["schema"] != "PASS" for a in attempts),
          "estimated_cost_usd": result.usage.get("estimated_cost_usd"), "curation": None,
          "input_tokens": result.usage.get("input_tokens"), "output_tokens": result.usage.get("output_tokens"),
          "pipeline_status": result.status}
file = OUT / "curation.json"
if result.status == "completed" and file.is_file() and len(attempts) == (1 if RECOVERY else 2):
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
