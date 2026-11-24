"""Normalized media manifest builder for the Phase 2 website collector.

Contract (Phase 2 approval + docs/PROJECT_SPEC.md §08):

Every manifest item carries asset_id, source URL, local path, media type,
filename, dimensions (where available), date status, file size, sha256 and
extraction status.

Honesty rules baked into the builder:
- Website assets without media-specific publication evidence keep `post_date`
  null and `date_status` set to "unknown_date".
- Public visibility is not permission: `rights_status` starts "unverified"
  (PROJECT_SPEC §14).
- Failures (robots-blocked, too-large, invalid, network errors, skips) are
  represented truthfully per item; they never become fake successes.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import tempfile
from datetime import UTC, datetime, timedelta
from app.publication import record_publication_review
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "3.0-unified"

SOCIAL_PREFIXES = {"instagram": "ig", "facebook": "fb"}
_PLATFORM_STATUSES = (
    "completed",
    "partial_success",
    "failed",
    "invalid_url",
    "restricted",
    "rate_limited",
    "inaccessible",
)

DOWNLOADED = "downloaded"
DUPLICATE = "duplicate"
_FAILED_STATUSES = {
    "failed",
    "invalid_image",
    "too_large",
    "empty",
    "not_recognized_media",
    "blocked_by_robots",
    "integrity_mismatch",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write_json(target: str | Path, payload: dict[str, Any]) -> None:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _media_kind(mime: Any) -> str:
    mime = str(mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    return "other"


def _asset_id(index: int, sha: Any, url: Any) -> str:
    if isinstance(sha, str) and len(sha) >= 12:
        return f"web_{index:04d}_{sha[:12]}"
    digest = hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()[:12]
    return f"web_{index:04d}_{digest}"


def build_item(index: int, row: dict[str, Any], *, job_id: str, media_dir: str = "website") -> dict[str, Any]:
    """Convert one legacy collector row into a normalized manifest item."""
    status = str(row.get("status") or "unknown")
    sha = row.get("sha256")
    rel_file = str(row.get("file") or row.get("same_as") or "")
    filename = Path(rel_file).name if rel_file else None
    dims = row.get("dimensions")
    width = height = None
    if isinstance(dims, list) and len(dims) == 2:
        try:
            width, height = int(dims[0]), int(dims[1])
        except (TypeError, ValueError):
            width = height = None

    detail: dict[str, Any] | None = None
    if row.get("error"):
        detail = {"error": str(row["error"])[:240]}
    elif row.get("same_as"):
        detail = {"same_as": "website/" + Path(str(row["same_as"])).name}
    elif row.get("limit_bytes") is not None:
        detail = {"limit_bytes": row["limit_bytes"]}

    return {
        "asset_id": _asset_id(index, sha, row.get("url")),
        "job_id": job_id,
        "platform": "website",
        "type": _media_kind(row.get("mime")),
        "source_url": row.get("url"),
        "source_page": row.get("source_page"),
        "filename": filename,
        "local_path": f"{media_dir}/{filename}" if status in (DOWNLOADED, DUPLICATE) and filename else None,
        "mime": row.get("mime"),
        "bytes": row.get("bytes"),
        "width": width,
        "height": height,
        "sha256": sha,
        "captured_at": row.get("captured_at"),
        "post_date": (row.get("publication_evidence") or {}).get("timestamp_utc"),
        "date_status": "verified" if row.get("publication_evidence") else "unknown_date",
        "publication_evidence": row.get("publication_evidence"),
        "rights_status": "unverified",
        "extraction_status": status,
        "detail": detail,
    }


def summarize(rows: list[dict[str, Any]], *, pages_crawled: int, bytes_downloaded: int) -> dict[str, Any]:
    downloaded = sum(1 for r in rows if r.get("status") == DOWNLOADED)
    duplicates = sum(1 for r in rows if r.get("status") == DUPLICATE)
    failed = sum(1 for r in rows if r.get("status") in _FAILED_STATUSES)
    skipped = sum(1 for r in rows if str(r.get("status") or "").startswith("skipped_"))
    if pages_crawled == 0:
        source_status = "failed"
    elif downloaded == 0 and failed > 0:
        source_status = "failed"
    elif failed > 0 or skipped > 0:
        source_status = "partial_success"
    else:
        source_status = "completed"
    return {
        "downloaded": downloaded,
        "duplicates": duplicates,
        "failed": failed,
        "skipped": skipped,
        "pages_crawled": pages_crawled,
        "bytes_downloaded": bytes_downloaded,
        "source_status": source_status,
    }


def build_manifest(
    *,
    job_id: str,
    website_url: str,
    rows: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    issues: list[dict[str, Any]] | None,
    limits: dict[str, int],
    pages_crawled: int,
    asset_candidates: int,
    bytes_downloaded: int,
    media_dir: str = "website",
) -> dict[str, Any]:
    counts = summarize(rows, pages_crawled=pages_crawled, bytes_downloaded=bytes_downloaded)
    injection_hits = 0
    for page in pages:
        hits = page.get("prompt_injection_hits")
        if isinstance(hits, list):
            injection_hits += len(hits)
    assets = [build_item(i, row, job_id=job_id, media_dir=media_dir) for i, row in enumerate(rows, start=1)]
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "website_url": website_url,
        "generated_at": utc_now_iso(),
        "source_status": counts["source_status"],
        "limits": {"max_pages": limits.get("max_pages"), "max_media": limits.get("max_media")},
        "counts": {
            "pages_crawled": pages_crawled,
            "asset_candidates": asset_candidates,
            **{k: counts[k] for k in ("downloaded", "duplicates", "failed", "skipped", "bytes_downloaded")},
        },
        "coverage": {
            "prompt_injection_hits_in_page_text": injection_hits,
            "issues": [issue for issue in (issues or [])][:50],
        },
        "notes": [
            "Website media without attributable publication evidence has date_status 'unknown_date'.",
            "Public visibility is not permission; rights_status starts 'unverified'.",
            "Files live under the job folder; local_path is relative to /data/projects/<job_id>.",
        ],
        "assets": assets,
    }


def write_manifest(job_dir: str | Path, manifest: dict[str, Any]) -> Path:
    target = Path(job_dir) / "manifest.json"
    atomic_write_json(target, manifest)
    return target


def validate_against_disk(job_dir: str | Path, manifest: dict[str, Any]) -> list[str]:
    """Deterministic post-write validation: paths must exist and hashes must match.

    Returns a list of problems (empty = valid). Never mutates the manifest.
    """
    problems: list[str] = []
    root = Path(job_dir)
    seen_ids: set[str] = set()
    for item in manifest.get("assets", []):
        asset_id = item.get("asset_id")
        if not asset_id or asset_id in seen_ids:
            problems.append(f"asset_id missing or duplicated: {asset_id!r}")
            continue
        seen_ids.add(asset_id)
        if item.get("extraction_status") not in (DOWNLOADED, "extracted_frame"):
            continue
        rel = item.get("local_path")
        if not rel or isinstance(rel, str) and (rel.startswith("/") or ".." in Path(rel).parts):
            problems.append(f"{asset_id}: unsafe local_path {rel!r}")
            continue
        disk = root / str(rel)
        if not disk.is_file():
            problems.append(f"{asset_id}: missing file {rel}")
            continue
        data = disk.read_bytes()
        if item.get("bytes") is not None and int(item["bytes"]) != len(data):
            problems.append(f"{asset_id}: byte size mismatch for {rel}")
        expected = item.get("sha256")
        if isinstance(expected, str):
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                problems.append(f"{asset_id}: sha256 mismatch for {rel}")
    return problems


def _infer_mime(filename: str) -> str | None:
    ctype, _ = mimetypes.guess_type(str(filename))
    return ctype


def build_social_item(
    index: int,
    row: dict[str, Any],
    *,
    job_id: str,
    platform: str,
    media_dir: str,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """Normalize one social row (legacy gallery-dl row or fallback row)."""
    prefix = SOCIAL_PREFIXES.get(platform, "".join(c for c in platform if c.isalpha())[:2])
    sha = row.get("sha256")
    rel_file = str(row.get("file") or "")
    filename = Path(rel_file).name if rel_file else None
    mime = row.get("mime") or (filename and _infer_mime(filename)) or None
    evidence = row.get("publication_evidence")
    detail: dict[str, Any] = {"origin": row.get("source") or "gallery_dl"}
    if row.get("author"):
        detail["author"] = row["author"]
    if row.get("optimized_file"):
        detail["metadata_stripped_copy"] = f"research/optimized/{Path(str(row['optimized_file'])).name}"
    return {
        "asset_id": _asset_id_social(index, prefix, sha, row.get("source_post_url")),
        "job_id": job_id,
        "platform": platform,
        "type": _media_kind(mime),
        "source_url": row.get("source_post_url") or row.get("original_post_url") or row.get("source_profile_url"),
        "source_page": row.get("source_profile_url") or row.get("source_profile_url"),
        "filename": filename,
        "local_path": f"{media_dir}/{filename}" if filename else None,
        "mime": mime,
        "bytes": row.get("bytes"),
        "width": width,
        "height": height,
        "sha256": sha,
        "captured_at": row.get("captured_at") or utc_now_iso(),
        "post_date": evidence.get("timestamp_utc") if evidence else None,
        "date_status": "verified" if evidence else "unknown_date",
        "publication_evidence": evidence,
        "rights_status": "unverified",
        "extraction_status": "downloaded",
        "detail": detail,
    }


def _asset_id_social(index: int, prefix: str, sha: Any, url: Any) -> str:
    if isinstance(sha, str) and len(sha) >= 12:
        return f"{prefix}_{index:04d}_{sha[:12]}"
    digest = hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{index:04d}_{digest}"


def merge_social_into_manifest(
    website_manifest: dict[str, Any] | None,
    social_sections: list[tuple[str, dict[str, Any]]],
    *,
    job_id: str,
    lookback_days: int | None,
    permission_confirmed: bool | None,
    collection_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce the unified multi-source manifest document.

    Keeps the Phase-2 top-level website fields (counts/coverage) for backward
    compatibility and adds `sources` summaries plus social assets.
    """
    merged: dict[str, Any] = dict(website_manifest) if website_manifest else {}
    merged["job_id"] = job_id
    merged["schema_version"] = SCHEMA_VERSION
    merged["generated_at"] = utc_now_iso()
    merged["lookback_days"] = lookback_days
    merged["social_permission_confirmed"] = permission_confirmed
    if collection_job and collection_job.get("window_start_utc"):
        merged["cutoff_date"] = collection_job["window_start_utc"][:10]
        merged["lookback_days"] = 365
    elif collection_job is None and lookback_days is not None:
        merged["cutoff_date"] = (datetime.now(UTC) - timedelta(days=lookback_days)).date().isoformat()

    sources: dict[str, Any] = {}
    if website_manifest:
        sources["website"] = {
            "status": website_manifest.get("source_status"),
            "counts": website_manifest.get("counts"),
        }
    assets: list[dict[str, Any]] = list(website_manifest.get("assets", [])) if website_manifest else []
    for platform, section in social_sections:
        sources[platform] = {k: v for k, v in section.items() if k != "items"}
        assets.extend(section.get("items", []))
    merged["sources"] = sources
    merged["assets"] = assets
    if collection_job is not None:
        record_publication_review(merged, collection_job)

    attempted = [
        s.get("status")
        for s in sources.values()
        if s.get("status") in _PLATFORM_STATUSES
    ]
    if not attempted:
        merged["source_status"] = (website_manifest or {}).get("source_status", "failed")
    elif all(s == "completed" for s in attempted):
        merged["source_status"] = "completed"
    elif any(s in ("completed", "partial_success") for s in attempted):
        merged["source_status"] = "partial_success"
    else:
        merged["source_status"] = "failed"

    notes = list(merged.get("notes") or [])
    if social_sections:
        notes.append(
            "Operator acknowledgement of permitted use is NOT platform authorization "
            "and NOT a copyright licence; rights remain unverified per asset."
        )
    merged["notes"] = notes
    return merged
