"""Intelligent media preparation for AI analysis (Phase 5, Step 4).

Consumes the Phase 4 enriched manifest. Selection rules:
- Only assets whose pixels are on disk and were technically validated
  (`preprocessing.outcome` in processed/video_processed) are supplied.
- Corrupted / unsupported (e.g. SVG) / missing assets are excluded with
  recorded reasons — never sent, never claimed.
- Duplicate groups: only the Phase 4 representative is analyzed; members
  inherit the analysis (`analysis_inherited_from`) — recorded, honest.
- Technical metrics are used ONLY for bounded prioritization when the
  candidate set exceeds the per-job asset cap (never as model judgments).
- Videos are represented by their Phase 4 extracted frames with timestamps;
  the analysis is therefore `frames_reviewed`, never `full_video_reviewed`.

Originals are never modified: compatibility conversion (EXIF-oriented,
JPEG, bounded dimensions) happens on in-memory copies.
"""

from __future__ import annotations

import io
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from PIL import Image, UnidentifiedImageError

from app.ai.provider import ImagePayload
from app.publication import date_eligibility, window_bounds

MAX_IMAGE_PIXELS = 25_000_000
Image.MAX_IMAGE_PIXELS = min(Image.MAX_IMAGE_PIXELS or MAX_IMAGE_PIXELS, MAX_IMAGE_PIXELS)

SENDABLE_IMAGE_MIMES = {"image/jpeg", "image/png"}
DEFAULT_MAX_IMAGE_DIM = 1024
DEFAULT_MAX_IMAGE_BYTES = 1_500_000


@dataclass
class Candidate:
    asset: dict[str, Any]
    kind: str                      # "image" | "video"
    payload_files: list[Path] = field(default_factory=list)
    frame_timestamps: list[float] = field(default_factory=list)
    reason: str = "selected"


@dataclass
class SelectionReport:
    candidates: list[Candidate] = field(default_factory=list)
    inherited: dict[str, str] = field(default_factory=dict)   # member -> representative
    excluded: list[dict[str, Any]] = field(default_factory=list)
    overflow_cut: list[str] = field(default_factory=list)


def _outcome(item: dict[str, Any]) -> str:
    return str((item.get("preprocessing") or {}).get("outcome") or "")


def select_ai_candidates(
    manifest: dict[str, Any],
    job_dir: str | Path,
    *,
    max_assets: int = 40,
) -> SelectionReport:
    job_dir = Path(job_dir)
    resolved_root = job_dir.resolve()
    report = SelectionReport()

    def safe_file(rel: Any) -> Path | None:
        if not isinstance(rel, str) or not rel or "\\" in rel or PureWindowsPath(rel).drive:
            return None
        candidate = Path(rel)
        if candidate.is_absolute() or ".." in candidate.parts:
            return None
        path = (job_dir / candidate).resolve()
        if not path.is_relative_to(resolved_root):
            return None
        return path if path.is_file() else None

    def on_disk(item: dict[str, Any]) -> Path | None:
        return safe_file(item.get("local_path"))

    bounds = window_bounds(manifest)
    # Pre-partition by honest exclusion reasons
    for item in manifest.get("assets", []):
        if item.get("extraction_status") not in ("downloaded", "duplicate", "extracted_frame"):
            continue
        if item.get("type") == "frame":
            continue  # frames are supplied via their parent video
        date_state = date_eligibility(item, bounds)
        if date_state != "in_window":
            report.excluded.append({"asset_id": item["asset_id"], "reason": date_state})
            continue
        outcome = _outcome(item)
        if outcome in ("corrupted", "missing_file", "video_probe_failed"):
            report.excluded.append({"asset_id": item["asset_id"], "reason": outcome})
            continue
        if outcome == "unsupported_format":
            report.excluded.append(
                {"asset_id": item["asset_id"], "reason": "unsupported_for_ai_payload"}
            )
            continue
        if outcome not in ("processed", "video_processed"):
            report.excluded.append({"asset_id": item["asset_id"], "reason": "not_preprocessed"})
            continue
        if on_disk(item) is None and item.get("type") != "video":
            report.excluded.append({"asset_id": item["asset_id"], "reason": "missing_file"})
            continue
        if item.get("type") == "video":
            frame_pairs = []
            for f in (item.get("frames") or []):
                if not isinstance(f, dict):
                    continue
                fpath = safe_file(f.get("local_path"))
                if fpath is not None:
                    frame_pairs.append((fpath, float(f.get("timestamp_s") or 0.0)))
            if not frame_pairs:
                report.excluded.append(
                    {"asset_id": item["asset_id"], "reason": "no_frames_extracted"}
                )
                continue
            report.candidates.append(
                Candidate(
                    asset=item,
                    kind="video",
                    payload_files=[p for p, _ in frame_pairs],
                    frame_timestamps=[ts for _, ts in frame_pairs],
                )
            )
        else:
            path = on_disk(item)
            if path is None:
                report.excluded.append({"asset_id": item["asset_id"], "reason": "missing_file"})
                continue
            report.candidates.append(
                Candidate(asset=item, kind="image", payload_files=[path])
            )

    # Duplicate-group handling: only representatives are analyzed
    kept: list[Candidate] = []
    for cand in report.candidates:
        item = cand.asset
        if item.get("representative") is False:
            group = item.get("duplicate_group_id")
            if group:
                rep = next(
                    (
                        c
                        for c in report.candidates
                        if c.asset.get("duplicate_group_id") == group
                        and c.asset.get("representative") is True
                    ),
                    None,
                )
                if rep is not None:
                    report.inherited[item["asset_id"]] = rep.asset["asset_id"]
                    continue  # inherit from the representative
        kept.append(cand)
    report.candidates = kept

    # Bounded prioritization (deterministic; technical metrics only rank,
    # they never judge): usable size, then sharpness, then stable id order.
    if len(report.candidates) > max_assets:
        def rank(c: Candidate) -> tuple:
            q = c.asset.get("quality") or {}
            return (
                0 if q.get("usable_size") else 1,
                -(float(q.get("sharpness") or 0.0)),
                str(c.asset["asset_id"]),
            )

        ordered = sorted(report.candidates, key=rank)
        report.overflow_cut = [c.asset["asset_id"] for c in ordered[max_assets:]]
        report.candidates = ordered[:max_assets]
    return report


def convert_for_payload(
    path: Path,
    *,
    max_dim: int = DEFAULT_MAX_IMAGE_DIM,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> tuple[bytes, str]:
    """Compatibility conversion on an in-memory copy: EXIF orientation
    applied, RGB JPEG, bounded dimensions/payload. Originals untouched."""
    with Image.open(path) as img:
        orientation = img.getexif().get(274)
        im = img
        if orientation in (3, 6, 8):
            rotations = {3: 180, 6: 270, 8: 90}
            im = img.transpose({180: Image.ROTATE_180, 270: Image.ROTATE_270, 90: Image.ROTATE_90}[rotations[orientation]])
        im = im.convert("RGB")
        if max(im.size) > max_dim:
            im.thumbnail((max_dim, max_dim), Image.Resampling.BILINEAR)
        data = _encode_jpeg_under(im, max_bytes)
    return data, "image/jpeg"


def _encode_jpeg_under(im: Image.Image, max_bytes: int) -> bytes:
    quality = 85
    for _ in range(4):
        buffer = io.BytesIO()
        im.save(buffer, format="JPEG", quality=quality)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return data
        quality -= 15
        im.thumbnail((max(1, int(im.size[0] * 0.7)), max(1, int(im.size[1] * 0.7))), Image.Resampling.BILINEAR)
    raise ValueError("Could not encode JPEG within the configured byte cap")


def build_image_payloads(
    candidate: Candidate,
    *,
    max_dim: int = DEFAULT_MAX_IMAGE_DIM,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> list[ImagePayload]:
    payloads: list[ImagePayload] = []
    asset_id = str(candidate.asset.get("asset_id"))
    if candidate.kind == "video":
        for index, frame_path in enumerate(candidate.payload_files):
            timestamps = candidate.frame_timestamps
            label = (
                f"frame @{timestamps[index]:.2f}s"
                if index < len(timestamps)
                else "frame"
            )
            data, mime = convert_for_payload(frame_path, max_dim=max_dim, max_bytes=max_bytes)
            payloads.append(ImagePayload(asset_id=asset_id, mime=mime, data=data, label=label))
        return payloads
    path = candidate.payload_files[0]
    data, mime = convert_for_payload(path, max_dim=max_dim, max_bytes=max_bytes)
    payloads.append(ImagePayload(asset_id=asset_id, mime=mime, data=data, label="primary image"))
    return payloads


def probe_payload_readable(path: Path) -> bool:
    try:
        with Image.open(path) as img:
            img.load()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


__all__ = [
    "Candidate",
    "SelectionReport",
    "select_ai_candidates",
    "convert_for_payload",
    "build_image_payloads",
    "probe_payload_readable",
    "DEFAULT_MAX_IMAGE_DIM",
    "DEFAULT_MAX_IMAGE_BYTES",
]
