"""Phase 4 media preprocessing engine.

Pipeline position (per approved Phase 4 integration flow):

    collection (website/instagram/facebook)
      -> unified manifest (schema 3.0-unified, unchanged)
      -> THIS MODULE (deterministic, offline, no AI):
           1. per-asset technical analysis (image quality / video probe)
           2. video frame extraction via ffmpeg (bounded sampling)
           3. cross-source deduplication (exact sha256 + perceptual dHash)
           4. date categorization from VERIFIED timestamps only
      -> enriched manifest (purely additive fields; schema_version unchanged
         so all Phase 2/3 consumers and tests remain compatible)

Honesty rules (docs/PROJECT_SPEC.md §01/§14, Phase 4 approval):
- Technical metrics (sharpness/brightness/contrast) are ENGINEERING
  measurements only. They are never presented as AI aesthetic judgments
  (the AI Visual Director is Phase 5 and uses real pixels).
- Verified publication dates are preserved; missing dates stay `unknown`;
  dates are NEVER inferred from download/captured-at time.
- Rights stay `unverified` unless evidence exists (none is produced here).
- Bounded social samples are never relabelled as complete 365-day coverage.
- Originals are never modified; frames are new derived files under
  <job>/frames/ inside the same shared storage (D: bind mount locally).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from PIL import Image, UnidentifiedImageError

from app.manifest import atomic_write_json, utc_now_iso, validate_against_disk
from app.security import sanitized_subprocess_env

# Bound Pillow decode size to prevent decompression-bomb OOMs in a 1 GiB worker container.
MAX_IMAGE_PIXELS = 25_000_000
Image.MAX_IMAGE_PIXELS = min(Image.MAX_IMAGE_PIXELS or MAX_IMAGE_PIXELS, MAX_IMAGE_PIXELS)

# --- Bounded resource limits (deterministic; documented in PHASE_4_REPORT) ---
MAX_VIDEO_DURATION_S = 600.0     # skip longer videos (bounded by design)
MAX_VIDEO_FRAMES = 6             # frames sampled per video
FRAME_MAX_DIM = 640               # longest frame edge (memory/disk bounded)
FFPROBE_TIMEOUT_S = 30            # per ffprobe call
FFMPEG_TIMEOUT_S = 45             # per single-frame ffmpeg call
QUALITY_ANALYZE_MAX_DIM = 512     # analysis is done on a bounded copy
USABLE_MIN_DIM = 480              # below this an image is "small_for_web"

# Perceptual dedup: dHash (64-bit). Conservative threshold to avoid grouping
# unrelated images; false positives are covered by an explicit regression test.
NEAR_DUP_HAMMING_MAX = 6

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov"}
_CROP_RATIOS = {"16:9": 16 / 9, "4:3": 4 / 3, "3:2": 1.5, "1:1": 1.0}

_PROCESSED_OK = {
    "processed",            # image: quality + phash computed
    "video_processed",      # video: probed + frames extracted
    "unsupported_format",    # e.g. SVG: expected, honest, not an error
}
_PROBLEM_OUTCOMES = {
    "corrupted",
    "missing_file",
    "video_probe_failed",
    "video_too_long",
    "video_frame_failed",
    "analysis_failed",
}


@dataclass
class PreprocessingResult:
    job_id: str
    status: str  # completed | partial | failed
    generated_at: str
    images_analyzed: int = 0
    images_unsupported: int = 0
    images_problem: int = 0
    videos_probed: int = 0
    videos_frames: int = 0
    videos_problem: int = 0
    exact_duplicate_groups: int = 0
    perceptual_duplicate_groups: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Image technical analysis
# ---------------------------------------------------------------------------

def _dhash_hex(gray_small: Image.Image) -> str:
    """Deterministic 64-bit difference hash (dHash) as 16 hex chars.

    Input must be a grayscale image resized to 9x8 (caller handles that).
    """
    px = list(gray_small.getdata())
    w = gray_small.width
    bits = 0
    for row in range(8):
        base = row * w
        for col in range(8):
            left = px[base + col]
            right = px[base + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def _hamming_hex(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _bounded_gray(img: Image.Image, max_dim: int) -> Image.Image:
    im = img.convert("L")
    if max(im.size) > max_dim:
        im.thumbnail((max_dim, max_dim), Image.Resampling.BILINEAR)
    return im


def _sharpness(gray_small: Image.Image) -> float:
    """Mean absolute horizontal+vertical gradient, normalized to 0..1.

    A deterministic blur/sharpness estimate (engineering metric, not AI)."""
    w, h = gray_small.size
    if w < 2 or h < 2:
        return 0.0
    px = list(gray_small.getdata())
    total = 0
    for y in range(h):
        base = y * w
        for x in range(w - 1):
            total += abs(px[base + x + 1] - px[base + x])
    for y in range(h - 1):
        base = y * w
        for x in range(w):
            total += abs(px[base + w + x] - px[base + x])
    return round(total / ((h * (w - 1) + (h - 1) * w) * 255), 4)


def _luma_stats(gray_small: Image.Image) -> dict[str, Any]:
    px = list(gray_small.getdata())
    n = max(1, len(px))
    mean = sum(px) / n
    var = sum((p - mean) ** 2 for p in px) / n
    shadows = sum(1 for p in px if p <= 8) / n * 100.0
    highlights = sum(1 for p in px if p >= 247) / n * 100.0
    return {
        "mean_luma": round(mean, 2),
        "contrast_std": round(var ** 0.5, 2),
        "clipped_shadows_pct": round(shadows, 2),
        "clipped_highlights_pct": round(highlights, 2),
    }


def _crop_windows(w: int, h: int) -> dict[str, Any]:
    """Largest centered crop per common aspect ratio (deterministic)."""
    crops: dict[str, Any] = {}
    for name, ratio in _CROP_RATIOS.items():
        if w / h >= ratio:
            ch = h
            cw = int(h * ratio)
        else:
            cw = w
            ch = int(w / ratio)
        crops[name] = {
            "width": cw,
            "height": ch,
            "x": (w - cw) // 2,
            "y": (h - ch) // 2,
            "feasible": cw >= 320 and ch >= 240,
        }
    return crops


def analyze_image(path: Path) -> dict[str, Any]:
    """Technical quality metrics for one image file. Raises on corruption."""
    with Image.open(path) as img:
        img.load()  # force full decode (integrity check)
        width, height = img.size
        orientation = img.getexif().get(274)
        gray = _bounded_gray(img, QUALITY_ANALYZE_MAX_DIM)
        analyze_w, analyze_h = gray.size

    metrics: dict[str, Any] = {
        "integrity": "ok",
        "width": width,
        "height": height,
        "aspect_ratio": round(width / height, 4) if height else None,
        "megapixels": round((width * height) / 1_000_000, 3),
        "usable_size": min(width, height) >= USABLE_MIN_DIM,
        "exif_orientation": orientation,
        "analysis_size": {"width": analyze_w, "height": analyze_h},
    }
    metrics.update(_luma_stats(gray))
    metrics["sharpness"] = _sharpness(gray)
    metrics["crop_windows"] = _crop_windows(width, height)
    return metrics


def phash_file(path: Path) -> str:
    with Image.open(path) as img:
        gray = img.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    return _dhash_hex(gray)


# ---------------------------------------------------------------------------
# Video preprocessing (ffprobe / ffmpeg, argument arrays, hard timeouts)
# ---------------------------------------------------------------------------

def probe_video(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT_S,
        env=sanitized_subprocess_env(allow_proxy=False),
    )
    if proc.returncode != 0:
        raise ValueError((proc.stderr or "ffprobe failed").strip()[-200:])
    data = json.loads(proc.stdout or "{}")
    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        raise ValueError("no video stream found")
    duration = None
    try:
        duration = float(data.get("format", {}).get("duration") or video_stream.get("duration"))
    except (TypeError, ValueError):
        duration = None
    return {
        "duration_s": round(duration, 3) if duration is not None else None,
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "codec": video_stream.get("codec_name"),
    }


def extract_frames(
    video: Path,
    out_dir: Path,
    *,
    duration_s: float,
    max_frames: int = MAX_VIDEO_FRAMES,
) -> list[dict[str, Any]]:
    """Bounded, evenly spaced frame extraction. Returns frame records."""
    out_dir.mkdir(parents=True, exist_ok=True)
    count = max(1, min(max_frames, MAX_VIDEO_FRAMES))
    interval = duration_s / (count + 1)
    frames: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        timestamp = round(interval * index, 3)
        target = out_dir / f"frame_{index:03d}.jpg"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(timestamp),
            "-i", str(video),
            "-frames:v", "1",
            "-vf",
            "scale=w=min(" + str(FRAME_MAX_DIM) + "\\,iw):h=min(" + str(FRAME_MAX_DIM)
            + "\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-q:v", "3",
            str(target),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=FFMPEG_TIMEOUT_S,
                env=sanitized_subprocess_env(allow_proxy=False),
            )
        except subprocess.TimeoutExpired:
            frames.append({"index": index, "timestamp_s": timestamp, "error": "ffmpeg_timeout"})
            continue
        if proc.returncode != 0 or not target.is_file():
            frames.append(
                {"index": index, "timestamp_s": timestamp, "error": (proc.stderr or "ffmpeg failed")[-160:]}
            )
            continue
        blob = target.read_bytes()
        with Image.open(target) as frame_img:
            fw, fh = frame_img.size
        frames.append(
            {
                "index": index,
                "timestamp_s": timestamp,
                "file": str(target.name),
                "bytes": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(),
                "width": fw,
                "height": fh,
                "error": None,
            }
        )
    return frames


# ---------------------------------------------------------------------------
# Enrichment driver
# ---------------------------------------------------------------------------

def _resolve(job_dir: Path, local_path: str | None) -> Path | None:
    if not isinstance(local_path, str) or not local_path or "\\" in local_path or PureWindowsPath(local_path).drive:
        return None
    candidate = Path(local_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None  # unsafe path is reported, never followed
    resolved_root = job_dir.resolve()
    resolved = (job_dir / candidate).resolve()
    if not resolved.is_relative_to(resolved_root):
        return None
    return resolved


def _date_category(item: dict[str, Any], cutoff_iso: str | None) -> str:
    """Verified dates only; never inferred from capture time."""
    if item.get("date_status") != "verified" or not item.get("post_date"):
        return "unknown"
    post = str(item["post_date"])
    if cutoff_iso and post[:10] >= cutoff_iso:
        return "in_lookback_verified"
    if cutoff_iso:
        return "older_verified"
    return "verified_date"


def run_media_preprocessing(
    job_dir: str | Path,
    job_id: str,
    *,
    max_video_frames: int = MAX_VIDEO_FRAMES,
    max_video_duration_s: float = MAX_VIDEO_DURATION_S,
    cutoff_date: str | None = None,
) -> PreprocessingResult:
    """Enrich the job manifest in place (atomic rewrite). Never modifies
    original media files. Raises only for a missing/unreadable base manifest;
    per-asset problems are reported honestly in per-asset outcomes."""
    job_dir = Path(job_dir)
    manifest_path = job_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = PreprocessingResult(job_id=job_id, status="completed", generated_at=utc_now_iso())

    phashes: dict[str, str] = {}
    sha_index: dict[str, list[str]] = {}
    duplicate_groups: list[dict[str, Any]] = []
    frame_assets: list[dict[str, Any]] = []

    # Make re-runs idempotent: drop previously derived ffmpeg frame entries before regenerating them.
    manifest["assets"] = [
        item for item in manifest.get("assets", [])
        if not (
            item.get("type") == "frame"
            and item.get("extraction_status") == "extracted_frame"
            and (item.get("detail") or {}).get("origin") == "ffmpeg_frame_extraction"
        )
    ]

    for item in manifest.get("assets", []):
        item.pop("duplicate_group_id", None)
        item.pop("representative", None)
        status = str(item.get("extraction_status") or "")
        if status not in ("downloaded", "extracted_frame") and not (status == "duplicate" and item.get("local_path")):
            continue  # honest per-item failure rows carry no file to analyze
        disk = _resolve(job_dir, item.get("local_path"))
        if disk is None or not disk.is_file():
            item["preprocessing"] = {"outcome": "missing_file"}
            item.setdefault("quality", None)
            result.images_problem += 1
            result.warnings.append(f"{item.get('asset_id')}: missing_file")
            continue

        ext = disk.suffix.lower()
        item_kind = str(item.get("type") or item.get("kind") or "").lower()
        item_mime = str(item.get("mime") or "").lower()
        is_video = ext in _VIDEO_EXTS or (ext == ".media" and (item_kind == "video" or item_mime.startswith("video/")))
        is_image = ext in _IMAGE_EXTS or (ext == ".media" and (item_kind == "image" or item_mime.startswith("image/")))

        # Date categorization applies to every downloaded asset with a file
        # (verified timestamps only; never inferred from capture time).
        item["date_category"] = item.get("date_eligibility") or _date_category(item, cutoff_date)

        if is_video:
            _process_video(
                item, disk, job_dir, result,
                max_frames=max_video_frames,
                max_duration=max_video_duration_s,
                frame_assets=frame_assets,
            )
            continue

        if not is_image:
            # e.g. SVG from website crawls: expected, honest, not an error.
            item["preprocessing"] = {"outcome": "unsupported_format", "note": f"{ext} not analyzable by Pillow"}
            item.setdefault("quality", None)
            result.images_unsupported += 1
            continue

        # --- image path ---------------------------------------------------
        sha = item.get("sha256") or hashlib.sha256(disk.read_bytes()).hexdigest()
        try:
            quality = analyze_image(disk)
            item["quality"] = quality
            if item.get("width") is None and quality.get("width"):
                item["width"] = quality["width"]
            if item.get("height") is None and quality.get("height"):
                item["height"] = quality["height"]
            item["phash"] = phashes[item["asset_id"]] = phash_file(disk)
            item["preprocessing"] = {"outcome": "processed"}
            result.images_analyzed += 1
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            item["quality"] = None
            item["preprocessing"] = {"outcome": "corrupted", "note": str(exc)[:160]}
            result.images_problem += 1
            result.warnings.append(f"{item.get('asset_id')}: corrupted")
            continue
        sha_index.setdefault(sha, []).append(item["asset_id"])

    # --- exact duplicate groups -------------------------------------------
    group_no = 0
    for sha, members in sorted(sha_index.items()):
        if len(members) < 2:
            continue
        group_no += 1
        duplicate_groups.append(
            _make_group(f"dg_{group_no:04d}", "exact_sha256", members, manifest, result)
        )

    # --- perceptual duplicate groups (conservative threshold) -------------
    # Assets already grouped by exact sha must not be double-grouped here
    # (identical bytes imply identical hashes, distance 0).
    exact_map: dict[str, str] = {}
    for g in duplicate_groups:
        if g["method"] == "exact_sha256":
            for m in g["member_asset_ids"]:
                exact_map[m] = g["group_id"]
    hash_items = [
        (aid, phashes[aid])
        for aid in sorted(phashes)
    ]
    parent: dict[str, str] = {}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = _find(a), _find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for i in range(len(hash_items)):
        aid_a, hash_a = hash_items[i]
        for j in range(i + 1, len(hash_items)):
            aid_b, hash_b = hash_items[j]
            if exact_map.get(aid_a) and exact_map.get(aid_a) == exact_map.get(aid_b):
                continue  # already grouped by exact sha256
            if _hamming_hex(hash_a, hash_b) <= NEAR_DUP_HAMMING_MAX:
                _union(aid_a, aid_b)

    components: dict[str, list[str]] = {}
    for aid in sorted(parent):
        components.setdefault(_find(aid), []).append(aid)
    for members in sorted((sorted(m) for m in components.values() if len(m) >= 2), key=lambda m: m[0]):
        group_no += 1
        duplicate_groups.append(
            _make_group(f"dg_{group_no:04d}", "perceptual_dhash", members, manifest, result)
        )

    # singletons get deterministic group ids for explainability
    grouped_ids = {m for g in duplicate_groups for m in g["member_asset_ids"]}
    for item in manifest.get("assets", []):
        if item.get("preprocessing", {}).get("outcome") in ("processed",) and item["asset_id"] not in grouped_ids:
            group_no += 1
            item["duplicate_group_id"] = f"dg_{group_no:04d}"
            item["representative"] = True

    manifest["preprocessing"] = {
        "engine": "phase4-v1",
        "status": None,  # finalized below
        "generated_at": result.generated_at,
        "images": {
            "analyzed": result.images_analyzed,
            "unsupported_format": result.images_unsupported,
            "problem": result.images_problem,
        },
        "videos": {
            "probed": result.videos_probed,
            "frames_extracted": result.videos_frames,
            "problem": result.videos_problem,
        },
        "duplicate_groups": duplicate_groups,
        "notes": [
            "Technical metrics are engineering measurements, not AI aesthetic judgments.",
            "Verified dates are preserved; unknown dates are never invented.",
            "Originals are never modified; frames are derived files under frames/.",
        ],
        "warnings": result.warnings[:50],
        "errors": result.errors[:50],
    }
    manifest.setdefault("assets", []).extend(frame_assets)

    problem = result.images_problem + result.videos_problem
    if result.errors:
        manifest["preprocessing"]["status"] = "failed"
        result.status = "failed"
    elif problem:
        manifest["preprocessing"]["status"] = "partial"
        result.status = "partial"
    else:
        manifest["preprocessing"]["status"] = "completed"
        result.status = "completed"

    atomic_write_json(manifest_path, manifest)

    # Final guarantee: every referenced file must exist on disk.
    disk_problems = validate_against_disk(job_dir, manifest)
    if disk_problems:
        manifest["preprocessing"]["warnings"] = (
            list(manifest["preprocessing"].get("warnings") or [])[:50]
            + [f"manifest_validation: {p}" for p in disk_problems[:20]]
        )
        manifest["preprocessing"]["status"] = "partial"
        result.status = "partial"
        result.warnings.extend(disk_problems[:20])
        atomic_write_json(manifest_path, manifest)
    return result


def _process_video(
    item: dict[str, Any],
    disk: Path,
    job_dir: Path,
    result: PreprocessingResult,
    *,
    max_frames: int,
    max_duration: float,
    frame_assets: list[dict[str, Any]],
) -> None:
    item.setdefault("quality", None)
    try:
        probe = probe_video(disk)
    except (ValueError, subprocess.TimeoutExpired, OSError) as exc:
        item["preprocessing"] = {"outcome": "video_probe_failed", "note": str(exc)[:160]}
        result.videos_problem += 1
        result.warnings.append(f"{item.get('asset_id')}: video_probe_failed")
        return
    result.videos_probed += 1
    item["video_probe"] = probe
    if item.get("width") is None and probe.get("width"):
        item["width"] = probe["width"]
    if item.get("height") is None and probe.get("height"):
        item["height"] = probe["height"]

    if probe["duration_s"] is None or probe["duration_s"] > max_duration:
        item["preprocessing"] = {
            "outcome": "video_too_long",
            "note": f"duration={probe['duration_s']}s (limit {max_duration}s)",
        }
        result.videos_problem += 1
        result.warnings.append(f"{item.get('asset_id')}: video_too_long")
        return

    frame_dir = job_dir / "frames" / str(item["asset_id"])
    frames = extract_frames(disk, frame_dir, duration_s=probe["duration_s"], max_frames=max_frames)
    ok_frames = [f for f in frames if f.get("error") is None]
    failed = [f for f in frames if f.get("error") is not None]
    item["frames"] = [
        {
            "frame_asset_id": f"{item['asset_id']}_f{f['index']:03d}",
            "timestamp_s": f["timestamp_s"],
            "local_path": f"frames/{item['asset_id']}/{f['file']}",
            "sha256": f["sha256"],
            "bytes": f["bytes"],
            "width": f["width"],
            "height": f["height"],
        }
        for f in ok_frames
    ]
    result.videos_frames += len(ok_frames)
    if ok_frames:
        # deterministic representative: the middle extracted frame
        middle = ok_frames[len(ok_frames) // 2]
        item["representative_thumbnail"] = {
            "frame_asset_id": f"{item['asset_id']}_f{middle['index']:03d}",
            "local_path": f"frames/{item['asset_id']}/{middle['file']}",
            "timestamp_s": middle["timestamp_s"],
        }
        for f in ok_frames:
            frame_assets.append(
                {
                    "asset_id": f"{item['asset_id']}_f{f['index']:03d}",
                    "job_id": item.get("job_id"),
                    "platform": item.get("platform"),
                    "type": "frame",
                    "source_url": item.get("source_url"),
                    "source_page": item.get("source_page"),
                    "filename": f["file"],
                    "local_path": f"frames/{item['asset_id']}/{f['file']}",
                    "mime": "image/jpeg",
                    "bytes": f["bytes"],
                    "width": f["width"],
                    "height": f["height"],
                    "sha256": f["sha256"],
                    "captured_at": utc_now_iso(),
                    "post_date": item.get("post_date"),
                    "date_status": "unknown",  # frame time != publication date
                    "rights_status": "unverified",
                    "extraction_status": "extracted_frame",
                    "detail": {
                        "origin": "ffmpeg_frame_extraction",
                        "parent_asset_id": item["asset_id"],
                        "timestamp_s": f["timestamp_s"],
                    },
                    "preprocessing": {"outcome": "processed"},
                }
            )
        item["preprocessing"] = {"outcome": "video_processed"}
    else:
        item["preprocessing"] = {"outcome": "video_frame_failed", "note": "no frames extracted"}
        result.videos_problem += 1
    if failed:
        result.warnings.append(
            f"{item.get('asset_id')}: {len(failed)} frame(s) failed extraction"
        )
        item["preprocessing"]["frame_errors"] = [f["error"] for f in failed]


def _make_group(
    group_id: str,
    method: str,
    members: list[str],
    manifest: dict[str, Any],
    result: PreprocessingResult,
) -> dict[str, Any]:
    """Deterministic, explainable representative selection.

    Criteria (in order): largest pixel area -> larger file bytes -> stable
    asset_id order. All provenance stays with every member."""
    by_id = {a["asset_id"]: a for a in manifest.get("assets", [])}
    ranked = []
    for aid in members:
        asset = by_id.get(aid, {})
        area = (asset.get("width") or 0) * (asset.get("height") or 0)
        ranked.append((asset.get("date_eligibility") == "in_window", area, int(asset.get("bytes") or 0), aid))
    ranked.sort(reverse=True)
    rep = ranked[0][3]
    for aid in members:
        asset = by_id.get(aid)
        if asset is not None:
            asset["duplicate_group_id"] = group_id
            asset["representative"] = aid == rep
    if method == "exact_sha256":
        result.exact_duplicate_groups += 1
    else:
        result.perceptual_duplicate_groups += 1
    rep_area = ranked[0][1]
    reason = (
        f"representative ranked by verified date eligibility and pixel area ({rep_area} px²); "
        "tie-breaks: larger file bytes, then asset_id order"
    )
    group = {
        "group_id": group_id,
        "method": method,
        "member_asset_ids": members,
        "representative_asset_id": rep,
        "representative_reason": reason,
    }
    if method == "perceptual_dhash":
        hashes = [by_id.get(m, {}).get("phash") for m in members]
        if all(hashes):
            distances = {
                f"{members[i]}~{members[j]}": _hamming_hex(hashes[i], hashes[j])
                for i in range(len(members))
                for j in range(i + 1, len(members))
            }
            group["hamming_distances"] = distances
            group["max_hamming_distance"] = max(distances.values()) if distances else 0
            group["threshold"] = NEAR_DUP_HAMMING_MAX
    return group
