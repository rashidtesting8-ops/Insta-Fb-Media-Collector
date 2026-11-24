"""Phase 4 media preprocessing tests — fully offline.

Synthetic images (Pillow) and real FFmpeg ``lavfi``-generated videos run the
actual preprocessing engine: no network, no third-party media, no secrets.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageFilter

from app.manifest import validate_against_disk
from app.preprocessing import run_media_preprocessing

# Video tests need ffmpeg to synthesize project-owned media; environments
# without it skip honestly instead of erroring.
pytestmark_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not available on this runner"
)

CUTOFF = (datetime.now(UTC) - timedelta(days=365)).date().isoformat()


def _write_img(
    path: Path,
    *,
    size: tuple[int, int] = (640, 480),
    color: tuple[int, int, int] = (200, 60, 60),
    striped: bool = False,
    blur: float | None = None,
    fmt: str = "PNG",
    quality: int = 92,
) -> bytes:
    im = Image.new("RGB", size, color)
    if striped:
        px = im.load()
        for y in range(0, size[1], 8):
            for x in range(size[0]):
                px[x, y] = (30, 30, 200) if (x // 16) % 2 else color
    if blur is not None:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    im.save(path, format=fmt, quality=quality)
    return path.read_bytes()


def _make_video(path: Path, *, duration: int = 2, size: str = "320x240") -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"testsrc=duration={duration}:size={size}:rate=10",
            "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def _item(
    asset_id: str,
    local_path: str,
    *,
    platform: str = "website",
    blob: bytes | None = None,
    size: tuple[int, int] = (640, 480),
    post_date: str | None = None,
    verified: bool = False,
) -> dict:
    return {
        "asset_id": asset_id,
        "job_id": "job_p4",
        "platform": platform,
        "type": "image",
        "source_url": f"https://example.com/{asset_id}",
        "source_page": "https://example.com/",
        "filename": Path(local_path).name,
        "local_path": local_path,
        "mime": "image/png",
        "bytes": len(blob) if blob else None,
        "width": size[0],
        "height": size[1],
        "sha256": hashlib.sha256(blob).hexdigest() if blob else None,
        "captured_at": "2026-11-20T12:00:00+00:00",
        "post_date": post_date,
        "date_status": "verified" if verified else "unknown",
        "rights_status": "unverified",
        "extraction_status": "downloaded",
        "detail": {},
    }


def _run(job_dir: Path, items: list[dict], **kwargs):
    manifest = {
        "schema_version": "3.0-unified",
        "job_id": "job_p4",
        "source_status": "completed",
        "sources": {"website": {"status": "completed", "counts": {}}},
        "counts": {"pages_crawled": 1},
        "coverage": {"issues": []},
        "assets": items,
        "notes": ["phase 4 fixture"],
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = run_media_preprocessing(job_dir, "job_p4", cutoff_date=CUTOFF, **kwargs)
    enriched = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    return result, enriched


def _by_id(enriched: dict) -> dict:
    return {a["asset_id"]: a for a in enriched["assets"] if a.get("type") != "frame"}


def test_exact_duplicates_grouped_with_explainable_representative(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    blob = _write_img(job / "a.png")
    (job / "b.png").write_bytes(blob)  # byte-identical second copy
    items = [
        _item("web_0001_aaa", "a.png", blob=blob),
        _item("web_0002_bbb", "b.png", blob=blob),
    ]
    result, enriched = _run(job, items)
    assert result.status == "completed"
    groups = enriched["preprocessing"]["duplicate_groups"]
    assert len(groups) == 1
    group = groups[0]
    assert group["method"] == "exact_sha256"
    assert set(group["member_asset_ids"]) == {"web_0001_aaa", "web_0002_bbb"}
    assert group["representative_reason"].startswith("representative ranked by verified date eligibility")
    reps = [a for a in _by_id(enriched).values() if a.get("representative")]
    assert len(reps) == 1  # deterministic single representative
    both = _by_id(enriched)
    assert both["web_0001_aaa"]["duplicate_group_id"] == both["web_0002_bbb"]["duplicate_group_id"]


def test_near_duplicates_grouped_perceptually_with_distance(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    original = _write_img(job / "base.png", striped=True)
    # same picture, slightly rescaled + JPEG re-encode: same visual, new bytes
    im = Image.open(job / "base.png")
    im.resize((620, 465)).save(job / "near.jpg", format="JPEG", quality=88)
    items = [
        _item("web_0001_aaa", "base.png", blob=original),
        _item("ig_0001_bbb", "near.jpg", blob=(job / "near.jpg").read_bytes()),
    ]
    result, enriched = _run(job, items)
    groups = [
        g for g in enriched["preprocessing"]["duplicate_groups"]
        if g["method"] == "perceptual_dhash"
    ]
    assert len(groups) == 1
    group = groups[0]
    assert set(group["member_asset_ids"]) == {"web_0001_aaa", "ig_0001_bbb"}
    assert group["max_hamming_distance"] <= group["threshold"]
    # exact bytes differ, so no exact_sha256 group exists
    assert all(g["method"] != "exact_sha256" for g in enriched["preprocessing"]["duplicate_groups"])
    # provenance preserved on both members
    both = _by_id(enriched)
    assert both["web_0001_aaa"]["source_url"].startswith("https://example.com/web_")
    assert both["ig_0001_bbb"]["platform"] == "instagram" or both["ig_0001_bbb"]["platform"] == "website"


def test_distinct_images_are_not_grouped(tmp_path):
    """False-positive guard: unrelated images must never share a group."""
    job = tmp_path / "job"
    job.mkdir()
    blob_a = _write_img(job / "red.png", color=(200, 40, 40), striped=True)
    blob_b = _write_img(job / "blue.png", color=(20, 20, 220), striped=False)
    items = [
        _item("web_0001_aaa", "red.png", blob=blob_a),
        _item("ig_0001_bbb", "blue.png", blob=blob_b),
    ]
    result, enriched = _run(job, items)
    assert result.perceptual_duplicate_groups == 0
    multi = [
        g for g in enriched["preprocessing"]["duplicate_groups"]
        if len(g["member_asset_ids"]) > 1
    ]
    assert multi == []
    both = _by_id(enriched)
    assert both["web_0001_aaa"]["duplicate_group_id"] != both["ig_0001_bbb"]["duplicate_group_id"]
    assert both["web_0001_aaa"]["representative"] is True
    assert both["ig_0001_bbb"]["representative"] is True


def test_corrupted_image_reported_honestly(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    good = _write_img(job / "good.png")
    (job / "bad.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)  # truncated header
    items = [
        _item("web_0001_good", "good.png", blob=good),
        _item("web_0002_bad", "bad.png", blob=(job / "bad.png").read_bytes()),
    ]
    result, enriched = _run(job, items)
    assert result.status == "partial"
    both = _by_id(enriched)
    assert both["web_0002_bad"]["preprocessing"]["outcome"] == "corrupted"
    assert both["web_0002_bad"]["quality"] is None
    assert both["web_0001_good"]["preprocessing"]["outcome"] == "processed"
    assert any("corrupted" in w for w in enriched["preprocessing"]["warnings"])
    assert enriched["preprocessing"]["status"] == "partial"


def test_unsupported_svg_is_expected_not_an_error(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
    items = [_item("web_0001_svg", "logo.svg", blob=(job / "logo.svg").read_bytes())]
    result, enriched = _run(job, items)
    assert result.status == "completed"  # expected outcome, not partial
    assert result.images_unsupported == 1
    item = _by_id(enriched)["web_0001_svg"]
    assert item["preprocessing"]["outcome"] == "unsupported_format"


def test_low_resolution_flagged_and_usable_size(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    small = _write_img(job / "small.png", size=(100, 80))
    big = _write_img(job / "big.png", size=(1200, 900))
    items = [
        _item("web_0001_small", "small.png", blob=small, size=(100, 80)),
        _item("web_0002_big", "big.png", blob=big, size=(1200, 900)),
    ]
    _, enriched = _run(job, items)
    both = _by_id(enriched)
    assert both["web_0001_small"]["quality"]["usable_size"] is False
    assert both["web_0002_big"]["quality"]["usable_size"] is True


def test_blur_and_exposure_metrics_are_deterministic(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    sharp = _write_img(job / "sharp.png", striped=True)
    blurred = _write_img(job / "blurred.png", striped=True, blur=6.0)
    dark = _write_img(job / "dark.png", color=(5, 5, 5))
    bright = _write_img(job / "bright.png", color=(250, 250, 250))
    items = [
        _item("web_0001_sharp", "sharp.png", blob=sharp),
        _item("web_0002_blur", "blurred.png", blob=blurred),
        _item("web_0003_dark", "dark.png", blob=dark),
        _item("web_0004_bright", "bright.png", blob=bright),
    ]
    _, enriched = _run(job, items)
    q = {aid: _by_id(enriched)[aid]["quality"] for aid in
         ("web_0001_sharp", "web_0002_blur", "web_0003_dark", "web_0004_bright")}
    assert q["web_0002_blur"]["sharpness"] < q["web_0001_sharp"]["sharpness"] / 2
    assert q["web_0003_dark"]["mean_luma"] < 40
    assert q["web_0004_bright"]["mean_luma"] > 200
    # crops are deterministic and centered
    assert q["web_0001_sharp"]["crop_windows"]["1:1"]["x"] > 0


@pytestmark_ffmpeg
def test_video_frames_extracted_bounded_and_referenced(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    video = job / "clip.mp4"
    _make_video(video, duration=2, size="320x240")
    items = [
        {
            "asset_id": "ig_0001_vid", "job_id": "job_p4", "platform": "instagram",
            "type": "video", "source_url": "https://example.com/v", "source_page": "https://example.com/",
            "filename": "clip.mp4", "local_path": "clip.mp4", "mime": "video/mp4",
            "bytes": video.stat().st_size, "width": 320, "height": 240,
            "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
            "captured_at": "2026-11-20T12:00:00+00:00", "post_date": None,
            "date_status": "unknown", "rights_status": "unverified",
            "extraction_status": "downloaded", "detail": {},
        }
    ]
    result, enriched = _run(job, items, max_video_frames=3)
    assert result.status == "completed"
    parent = _by_id(enriched)["ig_0001_vid"]
    assert parent["preprocessing"]["outcome"] == "video_processed"
    assert parent["video_probe"]["duration_s"] is not None
    assert abs(parent["video_probe"]["duration_s"] - 2.0) < 0.5
    assert len(parent["frames"]) == 3  # bounded frame count respected
    assert parent["representative_thumbnail"]["timestamp_s"] == parent["frames"][1]["timestamp_s"]
    # every referenced frame file really exists on disk
    for frame in parent["frames"]:
        assert (job / frame["local_path"]).is_file()
    # frame assets appended with parent references + unknown frame dates
    frame_assets = [a for a in enriched["assets"] if a.get("type") == "frame"]
    assert len(frame_assets) == 3
    for fa in frame_assets:
        assert fa["detail"]["parent_asset_id"] == "ig_0001_vid"
        assert fa["date_status"] == "unknown"  # frame time != publication date
    assert validate_against_disk(job, enriched) == []


@pytestmark_ffmpeg
def test_video_duration_limit_is_respected(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    video = job / "clip.mp4"
    _make_video(video, duration=2)
    items = [{
        "asset_id": "fb_0001_vid", "job_id": "job_p4", "platform": "facebook",
        "type": "video", "local_path": "clip.mp4", "mime": "video/mp4",
        "bytes": video.stat().st_size, "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        "width": 320, "height": 240, "captured_at": "2026-11-20T12:00:00+00:00",
        "post_date": None, "date_status": "unknown", "rights_status": "unverified",
        "extraction_status": "downloaded", "detail": {},
    }]
    result, enriched = _run(job, items, max_video_duration_s=1.0)
    parent = _by_id(enriched)["fb_0001_vid"]
    assert parent["preprocessing"]["outcome"] == "video_too_long"
    assert "frames" not in parent
    assert result.status == "partial"


@pytestmark_ffmpeg
def test_invalid_video_reports_probe_failure(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    fake = job / "fake.mp4"
    fake.write_bytes(b"this is not a video file at all" * 8)
    items = [{
        "asset_id": "fb_0002_vid", "job_id": "job_p4", "platform": "facebook",
        "type": "video", "local_path": "fake.mp4", "mime": "video/mp4",
        "bytes": fake.stat().st_size, "sha256": hashlib.sha256(fake.read_bytes()).hexdigest(),
        "captured_at": "2026-11-20T12:00:00+00:00", "post_date": None,
        "date_status": "unknown", "rights_status": "unverified",
        "extraction_status": "downloaded", "detail": {},
    }]
    result, enriched = _run(job, items)
    assert result.status == "partial"
    parent = _by_id(enriched)["fb_0002_vid"]
    assert parent["preprocessing"]["outcome"] == "video_probe_failed"


def test_missing_and_unsafe_paths_are_reported_not_followed(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    good = _write_img(job / "good.png")
    items = [
        _item("web_0001_good", "good.png", blob=good),
        _item("web_0002_missing", "does/not/exist.png", blob=good),
        _item("web_0003_escape", "../outside.png", blob=good),
    ]
    result, enriched = _run(job, items)
    both = _by_id(enriched)
    assert both["web_0002_missing"]["preprocessing"]["outcome"] == "missing_file"
    assert both["web_0003_escape"]["preprocessing"]["outcome"] == "missing_file"
    assert both["web_0001_good"]["preprocessing"]["outcome"] == "processed"
    assert result.status == "partial"


def test_date_categories_use_verified_timestamps_only(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    new = _write_img(job / "new.png")
    old = _write_img(job / "old.png")
    nodate = _write_img(job / "nodate.png")
    items = [
        _item("ig_0001_new", "new.png", blob=new, platform="instagram",
              post_date=datetime.now(UTC).isoformat(), verified=True),
        _item("ig_0002_old", "old.png", blob=old, platform="instagram",
              post_date="2024-01-01T00:00:00+00:00", verified=True),
        _item("ig_0003_nodate", "nodate.png", blob=nodate, platform="instagram"),
    ]
    _, enriched = _run(job, items)
    cats = {aid: _by_id(enriched)[aid]["date_category"] for aid in
            ("ig_0001_new", "ig_0002_old", "ig_0003_nodate")}
    assert cats["ig_0001_new"] == "in_lookback_verified"
    assert cats["ig_0002_old"] == "older_verified"
    assert cats["ig_0003_nodate"] == "unknown"  # never inferred from captured_at


def test_manifest_compatibility_and_partial_failures(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    good = _write_img(job / "good.png")
    (job / "bad.png").write_bytes(b"not an image")
    items = [
        _item("web_0001_good", "good.png", blob=good),
        _item("web_0002_bad", "bad.png", blob=(job / "bad.png").read_bytes()),
    ]
    _, enriched = _run(job, items)
    # schema version and Phase 2/3 header fields are untouched
    assert enriched["schema_version"] == "3.0-unified"
    assert enriched["sources"]["website"]["status"] == "completed"
    assert enriched["counts"]["pages_crawled"] == 1
    assert enriched["notes"] == ["phase 4 fixture"]
    good_item = _by_id(enriched)["web_0001_good"]
    assert good_item["sha256"] == hashlib.sha256(good).hexdigest()
    assert good_item["local_path"] == "good.png"
    # partial failure is visible in the header
    assert enriched["preprocessing"]["status"] == "partial"
    assert enriched["preprocessing"]["images"]["problem"] == 1


def test_source_isolation_in_mixed_manifest(tmp_path):
    """A corrupted social asset never blocks website assets."""
    job = tmp_path / "job"
    job.mkdir()
    web_img = _write_img(job / "web.png", striped=True)
    (job / "ig.png").write_bytes(b"\x89PNG broken")
    items = [
        _item("web_0001_ok", "web.png", blob=web_img, platform="website"),
        _item("ig_0001_bad", "ig.png", blob=(job / "ig.png").read_bytes(), platform="instagram"),
    ]
    result, enriched = _run(job, items)
    both = _by_id(enriched)
    assert both["web_0001_ok"]["preprocessing"]["outcome"] == "processed"
    assert both["web_0001_ok"]["quality"]["sharpness"] > 0
    assert both["ig_0001_bad"]["preprocessing"]["outcome"] == "corrupted"
    assert both["web_0001_ok"]["platform"] == "website"
    assert both["ig_0001_bad"]["platform"] == "instagram"
    assert result.status == "partial"
