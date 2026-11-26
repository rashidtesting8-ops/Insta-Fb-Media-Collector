"""Sprint 8 regression tests: real ModeBResponse client Drive reports, checkpoint stage lifecycle, and Named Admin resume."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.delivery import build_delivery_plan
from app.orchestration import derive_stage
from tests.test_named_auth import identity  # noqa: F401 — pytest fixture
from tests.test_phase9_security import worker  # noqa: F401 — pytest fixture


def test_delivery_report_packages_real_mode_b_schema_and_excludes_unverified(tmp_path: Path):
    job = tmp_path / "job_s8"
    job.mkdir()
    img1 = b"\xff\xd8\xff\xe0hero-photo\xff\xd9"
    img2 = b"\xff\xd8\xff\xe0unverified-gallery\xff\xd9"
    vid1 = b"\x00\x00\x00\x18ftypmp42groom-clip"
    (job / "website").mkdir()
    (job / "social").mkdir()
    (job / "website" / "hero.jpg").write_bytes(img1)
    (job / "social" / "unverified.jpg").write_bytes(img2)
    (job / "social" / "groom.mp4").write_bytes(vid1)

    manifest = {
        "schema_version": "3.0-unified",
        "job_id": "job_s8",
        "website_url": "https://www.lilys-club.co.uk/",
        "assets": [
            {
                "asset_id": "web_hero",
                "platform": "website",
                "type": "image",
                "local_path": "website/hero.jpg",
                "sha256": hashlib.sha256(img1).hexdigest(),
                "bytes": len(img1),
                "mime": "image/jpeg",
                "rights_status": "verified",
                "extraction_status": "downloaded",
                "source_url": "https://www.lilys-club.co.uk/hero.jpg",
            },
            {
                "asset_id": "fb_unverified",
                "platform": "facebook",
                "type": "image",
                "local_path": "social/unverified.jpg",
                "sha256": hashlib.sha256(img2).hexdigest(),
                "bytes": len(img2),
                "mime": "image/jpeg",
                "rights_status": "unverified",
                "extraction_status": "downloaded",
                "source_url": "https://www.facebook.com/example/2",
            },
            {
                "asset_id": "fb_video",
                "platform": "facebook",
                "type": "video",
                "local_path": "social/groom.mp4",
                "sha256": hashlib.sha256(vid1).hexdigest(),
                "bytes": len(vid1),
                "mime": "video/mp4",
                "rights_status": "verified",
                "extraction_status": "downloaded",
                "source_url": "https://www.facebook.com/example/3",
            },
        ],
    }
    (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Authentic Visual Director V2 ModeBResponse structure (no legacy best_images/best_videos keys!)
    curation = {
        "status": "completed",
        "job_id": "job_s8",
        "ai_envelope": {"mock": False, "prompt_version": "V2.0", "prompt_sha256": "f" * 64},
        "curation": {
            "schema_version": "visual-director-v2.0",
            "job_id": "job_s8",
            "mode": "FINAL_CURATE",
            "business_visual_identity": {
                "style_summary": "Warm luxury grooming studio",
                "atmosphere": "Calm and attentive",
                "brand_personality": "Trustworthy",
                "key_visual_strengths": ["Natural light"],
                "recommended_design_direction": "Editorial warmth",
            },
            "analysis_coverage": {"mock": False, "assets_supplied": 3, "assets_analyzed": 3},
            "hero_selection": [
                {
                    "rank": 1,
                    "asset_id": "web_hero",
                    "reason": "Crisp portrait with clean background",
                    "composition_notes": "Rule of thirds",
                    "desktop_crop": "16:9",
                    "mobile_crop": "4:5",
                    "focal_point": {"x": 0.5, "y": 0.4},
                    "text_overlay_suitability": "suitable",
                }
            ],
            "gallery_images": [
                {
                    "asset_id": "fb_unverified",
                    "reason": "Strong grooming finish",
                    "recommended_section": "gallery",
                    "selection_status": "recommended",
                }
            ],
            "video_recommendations": [
                {
                    "asset_id": "fb_video",
                    "filename": "groom.mp4",
                    "source": "facebook",
                    "video_review_status": "frames_reviewed",
                    "evaluated_frames": [0.5, 2.0],
                    "quality_score": 88.5,
                    "recommended_section": "grooming_transformation",
                    "selection_status": "recommended",
                    "reason": "Clear grooming sequence",
                }
            ],
            "design_art_direction": {"narrative": "Clean editorial"},
            "final_quality_assessment": {"overall_assessment": "Strong", "meets_premium_standard": True},
            "selected_asset_ids": ["web_hero", "fb_unverified", "fb_video"],
            "rights_review_required_ids": ["fb_unverified"],
        },
    }
    (job / "curation.json").write_text(json.dumps(curation), encoding="utf-8")

    plan = build_delivery_plan(job, "job_s8", "Lily's Club")
    assert plan["status"] == "ready"
    rep_path = job / "delivery_reports" / "ai-curation-report.json"
    ai_doc = json.loads(rep_path.read_text(encoding="utf-8"))

    assert ai_doc["schema_version"] == "1.1-client-ai-report"
    assert ai_doc["business_visual_identity"]["style_summary"] == "Warm luxury grooming studio"
    assert [h["asset_id"] for h in ai_doc["hero_selection"]] == ["web_hero"]
    assert ai_doc["hero_selection"][0]["filename"] == "hero.jpg"
    assert [v["asset_id"] for v in ai_doc["video_recommendations"]] == ["fb_video"]
    assert [i["asset_id"] for i in ai_doc["best_images"]] == ["web_hero"]  # unverified excluded!
    assert [v["asset_id"] for v in ai_doc["best_videos"]] == ["fb_video"]
    assert ai_doc["curated_sections"] == {}  # fb_unverified excluded from gallery_images!


def test_derive_stage_tracks_v21_checkpoint_ai_and_delivery_lifecycle():
    base = {
        "job_id": "job_ckpt",
        "status": "partial_success",
        "collection_mode": "public_checkpoint",
        "run_ai_curation": False,
        "error": "Public inventory is unverified and held for per-job owner review.",
    }
    assert derive_stage(base)["stage"] == "partial_success"
    ai_running = derive_stage({**base, "ai_curation": {"status": "started"}})
    assert ai_running["stage"] == "ai_processing"
    assert ai_running["terminal"] is False
    ai_done = derive_stage({**base, "ai_curation": {"status": "completed", "mock": False}})
    assert ai_done["stage"] == "awaiting_rights_approval"
    ai_mock = derive_stage({**base, "ai_curation": {"status": "completed", "mock": True}})
    assert ai_mock["stage"] == "blocked"
    delivered = derive_stage(
        {**base, "ai_curation": {"status": "completed", "mock": False}},
        {"status": "completed"},
    )
    assert delivered["stage"] == "completed"


def test_named_collection_resume_enforces_auth_and_resumes_checkpoint(worker, identity, monkeypatch):
    monkeypatch.setenv("M4_COLLECTION_APPROVAL_ENABLED", "1")
    monkeypatch.setenv("COLLECTION_ENGINE", "historical")
    _, call = identity
    session = call("login", {"name": "sample", "password": "synthetic-password"})[1]["session"]

    job_id = "e" * 32
    worker.store({
        "job_id": job_id,
        "status": "partial_success",
        "collection_mode": "public_checkpoint",
        "website_url": "https://example.test/",
        "facebook_url": "https://www.facebook.com/example/",
    })
    submitted = []
    monkeypatch.setattr(worker.executor, "submit", lambda *args: (submitted.append(args), worker.slots.release()))

    # Missing session denied
    assert call("collection-resume", {"job_id": job_id}, "")[0] == 403
    # Invalid job_id rejected
    assert call("collection-resume", {"job_id": "bad"}, session)[0] == 400
    # Valid Named Admin session resumes checkpoint with 202
    status, data = call("collection-resume", {"job_id": job_id}, session)
    assert status == 202
    assert data == {"job_id": job_id, "status": "queued", "resumed": True}
    assert len(submitted) == 1


def test_mode_b_normalizes_selected_status_and_compact_otpm_prompt(tmp_path, isolated_provider_unit):
    from PIL import Image
    from app.ai.pipeline import run_ai_curation
    from app.ai.prompts import system_prompt_for_mode
    from app.ai.provider import AIProviderConfig, CompletionResult, VisionProvider

    assert "never 'selected'" in system_prompt_for_mode("B")

    job_dir = tmp_path / "job_otpm"
    (job_dir / "media").mkdir(parents=True)
    img_path = job_dir / "media" / "dog.jpg"
    Image.new("RGB", (640, 480), color=(210, 150, 120)).save(img_path, format="JPEG")
    manifest = {
        "job_id": "job_otpm",
        "website_url": "https://example.test/",
        "created_at": "2026-11-25T00:00:00+00:00",
        "window_start_utc": "2025-11-25T00:00:00+00:00",
        "window_end_utc": "2026-11-25T00:00:00+00:00",
        "assets": [
            {
                "asset_id": "img_01",
                "filename": "dog.jpg",
                "platform": "website",
                "type": "image",
                "media_type": "image",
                "extraction_status": "downloaded",
                "local_path": "media/dog.jpg",
                "mime_type": "image/jpeg",
                "sha256": "a" * 64,
                "rights_status": "unverified",
                "preprocessing": {"outcome": "processed"},
            }
        ],
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    class _QwenDriftProvider(VisionProvider):
        name = "openai_compatible"
        model = "qwen/qwen3.8-27b"
        mode_b_calls = 0

        def complete(self, prompt, images, tracker, **_kwargs):
            if images:
                tracker.record({"input_tokens": 200, "output_tokens": 120}, retries=0)
                return CompletionResult(
                    text=json.dumps({
                        "mode": "ANALYZE_BATCH",
                        "items": [{
                            "asset_id": "img_01",
                            "filename": "dog.jpg",
                            "source": "website",
                            "media_type": "image",
                            "detected_content": "Groomed dog portrait",
                            "possible_categories": ["service"],
                            "quality_score": 80.0,
                            "score_breakdown": {
                                "technical_quality": 20.0,
                                "business_relevance_authenticity": 20.0,
                                "composition_usability": 16.0,
                                "emotion_aesthetics": 12.0,
                                "distinctiveness_collection_fit": 8.0,
                                "evidence_confidence": 4.0,
                            },
                            "visual_strengths": ["Clear subject"],
                            "visual_weaknesses": ["Plain background"],
                            "verification_status": "unverified",
                            "visual_analysis_status": "analyzed",
                            "recommended_use": "service",
                            "selection_status": "recommended",
                            "selection_reason": "Clear grooming sample",
                        }],
                    }),
                    usage={"input_tokens": 200, "output_tokens": 120},
                )
            assert "minified JSON" in prompt
            assert "design_art_direction" in prompt and "final_quality_assessment" in prompt
            self.mode_b_calls += 1
            tracker.record({"input_tokens": 300, "output_tokens": 180}, retries=0)
            payload = {
                "schema_version": "visual-director-v2.0",
                "job_id": "job_otpm",
                "mode": "FINAL_CURATE",
                "business_visual_identity": {
                    "style_summary": "Warm salon",
                    "atmosphere": "Friendly",
                    "brand_personality": "Caring",
                    "key_visual_strengths": ["Authentic pets"],
                    "recommended_design_direction": "Clean editorial",
                },
                "analysis_coverage": {"bogus_ignored": True},
                "service_images": [{
                    "asset_id": "img_01",
                    "reason": "Clear service visual",
                    "recommended_section": "service",
                    "selection_status": "selected",
                    "extra_model_field": "dropped_safely",
                }],
                "design_art_direction": {
                    "narrative": "Warm grooming story",
                    "layout_guidance": "Grid",
                    "color_and_lighting": "Bright",
                    "typography_mood": "Modern",
                    "section_pairing": "Hero+Service",
                    "diversity_notes": "Limited sample",
                },
                "selected_asset_ids": ["img_01"],
                "rights_review_required_ids": [],
            }
            if self.mode_b_calls == 1:
                # Simulate real Qwen omitting trailing final_quality_assessment on attempt 1
                return CompletionResult(text=json.dumps(payload), usage={"input_tokens": 300, "output_tokens": 180})
            assert "SCHEMA VALIDATION FIX REQUIRED" in prompt
            assert "final_quality_assessment" in prompt
            payload["final_quality_assessment"] = {
                "overall_assessment": "Good sample",
                "strengths": ["Sharp"],
                "weaknesses": ["Single photo"],
                "meets_premium_standard": False,
            }
            return CompletionResult(text=json.dumps(payload), usage={"input_tokens": 300, "output_tokens": 180})

    cfg = AIProviderConfig(
        provider="openai_compatible",
        model="qwen/qwen3.8-27b",
        endpoint="https://api.groq.com/openai/v1/chat/completions",
        api_key="test-key",
        mode_a_max_output_tokens=850,
        mode_b_max_output_tokens=950,
        max_images_per_request=1,
    )
    res = run_ai_curation(
        job_dir,
        "job_otpm",
        include_undated_website=True,
        config=cfg,
        provider=_QwenDriftProvider(),
    )
    assert res.status == "completed", res.errors
    assert res.mode_b["service_images"][0]["selection_status"] == "recommended"
    assert "extra_model_field" not in res.mode_b["service_images"][0]
    assert res.mode_b["analysis_coverage"]["assets_analyzed"] == 1
    assert res.mode_b["rights_review_required_ids"] == ["img_01"]
    assert any("selected" in w and "recommended" in w for w in res.warnings)
