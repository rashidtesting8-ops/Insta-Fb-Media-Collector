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
from pathlib import Path, PureWindowsPath
from typing import Any

from app.manifest import atomic_write_json

MOCK_MARKERS = ("mock", "fixture", "test-only")
_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


def sanitize_folder_name(business: str, job_id: str) -> str:
    raw = (business or "").strip().lower()
    if "://" in raw:
        raw = re.sub(r"^https?://", "", raw).split("/")[0]
    raw = raw.split(",")[0].strip()
    clean = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:60] or "pet-grooming-business"
    job_part = re.sub(r"[^a-zA-Z0-9]+", "-", job_id).strip("-")[:8] or "job"
    return f"{clean}-{job_part}"


def _safe_disk_path(job_dir: Path, rel: Any) -> Path | None:
    if not isinstance(rel, str) or not rel or "\\" in rel or PureWindowsPath(rel).drive:
        return None
    relative = Path(rel)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = (job_dir / relative).resolve()
    return candidate if candidate.is_relative_to(job_dir.resolve()) and candidate.is_file() else None


def _safe_delivery_filename(asset_id: str, disk: Path, mime: Any, sha: str) -> str | None:
    """Derive a safe Drive filename without exposing raw .media files or unsafe paths."""
    if disk.suffix.lower() != ".media":
        name = Path(disk.name).name
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return None
        return name
    clean_mime = str(mime or "").split(";", 1)[0].strip().lower()
    ext = _MIME_TO_EXT.get(clean_mime)
    if not ext:
        return None
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", str(asset_id or "")).strip("._-")[:40]
    if not stem:
        return None
    return f"{stem}-{sha[:8]}{ext}"


def _fixture_or_mock(envelope: dict[str, Any]) -> bool:
    if envelope.get("ai_envelope", {}).get("mock") is not False:
        return True
    if envelope.get("curation", {}).get("analysis_coverage", {}).get("mock") is not False:
        return True
    markers = [
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
    db_path: str | Path | None = None,
    projects_root: str | Path | None = None,
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
    use_rights_ledger = False
    verified_rights: dict[str, dict[str, Any]] = {}
    eligible_ids: set[str] | None = None
    if manifest.get("collection_mode") in ("public_checkpoint", "recorded_extractor"):
        if _fixture_or_mock(envelope):
            return {
                "status": "blocked_non_real_curation", "job_id": job_id, "assets": [],
                "reason": "checkpoint delivery requires completed non-mock curation",
            }
        if db_path is None:
            return {"status": "blocked_unverified_collection", "job_id": job_id, "assets": []}
        from app.collection_approval import verify_checkpoint_release

        root = Path(projects_root) if projects_root is not None else job_dir.parent
        ok, reason, verified_rights, eligible_ids = verify_checkpoint_release(Path(db_path), root, job_id, manifest)
        if not ok:
            return {"status": "blocked_unverified_collection", "job_id": job_id, "assets": [], "reason": reason}
        use_rights_ledger = True
    by_id = {a.get("asset_id"): a for a in manifest.get("assets", [])}
    selected = list(dict.fromkeys(envelope.get("curation", {}).get("selected_asset_ids", [])))
    rights_review = set(envelope.get("curation", {}).get("rights_review_required_ids", []))
    assets: list[dict[str, Any]] = []
    pending_rights = []
    errors: list[str] = []
    for aid in selected:
        item = by_id.get(aid)
        if item is None:
            errors.append(f"unknown asset_id: {aid}")
            continue
        if eligible_ids is not None and aid not in eligible_ids:
            errors.append(f"asset outside approved subset: {aid}")
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
        rights_meta: dict[str, Any] = {}
        if use_rights_ledger:
            ledger_entry = verified_rights.get(aid)
            if ledger_entry is None or ledger_entry.get("asset_sha") != sha:
                pending_rights.append(aid)
                continue
            rights_status = "verified"
            rights_meta = {
                "rights_evidence_ref": ledger_entry.get("evidence_ref"),
                "rights_verified_by": ledger_entry.get("actor"),
                "rights_verified_at": ledger_entry.get("utc"),
            }
        else:
            if item.get("rights_status") != "verified" or aid in rights_review:
                pending_rights.append(aid)
                continue
            rights_status = item.get("rights_status", "unverified")
        safe_name = _safe_delivery_filename(aid, disk, item.get("mime"), sha)
        if safe_name is None:
            errors.append(f"unsupported delivery mime: {aid}")
            continue
        assets.append({
            "asset_id": aid,
            "filename": safe_name,
            "local_path": str(item["local_path"]),
            "n8n_path": "/data/media/" + job_id + "/" + str(item["local_path"]),
            "sha256": sha,
            "bytes": len(data),
            "mime": item.get("mime") or "application/octet-stream",
            "source_url": item.get("source_url"),
            "rights_status": rights_status,
            "rights_review_required": False,
            **rights_meta,
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
    uploaded_keys = [
        u["upload_key"] for u in (existing_delivery.get("uploaded") or [])
        if u.get("upload_key") and u.get("status") == "uploaded" and u.get("drive_file_id")
    ]
    pending_uploads = [a for a in assets if a["upload_key"] not in uploaded_keys]
    status = "ready" if assets and not errors else ("partial" if assets else "failed")
    if not assets and pending_rights:
        status = "blocked_rights_review"
    return {
        "status": status, "job_id": job_id,
        "folder_name": sanitize_folder_name(business_name or manifest.get("website_url", ""), job_id),
        "visibility": "private",
        "public_sharing": False,
        "assets": assets, "errors": errors[:30],
        "pending_uploads": pending_uploads,
        "uploaded_keys": uploaded_keys,
        "drive_folder_id": existing_delivery.get("drive_folder_id"),
        "drive_folder_link": existing_delivery.get("drive_folder_link"),
        "pending_rights_ids": pending_rights,
        "note": "n8n owns Google Drive credentials/uploads; files are never made public automatically.",
    }


def finalize_delivery(
    job_dir: str | Path,
    plan: dict[str, Any],
    uploads: list[dict[str, Any]],
    *,
    authorization_confirmed: bool,
    drive_folder_id: str | None = None,
    drive_folder_link: str | None = None,
    public_sharing: bool = False,
    visibility: str = "private",
) -> dict[str, Any]:
    """Persist delivery.json. Rights gating: without explicit authorization,
    no upload is recorded as an approved commercial release."""
    job_dir = Path(job_dir)
    if public_sharing is True or visibility != "private":
        return {
            "job_id": plan.get("job_id"),
            "status": "blocked_unsafe_destination",
            "uploaded": [],
            "pending_rights_ids": plan.get("pending_rights_ids", []),
        }
    if not authorization_confirmed or plan.get("status") == "blocked_rights_review":
        return {"job_id": plan.get("job_id"), "status": "blocked_rights_review", "uploaded": [],
                "pending_rights_ids": plan.get("pending_rights_ids", [])}
    by_key = {a["upload_key"]: a for a in plan.get("assets", [])
              if a.get("rights_status") == "verified" and a.get("rights_review_required") is False}
    # Merge with any prior ledger so retries do not lose successful uploads.
    existing_ledger = []
    existing_path = job_dir / "delivery.json"
    if existing_path.is_file():
        try:
            existing_ledger = list((json.loads(existing_path.read_text(encoding="utf-8")) or {}).get("uploaded") or [])
        except (OSError, json.JSONDecodeError):
            existing_ledger = []
    by_upload = {row["upload_key"]: row for row in existing_ledger if row.get("upload_key")}
    for upload in uploads:
        key = str(upload.get("upload_key") or "")
        asset = by_key.get(key)
        if asset is None:
            continue
        previous = by_upload.get(key, {})
        if previous.get("status") == "uploaded" and previous.get("drive_file_id"):
            continue
        by_upload[key] = {
            "asset_id": asset["asset_id"], "upload_key": key,
            "filename": asset.get("filename"),
            "drive_file_id": upload.get("drive_file_id"),
            "drive_link": upload.get("drive_link"),
            "status": upload.get("status", "failed"),
            "error": upload.get("error"),
            "sha256": asset["sha256"], "rights_status": asset["rights_status"],
        }
    ledger = list(by_upload.values())
    ok = [u for u in ledger if u["upload_key"] in by_key and u["status"] == "uploaded" and u["drive_file_id"]]
    status = (
        "completed" if len(ok) == len(plan.get("assets", [])) and ok and not plan.get("pending_rights_ids") else
        "partial_success" if ok else "failed"
    )
    result = {
        "schema_version": "1.0-delivery", "job_id": plan.get("job_id"),
        "status": status,
        "folder_name": plan.get("folder_name"),
        "visibility": "private",
        "public_sharing": False,
        "drive_folder_id": plan.get("drive_folder_id") or drive_folder_id,
        "drive_folder_link": plan.get("drive_folder_link") or drive_folder_link,
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
