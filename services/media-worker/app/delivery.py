"""Phase 6 + V2.1 Google Drive delivery planning/finalization (worker side).

n8n owns Drive authentication and uploads; this module computes a deterministic,
validated plan from persisted curation + manifest, packages sanitized client
AI curation and media inventory reports for the Drive `Reports/` subfolder,
and records the upload ledger returned by n8n. No secrets, no public sharing,
no fabricated assets, no mock/fixture curation released by default.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

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
_ALLOWED_SUBFOLDERS = ("Best Images", "Best Videos", "Reports")


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


def _human_stem_from_url(source_url: Any) -> str:
    if not isinstance(source_url, str) or not source_url.strip():
        return ""
    try:
        raw_path = unquote(urlsplit(source_url).path or "")
    except ValueError:
        return ""
    leaf = raw_path.rsplit("/", 1)[-1]
    if not leaf:
        return ""
    stem = leaf.rsplit(".", 1)[0] if "." in leaf else leaf
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("._-")[:40]
    if len(cleaned) < 2 or cleaned.lower() in ("image", "img", "photo", "media", "file", "index"):
        return ""
    return cleaned


def _safe_delivery_filename(
    asset_id: str,
    disk: Path,
    mime: Any,
    sha: str,
    *,
    source_url: Any = None,
) -> str | None:
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
    stem = _human_stem_from_url(source_url) or re.sub(r"[^A-Za-z0-9_-]+", "_", str(asset_id or "")).strip("._-")[:40]
    if not stem:
        return None
    return f"{stem}-{sha[:8]}{ext}"


def _stage_delivery_media(job_dir: Path, job_id: str, assets: list[dict[str, Any]]) -> None:
    """Materialize real-extension staged files for `.media` originals so n8n detects exact MIME types.

    n8n's Read/Write Files from Disk infers `binary.data.mimeType` from the on-disk filename
    extension (`mime-types.lookup`). Reading a `.media` path yields `false` -> `text/plain`,
    causing Google Drive to store WebP/JPEG/MP4 files as unreadable text documents.
    """
    media_assets = [a for a in assets if str(a.get("local_path") or "").lower().endswith(".media")]
    if not media_assets:
        return
    stage_dir = (job_dir / "delivery_media").resolve()
    stage_dir.mkdir(parents=True, exist_ok=True)
    for asset in media_assets:
        src = _safe_disk_path(job_dir, asset.get("local_path"))
        safe_name = str(asset.get("filename") or "")
        expected_sha = str(asset.get("sha256") or "")
        if src is None or not safe_name or "/" in safe_name or "\\" in safe_name:
            continue
        target = (stage_dir / safe_name).resolve()
        if not target.is_relative_to(stage_dir):
            continue
        needs_write = True
        if target.is_file():
            try:
                with target.open("rb") as fh:
                    if hashlib.file_digest(fh, "sha256").hexdigest() == expected_sha:
                        needs_write = False
            except OSError:
                needs_write = True
        if needs_write:
            tmp = (stage_dir / f".{safe_name}.{os.getpid()}.tmp").resolve()
            try:
                if tmp.exists():
                    tmp.unlink()
                try:
                    os.link(src, tmp)
                except OSError:
                    shutil.copyfile(src, tmp)
                os.replace(tmp, target)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        asset["n8n_path"] = f"/data/media/{job_id}/delivery_media/{safe_name}"


def _target_subfolder_for_mime(mime: Any) -> str:
    clean = str(mime or "").split(";", 1)[0].strip().lower()
    return "Best Videos" if clean.startswith("video/") else "Best Images"


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


def _package_client_reports(
    job_dir: Path,
    job_id: str,
    folder_name: str,
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    released_assets: list[dict[str, Any]],
    pending_rights_ids: list[str],
) -> list[dict[str, Any]]:
    """Write sanitized client-facing AI curation and media inventory JSON reports for Drive `Reports/`.

    Never includes internal disk paths, tokens, or unverified binary paths.
    Deterministic content (stable sorted JSON without volatile timestamps) ensures SHA-256
    upload_keys stay idempotent across delivery retries.
    """
    reports_dir = job_dir / "delivery_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    released_by_id = {a["asset_id"]: a for a in released_assets}
    manifest_by_id = {
        a.get("asset_id"): a for a in (manifest.get("assets") or []) if isinstance(a, dict) and a.get("asset_id")
    }
    curation_body = envelope.get("curation") if isinstance(envelope.get("curation"), dict) else {}
    ai_env = envelope.get("ai_envelope") if isinstance(envelope.get("ai_envelope"), dict) else {}

    best_images = []
    seen_img_ids: set[str] = set()
    for rec in curation_body.get("best_images") or []:
        if not isinstance(rec, dict) or rec.get("asset_id") not in released_by_id:
            continue
        aid = rec["asset_id"]
        seen_img_ids.add(aid)
        asset_info = released_by_id[aid]
        best_images.append({
            "asset_id": aid,
            "filename": asset_info["filename"],
            "target_subfolder": asset_info["target_subfolder"],
            "recommended_section": rec.get("recommended_section"),
            "hero_rank": rec.get("hero_rank"),
            "overall_score": rec.get("overall_score"),
            "focal_point": rec.get("focal_point"),
            "crop_guidance": rec.get("crop_guidance"),
            "strengths": rec.get("strengths") or [],
            "weaknesses": rec.get("weaknesses") or [],
        })

    # Real Visual Director V2 ModeBResponse sections (hero_selection + section lists)
    hero_selection = []
    for rec in curation_body.get("hero_selection") or []:
        if not isinstance(rec, dict) or rec.get("asset_id") not in released_by_id:
            continue
        aid = rec["asset_id"]
        asset_info = released_by_id[aid]
        entry = {
            **rec,
            "filename": asset_info["filename"],
            "target_subfolder": asset_info["target_subfolder"],
        }
        hero_selection.append(entry)
        if aid not in seen_img_ids:
            seen_img_ids.add(aid)
            best_images.append({
                "asset_id": aid,
                "filename": asset_info["filename"],
                "target_subfolder": asset_info["target_subfolder"],
                "recommended_section": "hero",
                "hero_rank": rec.get("rank"),
                "focal_point": rec.get("focal_point"),
                "crop_guidance": f"desktop: {rec.get('desktop_crop', '')}; mobile: {rec.get('mobile_crop', '')}".strip("; "),
                "reason": rec.get("reason"),
            })

    curated_sections: dict[str, list[dict[str, Any]]] = {}
    for sec_key in (
        "salon_images",
        "founder_and_team_images",
        "service_images",
        "grooming_transformations",
        "gallery_images",
    ):
        sec_items = []
        for rec in curation_body.get(sec_key) or []:
            if not isinstance(rec, dict) or rec.get("asset_id") not in released_by_id:
                continue
            aid = rec["asset_id"]
            asset_info = released_by_id[aid]
            sec_items.append({
                **rec,
                "filename": asset_info["filename"],
                "target_subfolder": asset_info["target_subfolder"],
            })
            if aid not in seen_img_ids:
                seen_img_ids.add(aid)
                best_images.append({
                    "asset_id": aid,
                    "filename": asset_info["filename"],
                    "target_subfolder": asset_info["target_subfolder"],
                    "recommended_section": rec.get("recommended_section") or sec_key,
                    "focal_point": rec.get("focal_point"),
                    "crop_guidance": rec.get("crop_notes"),
                    "reason": rec.get("reason"),
                })
        if sec_items:
            curated_sections[sec_key] = sec_items

    best_videos = []
    seen_vid_ids: set[str] = set()
    for rec in curation_body.get("best_videos") or []:
        if not isinstance(rec, dict) or rec.get("asset_id") not in released_by_id:
            continue
        aid = rec["asset_id"]
        seen_vid_ids.add(aid)
        asset_info = released_by_id[aid]
        best_videos.append({
            "asset_id": aid,
            "filename": asset_info["filename"],
            "target_subfolder": asset_info["target_subfolder"],
            "recommended_section": rec.get("recommended_section"),
            "overall_score": rec.get("overall_score"),
            "best_thumbnail_frame_index": rec.get("best_thumbnail_frame_index"),
            "strengths": rec.get("strengths") or [],
            "weaknesses": rec.get("weaknesses") or [],
        })

    video_recommendations = []
    for rec in curation_body.get("video_recommendations") or []:
        if not isinstance(rec, dict) or rec.get("asset_id") not in released_by_id:
            continue
        aid = rec["asset_id"]
        asset_info = released_by_id[aid]
        video_recommendations.append({
            **rec,
            "filename": asset_info["filename"],
            "target_subfolder": asset_info["target_subfolder"],
        })
        if aid not in seen_vid_ids:
            seen_vid_ids.add(aid)
            best_videos.append({
                "asset_id": aid,
                "filename": asset_info["filename"],
                "target_subfolder": asset_info["target_subfolder"],
                "recommended_section": rec.get("recommended_section"),
                "overall_score": rec.get("quality_score"),
                "video_review_status": rec.get("video_review_status"),
                "evaluated_frames": rec.get("evaluated_frames") or [],
                "reason": rec.get("reason"),
            })

    ai_report = {
        "schema_version": "1.1-client-ai-report",
        "job_id": job_id,
        "folder_name": folder_name,
        "prompt_version": ai_env.get("prompt_version"),
        "prompt_sha256": ai_env.get("prompt_sha256"),
        "include_undated_website": bool(ai_env.get("include_undated_website")),
        "released_asset_count": len(released_assets),
        "pending_rights_count": len(pending_rights_ids),
        "released_asset_ids": [a["asset_id"] for a in released_assets],
        "business_visual_identity": curation_body.get("business_visual_identity"),
        "hero_selection": hero_selection,
        "curated_sections": curated_sections,
        "video_recommendations": video_recommendations,
        "best_images": best_images,
        "best_videos": best_videos,
        "design_art_direction": curation_body.get("design_art_direction"),
        "final_quality_assessment": curation_body.get("final_quality_assessment"),
        "missing_assets": curation_body.get("missing_assets") or [],
        "selection_summary": curation_body.get("selection_summary") or {},
        "note": "Commercial use applies only to rights-verified assets listed in released_asset_ids.",
    }

    released_inventory = []
    for a in released_assets:
        src = manifest_by_id.get(a["asset_id"]) or {}
        released_inventory.append({
            "asset_id": a["asset_id"],
            "filename": a["filename"],
            "target_subfolder": a["target_subfolder"],
            "platform": src.get("platform"),
            "type": src.get("type"),
            "mime": a["mime"],
            "bytes": a["bytes"],
            "sha256": a["sha256"],
            "width": src.get("width"),
            "height": src.get("height"),
            "duration_sec": src.get("duration_sec"),
            "published_at_utc": src.get("published_at_utc")
            or (src.get("post_date") if src.get("date_status") == "verified" else None),
            "publication_date_source": src.get("publication_date_source") or src.get("date_status"),
            "source_url": a.get("source_url"),
            "rights_status": a["rights_status"],
            "rights_evidence_ref": a.get("rights_evidence_ref"),
        })

    inventory_report = {
        "schema_version": "1.0-client-inventory-summary",
        "job_id": job_id,
        "folder_name": folder_name,
        "website_url": manifest.get("website_url"),
        "collection_window": manifest.get("collection_window") or {
            "start_utc": manifest.get("window_start_utc"),
            "end_utc": manifest.get("window_end_utc"),
        },
        "counts": manifest.get("counts"),
        "coverage": manifest.get("coverage"),
        "released_assets": released_inventory,
        "pending_rights_count": len(pending_rights_ids),
    }

    specs = [
        ("ai_curation_report", "ai-curation-report.json", ai_report),
        ("media_inventory_summary", "media-inventory-summary.json", inventory_report),
    ]
    out: list[dict[str, Any]] = []
    for report_id, filename, payload in specs:
        rel = f"delivery_reports/{filename}"
        disk = job_dir / rel
        atomic_write_json(disk, payload)
        raw = disk.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        out.append({
            "report_id": report_id,
            "filename": filename,
            "local_path": rel,
            "n8n_path": "/data/media/" + job_id + "/" + rel,
            "sha256": sha,
            "bytes": len(raw),
            "mime": "application/json",
            "target_subfolder": "Reports",
            "upload_key": f"report:{report_id}:{sha}",
        })
    return out


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
    elif db_path is not None and not _fixture_or_mock(envelope):
        # Unify M4 immutable rights ledger across standard (non-checkpoint) M4-approved jobs too,
        # while keeping pre-M4 historical jobs (no collection_revisions row) backward-compatible.
        import sqlite3
        from contextlib import closing

        has_revision = False
        try:
            with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as con:
                con.execute("PRAGMA busy_timeout=5000;")
                has_table = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_revisions'"
                ).fetchone()
                if has_table:
                    has_revision = bool(
                        con.execute("SELECT 1 FROM collection_revisions WHERE job=? LIMIT 1", (job_id,)).fetchone()
                    )
        except sqlite3.Error:
            has_revision = False
        if has_revision:
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
        safe_name = _safe_delivery_filename(aid, disk, item.get("mime"), sha, source_url=item.get("source_url"))
        if safe_name is None:
            errors.append(f"unsupported delivery mime: {aid}")
            continue
        mime = item.get("mime") or "application/octet-stream"
        assets.append({
            "asset_id": aid,
            "filename": safe_name,
            "local_path": str(item["local_path"]),
            "n8n_path": "/data/media/" + job_id + "/" + str(item["local_path"]),
            "sha256": sha,
            "bytes": len(data),
            "mime": mime,
            "target_subfolder": _target_subfolder_for_mime(mime),
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
    status = "ready" if assets and not errors else ("partial" if assets else "failed")
    if not assets and pending_rights:
        status = "blocked_rights_review"
    if status in ("ready", "partial") and assets:
        _stage_delivery_media(job_dir, job_id, assets)
    pending_uploads = [a for a in assets if a["upload_key"] not in uploaded_keys]
    folder_name = sanitize_folder_name(business_name or manifest.get("website_url", ""), job_id)

    reports: list[dict[str, Any]] = []
    pending_reports: list[dict[str, Any]] = []
    uploaded_report_keys = [
        u["upload_key"] for u in (existing_delivery.get("reports_uploaded") or [])
        if u.get("upload_key") and u.get("status") == "uploaded" and u.get("drive_file_id")
    ]
    if status == "ready":
        reports = _package_client_reports(
            job_dir, job_id, folder_name, manifest, envelope, assets, pending_rights
        )
        pending_reports = [r for r in reports if r["upload_key"] not in uploaded_report_keys]

    existing_subfolders = existing_delivery.get("subfolders")
    subfolders: dict[str, str] = {}
    if isinstance(existing_subfolders, dict):
        for name in _ALLOWED_SUBFOLDERS:
            val = existing_subfolders.get(name)
            if isinstance(val, str) and val.strip():
                subfolders[name] = val.strip()

    return {
        "status": status, "job_id": job_id,
        "folder_name": folder_name,
        "visibility": "private",
        "public_sharing": False,
        "assets": assets, "errors": errors[:30],
        "pending_uploads": pending_uploads,
        "uploaded_keys": uploaded_keys,
        "reports": reports,
        "pending_reports": pending_reports,
        "uploaded_report_keys": uploaded_report_keys,
        "subfolders": subfolders,
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
    report_uploads: list[dict[str, Any]] | None = None,
    subfolders: dict[str, Any] | None = None,
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
    existing_doc: dict[str, Any] = {}
    existing_path = job_dir / "delivery.json"
    if existing_path.is_file():
        try:
            loaded = json.loads(existing_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_doc = loaded
        except (OSError, json.JSONDecodeError):
            existing_doc = {}
    existing_ledger = list(existing_doc.get("uploaded") or [])
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
            "target_subfolder": asset.get("target_subfolder"),
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

    # Merge client report uploads (AI curation report + media inventory summary) idempotently.
    report_by_key = {r["upload_key"]: r for r in (plan.get("reports") or []) if r.get("upload_key")}
    existing_reports = list(existing_doc.get("reports_uploaded") or [])
    by_report = {row["upload_key"]: row for row in existing_reports if row.get("upload_key")}
    for ru in (report_uploads or []):
        key = str(ru.get("upload_key") or "")
        rep = report_by_key.get(key)
        if rep is None:
            continue
        prev_r = by_report.get(key, {})
        if prev_r.get("status") == "uploaded" and prev_r.get("drive_file_id"):
            continue
        by_report[key] = {
            "report_id": rep["report_id"],
            "upload_key": key,
            "filename": rep["filename"],
            "target_subfolder": rep.get("target_subfolder", "Reports"),
            "drive_file_id": ru.get("drive_file_id"),
            "drive_link": ru.get("drive_link"),
            "status": ru.get("status", "failed"),
            "error": ru.get("error"),
            "sha256": rep["sha256"],
        }
    report_ledger = list(by_report.values())
    reports_ok = [
        r for r in report_ledger
        if r["upload_key"] in report_by_key and r["status"] == "uploaded" and r["drive_file_id"]
    ]
    if not report_by_key:
        reports_status = "none"
    elif len(reports_ok) == len(report_by_key):
        reports_status = "completed"
    elif reports_ok:
        reports_status = "partial_success"
    else:
        reports_status = "pending" if not report_uploads else "failed"

    merged_subfolders: dict[str, str] = {}
    for src_map in (existing_doc.get("subfolders"), plan.get("subfolders"), subfolders):
        if isinstance(src_map, dict):
            for name in _ALLOWED_SUBFOLDERS:
                val = src_map.get(name)
                if isinstance(val, str) and val.strip():
                    merged_subfolders[name] = val.strip()

    result = {
        "schema_version": "1.1-delivery", "job_id": plan.get("job_id"),
        "status": status,
        "reports_status": reports_status,
        "folder_name": plan.get("folder_name"),
        "visibility": "private",
        "public_sharing": False,
        "drive_folder_id": plan.get("drive_folder_id") or drive_folder_id,
        "drive_folder_link": plan.get("drive_folder_link") or drive_folder_link,
        "subfolders": merged_subfolders,
        "authorization_confirmed": bool(authorization_confirmed),
        "uploaded": ledger,
        "reports_uploaded": report_ledger,
        "pending_rights_ids": plan.get("pending_rights_ids", []),
        "errors": plan.get("errors", []),
        "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "note": "Mock/fixture curation is never released as a customer deliverable. "
        "Commercial use requires explicit operator authorization.",
    }
    atomic_write_json(job_dir / "delivery.json", result)
    return result


def json_load(path: Path) -> dict[str, Any]:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
