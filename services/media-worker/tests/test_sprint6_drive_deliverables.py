"""Sprint 6 tests: Client AI & Inventory Drive reports, subfolder mapping, and retry idempotency."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.delivery import build_delivery_plan, finalize_delivery


def _build_fixture_job(tmp_path: Path) -> Path:
    job = tmp_path / "job_s6"
    job.mkdir()
    img_bytes = b"\xff\xd8\xff\xe0sprint6-img\xff\xd9"
    vid_bytes = b"\x00\x00\x00\x18ftypmp42sprint6-vid"
    unverified_bytes = b"\xff\xd8\xff\xe0unverified\xff\xd9"
    (job / "website").mkdir()
    (job / "social").mkdir()
    (job / "website" / "hero.jpg").write_bytes(img_bytes)
    (job / "social" / "clip.mp4").write_bytes(vid_bytes)
    (job / "social" / "unverified.jpg").write_bytes(unverified_bytes)

    manifest = {
        "schema_version": "3.0-unified",
        "job_id": "job_s6",
        "website_url": "https://www.lilys-club.co.uk/",
        "window_start_utc": "2025-11-25T00:00:00+00:00",
        "window_end_utc": "2026-11-25T00:00:00+00:00",
        "counts": {"total": 3, "downloaded": 3},
        "coverage": {"website": "complete", "facebook": "partial"},
        "assets": [
            {
                "asset_id": "web_001",
                "platform": "website",
                "type": "image",
                "local_path": "website/hero.jpg",
                "sha256": hashlib.sha256(img_bytes).hexdigest(),
                "bytes": len(img_bytes),
                "mime": "image/jpeg",
                "width": 1600,
                "height": 1200,
                "rights_status": "verified",
                "extraction_status": "downloaded",
                "source_url": "https://www.lilys-club.co.uk/hero.jpg",
                "published_at_utc": None,
            },
            {
                "asset_id": "fb_vid_001",
                "platform": "facebook",
                "type": "video",
                "local_path": "social/clip.mp4",
                "sha256": hashlib.sha256(vid_bytes).hexdigest(),
                "bytes": len(vid_bytes),
                "mime": "video/mp4",
                "duration_sec": 12.5,
                "rights_status": "verified",
                "extraction_status": "downloaded",
                "source_url": "https://www.facebook.com/example/videos/1",
                "published_at_utc": "2026-09-10T10:00:00+00:00",
                "publication_date_source": "facebook_created_time",
            },
            {
                "asset_id": "fb_unverified",
                "platform": "facebook",
                "type": "image",
                "local_path": "social/unverified.jpg",
                "sha256": hashlib.sha256(unverified_bytes).hexdigest(),
                "bytes": len(unverified_bytes),
                "mime": "image/jpeg",
                "rights_status": "unverified",
                "extraction_status": "downloaded",
                "source_url": "https://www.facebook.com/example/photos/2",
                "published_at_utc": "2026-09-11T10:00:00+00:00",
            },
        ],
    }
    (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    curation = {
        "status": "completed",
        "job_id": "job_s6",
        "ai_envelope": {
            "mock": False,
            "prompt_version": "v2.0",
            "prompt_sha256": "a" * 64,
            "include_undated_website": True,
        },
        "curation": {
            "selected_asset_ids": ["web_001", "fb_vid_001", "fb_unverified"],
            "rights_review_required_ids": ["fb_unverified"],
            "best_images": [
                {
                    "asset_id": "web_001",
                    "recommended_section": "hero",
                    "hero_rank": 1,
                    "overall_score": 95,
                    "focal_point": "center",
                    "crop_guidance": "16:9",
                    "strengths": ["sharp"],
                    "weaknesses": [],
                },
                {
                    "asset_id": "fb_unverified",
                    "recommended_section": "gallery",
                    "overall_score": 80,
                },
            ],
            "best_videos": [
                {
                    "asset_id": "fb_vid_001",
                    "recommended_section": "feature",
                    "overall_score": 89,
                    "best_thumbnail_frame_index": 0,
                    "strengths": ["smooth"],
                    "weaknesses": [],
                }
            ],
            "selection_summary": {"headline": "Strong grooming showcase"},
            "analysis_coverage": {"mock": False},
        },
    }
    (job / "curation.json").write_text(json.dumps(curation), encoding="utf-8")
    return job


def test_delivery_plan_assigns_subfolders_and_packages_sanitized_reports(tmp_path):
    job = _build_fixture_job(tmp_path)
    plan = build_delivery_plan(job, "job_s6", "Lily's Club")
    assert plan["status"] == "ready"

    by_id = {a["asset_id"]: a for a in plan["assets"]}
    assert set(by_id) == {"web_001", "fb_vid_001"}
    assert by_id["web_001"]["target_subfolder"] == "Best Images"
    assert by_id["fb_vid_001"]["target_subfolder"] == "Best Videos"
    assert plan["pending_rights_ids"] == ["fb_unverified"]

    # Sanitized client reports packaged for Drive `Reports/`
    reports = {r["report_id"]: r for r in plan["reports"]}
    assert set(reports) == {"ai_curation_report", "media_inventory_summary"}
    assert len(plan["pending_reports"]) == 2
    for rep in reports.values():
        assert rep["target_subfolder"] == "Reports"
        assert rep["mime"] == "application/json"
        assert rep["n8n_path"].startswith("/data/media/job_s6/delivery_reports/")
        assert (job / rep["local_path"]).is_file()

    ai_doc = json.loads((job / reports["ai_curation_report"]["local_path"]).read_text(encoding="utf-8"))
    assert ai_doc["released_asset_ids"] == ["web_001", "fb_vid_001"]
    assert [i["asset_id"] for i in ai_doc["best_images"]] == ["web_001"]  # unverified excluded!
    assert [v["asset_id"] for v in ai_doc["best_videos"]] == ["fb_vid_001"]
    assert ai_doc["pending_rights_count"] == 1
    assert "/data/projects" not in json.dumps(ai_doc)

    inv_doc = json.loads((job / reports["media_inventory_summary"]["local_path"]).read_text(encoding="utf-8"))
    assert [a["asset_id"] for a in inv_doc["released_assets"]] == ["web_001", "fb_vid_001"]
    assert inv_doc["coverage"] == {"website": "complete", "facebook": "partial"}
    assert "local_path" not in json.dumps(inv_doc)

    # Deterministic report hashes across re-planning (retry idempotency)
    plan2 = build_delivery_plan(job, "job_s6", "Lily's Club")
    assert [r["upload_key"] for r in plan2["reports"]] == [r["upload_key"] for r in plan["reports"]]


def test_finalize_delivery_persists_subfolders_and_dedupes_report_uploads(tmp_path):
    job = _build_fixture_job(tmp_path)
    plan = build_delivery_plan(job, "job_s6", "Lily's Club")
    subfolders = {"Best Images": "sub_img_1234", "Best Videos": "sub_vid_5678", "Reports": "sub_rep_9012"}

    first = finalize_delivery(
        job,
        plan,
        [{"upload_key": plan["assets"][0]["upload_key"], "drive_file_id": "drv_img_1", "status": "uploaded"}],
        authorization_confirmed=True,
        drive_folder_id="root_fld_1234",
        drive_folder_link="https://drive.google.com/drive/folders/root_fld_1234",
        subfolders=subfolders,
        report_uploads=[
            {"upload_key": plan["reports"][0]["upload_key"], "drive_file_id": "drv_rep_1", "status": "uploaded"},
            {"upload_key": plan["reports"][1]["upload_key"], "status": "failed", "error": "timeout"},
            {"upload_key": "report:spoofed:deadbeef", "drive_file_id": "bad", "status": "uploaded"},
        ],
    )
    assert first["status"] == "partial_success"
    assert first["reports_status"] == "partial_success"
    assert first["subfolders"] == subfolders
    assert len(first["reports_uploaded"]) == 2

    # Retry plan reuses subfolders and skips already uploaded report + image
    retry_plan = build_delivery_plan(job, "job_s6", "Lily's Club")
    assert retry_plan["subfolders"] == subfolders
    assert [a["asset_id"] for a in retry_plan["pending_uploads"]] == ["fb_vid_001"]
    assert [r["report_id"] for r in retry_plan["pending_reports"]] == ["media_inventory_summary"]

    second = finalize_delivery(
        job,
        retry_plan,
        [{"upload_key": retry_plan["pending_uploads"][0]["upload_key"], "drive_file_id": "drv_vid_2", "status": "uploaded"}],
        authorization_confirmed=True,
        report_uploads=[
            {"upload_key": plan["reports"][0]["upload_key"], "drive_file_id": "duplicate_ignored", "status": "uploaded"},
            {"upload_key": retry_plan["pending_reports"][0]["upload_key"], "drive_file_id": "drv_rep_2", "status": "uploaded"},
        ],
    )
    assert second["reports_status"] == "completed"
    by_rep = {r["report_id"]: r for r in second["reports_uploaded"]}
    assert by_rep["ai_curation_report"]["drive_file_id"] == "drv_rep_1"
    assert by_rep["media_inventory_summary"]["drive_file_id"] == "drv_rep_2"
    assert second["subfolders"] == subfolders


def test_blocked_rights_produces_zero_reports(tmp_path):
    job = _build_fixture_job(tmp_path)
    manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
    for a in manifest["assets"]:
        a["rights_status"] = "unverified"
    (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    plan = build_delivery_plan(job, "job_s6", "Lily's Club")
    assert plan["status"] == "blocked_rights_review"
    assert plan["reports"] == []
    assert plan["pending_reports"] == []
    assert not (job / "delivery_reports").exists()
    assert not (job / "delivery_media").exists()


def test_media_extension_assets_stage_with_real_mime_extension_and_human_filename(tmp_path):
    """Checkpoint `.media` files must stage to `delivery_media/<clean-name>.webp` so n8n infers
    `image/webp` instead of `text/plain` when uploading to Google Drive."""
    job = tmp_path / "job_webp"
    (job / "collection").mkdir(parents=True)
    raw = b"RIFF\x18\x00\x00\x00WEBPVP8 \x0c\x00\x00\x000123456789ab"
    sha = hashlib.sha256(raw).hexdigest()
    (job / "collection" / "b0c109311b0d297cb35d7b951bb9be4be2209241.media").write_bytes(raw)
    manifest = {
        "job_id": "job_webp",
        "website_url": "https://vanityfurr.co.uk/",
        "assets": [
            {
                "asset_id": "b0c109311b0d297cb35d7b951bb9be4be2209241",
                "platform": "website",
                "type": "image",
                "extraction_status": "downloaded",
                "local_path": "collection/b0c109311b0d297cb35d7b951bb9be4be2209241.media",
                "sha256": sha,
                "mime": "image/webp",
                "source_url": "https://vanityfurr.co.uk/uploads/cat-landing-960.webp",
                "rights_status": "verified",
            }
        ],
    }
    curation = {
        "ai_envelope": {"mock": False, "prompt_version": "V2.0"},
        "curation": {
            "mode": "FINAL_CURATE",
            "selected_asset_ids": ["b0c109311b0d297cb35d7b951bb9be4be2209241"],
            "best_images": [{"asset_id": "b0c109311b0d297cb35d7b951bb9be4be2209241", "recommended_section": "hero"}],
            "analysis_coverage": {"mock": False},
        },
    }
    (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (job / "curation.json").write_text(json.dumps(curation), encoding="utf-8")

    plan = build_delivery_plan(job, "job_webp", "Vanity Furr")
    assert plan["status"] == "ready"
    item = plan["pending_uploads"][0]
    assert item["filename"] == f"cat-landing-960-{sha[:8]}.webp"
    assert item["n8n_path"] == f"/data/media/job_webp/delivery_media/cat-landing-960-{sha[:8]}.webp"
    assert not item["n8n_path"].endswith(".media")
    staged = job / "delivery_media" / item["filename"]
    assert staged.is_file()
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == sha
