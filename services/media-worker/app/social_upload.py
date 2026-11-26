"""Owner-supplied Facebook media & official Export ZIP ingestion (100% open-source, zero Meta API).

Allows an authenticated Named Admin to upload either:
- An official Facebook "Download Page Information" ZIP archive (containing media
  files + JSON post/photo/video metadata with `creation_timestamp` matched by URI),
- A plain ZIP folder of client Facebook photos/videos, or
- A single direct image/video file (.jpg, .jpeg, .png, .webp, .gif, .avif, .mp4, .mov, .webm).

Security & honesty invariants:
- Strict zip-slip, symlink, encrypted-entry, file-count (<= 60), single-file (<= 25 MB),
  and total uncompressed size (<= 150 MB) bounds.
- Every accepted binary passes magic-byte validation (`_verify_media_magic`).
- Publication dates come ONLY from an exact matched Facebook Export JSON `creation_timestamp`
  (or optional explicit operator-attested `YYYY-MM-DD` date) and are evaluated against the
  job's immutable frozen 365-day window; otherwise assets stay honestly `unknown_date`.
- `rights_status` ALWAYS starts `"unverified"`; M4 two-admin approval and per-asset rights
  verification remain strictly required before Groq AI or Google Drive delivery.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from typing import Any
import zipfile

from fastapi import HTTPException

from app.collection_checkpoint import _verify_media_magic
from app.manifest import validate_against_disk, write_manifest
from app.preprocessing import run_media_preprocessing
from app.publication import publication_evidence, record_publication_review, window_bounds

MAX_UPLOAD_BYTES = 100 * 1024 * 1024        # 100 MB raw request cap
MAX_SINGLE_FILE_BYTES = 25 * 1024 * 1024    # 25 MB per image/video
MAX_TOTAL_UNCOMPRESSED_BYTES = 150 * 1024 * 1024
MAX_MEDIA_FILES_PER_UPLOAD = 60
MAX_JSON_META_BYTES = 4 * 1024 * 1024       # 4 MB per Facebook export JSON file

_EXT_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IGNORED_ZIP_PREFIXES = ("__macosx/",)


def _normalize_zip_relpath(raw_name: str) -> str | None:
    """Reject zip-slip, absolute paths, drive prefixes, NUL bytes, and hidden macOS junk."""
    if not raw_name or "\x00" in raw_name or "\\" in raw_name:
        return None
    if PureWindowsPath(raw_name).drive:
        return None
    posix = PurePosixPath(raw_name)
    if posix.is_absolute() or ".." in posix.parts:
        return None
    cleaned = "/".join(part for part in posix.parts if part not in ("", "."))
    if not cleaned:
        return None
    low = cleaned.lower()
    if any(low.startswith(prefix) for prefix in _IGNORED_ZIP_PREFIXES):
        return None
    if posix.name.startswith("._"):
        return None
    return cleaned


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return bool(mode and stat.S_ISLNK(mode))


def _sanitize_filename(name: str, fallback_ext: str) -> str:
    base = Path(name).name
    stem = _SAFE_NAME_RE.sub("_", Path(base).stem).strip("._") or "media"
    ext = Path(base).suffix.lower()
    if ext not in _EXT_TO_MIME:
        ext = fallback_ext
    return f"{stem[:64]}{ext}"


def _fix_fb_mojibake(text: str) -> str:
    """Official Facebook JSON exports encode UTF-8 strings as latin-1 escapes; decode safely."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _extract_fb_json_metadata(zf: zipfile.ZipFile) -> dict[str, dict[str, Any]]:
    """Index Facebook Export JSON `uri -> {creation_timestamp, caption}` entries inside the ZIP."""
    index: dict[str, dict[str, Any]] = {}

    def _walk(node: Any, parent_caption: str | None = None, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(node, list):
            for child in node:
                _walk(child, parent_caption, depth + 1)
            return
        if not isinstance(node, dict):
            return
        local_caption = parent_caption
        for cap_key in ("description", "title", "post"):
            val = node.get(cap_key)
            if isinstance(val, str) and val.strip():
                local_caption = _fix_fb_mojibake(val.strip())[:400]
                break
        uri = node.get("uri")
        if isinstance(uri, str) and uri.strip():
            norm_uri = _normalize_zip_relpath(uri.strip().lstrip("/"))
            ts = node.get("creation_timestamp")
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                media_meta = node.get("media_metadata") or {}
                if isinstance(media_meta, dict):
                    for meta_branch in media_meta.values():
                        if isinstance(meta_branch, dict):
                            exif = meta_branch.get("exif_data")
                            if isinstance(exif, list) and exif and isinstance(exif[0], dict):
                                taken = exif[0].get("taken_timestamp")
                                if isinstance(taken, (int, float)) and not isinstance(taken, bool):
                                    ts = taken
                                    break
            if norm_uri:
                entry = index.setdefault(norm_uri.lower(), {})
                if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
                    entry["creation_timestamp"] = float(ts)
                if local_caption and not entry.get("caption"):
                    entry["caption"] = local_caption
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk(value, local_caption, depth + 1)

    for info in zf.infolist():
        if info.is_dir() or _is_zip_symlink(info):
            continue
        if info.flag_bits & 0x1:  # encrypted
            continue
        norm = _normalize_zip_relpath(info.filename)
        if not norm or not norm.lower().endswith(".json"):
            continue
        if info.file_size <= 0 or info.file_size > MAX_JSON_META_BYTES:
            continue
        try:
            raw = zf.read(info)
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except (zipfile.BadZipFile, OSError, ValueError):
            continue
        _walk(parsed)
    return index


def _lookup_fb_meta(rel_path: str, fb_index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Match a ZIP member against Facebook Export JSON URI (exact path or suffix after top folder)."""
    low = rel_path.lower()
    if low in fb_index:
        return fb_index[low]
    # Handle archives wrapped in a single top-level directory (e.g. facebook-export/posts/media/a.jpg)
    parts = low.split("/", 1)
    if len(parts) == 2 and parts[1] in fb_index:
        return fb_index[parts[1]]
    return None


def _parse_attested_date(raw_date: str | None) -> str | None:
    if raw_date is None or not str(raw_date).strip():
        return None
    clean = str(raw_date).strip()
    if not _DATE_RE.fullmatch(clean):
        raise HTTPException(400, "published_date must use YYYY-MM-DD format")
    try:
        dt = datetime.strptime(clean, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HTTPException(400, "Invalid calendar date for published_date") from exc
    return dt.isoformat()


def parse_framed_upload(raw_body: bytes) -> tuple[dict[str, Any], bytes]:
    """Parse `<utf8-json-header>\\n<binary-payload>` signed as one byte sequence."""
    sep = raw_body.find(b"\n")
    if sep <= 0 or sep > 4096:
        raise HTTPException(400, "Malformed media upload envelope")
    try:
        meta = json.loads(raw_body[:sep].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "Invalid upload metadata JSON") from None
    if not isinstance(meta, dict):
        raise HTTPException(400, "Invalid upload metadata object")
    payload = raw_body[sep + 1 :]
    if not payload:
        raise HTTPException(400, "Uploaded file is empty")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Upload exceeds 100 MB limit")
    return meta, payload


def _extract_candidates(
    filename: str,
    payload: bytes,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return (accepted_media_items, skipped_entries) from either a ZIP archive or single file."""
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    ext = Path(filename).suffix.lower()

    if ext == ".zip" or payload[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "Uploaded file is not a valid ZIP archive") from exc
        with zf:
            fb_index = _extract_fb_json_metadata(zf)
            total_uncompressed = 0
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if _is_zip_symlink(info):
                    skipped.append({"entry": info.filename[:120], "reason": "symlink_blocked"})
                    continue
                if info.flag_bits & 0x1:
                    skipped.append({"entry": info.filename[:120], "reason": "encrypted_entry_unsupported"})
                    continue
                norm = _normalize_zip_relpath(info.filename)
                if not norm:
                    skipped.append({"entry": info.filename[:120], "reason": "unsafe_path_blocked"})
                    continue
                member_ext = Path(norm).suffix.lower()
                if member_ext not in _EXT_TO_MIME:
                    continue
                if len(accepted) >= MAX_MEDIA_FILES_PER_UPLOAD:
                    skipped.append({"entry": norm[:120], "reason": "max_files_per_upload_reached"})
                    continue
                if info.file_size <= 0 or info.file_size > MAX_SINGLE_FILE_BYTES:
                    skipped.append({"entry": norm[:120], "reason": "file_size_out_of_bounds"})
                    continue
                if total_uncompressed + info.file_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    skipped.append({"entry": norm[:120], "reason": "archive_byte_budget_exceeded"})
                    continue
                # Stream-read with hard cap to defend against spoofed zip header sizes.
                with zf.open(info, "r") as member_stream:
                    data = member_stream.read(MAX_SINGLE_FILE_BYTES + 1)
                if len(data) == 0 or len(data) > MAX_SINGLE_FILE_BYTES:
                    skipped.append({"entry": norm[:120], "reason": "file_size_out_of_bounds"})
                    continue
                if total_uncompressed + len(data) > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    skipped.append({"entry": norm[:120], "reason": "archive_byte_budget_exceeded"})
                    continue
                total_uncompressed += len(data)
                fb_meta = _lookup_fb_meta(norm, fb_index)
                accepted.append({
                    "rel_source": norm,
                    "filename": _sanitize_filename(norm, member_ext),
                    "mime": _EXT_TO_MIME[member_ext],
                    "bytes": data,
                    "fb_meta": fb_meta,
                })
        return accepted, skipped

    if ext not in _EXT_TO_MIME:
        raise HTTPException(
            400,
            "Unsupported file type. Upload a .zip archive or a JPEG, PNG, WebP, GIF, AVIF, MP4, MOV, or WebM file.",
        )
    if len(payload) > MAX_SINGLE_FILE_BYTES:
        raise HTTPException(413, "Single media file exceeds 25 MB limit")
    accepted.append({
        "rel_source": Path(filename).name,
        "filename": _sanitize_filename(filename, ext),
        "mime": _EXT_TO_MIME[ext],
        "bytes": payload,
        "fb_meta": None,
    })
    return accepted, skipped


def ingest_owner_media_upload(
    job_dir: Path,
    job: dict[str, Any],
    *,
    platform: str,
    filename: str,
    payload: bytes,
    actor: str,
    published_date: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate, persist, preprocess, and merge owner-supplied media into `manifest.json`.

    Returns `(updated_manifest, upload_summary)`. Caller is responsible for
    publishing the new immutable M4 collection revision via `store(..., collection_base=...)`.
    """
    if platform != "facebook":
        raise HTTPException(400, "Only platform='facebook' owner uploads are supported in V2.1")
    job_id = str(job.get("job_id") or "")
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(400, "Invalid job_id")
    if (job_dir / "curation.json").exists():
        raise HTTPException(409, "Cannot add media after AI curation has completed for this job")
    if job.get("status") in ("queued", "running"):
        raise HTTPException(409, "Wait for active collection to finish before uploading media")

    manifest_path = job_dir / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(409, "Job manifest not yet available; wait for initial collection to complete")
    if window_bounds(job) is None:
        raise HTTPException(409, "Job is missing a valid frozen 365-day window")

    attested_iso = _parse_attested_date(published_date)
    candidates, skipped = _extract_candidates(filename, payload)
    if not candidates:
        raise HTTPException(
            400,
            "No supported image or video files found in upload (supported: jpg, png, webp, gif, avif, mp4, mov, webm).",
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets: list[dict[str, Any]] = list(manifest.get("assets") or [])
    existing_ids = {str(a.get("asset_id") or "") for a in assets}
    existing_shas = {
        str(a.get("sha256") or "")
        for a in assets
        if a.get("extraction_status") == "downloaded" and a.get("sha256")
    }

    upload_dir = job_dir / "owner_uploads" / platform
    upload_dir.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now(UTC).isoformat()

    added_ids: list[str] = []
    duplicate_count = 0
    invalid_magic_count = 0
    in_window_count = 0
    out_of_window_count = 0
    unknown_date_count = 0

    for item in candidates:
        raw_bytes: bytes = item["bytes"]
        sha = hashlib.sha256(raw_bytes).hexdigest()
        if sha in existing_shas:
            duplicate_count += 1
            skipped.append({"entry": item["rel_source"][:120], "reason": "duplicate_sha256"})
            continue

        target_name = f"{sha[:12]}_{item['filename']}"
        target_path = upload_dir / target_name
        tmp_path = upload_dir / f".{target_name}.tmp"
        tmp_path.write_bytes(raw_bytes)
        try:
            if not _verify_media_magic(tmp_path, item["mime"]):
                invalid_magic_count += 1
                skipped.append({"entry": item["rel_source"][:120], "reason": "invalid_media_magic"})
                tmp_path.unlink(missing_ok=True)
                continue
            os.replace(tmp_path, target_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        asset_id = f"fb_upload_{sha[:20]}"
        idx = 1
        while asset_id in existing_ids:
            asset_id = f"fb_upload_{sha[:16]}_{idx}"
            idx += 1
        existing_ids.add(asset_id)
        existing_shas.add(sha)

        fb_page_url = str(job.get("facebook_url") or "").strip()
        source_url = (
            f"{fb_page_url}#owner-upload-{sha[:16]}"
            if fb_page_url.startswith(("https://", "http://"))
            else f"urn:owner-upload:{platform}:{sha}"
        )
        fb_meta = item.get("fb_meta") or {}
        evidence = None
        if isinstance(fb_meta.get("creation_timestamp"), (int, float)):
            try:
                fb_iso = datetime.fromtimestamp(float(fb_meta["creation_timestamp"]), tz=UTC).isoformat()
            except (ValueError, OverflowError, OSError):
                fb_iso = None
            if fb_iso is not None:
                evidence = publication_evidence(
                    fb_iso,
                    source_url=source_url,
                    field="creation_timestamp",
                    attribution="owner_supplied_facebook_export_json",
                )
        elif attested_iso is not None:
            evidence = publication_evidence(
                attested_iso,
                source_url=source_url,
                field="owner_attested_published_date",
                attribution="owner_attested_upload_date",
            )

        kind = "video" if item["mime"].startswith("video/") else "image"
        rel_local = f"owner_uploads/{platform}/{target_name}"
        asset_doc: dict[str, Any] = {
            "asset_id": asset_id,
            "job_id": job_id,
            "platform": platform,
            "type": kind,
            "kind": kind,
            "filename": item["filename"],
            "local_path": rel_local,
            "source_url": source_url,
            "source_page": str(job.get("facebook_url") or source_url),
            "mime": item["mime"],
            "bytes": len(raw_bytes),
            "sha256": sha,
            "captured_at": captured_at,
            "post_date": evidence["timestamp_utc"] if evidence else None,
            "date_status": "verified" if evidence else "unknown_date",
            "publication_evidence": evidence,
            "rights_status": "unverified",
            "extraction_status": "downloaded",
            "detail": {
                "origin": "owner_upload",
                "uploaded_by": actor,
                "archive_member": item["rel_source"][:200],
            },
        }
        if fb_meta.get("caption"):
            asset_doc["source_caption"] = str(fb_meta["caption"])[:400]
        assets.append(asset_doc)
        added_ids.append(asset_id)

    if not added_ids and duplicate_count == 0:
        raise HTTPException(
            400,
            "No valid media files were accepted (all files failed magic-byte or size validation).",
        )

    manifest["assets"] = assets
    record_publication_review(manifest, job)

    # Count window outcomes strictly for newly uploaded assets.
    added_set = set(added_ids)
    for asset in manifest["assets"]:
        if asset.get("asset_id") in added_set:
            elig = asset.get("date_eligibility")
            if elig == "in_window":
                in_window_count += 1
            elif elig == "out_of_window":
                out_of_window_count += 1
            else:
                unknown_date_count += 1

    # Update Facebook source summary honestly without erasing public discovery metadata.
    sources = manifest.setdefault("sources", {})
    fb_source = dict(sources.get("facebook") or {})
    fb_issues = list(fb_source.get("issues") or [])
    if "owner_media_uploaded" not in fb_issues and added_ids:
        fb_issues.append("owner_media_uploaded")
    fb_source["issues"] = fb_issues
    fb_source["owner_uploads_added"] = int(fb_source.get("owner_uploads_added") or 0) + len(added_ids)
    sources["facebook"] = fb_source

    manifest["unknown_date_review"] = [
        a["asset_id"]
        for a in manifest["assets"]
        if a.get("type") in ("image", "video") and a.get("date_status") == "unknown_date"
    ]

    problems = validate_against_disk(job_dir, manifest)
    if problems:
        raise HTTPException(500, f"Uploaded manifest integrity failure: {problems[0]}")
    write_manifest(job_dir, manifest)

    # Run Pillow quality metrics, perceptual deduplication, and FFmpeg video keyframe extraction.
    if added_ids:
        run_media_preprocessing(
            job_dir,
            job_id,
            cutoff_date=(job.get("window_start_utc") or "")[:10] or None,
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    summary = {
        "ok": True,
        "job_id": job_id,
        "platform": platform,
        "uploaded_count": len(added_ids),
        "uploaded_asset_ids": added_ids,
        "in_window_count": in_window_count,
        "out_of_window_count": out_of_window_count,
        "unknown_date_count": unknown_date_count,
        "duplicate_count": duplicate_count,
        "invalid_magic_count": invalid_magic_count,
        "skipped": skipped[:25],
    }
    return manifest, summary
