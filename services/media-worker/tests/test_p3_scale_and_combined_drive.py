"""Milestone P3 offline tests: Groq Dev Tier preset (`AI_TIER_PRESET=dev`), 25-asset mixed
(images + FFmpeg videos) scale curation & MODE B evidence compacting, single-job combined
`Best Images/` + `Best Videos/` + `Reports/` Google Drive planning, and repeat-run / incremental
Drive folder reuse idempotency.

100% offline — zero live sockets, zero paid API calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
import socket
import subprocess
import uuid

from PIL import Image, ImageDraw
import pytest

from app.ai.pipeline import run_ai_curation
from app.ai.provider import (
    AIProviderConfig,
    CompletionResult,
    GEMINI_OPENAI_ENDPOINT,
    GROQ_ACCEPTANCE_ENDPOINT,
    GROQ_ACCEPTANCE_MODEL,
    OpenAICompatibleProvider,
    UsageTracker,
    VisionProvider,
    _safe_provider_message,
)
from app.delivery import build_delivery_plan, finalize_delivery
from app.publication import frozen_window, publication_evidence


pytestmark = pytest.mark.usefixtures("isolated_provider_unit")


@pytest.fixture(autouse=True)
def block_live_sockets(monkeypatch):
    def deny(*_args, **_kwargs):
        pytest.fail("P3 offline test attempted a live socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny)


def _jpeg_bytes(idx: int) -> bytes:
    img = Image.new("RGB", (400, 300), ((idx * 37) % 220 + 20, (idx * 53) % 200 + 30, 140))
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 380, 280], outline=(30, 40, 60), width=6)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _mp4_bytes(tmp_path: Path, idx: int) -> bytes:
    target = tmp_path / f"p3-vid-{idx}-{uuid.uuid4().hex[:6]}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration=1.0:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-t", "1.0", str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return target.read_bytes()


def test_p3_ai_tier_presets_and_explicit_overrides():
    # 1. Dev Tier preset enables high-throughput batching & 3500-token MODE B output.
    dev_cfg = AIProviderConfig.from_env({
        "AI_TIER_PRESET": "dev",
        "AI_PROVIDER": "openai_compatible",
        "AI_MODEL": GROQ_ACCEPTANCE_MODEL,
        "AI_ENDPOINT": GROQ_ACCEPTANCE_ENDPOINT,
        "AI_API_KEY": "test-key",
    })
    assert dev_cfg.tier_preset == "dev"
    assert dev_cfg.batch_size == 3
    assert dev_cfg.max_images_per_request == 3
    assert dev_cfg.mode_a_max_output_tokens == 1800
    assert dev_cfg.mode_b_max_output_tokens == 3500
    assert dev_cfg.min_request_spacing_s == 0.5
    assert dev_cfg.max_requests_per_job == 60
    assert dev_cfg.max_cost_usd_per_job == 1.5

    # 2. Explicit env overrides take precedence over preset values.
    dev_override = AIProviderConfig.from_env({
        "AI_TIER_PRESET": "dev",
        "AI_MODE_B_MAX_OUTPUT_TOKENS": "2800",
        "AI_MAX_IMAGES_PER_REQUEST": "4",
    })
    assert dev_override.mode_b_max_output_tokens == 2800
    assert dev_override.max_images_per_request == 4
    assert dev_override.batch_size == 3  # inherited from dev preset

    # 3. on_demand preset preserves OTPM-safe defaults.
    od_cfg = AIProviderConfig.from_env({"AI_TIER_PRESET": "on_demand"})
    assert od_cfg.tier_preset == "on_demand"
    assert od_cfg.batch_size == 1
    assert od_cfg.max_images_per_request == 1
    assert od_cfg.mode_a_max_output_tokens == 850
    assert od_cfg.mode_b_max_output_tokens == 950
    assert od_cfg.min_request_spacing_s == 5.0

    # 4. Bounded acceptance hard caps override dev preset unconditionally.
    bounded = AIProviderConfig.from_env({
        "AI_TIER_PRESET": "dev",
        "AI_BOUNDED_ACCEPTANCE": "1",
        "AI_PROVIDER": "openai_compatible",
        "AI_MODEL": GROQ_ACCEPTANCE_MODEL,
        "AI_ENDPOINT": GROQ_ACCEPTANCE_ENDPOINT,
        "AI_API_KEY": "test-key",
        "AI_PRICE_IN_PER_1K": "0.0008",
        "AI_PRICE_OUT_PER_1K": "0.004",
    })
    assert bounded.batch_size == 1
    assert bounded.max_images_per_request == 1
    assert bounded.max_requests_per_job == 2
    assert bounded.mode_b_max_output_tokens == 1800


def test_p3_25_asset_mixed_dev_tier_scale_and_combined_drive_idempotency(tmp_path):
    """Run a 25-asset mixed (22 images + 3 videos) job on Dev Tier preset through MODE A -> MODE B
    and verify: (1) compact MODE B evidence keeps prompt bounded, (2) combined delivery plan routes
    both Best Images/ and Best Videos/ + Reports/ simultaneously with verified post dates, and
    (3) repeat-run & incremental delivery preserve root folder + subfolders with zero duplicates."""
    job_id = "c" * 32
    job_dir = tmp_path / job_id
    (job_dir / "media").mkdir(parents=True)
    (job_dir / "frames").mkdir(parents=True)

    now_dt = datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC)
    window = frozen_window(now_dt)
    in_window_ts = "2026-10-18T10:00:00+00:00"

    assets: list[dict] = []
    # 22 distinct real JPEG images (verified Facebook Export dates)
    for i in range(1, 23):
        aid = f"fb_img_{i:02d}"
        data = _jpeg_bytes(i)
        rel = f"media/{aid}.jpg"
        (job_dir / rel).write_bytes(data)
        assets.append({
            "asset_id": aid,
            "platform": "facebook",
            "type": "image",
            "local_path": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "mime": "image/jpeg",
            "width": 400,
            "height": 300,
            "extraction_status": "downloaded",
            "rights_status": "verified",
            "source_url": f"https://www.facebook.com/p3salon#owner-upload-{i:02d}",
            "post_date": in_window_ts,
            "date_status": "verified",
            "publication_evidence": publication_evidence(
                in_window_ts,
                source_url=f"https://www.facebook.com/p3salon#owner-upload-{i:02d}",
                field="creation_timestamp",
                attribution="facebook export json",
            ),
            "preprocessing": {"outcome": "processed", "duplicate_of": None},
            "quality": {"width": 400, "height": 300, "sharpness": 120.0, "mean_luma": 135.0, "usable_size": True},
        })

    # 3 real MP4 videos with extracted keyframe JPEGs
    vid_bytes = _mp4_bytes(tmp_path, 1)
    for v in range(1, 4):
        vid_id = f"web_vid_{v:02d}"
        # Slightly perturb unique copy or use distinct frames so SHA-256 differs per video
        v_raw = vid_bytes + (b"\x00" * v)
        v_rel = f"media/{vid_id}.mp4"
        (job_dir / v_rel).write_bytes(v_raw)
        frame_rel = f"frames/{vid_id}_f1.jpg"
        (job_dir / frame_rel).write_bytes(_jpeg_bytes(50 + v))
        assets.append({
            "asset_id": vid_id,
            "platform": "website",
            "type": "video",
            "local_path": v_rel,
            "sha256": hashlib.sha256(v_raw).hexdigest(),
            "bytes": len(v_raw),
            "mime": "video/mp4",
            "width": 320,
            "height": 240,
            "duration_sec": 1.0,
            "extraction_status": "downloaded",
            "rights_status": "verified",
            "source_url": f"https://p3salon.example.test/vid-{v}.mp4",
            "post_date": in_window_ts,
            "date_status": "verified",
            "publication_evidence": publication_evidence(
                in_window_ts,
                source_url=f"https://p3salon.example.test/vid-{v}.mp4",
                field="datePublished",
                attribution="JSON-LD exact contentUrl",
            ),
            "frames": [{"local_path": frame_rel, "timestamp_s": 0.5}],
            "preprocessing": {
                "outcome": "video_processed",
                "duplicate_of": None,
            },
        })

    manifest = {
        "schema_version": "3.0-unified",
        "job_id": job_id,
        "website_url": "https://p3salon.example.test/",
        **window,
        "assets": assets,
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    mode_a_batches = 0
    mode_b_prompts: list[str] = []

    class _DevTierScaleProvider(VisionProvider):
        name = "openai_compatible"
        model = GROQ_ACCEPTANCE_MODEL

        def complete(self, prompt, images, tracker, **_kwargs):
            nonlocal mode_a_batches
            if images:
                mode_a_batches += 1
                # Parse attached asset_ids cleanly from ASSET_SPECS JSON block
                ids = list(dict.fromkeys(img.asset_id for img in images))
                items = []
                for aid in ids:
                    is_vid = aid.startswith("web_vid_")
                    entry = {
                        "asset_id": aid,
                        "filename": f"{aid}.mp4" if is_vid else f"{aid}.jpg",
                        "source": "website" if is_vid else "facebook",
                        "media_type": "video" if is_vid else "image",
                        "detected_content": "Verbose grooming portrait description " * 12,
                        "possible_categories": ["video" if is_vid else "hero"],
                        "quality_score": 88.0 if aid in ("fb_img_01", "fb_img_02", "web_vid_01") else 62.0,
                        "score_breakdown": {
                            "technical_quality": 22.0,
                            "business_relevance_authenticity": 22.0,
                            "composition_usability": 18.0,
                            "emotion_aesthetics": 12.0,
                            "distinctiveness_collection_fit": 9.0,
                            "evidence_confidence": 5.0,
                        } if aid in ("fb_img_01", "fb_img_02", "web_vid_01") else {
                            "technical_quality": 15.0,
                            "business_relevance_authenticity": 15.0,
                            "composition_usability": 12.0,
                            "emotion_aesthetics": 10.0,
                            "distinctiveness_collection_fit": 6.0,
                            "evidence_confidence": 4.0,
                        },
                        "visual_strengths": ["Sharp coat detail " * 10, "Warm lighting", "Extra bullet 3", "Extra bullet 4"],
                        "visual_weaknesses": ["Minor background clutter", "Second weakness", "Third weakness"],
                        "verification_status": "verified",
                        "visual_analysis_status": "analyzed",
                        "recommended_use": "video" if is_vid else "hero",
                        "selection_status": "recommended",
                        "selection_reason": "Strong authentic pet grooming visual " * 10,
                    }
                    if is_vid:
                        entry["video_review_status"] = "frames_reviewed"
                        entry["evaluated_frames"] = [0.5]
                    items.append(entry)
                tracker.record({"input_tokens": 1200, "output_tokens": 420}, retries=0)
                return CompletionResult(text=json.dumps({"mode": "ANALYZE_BATCH", "items": items}), usage={"input_tokens": 1200, "output_tokens": 420})

            mode_b_prompts.append(prompt)
            tracker.record({"input_tokens": 3500, "output_tokens": 750}, retries=0)
            return CompletionResult(
                text=json.dumps({
                    "schema_version": "visual-director-v2.0",
                    "job_id": job_id,
                    "mode": "FINAL_CURATE",
                    "business_visual_identity": {
                        "style_summary": "Warm boutique pet grooming studio",
                        "atmosphere": "Calm and friendly",
                        "brand_personality": "Caring professionals",
                        "key_visual_strengths": ["Crisp pet portraits", "Real salon walkthrough clip"],
                        "recommended_design_direction": "Editorial warm ivory layout",
                    },
                    "analysis_coverage": {
                        "assets_supplied": 25,
                        "assets_analyzed": 25,
                        "inherited_from_duplicates": 0,
                        "unavailable": 0,
                        "prompt_version": "V2.0",
                        "provider": "openai_compatible",
                        "model": GROQ_ACCEPTANCE_MODEL,
                        "mock": False,
                        "usage": {},
                    },
                    "hero_selection": [
                        {
                            "rank": 1,
                            "asset_id": "fb_img_01",
                            "reason": "Standout groomed portrait",
                            "composition_notes": "Centered pup with clean margin",
                            "desktop_crop": "16:9",
                            "mobile_crop": "4:5",
                            "focal_point": {"x": 0.5, "y": 0.4},
                            "text_overlay_suitability": "suitable",
                            "limitations": "None",
                        }
                    ],
                    "gallery_images": [
                        {
                            "asset_id": "fb_img_02",
                            "reason": "Crisp secondary grooming shot",
                            "recommended_section": "gallery",
                            "selection_status": "recommended",
                        }
                    ],
                    "video_recommendations": [
                        {
                            "asset_id": "web_vid_01",
                            "filename": "web_vid_01.mp4",
                            "source": "website",
                            "video_review_status": "frames_reviewed",
                            "evaluated_frames": [0.5],
                            "quality_score": 88.0,
                            "recommended_section": "hero",
                            "selection_status": "recommended",
                            "reason": "Smooth salon walkthrough",
                        }
                    ],
                    "design_art_direction": {
                        "narrative": "Boutique care from bath to finish",
                        "layout_guidance": "Hero portrait + video showcase",
                        "color_and_lighting": "Warm ivory and soft daylight",
                        "typography_mood": "Modern editorial serif",
                        "section_pairing": "Hero + Gallery + Video",
                        "diversity_notes": "Covers photos and salon video",
                    },
                    "selected_asset_ids": ["fb_img_01", "fb_img_02", "web_vid_01"],
                    "rights_review_required_ids": [],
                    "final_quality_assessment": {
                        "overall_assessment": "Strong multi-media collection",
                        "strengths": ["High-res grooming portraits", "Verified walkthrough clip"],
                        "weaknesses": ["Needs founder portrait"],
                        "meets_premium_standard": True,
                    },
                }),
                usage={"input_tokens": 3500, "output_tokens": 750},
            )

    cfg = AIProviderConfig.from_env({
        "AI_TIER_PRESET": "dev",
        "AI_PROVIDER": "openai_compatible",
        "AI_MODEL": GROQ_ACCEPTANCE_MODEL,
        "AI_ENDPOINT": GROQ_ACCEPTANCE_ENDPOINT,
        "AI_API_KEY": "test-key",
        "AI_MIN_REQUEST_SPACING_S": "0",
    })
    res = run_ai_curation(
        job_dir,
        job_id,
        config=cfg,
        provider=_DevTierScaleProvider(),
        batch_size=cfg.batch_size,
        max_images_per_request=cfg.max_images_per_request,
        max_images=10,
        max_videos=2,
    )
    assert res.status == "completed", res.errors
    assert res.coverage["assets_analyzed"] == 25
    # Dev Tier batch_size=3 processes 25 assets in ~9-11 batches instead of 25 sequential calls!
    assert mode_a_batches <= 12
    # Large-batch MODE B compacting kept all 25 IDs while trimming >2 bullets & verbose >140-char blurbs!
    assert len(mode_b_prompts) == 1
    assert "Extra bullet 3" not in mode_b_prompts[0]
    assert "fb_img_22" in mode_b_prompts[0] and "web_vid_03" in mode_b_prompts[0]

    # Combined single-job Drive delivery plan: Best Images + Best Videos + Reports together!
    plan = build_delivery_plan(job_dir, job_id, "Lily's Club")
    assert plan["status"] == "ready"
    subfolders_needed = {a["target_subfolder"] for a in plan["assets"]} | {r["target_subfolder"] for r in plan["reports"]}
    assert subfolders_needed == {"Best Images", "Best Videos", "Reports"}
    assert len(plan["pending_uploads"]) == 3
    assert len(plan["pending_reports"]) == 2

    # Inventory report must include verified Facebook post_date in published_at_utc!
    inv_rep = next(r for r in plan["reports"] if r["report_id"] == "media_inventory_summary")
    inv_doc = json.loads((job_dir / inv_rep["local_path"]).read_text(encoding="utf-8"))
    fb_inv = next(a for a in inv_doc["released_assets"] if a["asset_id"] == "fb_img_01")
    assert fb_inv["published_at_utc"] == in_window_ts

    # Finalize combined delivery across all 3 subfolders
    drive_subs = {"Best Images": "sub_img_p3", "Best Videos": "sub_vid_p3", "Reports": "sub_rep_p3"}
    finalized = finalize_delivery(
        job_dir,
        plan,
        [{"upload_key": a["upload_key"], "drive_file_id": f"drv_{a['asset_id']}", "status": "uploaded"} for a in plan["assets"]],
        authorization_confirmed=True,
        drive_folder_id="root_p3_folder",
        drive_folder_link="https://drive.google.com/drive/folders/root_p3_folder",
        subfolders=drive_subs,
        report_uploads=[
            {"upload_key": r["upload_key"], "drive_file_id": f"drv_{r['report_id']}", "status": "uploaded"}
            for r in plan["reports"]
        ],
    )
    assert finalized["status"] == "completed"
    assert finalized["reports_status"] == "completed"
    assert finalized["subfolders"] == drive_subs

    # Repeat-run idempotency check: re-planning returns ZERO pending uploads/reports and preserves Drive IDs!
    repeat_plan = build_delivery_plan(job_dir, job_id, "Lily's Club")
    assert repeat_plan["status"] == "ready"
    assert repeat_plan["drive_folder_id"] == "root_p3_folder"
    assert repeat_plan["subfolders"] == drive_subs
    assert repeat_plan["pending_uploads"] == []
    assert repeat_plan["pending_reports"] == []


def test_gemini_3_vision_preset_reasoning_effort_and_secret_scrubbing():
    """Verify AI_TIER_PRESET=gemini caps, reasoning_effort='none' for Gemini 3 thinking models,
    and Gemini key scrubbing (both AIza and AQ. formats) without any live network calls."""
    cfg = AIProviderConfig.from_env({
        "AI_TIER_PRESET": "gemini",
        "AI_PROVIDER": "openai_compatible",
        "AI_MODEL": "gemini-3.1-flash-lite",
        "AI_ENDPOINT": GEMINI_OPENAI_ENDPOINT,
        "AI_API_KEY": "test-gemini-key",
        "AI_MIN_REQUEST_SPACING_S": "0",
    })
    assert cfg.tier_preset == "gemini"
    assert cfg.batch_size == 3
    assert cfg.max_images_per_request == 3
    assert cfg.mode_a_max_output_tokens == 2200
    assert cfg.mode_b_max_output_tokens == 4000
    assert cfg.max_requests_per_job == 40
    assert cfg.max_cost_usd_per_job == 1.0

    captured: list[dict] = []

    def fake_transport(req: dict) -> dict:
        captured.append(req)
        return {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 12},
        }

    provider = OpenAICompatibleProvider(cfg, sleep=lambda _s: None, transport=fake_transport)
    tracker = UsageTracker(cfg)
    result = provider.complete("Return JSON", [], tracker)
    assert result.text == '{"ok": true}'
    assert len(captured) == 1
    body = captured[0]["body"]
    assert body["model"] == "gemini-3.1-flash-lite"
    assert body["reasoning_effort"] == "none"
    assert body["max_tokens"] == 4000
    assert body["response_format"] == {"type": "json_object"}

    # Verify both Google AI Studio (AIza...) and Vertex/Express (AQ....) keys are scrubbed in error messages.
    fake_aiza = "AIza" + "AbCdEfGhIjKlMnOpQrStUvWxYz123456789"
    fake_aq = "AQ." + "Ab8RN6K8uVxYz1234567890AbCdEfGhIjKlMnOpQrStUv"
    scrubbed_aiza = _safe_provider_message(f"Upstream rejected key {fake_aiza}", "")
    scrubbed_aq = _safe_provider_message(f"Upstream rejected key {fake_aq}", "")
    assert fake_aiza not in scrubbed_aiza
    assert fake_aq not in scrubbed_aq
    assert "[REDACTED]" in scrubbed_aiza and "[REDACTED]" in scrubbed_aq
