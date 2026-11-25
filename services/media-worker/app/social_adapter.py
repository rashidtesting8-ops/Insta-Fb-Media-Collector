"""Phase 3 social media collection adapters (Instagram + Facebook).

Design (per Phase 3 approval + docs/PROJECT_SPEC.md §07):

- gallery-dl is PRIMARY, executed through the byte-for-byte vendored legacy
  module `app/legacy_social_collect.py` (SHA-256 328ebcc00943…). That module
  contributes: argument-array subprocess execution, `--dump-json` metadata
  dry-run, `--download-archive` idempotency, per-item/staging/project size
  caps, robots-free public HTTPS URL allowlist (`check_social_url`),
  failure classification, secret scrubbing, originals kept separately from
  metadata-stripped copies, and its explicit permission gate.
- Instaloader is an Instagram-specific fallback (anonymous, bounded,
  aborts on 403/429 — never hammers a rate-limited source).
- yt-dlp is a fallback for supported Facebook *individual* post/video URLs
  only (page-level scraping is not attempted).
- The operator-consent gate: collection only runs when the job request sets
  `social_permission_confirmed=true`. That acknowledgement is an operator
  statement of intended, permitted use — it is NOT platform authorization and
  NOT a copyright licence, and it never bypasses login walls, CAPTCHAs,
  checkpoints or rate limits (those are reported honestly as statuses).
- Limits are configurable and bounded per request (`max_social_files`,
  `lookback_days`); no legacy fixed 40/80 assumptions.
- Media is relocated to `<job>/instagram/` and `<job>/facebook/`; metadata-
  stripped copies are kept under `<job>/research/optimized/`.
- Dates: only platform-provided timestamps are trusted (`date_status`
  "verified"); anything else stays "unknown" — never invented.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from PIL import Image, UnidentifiedImageError

from app.legacy_social_collect import (
    _execute_subprocess,
    check_social_url,
    classify_social_failure,
    collect_social,
)
from app.manifest import build_social_item, validate_against_disk
from app.publication import aware_timestamp, publication_evidence
from app.security import sanitized_subprocess_env

RunnerFn = Callable[[list[str], Path], tuple[int, str]]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | {".mp4", ".webm", ".mov"}
MAX_SOCIAL_ITEM_BYTES = 25 * 1024 * 1024  # mirrors the legacy cap

# ---------------------------------------------------------------------------
# Source-level status taxonomy (Phase 3 acceptance requirement #2).
#   completed        - files downloaded, no gating/failure evidence
#   partial_success  - some files plus gating/failure evidence (or fallback use)
#   restricted       - platform access restriction (login wall / access gate)
#   rate_limited     - rate limited; never retried or fallback-hammered
#   inaccessible     - source/profile not found or unreachable
#   failed           - tool/adapter errors without a more precise class
#   permission_required / invalid_url / not_requested - pre-execution states
# ---------------------------------------------------------------------------
_RATE_LIMITED_REASONS = {"rate_limited"}
_RESTRICTED_REASONS = {"login_required", "gallery_dl_error_6"}
_INACCESSIBLE_REASONS = {"gallery_dl_error_4", "not_found", "profile_not_found"}


def _empty_source_status(reason: str | None) -> str:
    if reason in _RATE_LIMITED_REASONS:
        return "rate_limited"
    if reason in _RESTRICTED_REASONS:
        return "restricted"
    if reason in _INACCESSIBLE_REASONS:
        return "inaccessible"
    return "failed"


def _tool_version(executable: str, runner: RunnerFn | None) -> str | None:
    """Best-effort tool version capture. Live mode only: with a test runner
    injected (offline mode) no extra subprocess is fired."""
    if runner is not None:
        return None
    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            env=sanitized_subprocess_env(allow_proxy=False),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or proc.stderr or "").strip().splitlines()
    return lines[0][:60] if lines else None


_FALLBACK_IG_REASONS = {
    "login_required",
    "extractor_broken",
    # Observed 2026-11-20: gallery-dl emits access-gated error envelopes
    # (code 6) with exit 0 for anonymous Instagram access. Exact code
    # semantics are version-dependent; only the access-gate case triggers
    # the fallback — rate limits never do.
    "gallery_dl_error_6",
}
_GALLERY_ERROR_RE = re.compile(r"\[\s*(\d+)\s*,")
_FB_SINGLE_URL_RE = re.compile(
    r"(?:"
    r"(?:facebook\.com|fb\.watch)/(?:[^?#]*/)?(?:posts|videos|watch|reel|story\.php|photo)[/?]"
    r"|fb\.watch/[A-Za-z0-9]+/?"
    r")",
    re.I,
)


@dataclass
class SocialExtractionResult:
    job_id: str
    platform: str
    url: str
    # completed | partial_success | restricted | rate_limited | inaccessible |
    # failed | permission_required | invalid_url | not_requested
    status: str
    files_added: int = 0
    duplicates: int = 0
    bytes_downloaded: int = 0
    reason: str | None = None
    fallback_attempted: str | None = None
    fallback_outcome: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Exactly which tools executed for this source and their outcomes (req #8).
    tools: list[dict[str, Any]] = field(default_factory=list)
    # Honest coverage metadata (req #1/#9): a bounded sample, never a claim of
    # full 365-day extraction.
    coverage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("items")
        return data


def section_from_result(result: SocialExtractionResult) -> dict[str, Any]:
    """Manifest/job-record section for one social source (items included)."""
    return {
        "status": result.status,
        "reason": result.reason,
        "url": result.url,
        "fallback_attempted": result.fallback_attempted,
        "fallback_outcome": result.fallback_outcome,
        "tools_executed": result.tools,
        "coverage": result.coverage,
        "counts": {
            "files_added": result.files_added,
            "duplicates": result.duplicates,
            "bytes_downloaded": result.bytes_downloaded,
        },
        "notes": result.notes,
        "items": result.items,
    }


def _dims_for(path: Path) -> tuple[int | None, int | None]:
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None, None
    try:
        with Image.open(path) as im:
            return im.width, im.height
    except (UnidentifiedImageError, OSError, ValueError):
        return None, None


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _instaloader_fallback(
    profile_url: str,
    stage: Path,
    *,
    max_files: int,
    cutoff: date,
    timeout: int,
    runner: RunnerFn | None = None,
    executable: str = "instaloader",
) -> tuple[list[dict[str, Any]], str | None]:
    """Anonymous, bounded Instaloader run. Returns (rows, failure_reason)."""
    profile = (re.sub(r"/+$", "", profile_url.rstrip("/")).rsplit("/", maxsplit=1)[-1]).lower()
    if not profile or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", profile):
        return [], "invalid_url"
    cmd = [
        executable,
        "--no-compress-json",
        "--count", str(max(1, max_files)),
        "--dirname-pattern", str(stage),
        "--post-filter", f"date_utc>=datetime({cutoff.year},{cutoff.month},{cutoff.day})",
        "--abort-on=429,403",
        profile,
    ]
    code, output = _execute_subprocess(cmd, stage, timeout=timeout, runner=runner)
    rows: list[dict[str, Any]] = []
    sidecars: dict[str, dict[str, Any]] = {}
    for sidecar in stage.glob("*.json"):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                node = data.get("node") if isinstance(data.get("node"), dict) else data
                prefix = sidecar.stem
                sidecars[prefix] = node
        except (OSError, json.JSONDecodeError):
            continue
    for media in sorted(stage.iterdir()):
        if media.suffix.lower() not in MEDIA_EXTENSIONS or not media.is_file():
            continue
        if media.stat().st_size > MAX_SOCIAL_ITEM_BYTES:
            continue
        node = sidecars.get(media.stem) or sidecars.get(re.sub(r"_\d+$", "", media.stem)) or {}
        shortcode = node.get("shortcode")
        post_url = f"https://www.instagram.com/p/{shortcode}/" if isinstance(shortcode, str) and re.fullmatch(r"[\w-]+", shortcode) else None
        evidence = publication_evidence(node.get("date_utc"), source_url=post_url,
                                        field="date_utc", attribution="Instaloader post sidecar")
        epoch = publication_evidence(node.get("taken_at_timestamp"), source_url=post_url,
                                     field="taken_at_timestamp", attribution="Instaloader post sidecar", unix_seconds=True)
        if evidence and epoch and evidence["timestamp_utc"] != epoch["timestamp_utc"]:
            evidence = None
        else:
            evidence = evidence or epoch
        sha = _sha256_file(media)
        width, height = _dims_for(media)
        rows.append(
            {
                "file": media.name,
                "sha256": sha,
                "bytes": media.stat().st_size,
                "publication_evidence": evidence,
                "source_post_url": post_url,
                "author": profile,
                "dimensions": [width, height] if width else None,
                "source": "instaloader_fallback",
            }
        )
    if not rows:
        reason = classify_social_failure(code, output) if code != 0 else "no_media_accessible"
        return [], reason or "no_media_accessible"
    return rows, None


def _ytdlp_fallback(
    url: str,
    stage: Path,
    *,
    max_files: int,
    timeout: int,
    runner: RunnerFn | None = None,
    executable: str = "yt-dlp",
) -> tuple[list[dict[str, Any]], str | None]:
    """yt-dlp for a supported individual Facebook post/video URL."""
    cmd = [
        executable,
        "--no-playlist",
        "--max-downloads", str(max(1, max_files)),
        "--socket-timeout", "20",
        "--retries", "1",
        "--sleep-requests", "1",
        "--write-info-json",
        "--no-overwrites",
        "--restrict-filenames",
        "-o", str(stage / "%(id)s_%(title).40s.%(ext)s"),
        url,
    ]
    code, output = _execute_subprocess(cmd, stage, timeout=timeout, runner=runner)
    rows: list[dict[str, Any]] = []
    infos: dict[str, dict[str, Any]] = {}
    for info in stage.glob("*.info.json"):
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                infos[info.name.removesuffix(".info.json")] = data
        except (OSError, json.JSONDecodeError):
            continue
    for media in sorted(stage.iterdir()):
        if media.suffix.lower() not in MEDIA_EXTENSIONS or not media.is_file():
            continue
        info = infos.get(media.stem, {})
        post_url = info.get("webpage_url")
        try:
            check_social_url("facebook", post_url or "")
        except (TypeError, ValueError):
            post_url = None
        evidence = publication_evidence(info.get("release_timestamp"), source_url=post_url,
                                        field="release_timestamp", attribution="yt-dlp video sidecar",
                                        unix_seconds=True)
        sha = _sha256_file(media)
        width, height = _dims_for(media)
        rows.append(
            {
                "file": media.name,
                "sha256": sha,
                "bytes": media.stat().st_size,
                "publication_evidence": evidence,
                "source_post_url": post_url or url,
                "author": info.get("uploader") or info.get("channel"),
                "dimensions": [width, height] if width else None,
                "source": "ytdlp_fallback",
            }
        )
    if not rows:
        reason = classify_social_failure(code, output) if code != 0 else "no_media_accessible"
        return [], reason or "no_media_accessible"
    return rows, None


def _move_rows_into_job(
    rows: list[dict[str, Any]],
    *,
    job_dir: Path,
    platform: str,
    attr_file: str,
    seen: set[str],
) -> tuple[list[dict[str, Any]], int, int]:
    """Move staged gallery-dl files into <job>/<platform>/ and normalize rows.

    Returns (normalized_rows, moved, duplicates) where duplicates are
    sha-collisions with already-collected assets in this job.
    """
    dest = job_dir / platform
    dest.mkdir(parents=True, exist_ok=True)
    normalized: list[dict[str, Any]] = []
    moved = dupes = 0
    for row in rows:
        rel = str(row.get(attr_file) or "")
        if not rel:
            continue
        src = job_dir / rel
        if not src.is_file():
            continue
        sha = row.get("sha256") or _sha256_file(src)
        if sha in seen:
            dupes += 1
            original = next(item for item in normalized if item["sha256"] == sha)
            src.unlink()
            normalized.append({**row, "file": original["file"], "sha256": sha, "bytes": original["bytes"]})
            continue
        seen.add(sha)
        size = src.stat().st_size
        target = dest / src.name
        src.replace(target)
        moved += 1
        item = dict(row)
        item["file"] = f"{platform}/{target.name}"
        item["sha256"] = sha
        item["bytes"] = row.get("bytes") or size
        normalized.append(item)
    return normalized, moved, dupes


def _refine_gallery_reason(legacy: dict[str, Any]) -> tuple[str | None, str | None]:
    """Sharpen the legacy classifier's result with the raw extractor evidence.

    gallery-dl >=1.27 emits failures inside `--dump-json` as JSON error
    envelopes (``[code, url, {...}]``) with exit 0; the legacy textual
    classifier only sees "unknown" for those. We surface the codes honestly
    (e.g. `gallery_dl_error_6`) and keep a short scrubbed diagnostic tail.
    """
    reason = legacy.get("reason")
    msg = str(legacy.get("extractor_message") or "")
    codes = sorted(set(_GALLERY_ERROR_RE.findall(msg)))
    if codes and reason in (None, "unknown"):
        reason = "gallery_dl_error_" + "_".join(codes[:3])
    diagnostic = msg[-240:].strip() or None
    return reason, diagnostic


def run_social_extraction(
    job_id: str,
    job_dir: str | Path,
    platform: str,
    url: str,
    *,
    max_files: int,
    lookback_days: int,
    permission_confirmed: bool,
    timeout: int = 120,
    runner: RunnerFn | None = None,
    instaloader_runner: RunnerFn | None = None,
    ytdlp_runner: RunnerFn | None = None,
    enable_fallbacks: bool = True,
    window_start_utc: str | None = None,
) -> SocialExtractionResult:
    """Collect one social platform for one job. Never raises for collection
    failures; everything is reported honestly in the result.

    `enable_fallbacks=False` (used by offline tests) disables the
    Instaloader/yt-dlp fallback paths entirely; with fallbacks enabled and no
    test runner injected, the fallbacks execute the real external tools.
    """
    job_dir = Path(job_dir)
    result = SocialExtractionResult(job_id=job_id, platform=platform, url=url, status="failed")

    if not permission_confirmed:
        result.status = "permission_required"
        result.reason = "operator_consent_missing"
        result.notes.append(
            "Collection not attempted: operator did not confirm permitted use "
            "(social_permission_confirmed=false)."
        )
        return result

    try:
        check_social_url(platform, url)
    except ValueError as exc:
        result.status = "invalid_url"
        result.reason = str(exc)[:200]
        return result

    (job_dir / "research").mkdir(parents=True, exist_ok=True)
    manifest_path = job_dir / "research" / "social-media-manifest.json"
    rows_before = 0
    if manifest_path.is_file():
        try:
            rows_before = len(json.loads(manifest_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            rows_before = 0

    legacy = collect_social(
        job_dir,
        platform,
        url,
        max_files=max_files,
        days_limit=lookback_days,
        timeout=timeout,
        i_have_permission=True,
        runner=runner,
        window_start_utc=window_start_utc,
    )

    try:
        all_rows = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        all_rows = []
    new_rows = all_rows[rows_before:] if len(all_rows) >= rows_before else all_rows

    seen: set[str] = set()
    normalized, moved, dupes = _move_rows_into_job(
        new_rows, job_dir=job_dir, platform=platform, attr_file="file", seen=seen
    )

    # Relocate metadata-stripped copies under research/optimized (keep, do not delete).
    opt_dest = job_dir / "research" / "optimized"
    for row in new_rows:
        opt_rel = str(row.get("optimized_file") or "")
        if not opt_rel:
            continue
        src = job_dir / opt_rel
        if src.is_file():
            opt_dest.mkdir(parents=True, exist_ok=True)
            (opt_dest / src.name).write_bytes(src.read_bytes())
            src.unlink(missing_ok=True)

    fallback_rows: list[dict[str, Any]] = []
    legacy_reason, diagnostic = _refine_gallery_reason(legacy)
    result.tools.append(
        {
            "tool": "gallery-dl",
            "version": _tool_version("gallery-dl", runner),
            "outcome": legacy_reason or f"downloaded_{moved}",
        }
    )
    if diagnostic:
        result.notes.append(f"extractor diagnostic: {diagnostic}")
    if legacy_reason == "unknown" and not normalized:
        result.notes.append(
            "gallery-dl produced no data for this source (unsupported/gated "
            "extractor for this URL type)."
        )
    frozen_start = aware_timestamp(window_start_utc)
    cutoff = (frozen_start.astimezone(UTC).date() if frozen_start else
              (datetime.now(UTC) - timedelta(days=lookback_days)).date())
    if (
        enable_fallbacks
        and not normalized
        and legacy_reason in _FALLBACK_IG_REASONS
    ):
        if platform == "instagram":
            result.fallback_attempted = "instaloader"
            with tempfile.TemporaryDirectory(prefix="ig-fallback-") as td:
                stage = Path(td)
                fallback_rows, fb_reason = _instaloader_fallback(
                    url, stage, max_files=max_files, cutoff=cutoff,
                    timeout=timeout, runner=instaloader_runner,
                )
                result.fallback_outcome = fb_reason or f"downloaded_{len(fallback_rows)}"
                result.tools.append(
                    {
                        "tool": "instaloader",
                        "version": _tool_version("instaloader", instaloader_runner),
                        "outcome": result.fallback_outcome,
                    }
                )
                for row in fallback_rows:
                    src = stage / str(row["file"])
                    if not src.is_file():
                        continue
                    sha = row["sha256"]
                    if sha in seen:
                        dupes += 1
                        if row.get("source_post_url"):
                            original = next(item for item in normalized if item["sha256"] == sha)
                            normalized.append({**row, "file": original["file"]})
                        continue
                    seen.add(sha)
                    dest = job_dir / platform / src.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    row["file"] = f"{platform}/{dest.name}"
                    normalized.append(row)
        elif platform == "facebook" and _FB_SINGLE_URL_RE.search(url):
            result.fallback_attempted = "yt-dlp"
            with tempfile.TemporaryDirectory(prefix="fb-fallback-") as td:
                stage = Path(td)
                fallback_rows, fb_reason = _ytdlp_fallback(
                    url, stage, max_files=max_files, timeout=timeout, runner=ytdlp_runner,
                )
                result.fallback_outcome = fb_reason or f"downloaded_{len(fallback_rows)}"
                result.tools.append(
                    {
                        "tool": "yt-dlp",
                        "version": _tool_version("yt-dlp", ytdlp_runner),
                        "outcome": result.fallback_outcome,
                    }
                )
                for row in fallback_rows:
                    src = stage / str(row["file"])
                    if not src.is_file():
                        continue
                    sha = row["sha256"]
                    if sha in seen:
                        dupes += 1
                        if row.get("source_post_url"):
                            original = next(item for item in normalized if item["sha256"] == sha)
                            normalized.append({**row, "file": original["file"]})
                        continue
                    seen.add(sha)
                    dest = job_dir / platform / src.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    row["file"] = f"{platform}/{dest.name}"
                    normalized.append(row)

    items: list[dict[str, Any]] = []
    for index, row in enumerate(normalized, start=1):
        width, height = None, None
        dims = row.get("dimensions")
        if isinstance(dims, list) and len(dims) == 2:
            width, height = dims
        else:
            disk = job_dir / str(row["file"])
            if disk.is_file():
                width, height = _dims_for(disk)
        items.append(
            build_social_item(
                index,
                row,
                job_id=job_id,
                platform=platform,
                media_dir=platform,
                width=width,
                height=height,
            )
        )

    result.items = items
    result.files_added = len(items)
    result.duplicates = dupes
    result.bytes_downloaded = sum(int(i.get("bytes") or 0) for i in items)

    # Honest coverage metadata (req #1/#9): this is a bounded, date-filtered
    # SAMPLE — never presented as complete 365-day coverage.
    result.coverage = {
        "lookback_days": lookback_days,
        "cutoff_date": cutoff.isoformat(),
        "discovery_limit_files": max_files,
        "discovery_selection": "bounded_sample_not_full_coverage",
        "date_filter": "discovery_hint_only",
        "full_coverage_claimed": False,
    }

    # Every manifest asset must reference an existing file on disk (req #5).
    problems = validate_against_disk(job_dir, {"assets": items})
    if problems:
        for item in items:
            if item["extraction_status"] == "downloaded" and any(
                p.startswith(item["asset_id"] + ":") for p in problems
            ):
                item["extraction_status"] = "integrity_mismatch"
                item["local_path"] = None
        result.notes.append(
            f"{len(problems)} manifest validation problem(s); see coverage.issues."
        )

    if items:
        result.status = "completed"
        if legacy_reason:
            result.reason = legacy_reason
        result.notes.append(
            "Public visibility is not permission to reuse; rights remain unverified "
            "until reviewed."
        )
        if legacy_reason or result.fallback_attempted:
            result.status = "partial_success"
            result.notes.append(
                f"gallery-dl reason={legacy_reason or 'n/a'}; "
                f"fallback={result.fallback_attempted}:{result.fallback_outcome}."
            )
    else:
        result.status = _empty_source_status(legacy_reason)
        result.reason = legacy_reason or "no_media_accessible"
        if result.status == "rate_limited":
            result.notes.append(
                "The platform rate-limited this attempt. No retry or fallback was "
                "performed, by design."
            )
        elif result.status == "restricted":
            result.notes.append(
                "The platform restricted anonymous access. No login, cookie, or "
                "restriction bypass was attempted, by design."
            )
        else:
            result.notes.append(
                "No media was accessible with permitted anonymous access. This is an "
                "honest limitation, not a job success."
            )
    return result
