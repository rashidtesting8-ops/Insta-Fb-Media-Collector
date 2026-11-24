"""Single-job acceptance guards for a dedicated QA worker. No routes are registered here."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
from secrets import compare_digest
from app import ai_execution

import requests
from fastapi import FastAPI, Header, HTTPException
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from app.ai.pipeline import extract_json_text, run_ai_curation
from app.ai.provider import AIProviderConfig, ImagePayload, OpenAICompatibleProvider
from app.ai.schemas import ModeAResponse, ModeBResponse
from app.delivery import _fixture_or_mock, _safe_disk_path
from app.manifest import atomic_write_json


MODEL = "qwen/qwen3.8-27b"
ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
MAX_BYTES = 100_000
FOLDER_ID = "1hsSZM63PQBO5jmi_-hM80lPGpr_LD7BF"


@dataclass(frozen=True)
class ScopedApproval:
    # Construct only from operator-controlled approval, never a webhook or dashboard body.
    job_id: str
    website_url: str
    asset_id: str
    source_url: str
    sha256: str
    groq_approved: bool
    drive_approved: bool = False
    folder_id: str = FOLDER_ID
    start_etag: str = ""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def scoped_original(job_dir: Path, job: dict, approval: ScopedApproval) -> tuple[dict, bytes]:
    social_urls = (job.get("instagram_url") or "", job.get("facebook_url") or "")
    fixture_urls = (
        "https://www.instagram.com/pgm_qa_fixture/",
        "https://www.facebook.com/pgm_qa_fixture/",
    )
    require(re.fullmatch(r"[a-f0-9]{32}", approval.job_id) and job_dir.name == approval.job_id
            and job.get("job_id") == approval.job_id and job.get("website_url") == approval.website_url
            and job.get("status") == "extraction_complete"
            and (social_urls == ("", "") or (
                social_urls == fixture_urls and job.get("social_permission_confirmed") is False
            )), "Unauthorized QA job")
    manifest = _read(job_dir / "manifest.json")
    require(manifest.get("job_id") == approval.job_id, "Manifest belongs to another job")
    assets = manifest.get("assets") or []
    matches = [asset for asset in assets if asset.get("asset_id") == approval.asset_id]
    require(len(assets) == len(matches) == 1, "QA requires exactly one explicitly approved asset")
    asset = matches[0]
    require(asset.get("source_url") == approval.source_url and asset.get("sha256") == approval.sha256
            and asset.get("extraction_status") == "downloaded" and asset.get("mime") == "image/jpeg"
            and asset.get("type") == "image" and (asset.get("preprocessing") or {}).get("outcome") == "processed",
            "Asset does not match the authorized source")
    path = _safe_disk_path(job_dir, asset.get("local_path"))
    require(path is not None, "Unsafe QA asset path")
    with path.open("rb") as original:
        pixels = original.read(MAX_BYTES + 1)
    require(len(pixels) <= MAX_BYTES and sha256(pixels).hexdigest() == approval.sha256, "QA asset hash or byte limit mismatch")
    with Image.open(BytesIO(pixels)) as image:
        require(image.format == "JPEG" and max(image.size) <= 256, "QA image format or dimensions exceed approval")
        image.verify()
    return asset, pixels


def bounded_config(key: str) -> AIProviderConfig:
    require(bool(key), "Groq credential missing")
    return AIProviderConfig(provider="openai_compatible", model=MODEL, endpoint=ENDPOINT, api_key=key,
                            bounded_acceptance=True, mode_a_max_output_tokens=1200, mode_b_max_output_tokens=900,
                            max_retries=0, max_requests_per_job=2, max_cost_usd_per_job=0.25,
                            batch_size=1, max_image_dim=256, max_image_bytes=MAX_BYTES,
                            price_in_per_1k=0.0008, price_out_per_1k=0.004)


def _asset_refs(value):
    if isinstance(value, dict):
        for field, entry in value.items():
            if (field == "asset_id" or field.endswith("_asset_id")) and entry is not None:
                yield entry
            elif field.endswith("_ids") and isinstance(entry, list):
                yield from entry
            yield from _asset_refs(entry)
    elif isinstance(value, list):
        for entry in value:
            yield from _asset_refs(entry)


def _strict_curation(raw, approval) -> ModeBResponse:
    parsed = ModeBResponse.model_validate(raw, strict=True)
    require(ModeBResponse.model_fields.keys() <= raw.keys() and parsed.job_id == approval.job_id
            and parsed.analysis_coverage.mock is False
            and not any(marker in (parsed.analysis_coverage.provider + " " + parsed.analysis_coverage.model).lower()
                        for marker in ("mock", "fixture", "test-only"))
            and all(ref == approval.asset_id for ref in _asset_refs(raw)), "Curation schema, job or asset scope mismatch")
    return parsed


def run_guarded_groq(job_dir: Path, job: dict, approval: ScopedApproval, key: str, *, save_analysis=None) -> dict:
    """Use only in an isolated worker after approval. This function has no mock mode."""
    require(approval.groq_approved is True and job.get("run_ai_curation") is False, "Explicit single-job inference approval required")
    asset, pixels = scoped_original(job_dir, job, approval)
    from app.publication import date_eligibility, window_bounds

    original_manifest = _read(job_dir / "manifest.json")
    bounds = window_bounds(job)
    require(bounds is not None and window_bounds(original_manifest) == bounds
            and date_eligibility(asset, bounds) == "in_window", "QA asset lacks verified in-window publication")
    config = bounded_config(key)
    target = job_dir / "curation.json"
    require(not target.exists(), "QA job already has curation")

    attempts = []

    def transport(request):
        mode_a = not attempts
        require(len(attempts) < 2 and request["url"] == ENDPOINT, "Groq request scope mismatch")
        attempt = {"mode": "A" if mode_a else "B", "http": None, "schema": "FAIL"}
        attempts.append(attempt)
        atomic_write_json(job_dir / "guarded-groq-attempts.json", attempts)
        try:
            ai_execution.require_transport()
            reply = requests.post(ENDPOINT, headers=request["headers"], json=request["body"],
                                  timeout=request["timeout_s"], verify=True, allow_redirects=False)
            attempt["http"] = reply.status_code
            if reply.status_code != 200:
                raise requests.HTTPError("Groq request rejected", response=reply)
            body = reply.json()
            choice = body["choices"][0]
            text = choice["message"]["content"]
            require(choice["finish_reason"] == "stop" and key not in text, "Groq completion did not safely finish")
            parsed = json.loads(extract_json_text(text))
            if mode_a:
                response = ModeAResponse.model_validate(parsed, strict=True)
                require([(item.asset_id, item.visual_analysis_status) for item in response.items]
                        == [(approval.asset_id, "analyzed")], "Unexpected MODE A asset")
            else:
                attempt["selected_asset_ids"] = _strict_curation(parsed, approval).selected_asset_ids
            attempt["schema"] = "PASS"
            return body
        except (KeyError, TypeError, ValueError):
            raise ValueError("Groq response failed strict validation") from None
        finally:
            with ai_execution.publication():
                atomic_write_json(job_dir / "guarded-groq-attempts.json", attempts)

    class ApprovedJpegProvider(OpenAICompatibleProvider):
        def complete(self, prompt, images, tracker):
            if images:
                require(len(images) == 1 and images[0].asset_id == approval.asset_id, "Unexpected image payload")
                images = [ImagePayload(approval.asset_id, "image/jpeg", pixels, "approved original")]
            return super().complete(prompt, images, tracker)

    consent = {approval.asset_id: (approval.source_url, approval.sha256)}
    with ai_execution.execution(job_dir, approval.job_id, consent, approval.start_etag) as staging:
        with (job_dir / "guarded-groq-intent.json").open("x", encoding="utf-8") as intent:
            json.dump({"job_id": approval.job_id, "asset_id": approval.asset_id, "sha256": approval.sha256}, intent)
            intent.flush()
            os.fsync(intent.fileno())
        result = run_ai_curation(staging, approval.job_id, config=config, batch_size=1, max_images=1, max_videos=0,
                                 provider=ApprovedJpegProvider(config, transport=transport))
        require(result.status == "completed" and result.usage.get("requests") == 2
                and result.usage.get("retries") == 0 and result.usage.get("estimated_cost_usd", 1) <= 0.25
                and [(a["mode"], a["http"], a["schema"]) for a in attempts] == [("A", 200, "PASS"), ("B", 200, "PASS")],
                "Bounded genuine Groq analysis did not pass")
        artifact = staging / "curation.json"
        envelope = _read(artifact)
        selected = _strict_curation(envelope["curation"], approval).selected_asset_ids
        require(selected == attempts[1]["selected_asset_ids"], "MODE B selection changed during normalization")
        require(envelope["job_id"] == approval.job_id and not _fixture_or_mock(envelope), "Mock or wrong-job curation")
        proof = {"job_id": approval.job_id, "asset_id": approval.asset_id, "sha256": approval.sha256,
                 "curation_sha256": sha256(artifact.read_bytes()).hexdigest(), "model": MODEL, "attempts": attempts}
        with ai_execution.publication():
            os.link(artifact, target)
            atomic_write_json(job_dir / "guarded-groq-verified.json", proof)
            if save_analysis is not None:
                save_analysis(approval.job_id, result.as_dict())
        return result.as_dict()


def eligible_delivery_key(job_dir: Path, job: dict, approval: ScopedApproval) -> str | None:
    asset, _ = scoped_original(job_dir, job, approval)
    if approval.drive_approved is not True or approval.groq_approved is not True or approval.folder_id != FOLDER_ID:
        return None
    curation_path = job_dir / "curation.json"
    raw = curation_path.read_bytes()
    envelope = json.loads(raw)
    ai = envelope.get("ai_envelope", {})
    require(not _fixture_or_mock(envelope) and envelope.get("status") == "completed" and envelope.get("job_id") == approval.job_id
            and ai.get("provider") == "openai_compatible" and ai.get("model") == MODEL, "Untrusted curation")
    proof = _read(job_dir / "guarded-groq-verified.json")
    require(proof.get("job_id") == approval.job_id and proof.get("asset_id") == approval.asset_id
            and proof.get("sha256") == approval.sha256 and proof.get("curation_sha256") == sha256(raw).hexdigest()
            and proof.get("model") == MODEL and [(a["mode"], a["http"], a["schema"]) for a in proof["attempts"]]
            == [("A", 200, "PASS"), ("B", 200, "PASS")], "Curation proof does not match this job and asset")
    selected = _strict_curation(envelope["curation"], approval)
    if selected.selected_asset_ids != [approval.asset_id] or selected.rights_review_required_ids or asset.get("rights_status") != "verified":
        return None
    return approval.asset_id + ":" + approval.sha256


def worker_delivery_payload(job_dir: Path, job: dict, approval: ScopedApproval, receipt: dict) -> dict:
    key = eligible_delivery_key(job_dir, job, approval)
    require(key and receipt.get("job_id") == approval.job_id and receipt.get("folder_id") == approval.folder_id
            and receipt.get("upload_key") == key and receipt.get("sha256") == approval.sha256
            and receipt.get("owner_only") is True and receipt.get("status") == "verified"
            and re.fullmatch(r"[A-Za-z0-9_-]{8,200}", receipt.get("drive_file_id") or ""),
            "Drive receipt does not match the approved job and asset")
    return {"authorization_confirmed": True, "drive_folder_id": approval.folder_id,
            "uploads": [{"upload_key": key, "drive_file_id": receipt["drive_file_id"], "status": "uploaded"}]}


def run_guarded_job(job_dir, job, approval, key, *, save_analysis, deliver_private):
    """Contract for the disabled bridge. Both callbacks belong to the isolated QA instance."""
    run_guarded_groq(job_dir, job, approval, key, save_analysis=save_analysis)
    upload_key = eligible_delivery_key(job_dir, job, approval)
    if upload_key is None:
        return {"job_id": approval.job_id, "status": "no_eligible_delivery"}
    asset, original = scoped_original(job_dir, job, approval)
    # The n8n callback must reconcile using a stable per-job ledger and verify owner-only access and downloaded bytes.
    receipt = deliver_private(approval, upload_key, asset, original)
    payload = worker_delivery_payload(job_dir, job, approval, receipt)
    upload, = payload["uploads"]
    return {"job_id": approval.job_id, "status": "verified", "mode": "live", "drive_folder_id": payload["drive_folder_id"],
            "uploads": [{**upload, "owner_only": True, "sha256": approval.sha256, "status": "verified"}]}


class GuardedJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def build_guarded_qa_app(*, jobs_root, approval, adapter_token, key, load_job, save_analysis, deliver_private):
    """Opt-in router for an isolated worker; the default worker never mounts it."""
    require(isinstance(adapter_token, str) and len(adapter_token) >= 32, "A separate adapter token is required")
    require(approval.groq_approved is True, "No approved live QA run has been configured")
    root = Path(jobs_root).resolve(strict=True)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/guarded-job")
    def guarded_job(request: GuardedJobRequest, x_qa_adapter_token: str = Header(default="")):
        if not compare_digest(x_qa_adapter_token.encode(), adapter_token.encode()):
            raise HTTPException(status_code=401, detail="QA adapter authorization required")
        if request.job_id != approval.job_id:
            raise HTTPException(status_code=403, detail="Job outside approved QA scope")
        job = load_job(request.job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="QA job not found")
        directory = (root / request.job_id).resolve()
        if directory.parent != root or directory.name != request.job_id:
            raise HTTPException(status_code=403, detail="QA storage outside approved scope")
        try:
            return run_guarded_job(directory, job, approval, key, save_analysis=save_analysis, deliver_private=deliver_private)
        except (ValueError, OSError, KeyError, requests.RequestException):
            raise HTTPException(status_code=409, detail="Guarded QA job stopped; inspect the local receipt") from None

    return app
