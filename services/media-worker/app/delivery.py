"""Phase 6 Google Drive delivery planning/finalization (worker side).

n8n owns Drive authentication and uploads; this module only computes a
deterministic, validated plan from the persisted curation + manifest and
records the upload ledger returned by n8n. No secrets, no public sharing,
no fabricated assets, no mock/fixture curation released by default.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.manifest import atomic_write_json

MOCK_MARKERS = ("mock", "fixture", "test-only")


def sanitize_folder_name(business: str, job_id: str) -> str:
    raw = (business or "").strip().lower()
    if "://" in raw:
        raw = re.sub(r"^https?://", "", raw).split("/")[0]
    raw = raw.split(",")[0].strip()
    clean = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:60] or "pet-grooming-business"
    job_part = re.sub(r"[^a-zA-Z0-9]+", "-", job_id).strip("-")[:8] or "job"
    return f"{clean}-{job_part}"


def _safe_disk_path(job_dir: Path, rel: Any) -> Path | None:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    candidate = job_dir / rel
    return candidate if candidate.is_file() else None


def _fixture_or_mock(envelope: dict[str, Any]) -> bool:
    markers = [
        str(envelope.get("ai_envelope", {}).get("mock")).lower(),
        str(envelope.get("curation", {}).get("analysis_coverage", {}).get("mock")).lower(),
        str(envelope.get("ai_envelope", {}).get("prompt_version", "")).lower(),
        str(envelope.get("fixture", "")).lower(),
    ]
    return any(any(m in marker for m in MOCK_MARKERS) for marker in markers)


def build_delivery_plan(
    job_dir: str | Path,
    job_id: str,
    business_name: str = "",
    *,
    allow_fixture: bool = False,
) -> dict[str, Any]:
    job_dir = Path(job_dir)
    curation_path = job_dir / "curation.json"
    if not curation_path.is_file():
        return {"status": "no_curation", "job_id": job_id, "assets": [], "reason": "curation.json missing"}
    envelope = json_load(curation_path)
    if _fixture_or_mock(envelope) and not allow_fixture:
        return {
            "status": "blocked_non_real_curation", "job_id": job_id, "assets": [],
            "reason": "curation is mock/fixture/test-only; never release as a customer deliverable",
        }
    manifest = json_load(job_dir / "manifest.json")
    by_id = {a.get("asset_id"): a for a in manifest.get("assets", [])}
    selected = list(dict.fromkeys(envelope.get("curation", {}).get("selected_asset_ids", [])))
    rights_review = set(envelope.get("curation", {}).get("rights_review_required_ids", []))
    assets: list[dict[str, Any]] = []
    errors: list[str] = []
    for aid in selected:
        item = by_id.get(aid)
        if item is None:
            errors.append(f"unknown asset_id: {aid}")
            continue
        if not aid or item.get("extraction_status") not in ("downloaded", "extracted_frame"):
            errors.append(f"asset not eligible: {aid}")
            continue
        disk = _safe_disk_path(job_dir, item.get("local_path"))
        if disk is None:
            errors.append(f"missing or unsafe path: {aid}")
            continue
        data = disk.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if item.get("sha256") and sha != item["sha256"]:
            errors.append(f"sha256 mismatch: {aid}")
            continue
        assets.append({
            "asset_id": aid,
            "filename": disk.name,
            "local_path": str(item["local_path"]),
            "n8n_path": "/data/media/" + job_id + "/" + str(item["local_path"]),
            "sha256": sha,
            "bytes": len(data),
            "mime": item.get("mime") or "application/octet-stream",
            "source_url": item.get("source_url"),
            "rights_status": item.get("rights_status", "unverified"),
            "rights_review_required": item.get("rights_status", "unverified") != "verified" or aid in rights_review,
            "upload_key": f"{aid}:{sha}",  # deterministic retry/idempotency key
        })
    # Reuse prior delivery state (retry safety): never re-upload an already uploaded key.
    existing_delivery = {}
    delivery_path = job_dir / "delivery.json"
    if delivery_path.is_file():
        try:
            existing_delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing_delivery = {}
    uploaded_keys = [str(u.get("upload_key")) for u in (existing_delivery.get("uploaded") or []) if u.get("upload_key")]
    pending_uploads = [a for a in assets if a["upload_key"] not in uploaded_keys]
    status = "ready" if assets and not errors else ("partial" if assets else "failed")
    return {
        "status": status, "job_id": job_id,
        "folder_name": sanitize_folder_name(business_name or manifest.get("website_url", ""), job_id),
        "assets": assets, "errors": errors[:30],
        "pending_uploads": pending_uploads,
        "uploaded_keys": uploaded_keys,
        "drive_folder_id": existing_delivery.get("drive_folder_id"),
        "pending_rights_ids": [a["asset_id"] for a in assets if a["rights_review_required"]],
        "note": "n8n owns Google Drive credentials/uploads; files are never made public automatically.",
    }


def finalize_delivery(
    job_dir: str | Path,
    plan: dict[str, Any],
    uploads: list[dict[str, Any]],
    *,
    authorization_confirmed: bool,
) -> dict[str, Any]:
    """Persist delivery.json. Rights gating: without explicit authorization,
    no upload is recorded as an approved commercial release."""
    job_dir = Path(job_dir)
    by_key = {a["upload_key"]: a for a in plan.get("assets", [])}
    # Merge with any prior ledger so retries do not lose successful uploads.
    existing_ledger = []
    existing_path = job_dir / "delivery.json"
    if existing_path.is_file():
        try:
            existing_ledger = list((json.loads(existing_path.read_text(encoding="utf-8")) or {}).get("uploaded") or [])
        except (OSError, json.JSONDecodeError):
            existing_ledger = []
    ledger = []
    seen = set()
    for upload in existing_ledger:
        key = str(upload.get("upload_key") or "")
        asset = by_key.get(key)
        if asset is None or key in seen:
            continue
        seen.add(key)
        ledger.append({
            "asset_id": asset["asset_id"], "upload_key": key,
            "drive_file_id": upload.get("drive_file_id"),
            "drive_link": upload.get("drive_link"),
            "status": upload.get("status", "failed"),
            "error": upload.get("error"),
            "sha256": asset["sha256"], "rights_status": asset["rights_status"],
        })
    for upload in uploads:
        key = str(upload.get("upload_key") or "")
        asset = by_key.get(key)
        if asset is None or key in seen:  # unknown asset or duplicate retry
            continue
        seen.add(key)
        ledger.append({
            "asset_id": asset["asset_id"], "upload_key": key,
            "drive_file_id": upload.get("drive_file_id"),
            "drive_link": upload.get("drive_link"),
            "status": upload.get("status", "failed"),
            "error": str(upload.get("error") or "")[:200] or None,
            "sha256": asset["sha256"], "rights_status": asset["rights_status"],
        })
    ok = [u for u in ledger if u["status"] == "uploaded" and u["drive_file_id"]]
    status = "blocked_rights_review" if not authorization_confirmed else (
        "completed" if len(ok) == len(plan.get("assets", [])) and ok else
        "partial_success" if ok else "failed"
    )
    result = {
        "schema_version": "1.0-delivery", "job_id": plan.get("job_id"),
        "status": status,
        "folder_name": plan.get("folder_name"),
        "drive_folder_id": plan.get("drive_folder_id"),
        "drive_folder_link": plan.get("drive_folder_link"),
        "authorization_confirmed": bool(authorization_confirmed),
        "uploaded": ledger,
        "pending_rights_ids": plan.get("pending_rights_ids", []),
        "errors": plan.get("errors", []),
        "note": "Mock/fixture curation is never released as a customer deliverable. "
        "Commercial use requires explicit operator authorization.",
    }
    atomic_write_json(job_dir / "delivery.json", result)
    return result


def json_load(path: Path) -> dict[str, Any]:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
