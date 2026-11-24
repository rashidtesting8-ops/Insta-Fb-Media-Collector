"""Offline end-to-end integration tests for real M4 approval, Start AI dispatch,
audited rights verification ledger, and conditional private Google Drive delivery.

All tests run with live sockets hard-blocked (--network none), using real locally
generated Pillow images, real ffmpeg MP4 videos, fake non-mock Groq transport over
OpenAICompatibleProvider, and a fake private Google Drive adapter. Zero live
website, social, Groq, or Google Drive calls.
"""

from __future__ import annotations

import base64
from contextlib import closing
from datetime import UTC, datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import uuid

from PIL import Image, ImageDraw
import pytest
import requests

from app import collection_approval as ledger
from app import delivery, named_auth
from app.ai.provider import (
    GROQ_ACCEPTANCE_ENDPOINT,
    AIProviderConfig,
    OpenAICompatibleProvider,
)
from app.ai_execution import require_transport
from app.public_collection import collect_public_job
from app.publication import frozen_window
from tests.test_named_auth import KEY as ASSERTION_KEY
from tests.test_named_auth import envelope
from tests.test_phase9_security import request, worker  # noqa: F401

WEB = "https://m4-groomer.fixture.test"
IG = "https://www.instagram.com/lilysclubuk/"
FB = "https://www.facebook.com/people/Lilys-club-Dog-Grooming/61564648418385/"
APPROVED_CDN = "https://approved-media.fixture.test"
WINDOW = frozen_window(datetime(2026, 11, 24, 12, 18, 41, tzinfo=UTC))
FAKE_GROQ_KEY = "gsk_offline_synthetic_never_real_0123456789"


@pytest.fixture(autouse=True)
def block_all_sockets(monkeypatch):
    def deny(*_args, **_kwargs):
        pytest.fail("Offline M4 pipeline test attempted a live socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny)


def _jpeg_bytes(width: int, height: int, pattern: str) -> bytes:
    img = Image.new("RGB", (width, height), (242, 238, 230))
    draw = ImageDraw.Draw(img)
    if pattern == "bars":
        for x in range(0, width, 40):
            draw.rectangle([x, 0, x + 20, height], fill=(185, 85, 35))
        draw.rectangle([0, height // 2, width, height], fill=(35, 55, 85))
    elif pattern == "diagonal":
        draw.rectangle([0, 0, width, height], fill=(30, 50, 70))
        for offset in range(-height, width, 36):
            draw.line([(offset, 0), (offset + height, height)], fill=(235, 220, 140), width=14)
        draw.rectangle([width // 2, 0, width, height // 2], fill=(220, 90, 90))
    else:  # "checker"
        draw.rectangle([0, 0, width, height], fill=(20, 120, 95))
        draw.ellipse([20, 20, width // 2, height - 20], fill=(245, 240, 225))
        draw.polygon([(width // 2, height), (width, 20), (width, height)], fill=(45, 30, 60))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _mp4_bytes(tmp_path: Path) -> bytes:
    target = tmp_path / "m4-fixture-clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1.5:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-t", "1.5", str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return target.read_bytes()


def _mock_http_get(responses: dict[str, tuple[int, dict, bytes | str]]):
    def fake_get(session, url, **kwargs):
        assert not session.cookies and not session.trust_env and kwargs.get("allow_redirects") is False
        assert url in responses, f"Unexpected HTTP fetch: {url}"
        status, headers, body = responses[url]
        resp = requests.Response()
        resp.status_code = status
        resp.url = url
        resp.headers.update(headers)
        resp._content = body.encode("utf-8") if isinstance(body, str) else body
        resp._content_consumed = True
        return resp

    return fake_get


def _extract_block(prompt: str, marker: str) -> list[dict]:
    start = prompt.find(marker)
    assert start >= 0
    fence_start = prompt.find("```", start)
    fence_end = prompt.find("```", fence_start + 3)
    block = prompt[fence_start + 3 : fence_end].strip()
    if block.startswith("json"):
        block = block[4:].strip()
    return json.loads(block)


def _build_fake_groq_transport(calls: list[dict], *, fail_http_500: bool = False):
    """Non-mock OpenAI-compatible fake Groq transport exercising full provider + ai_execution paths."""

    def transport(req: dict) -> dict:
        require_transport()
        assert req["url"] == GROQ_ACCEPTANCE_ENDPOINT
        assert req["headers"].get("Authorization") == f"Bearer {FAKE_GROQ_KEY}"
        assert req["body"]["response_format"] == {"type": "json_object"}
        if fail_http_500:
            resp = requests.Response()
            resp.status_code = 500
            resp._content = json.dumps({
                "error": {"code": "upstream_error", "message": f"Upstream boom leaking {FAKE_GROQ_KEY}"}
            }).encode("utf-8")
            raise requests.HTTPError("500 Server Error", response=resp)

        content = req["body"]["messages"][0]["content"]
        prompt_text = content[0]["text"]
        image_parts = content[1:]
        mode = "B" if not image_parts else "A"
        if mode == "A":
            # Verify every image payload sent to fake Groq is a valid base64 JPEG.
            for part in image_parts:
                url = part["image_url"]["url"]
                assert url.startswith("data:image/jpeg;base64,")
                raw = base64.b64decode(url.split(",", 1)[1])
                with Image.open(BytesIO(raw)) as img:
                    assert img.format == "JPEG"
            specs = _extract_block(prompt_text, "ASSET_SPECS")
            calls.append({"mode": "A", "asset_ids": [s["asset_id"] for s in specs], "image_parts": len(image_parts)})
            items = []
            for idx, spec in enumerate(specs):
                media_type = spec.get("media_type") or "image"
                entry = {
                    "asset_id": spec["asset_id"],
                    "filename": spec.get("filename") or spec["asset_id"],
                    "source": spec.get("platform") or "website",
                    "media_type": media_type,
                    "detected_content": f"Verified grooming visual for {spec['asset_id']}",
                    "possible_categories": ["gallery", "service"],
                    "quality_score": 82.0 - idx,
                    "score_breakdown": {
                        "technical_quality": 20.0,
                        "business_relevance_authenticity": 22.0,
                        "composition_usability": 16.0,
                        "emotion_aesthetics": 12.0,
                        "distinctiveness_collection_fit": 8.0,
                        "evidence_confidence": 4.0 - idx if idx <= 3 else 1.0,
                    },
                    "visual_strengths": ["Clear framing and authentic pet grooming subject"],
                    "visual_weaknesses": ["Minor background clutter"],
                    "verification_status": "unverified",
                    "visual_analysis_status": "analyzed",
                    "recommended_use": "video" if media_type == "video" else "gallery",
                    "selection_status": "considered",
                    "selection_reason": "Strong offline fake-Groq quality score",
                }
                if media_type == "video":
                    entry["video_review_status"] = "frames_reviewed"
                    entry["evaluated_frames"] = spec.get("frame_timestamps") or [0.5]
                items.append(entry)
            payload_json = {"mode": "ANALYZE_BATCH", "items": items}
            return {
                "choices": [{"message": {"content": json.dumps(payload_json)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 450, "completion_tokens": 220},
            }

        # MODE B: select all analyzed images and videos strictly from VALIDATED_ANALYSES.
        analyses = _extract_block(prompt_text, "VALIDATED_ANALYSES")
        calls.append({"mode": "B", "asset_ids": [a["asset_id"] for a in analyses]})
        match = re.search(r"job_id[\"':\s]+([a-f0-9]{32})", prompt_text)
        job_id = match.group(1) if match else "unknown"
        images = [a for a in analyses if a.get("media_type") != "video"]
        videos = [a for a in analyses if a.get("media_type") == "video"]
        hero = [
            {
                "rank": i + 1,
                "asset_id": a["asset_id"],
                "reason": "Top curated image",
                "composition_notes": "Balanced framing",
                "desktop_crop": "16:9 safe",
                "mobile_crop": "4:5 safe",
                "focal_point": {"x": 0.5, "y": 0.5},
                "text_overlay_suitability": "suitable",
                "limitations": "Rights verification pending",
            }
            for i, a in enumerate(images[:3])
        ]
        video_recs = [
            {
                "asset_id": v["asset_id"],
                "filename": v.get("filename") or v["asset_id"],
                "source": v.get("source") or "website",
                "video_review_status": "frames_reviewed",
                "evaluated_frames": v.get("evaluated_frames") or [0.5],
                "quality_score": float(v.get("quality_score") or 80.0),
                "recommended_section": "hero",
                "selection_status": "considered",
                "reason": "Authentic grooming clip",
                "limitations": "Rights verification pending",
            }
            for v in videos
        ]
        selected = [h["asset_id"] for h in hero] + [v["asset_id"] for v in video_recs]
        mode_b = {
            "schema_version": "visual-director-v2.0",
            "job_id": job_id,
            "mode": "FINAL_CURATE",
            "business_visual_identity": {
                "style_summary": "Warm salon grooming showcase",
                "atmosphere": "Clean and welcoming",
                "brand_personality": "Professional pet care",
                "key_visual_strengths": ["Authentic dog grooming photos and clip"],
                "recommended_design_direction": "Bright editorial grid",
            },
            "analysis_coverage": {
                "assets_supplied": len(analyses),
                "assets_analyzed": len(analyses),
                "inherited_from_duplicates": 0,
                "unavailable": 0,
                "prompt_version": "V2.0",
                "provider": "openai_compatible",
                "model": req["body"]["model"],
                "mock": False,
                "usage": {"requests": 2, "retries": 0, "input_tokens": 700, "output_tokens": 420, "estimated_cost_usd": 0.002},
            },
            "hero_selection": hero,
            "salon_images": [],
            "founder_and_team_images": [],
            "service_images": [],
            "grooming_transformations": [],
            "before_after_pairs": [],
            "gallery_images": [],
            "video_recommendations": video_recs,
            "design_art_direction": {
                "narrative": "Show authentic grooming craft",
                "layout_guidance": "Lead with bright portrait",
                "color_and_lighting": "Natural warm tones",
                "typography_mood": "Friendly modern serif",
                "section_pairing": "Pair hero with clip",
                "diversity_notes": "Covers website and Facebook sources",
            },
            "missing_assets": [],
            "rejected_assets": [],
            "selected_asset_ids": selected,
            "rights_review_required_ids": selected,
            "final_quality_assessment": {
                "overall_assessment": "Ready for rights review",
                "strengths": ["Clean verified media"],
                "weaknesses": ["Pending rights ledger clearance"],
                "meets_premium_standard": True,
                "notes": "Offline fake Groq non-mock run",
            },
        }
        return {
            "choices": [{"message": {"content": json.dumps(mode_b)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 250, "completion_tokens": 200},
        }

    return transport


@pytest.fixture
def m4_env(worker, tmp_path, monkeypatch):
    """Production-layout isolated worker: DATA_DIR/_system/jobs.sqlite, default-OFF M4 flag."""
    monkeypatch.setenv("NAMED_ASSERTION_SECRET", ASSERTION_KEY)
    monkeypatch.delenv("M4_COLLECTION_APPROVAL_ENABLED", raising=False)
    monkeypatch.setattr(worker.app.state, "offline_approval_qa", False, raising=False)
    monkeypatch.setattr(worker.app.state, "ai_provider_factory", None, raising=False)
    system_db = worker.DATA_DIR / "_system" / "jobs.sqlite"
    system_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(system_db)) as copied, worker.connect() as original:
        original.backup(copied)
    monkeypatch.setattr(worker, "DB", system_db)

    admin_a_id = named_auth.bootstrap(system_db, "admin-a", "pass-admin-a", verified_operator="test-operator")

    def invoke(action: str, body: dict, session: str = "", **overrides):
        raw, headers = envelope(action, body, session, **overrides)
        status, _, data = request(worker.app, "/named/" + action, "POST", "test-dashboard", (raw,), headers)
        return status, json.loads(data)

    st, login_a = invoke("login", {"name": "admin-a", "password": "pass-admin-a"})
    assert st == 200
    session_a = login_a["session"]

    st, created_b = invoke("create", {"name": "admin-b", "password": "pass-admin-b"}, session_a)
    assert st == 200
    admin_b_id = created_b["principal_id"]
    st, login_b = invoke("login", {"name": "admin-b", "password": "pass-admin-b"})
    assert st == 200
    session_b = login_b["session"]

    return {
        "worker": worker,
        "db": system_db,
        "tmp_path": tmp_path,
        "invoke": invoke,
        "admin_a_id": admin_a_id,
        "admin_b_id": admin_b_id,
        "session_a": session_a,
        "session_b": session_b,
    }


def _create_real_checkpoint_job(m4_env, monkeypatch):
    """Run real CollectionCheckpoint + preprocessing offline through service._process_job."""
    worker = m4_env["worker"]
    tmp_path = m4_env["tmp_path"]
    invoke = m4_env["invoke"]
    session_a = m4_env["session_a"]

    web_jpg = _jpeg_bytes(640, 480, "bars")
    undated_jpg = _jpeg_bytes(480, 360, "diagonal")
    fb_jpg = _jpeg_bytes(540, 540, "checker")
    clip_mp4 = _mp4_bytes(tmp_path)

    ld_json = json.dumps([
        {"@type": "ImageObject", "contentUrl": f"{APPROVED_CDN}/web-hero.jpg", "datePublished": "2026-10-15T10:00:00Z"},
        {"@type": "VideoObject", "contentUrl": f"{APPROVED_CDN}/web-clip.mp4", "datePublished": "2026-10-20T12:00:00Z"},
    ])
    home_html = (
        f'<html><head><script type="application/ld+json">{ld_json}</script></head><body>'
        f'<img src="{APPROVED_CDN}/web-hero.jpg">'
        f'<img src="{APPROVED_CDN}/web-undated.jpg">'
        f'<video><source src="{APPROVED_CDN}/web-clip.mp4"></video>'
        f"</body></html>"
    )
    responses = {
        f"{WEB}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{WEB}/": (200, {"Content-Type": "text/html"}, home_html),
        f"{APPROVED_CDN}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{APPROVED_CDN}/web-hero.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(web_jpg))}, web_jpg),
        f"{APPROVED_CDN}/web-undated.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(undated_jpg))}, undated_jpg),
        f"{APPROVED_CDN}/web-clip.mp4": (200, {"Content-Type": "video/mp4", "Content-Length": str(len(clip_mp4))}, clip_mp4),
        f"{APPROVED_CDN}/fb-photo-1.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(fb_jpg))}, fb_jpg),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses))

    def fake_social_cli(cmd: list[str], _stage: Path) -> tuple[int, str]:
        if "facebook" in cmd:
            return 0, json.dumps([[3, f"{APPROVED_CDN}/fb-photo-1.jpg", {
                "category": "facebook", "subcategory": "photos",
                "id": "122230723652488280", "set_id": "pb.61564648418385.-2207520000",
                "caption": "Fresh puppy groom", "date": "2026-10-21 08:35:34", "extension": "jpg",
            }]])
        return 0, json.dumps([])

    cfg = {
        "run_preprocessing": True,
        "sources": {
            "website": {
                "url": f"{WEB}/", "sitemaps": [], "media_origins": [APPROVED_CDN],
                "request_budget": 12, "page_budget": 2, "document_limit": 16384, "entry_limit": 10,
                "transfer_budget": 5, "byte_limit": 5_000_000, "time_budget_seconds": 30,
            },
            "instagram": {
                "url": IG, "discovery_mode": "open_source_cli", "download_authorized": False,
                "media_origins": [APPROVED_CDN],
                "cli_request_budget": 1, "request_budget": 2, "page_budget": 1, "entry_limit": 5,
                "transfer_budget": 0, "byte_limit": 5_000_000, "time_budget_seconds": 20,
            },
            "facebook": {
                "url": FB, "discovery_mode": "open_source_cli", "download_authorized": True,
                "media_origins": [APPROVED_CDN],
                "cli_request_budget": 1, "request_budget": 6, "page_budget": 1, "entry_limit": 5,
                "transfer_budget": 3, "byte_limit": 5_000_000, "time_budget_seconds": 30,
            },
        },
    }
    cfg_path = tmp_path / "checkpoint-cfg.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("PUBLIC_COLLECTION_CHECKPOINTS", "1")
    monkeypatch.setenv("PUBLIC_COLLECTION_CONFIG", str(cfg_path))
    monkeypatch.setattr(worker, "frozen_window", lambda _: WINDOW)

    from app import public_collection
    real_collect = public_collection.collect_public_job
    monkeypatch.setattr(
        public_collection,
        "collect_public_job",
        lambda job_dir, job, config, **kw: real_collect(job_dir, job, config, social_runner=fake_social_cli),
    )

    intake_req = {
        "website_url": f"{WEB}/",
        "instagram_url": IG,
        "facebook_url": FB,
        "social_permission_confirmed": True,
        "run_ai_curation": False,
        "idempotency_key": f"m4-e2e-{uuid.uuid4().hex[:8]}",
    }
    st, ticket_resp = invoke("intake", intake_req, session_a)
    assert st == 200, ticket_resp
    job_payload = {
        "website_url": f"{WEB}/",
        "instagram_url": IG,
        "facebook_url": FB,
        "social_permission_confirmed": True,
        "run_ai_curation": False,
        "idempotency_key": ticket_resp["idempotency_key"],
    }
    scheduled: list[tuple] = []
    monkeypatch.setattr(worker.executor, "submit", lambda *args: scheduled.append(args))
    st, _, raw = request(worker.app, "/jobs", "POST", "test-admin", (json.dumps(job_payload).encode(),))
    assert st == 202, raw
    job_id = json.loads(raw)["job_id"]
    fn, *args = scheduled.pop()
    fn(*args)
    return job_id


def test_task2_production_m4_default_off_unmigrated_fail_closed_and_idempotent_migration(m4_env, monkeypatch):
    worker = m4_env["worker"]
    db = m4_env["db"]
    invoke = m4_env["invoke"]
    session_a = m4_env["session_a"]
    dummy_job = "a" * 32

    # 1. Default OFF: all M4 collection routes and QA routes return 404.
    for action, body in (
        ("collection-panel", {"job_id": dummy_job}),
        ("collection-review", {"job_id": dummy_job}),
        ("collection-rights-review", {"job_id": dummy_job}),
        ("collection-execute", {"job_id": dummy_job, "if_match": "x", "processing_consent": {"a": {"source_url": "u", "sha256": "s"}}}),
    ):
        assert invoke(action, body, session_a)[0] == 404
    assert request(worker.app, f"/qa/collection/{dummy_job}/poll", "GET", "test-admin")[0] == 404
    assert request(worker.app, f"/qa/collection/{dummy_job}/resume", "POST", "test-admin", (b'{"etag":"x"}',))[0] == 404

    # 2. Production flag ON before migration: fail-closed with 503 (never silent auto-migration),
    #    and QA-only mock endpoints remain strictly 404.
    monkeypatch.setenv("M4_COLLECTION_APPROVAL_ENABLED", "1")
    st, err = invoke("collection-panel", {"job_id": dummy_job}, session_a)
    assert st == 503 and "not migrated" in err["detail"]
    assert request(worker.app, f"/qa/collection/{dummy_job}/poll", "GET", "test-admin")[0] == 404
    assert request(worker.app, f"/qa/collection/{dummy_job}/resume", "POST", "test-admin", (b'{"etag":"x"}',))[0] == 404

    # 3. CLI migration refuses to create a missing DB, and migrates existing DB idempotently twice.
    missing_db = m4_env["tmp_path"] / "does-not-exist.sqlite"
    bad = subprocess.run(
        [sys.executable, "-m", "app.collection_approval", "--migrate-db", str(missing_db)],
        capture_output=True, text=True, check=False,
    )
    assert bad.returncode != 0 and not missing_db.exists()

    with sqlite3.connect(db) as con:
        jobs_before = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        accounts_before = con.execute("SELECT id, name, role, active FROM named_accounts ORDER BY id").fetchall()

    for _ in range(2):
        ok = subprocess.run(
            [sys.executable, "-m", "app.collection_approval", "--migrate-db", str(db)],
            capture_output=True, text=True, check=True,
        )
        assert "M4_MIGRATION_OK" in ok.stdout

    with sqlite3.connect(db) as con:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("SELECT * FROM jobs ORDER BY id").fetchall() == jobs_before
        assert con.execute("SELECT id, name, role, active FROM named_accounts ORDER BY id").fetchall() == accounts_before
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {
            "collection_revisions", "collection_decisions", "collection_ai_starts",
            "collection_rights_decisions", "ai_execution_claims", "ai_provider_attempts",
        } <= tables


def test_full_offline_m4_real_checkpoint_to_fake_groq_rights_ledger_and_fake_private_drive(m4_env, monkeypatch):
    worker = m4_env["worker"]
    db = m4_env["db"]
    invoke = m4_env["invoke"]
    session_a = m4_env["session_a"]
    session_b = m4_env["session_b"]

    # Activate production M4 mode (NOT offline_approval_qa!) and apply explicit additive migration.
    with closing(sqlite3.connect(db)) as con:
        ledger.migrate(con)
    monkeypatch.setenv("M4_COLLECTION_APPROVAL_ENABLED", "1")
    assert worker.app.state.offline_approval_qa is False

    # 1. Real CollectionCheckpoint collection + preprocessing -> immutable revision 1.
    job_id = _create_real_checkpoint_job(m4_env, monkeypatch)
    job_dir = worker.DATA_DIR / job_id
    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest_bytes_before = (job_dir / "manifest.json").read_bytes()
    manifest_sha_before = hashlib.sha256(manifest_bytes_before).hexdigest()

    frames = [a for a in manifest["assets"] if a.get("type") == "frame"]
    assert len(frames) >= 1, "Expected ffmpeg video frames in manifest"

    # TASK 1 CHECK: approval_panel excludes derived video frames from discovered/unknown_date counts,
    # while keeping video in eligible_assets and derived frames on disk for AI analysis.
    st, panel_b = invoke("collection-panel", {"job_id": job_id}, session_b)
    assert st == 200, panel_b
    web_row = next(r for r in panel_b["sources"] if r["source"] == "website")
    fb_row = next(r for r in panel_b["sources"] if r["source"] == "facebook")
    assert web_row["discovered"] == 3  # 1 dated JPEG + 1 undated JPEG + 1 dated MP4 (frames excluded!)
    assert web_row["downloaded"] == 3
    assert web_row["unknown_date"] == 1  # only web-undated.jpg (frames excluded!)
    assert fb_row["discovered"] == 1 and fb_row["downloaded"] == 1 and fb_row["unknown_date"] == 0

    by_url = {a.get("source_url"): a for a in manifest["assets"] if a.get("type") != "frame"}
    web_hero = by_url[f"{APPROVED_CDN}/web-hero.jpg"]
    web_undated = by_url[f"{APPROVED_CDN}/web-undated.jpg"]
    web_clip = by_url[f"{APPROVED_CDN}/web-clip.mp4"]
    fb_photo = by_url[f"{APPROVED_CDN}/fb-photo-1.jpg"]
    assert set(panel_b["eligible_assets"]) == {web_hero["asset_id"], web_clip["asset_id"], fb_photo["asset_id"]}
    assert web_undated["asset_id"] not in panel_b["eligible_assets"]
    assert all((job_dir / f["local_path"]).is_file() for f in frames)

    # 2. Admin A cannot self-approve; unapproved job blocks execute & delivery.
    st, panel_a = invoke("collection-panel", {"job_id": job_id}, session_a)
    assert st == 200 and panel_a["permissions"]["approve"] is False
    st, _ = invoke("collection-approve", {
        "job_id": job_id, "if_match": panel_a["etag"], "request_key": "self-approve", "reason": "Self attempt",
    }, session_a)
    assert st == 403

    consent = {
        a["asset_id"]: {"source_url": a["source_url"], "sha256": a["sha256"]}
        for a in (web_hero, web_clip, fb_photo)
    }
    groq_calls: list[dict] = []
    fake_transport = _build_fake_groq_transport(groq_calls)
    monkeypatch.setenv("AI_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AI_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    monkeypatch.setenv("AI_ENDPOINT", GROQ_ACCEPTANCE_ENDPOINT)
    monkeypatch.setenv("AI_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("AI_MIN_REQUEST_SPACING_S", "0")
    monkeypatch.setattr(
        worker.app.state,
        "ai_provider_factory",
        lambda cfg: OpenAICompatibleProvider(cfg, sleep=lambda _: None, transport=fake_transport),
        raising=False,
    )

    # Execute before approval -> 409, zero Groq calls.
    assert invoke("collection-execute", {
        "job_id": job_id, "if_match": panel_b["etag"], "processing_consent": consent,
    }, session_b)[0] == 409
    assert groq_calls == []

    # 3. Admin B approves; execute STILL blocked until separate Start AI.
    st, approved = invoke("collection-approve", {
        "job_id": job_id, "if_match": panel_b["etag"], "request_key": "approve-1", "reason": "Inventory reviewed",
    }, session_b)
    assert st == 200 and approved["state"] == "approved" and approved["start_authorized"] is False

    assert invoke("collection-execute", {
        "job_id": job_id, "if_match": approved["etag"], "processing_consent": consent,
    }, session_b)[0] == 409
    assert groq_calls == []

    # 4. Separate Start AI authorization + duplicate Start AI rejection.
    st, started = invoke("collection-start", {
        "job_id": job_id, "if_match": approved["etag"], "request_key": "start-1",
    }, session_a)
    assert st == 200 and started["start_authorized"] is True
    assert invoke("collection-start", {
        "job_id": job_id, "if_match": started["etag"], "request_key": "start-dup",
    }, session_b)[0] == 409

    # 5. Production mode rejects AI_PROVIDER=mock; missing/bad processing_consent rejected before calling Groq.
    monkeypatch.setenv("AI_PROVIDER", "mock")
    assert invoke("collection-execute", {
        "job_id": job_id, "if_match": started["etag"], "processing_consent": consent,
    }, session_b)[0] == 409
    monkeypatch.setenv("AI_PROVIDER", "openai_compatible")

    bad_consent = {web_hero["asset_id"]: {"source_url": web_hero["source_url"], "sha256": "0" * 64}}
    assert invoke("collection-execute", {
        "job_id": job_id, "if_match": started["etag"], "processing_consent": bad_consent,
    }, session_b)[0] == 409
    assert groq_calls == []

    # 6. Execute Start AI with fake non-mock Groq provider over OpenAICompatibleProvider.
    st, exec_out = invoke("collection-execute", {
        "job_id": job_id, "if_match": started["etag"], "processing_consent": consent,
    }, session_b)
    assert st == 200, exec_out
    assert exec_out["status"] == "completed" and exec_out["mock"] is False
    assert exec_out["provider"] == "openai_compatible"
    assert [c["mode"] for c in groq_calls] == ["A", "B"]
    assert set(groq_calls[0]["asset_ids"]) == {web_hero["asset_id"], web_clip["asset_id"], fb_photo["asset_id"]}
    assert web_undated["asset_id"] not in groq_calls[0]["asset_ids"]
    assert (job_dir / "curation.json").is_file()
    curation_raw = (job_dir / "curation.json").read_text(encoding="utf-8")
    assert FAKE_GROQ_KEY not in curation_raw
    curation_doc = json.loads(curation_raw)
    assert curation_doc["ai_envelope"]["mock"] is False
    assert curation_doc["curation"]["analysis_coverage"]["mock"] is False
    assert worker.fetch(job_id)["ai_curation"]["status"] == "completed"

    # Duplicate execution is blocked (409) and never re-calls Groq.
    assert invoke("collection-execute", {
        "job_id": job_id, "if_match": started["etag"], "processing_consent": consent,
    }, session_b)[0] == 409
    assert len(groq_calls) == 2

    # 7. Delivery BEFORE rights verification is blocked (blocked_rights_review / 409).
    st, _, plan_raw = request(worker.app, f"/jobs/{job_id}/delivery-plan", "GET", "test-admin")
    assert st == 200
    pre_rights_plan = json.loads(plan_raw)
    assert pre_rights_plan["status"] == "blocked_rights_review"
    assert pre_rights_plan["assets"] == []
    assert set(pre_rights_plan["pending_rights_ids"]) == {web_hero["asset_id"], web_clip["asset_id"], fb_photo["asset_id"]}
    assert request(worker.app, f"/jobs/{job_id}/delivery-result", "POST", "test-admin",
                   (json.dumps({"authorization_confirmed": True, "uploads": []}).encode(),))[0] == 409

    # 8. Rights verification ledger: verify ONLY web_hero (.media JPEG) + web_clip (.media MP4), leave fb_photo unverified.
    #    Rejects derived frame asset (409) and stale if_match (412).
    assert invoke("collection-rights-decide", {
        "job_id": job_id, "asset_id": frames[0]["asset_id"], "decision": "verified",
        "reason": "Frame not original", "evidence_ref": "doc-frame",
        "if_match": started["etag"], "request_key": "rights-frame",
    }, session_b)[0] == 409
    assert invoke("collection-rights-decide", {
        "job_id": job_id, "asset_id": web_hero["asset_id"], "decision": "verified",
        "reason": "Stale etag test", "evidence_ref": "doc-stale",
        "if_match": "stale-etag", "request_key": "rights-stale",
    }, session_b)[0] == 412

    hero_decision = {
        "job_id": job_id, "asset_id": web_hero["asset_id"], "decision": "verified",
        "reason": "Signed client commercial license", "evidence_ref": "license-2026-001",
        "if_match": started["etag"], "request_key": "rights-hero-1",
    }
    st, r1 = invoke("collection-rights-decide", hero_decision, session_b)
    assert st == 200 and web_hero["asset_id"] in r1["verified_asset_ids"]
    # Idempotent replay succeeds; conflicting request_key fails 409.
    st, r1_replay = invoke("collection-rights-decide", hero_decision, session_b)
    assert st == 200 and r1_replay["idempotent_replay"] is True
    assert invoke("collection-rights-decide", {**hero_decision, "decision": "rejected"}, session_b)[0] == 409

    st, r2 = invoke("collection-rights-decide", {
        "job_id": job_id, "asset_id": web_clip["asset_id"], "decision": "verified",
        "reason": "Signed client video release", "evidence_ref": "license-2026-002",
        "if_match": started["etag"], "request_key": "rights-clip-1",
    }, session_b)
    assert st == 200 and set(r2["verified_asset_ids"]) == {web_hero["asset_id"], web_clip["asset_id"]}

    # Manifest MUST remain byte-for-byte immutable after rights verification!
    assert (job_dir / "manifest.json").read_bytes() == manifest_bytes_before
    assert hashlib.sha256((job_dir / "manifest.json").read_bytes()).hexdigest() == manifest_sha_before

    # Immutability triggers on collection_rights_decisions forbid UPDATE, DELETE, and INSERT OR REPLACE.
    with sqlite3.connect(db) as con:
        for stmt in (
            "DELETE FROM collection_rights_decisions",
            "UPDATE collection_rights_decisions SET decision='rejected'",
            "INSERT OR REPLACE INTO collection_rights_decisions SELECT * FROM collection_rights_decisions",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(stmt)

    # 9. Conditional secure delivery plan releases ONLY verified web_hero + web_clip with safe MIME-derived filenames.
    st, _, plan_raw = request(worker.app, f"/jobs/{job_id}/delivery-plan", "GET", "test-admin", query=b"business_name=Lilys+Club")
    assert st == 200
    plan = json.loads(plan_raw)
    assert plan["status"] == "ready"
    assert plan["visibility"] == "private" and plan["public_sharing"] is False
    assert {a["asset_id"] for a in plan["assets"]} == {web_hero["asset_id"], web_clip["asset_id"]}
    assert plan["pending_rights_ids"] == [fb_photo["asset_id"]]
    filenames = {a["asset_id"]: a["filename"] for a in plan["assets"]}
    assert filenames[web_hero["asset_id"]].endswith(".jpg")
    assert filenames[web_clip["asset_id"]].endswith(".mp4")
    assert all(not name.endswith(".media") and "/" not in name and "\\" not in name and ".." not in name for name in filenames.values())

    # 10. Delivery finalization rejects public_sharing, non-private visibility, and missing confirmation;
    #     fake private Drive adapter uploads ONLY verified assets, ignores smuggled unverified asset, and dedupes retries.
    for bad_body in (
        {"authorization_confirmed": True, "public_sharing": True, "uploads": []},
        {"authorization_confirmed": True, "visibility": "public", "uploads": []},
        {"authorization_confirmed": False, "uploads": []},
    ):
        assert request(worker.app, f"/jobs/{job_id}/delivery-result", "POST", "test-admin",
                       (json.dumps(bad_body).encode(),))[0] == 403

    fake_drive_store: list[dict] = []

    def fake_private_drive_upload(delivery_plan: dict) -> list[dict]:
        assert delivery_plan["visibility"] == "private" and delivery_plan["public_sharing"] is False
        uploaded = []
        for item in delivery_plan["pending_uploads"]:
            src = job_dir / item["local_path"]
            assert src.is_file() and hashlib.sha256(src.read_bytes()).hexdigest() == item["sha256"]
            drive_id = f"priv-drive-{len(fake_drive_store) + 1}"
            fake_drive_store.append({
                "drive_file_id": drive_id,
                "folder": delivery_plan["folder_name"],
                "filename": item["filename"],
                "visibility": "private",
                "public_sharing": False,
                "sha256": item["sha256"],
            })
            uploaded.append({
                "upload_key": item["upload_key"],
                "drive_file_id": drive_id,
                "drive_link": f"https://drive.google.com/file/d/{drive_id}/view",
                "status": "uploaded",
            })
        return uploaded

    uploads_batch_1 = fake_private_drive_upload(plan)
    # Attempt to smuggle unverified fb_photo into uploads payload: must be silently ignored.
    uploads_with_smuggle = uploads_batch_1 + [{
        "upload_key": f"{fb_photo['asset_id']}:{fb_photo['sha256']}",
        "drive_file_id": "smuggled-drive-id",
        "status": "uploaded",
    }]
    st, _, fin_raw = request(worker.app, f"/jobs/{job_id}/delivery-result", "POST", "test-admin", (json.dumps({
        "authorization_confirmed": True,
        "visibility": "private",
        "public_sharing": False,
        "drive_folder_id": "priv-folder-lilys",
        "drive_folder_link": "https://drive.google.com/drive/folders/priv-folder-lilys",
        "uploads": uploads_with_smuggle,
    }).encode(),))
    assert st == 200, fin_raw
    fin = json.loads(fin_raw)
    assert fin["status"] == "partial_success"  # fb_photo still awaiting rights verification
    assert fin["visibility"] == "private" and fin["public_sharing"] is False
    assert {u["asset_id"] for u in fin["uploaded"]} == {web_hero["asset_id"], web_clip["asset_id"]}
    assert all(u["drive_file_id"] != "smuggled-drive-id" for u in fin["uploaded"])

    # Retry plan sees zero pending uploads for already uploaded assets (no duplicate uploads).
    st, _, retry_plan_raw = request(worker.app, f"/jobs/{job_id}/delivery-plan", "GET", "test-admin")
    assert st == 200 and json.loads(retry_plan_raw)["pending_uploads"] == []

    # Verify remaining fb_photo -> uploads ONLY fb_photo -> status becomes completed!
    st, _ = invoke("collection-rights-decide", {
        "job_id": job_id, "asset_id": fb_photo["asset_id"], "decision": "verified",
        "reason": "Client confirmed Facebook photo ownership", "evidence_ref": "license-2026-003",
        "if_match": started["etag"], "request_key": "rights-fb-1",
    }, session_b)
    assert st == 200
    st, _, plan2_raw = request(worker.app, f"/jobs/{job_id}/delivery-plan", "GET", "test-admin")
    plan2 = json.loads(plan2_raw)
    assert [a["asset_id"] for a in plan2["pending_uploads"]] == [fb_photo["asset_id"]]
    assert plan2["pending_uploads"][0]["filename"].endswith(".jpg")
    uploads_batch_2 = fake_private_drive_upload(plan2)
    st, _, fin2_raw = request(worker.app, f"/jobs/{job_id}/delivery-result", "POST", "test-admin", (json.dumps({
        "authorization_confirmed": True, "uploads": uploads_batch_2,
    }).encode(),))
    assert st == 200
    fin2 = json.loads(fin2_raw)
    assert fin2["status"] == "completed"
    assert len(fin2["uploaded"]) == 3 and len(fake_drive_store) == 3


def test_rights_revocation_suspension_revision_tamper_mock_and_bad_mime_block_delivery(m4_env, monkeypatch):
    worker = m4_env["worker"]
    db = m4_env["db"]
    invoke = m4_env["invoke"]
    session_a = m4_env["session_a"]
    session_b = m4_env["session_b"]

    with closing(sqlite3.connect(db)) as con:
        ledger.migrate(con)
    monkeypatch.setenv("M4_COLLECTION_APPROVAL_ENABLED", "1")

    # Add a third admin (admin-c) specifically to test suspending the rights-deciding admin
    # independently from the collection-approving admin (admin-b).
    st, created_c = invoke("create", {"name": "admin-c", "password": "pass-admin-c"}, session_a)
    assert st == 200
    admin_c_id = created_c["principal_id"]
    session_c = invoke("login", {"name": "admin-c", "password": "pass-admin-c"})[1]["session"]

    job_id = _create_real_checkpoint_job(m4_env, monkeypatch)
    job_dir = worker.DATA_DIR / job_id
    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    by_url = {a.get("source_url"): a for a in manifest["assets"] if a.get("type") != "frame"}
    web_hero = by_url[f"{APPROVED_CDN}/web-hero.jpg"]
    web_clip = by_url[f"{APPROVED_CDN}/web-clip.mp4"]
    fb_photo = by_url[f"{APPROVED_CDN}/fb-photo-1.jpg"]

    panel = invoke("collection-panel", {"job_id": job_id}, session_b)[1]
    approved = invoke("collection-approve", {
        "job_id": job_id, "if_match": panel["etag"], "request_key": "app-neg", "reason": "Reviewed",
    }, session_b)[1]
    started = invoke("collection-start", {
        "job_id": job_id, "if_match": approved["etag"], "request_key": "start-neg",
    }, session_a)[1]

    calls: list[dict] = []
    monkeypatch.setenv("AI_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AI_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    monkeypatch.setenv("AI_ENDPOINT", GROQ_ACCEPTANCE_ENDPOINT)
    monkeypatch.setenv("AI_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("AI_MIN_REQUEST_SPACING_S", "0")
    monkeypatch.setattr(
        worker.app.state,
        "ai_provider_factory",
        lambda cfg: OpenAICompatibleProvider(cfg, sleep=lambda _: None, transport=_build_fake_groq_transport(calls)),
        raising=False,
    )
    consent = {a["asset_id"]: {"source_url": a["source_url"], "sha256": a["sha256"]} for a in (web_hero, web_clip, fb_photo)}
    assert invoke("collection-execute", {
        "job_id": job_id, "if_match": started["etag"], "processing_consent": consent,
    }, session_b)[0] == 200

    # Admin C verifies web_hero rights -> delivery ready.
    assert invoke("collection-rights-decide", {
        "job_id": job_id, "asset_id": web_hero["asset_id"], "decision": "verified",
        "reason": "Cleared by C", "evidence_ref": "ev-c-1",
        "if_match": started["etag"], "request_key": "rights-c-verify",
    }, session_c)[0] == 200
    assert delivery.build_delivery_plan(job_dir, job_id, db_path=db, projects_root=worker.DATA_DIR)["status"] == "ready"

    # (a) Appending 'revoked' rights decision immediately blocks web_hero.
    assert invoke("collection-rights-decide", {
        "job_id": job_id, "asset_id": web_hero["asset_id"], "decision": "revoked",
        "reason": "Client withdrew consent", "evidence_ref": "ev-c-revoke",
        "if_match": started["etag"], "request_key": "rights-c-revoke",
    }, session_c)[0] == 200
    assert delivery.build_delivery_plan(job_dir, job_id, db_path=db, projects_root=worker.DATA_DIR)["status"] == "blocked_rights_review"

    # Re-verify web_hero by Admin C -> ready again.
    assert invoke("collection-rights-decide", {
        "job_id": job_id, "asset_id": web_hero["asset_id"], "decision": "verified",
        "reason": "Re-cleared by C", "evidence_ref": "ev-c-2",
        "if_match": started["etag"], "request_key": "rights-c-reverify",
    }, session_c)[0] == 200
    assert delivery.build_delivery_plan(job_dir, job_id, db_path=db, projects_root=worker.DATA_DIR)["status"] == "ready"

    # (b) Suspending deciding Admin C immediately invalidates C's rights clearance!
    assert invoke("suspend", {"principal_id": admin_c_id}, session_a)[0] == 200
    plan_after_suspend = delivery.build_delivery_plan(job_dir, job_id, db_path=db, projects_root=worker.DATA_DIR)
    assert plan_after_suspend["status"] == "blocked_rights_review" and plan_after_suspend["assets"] == []

    # Re-verify web_hero by active Admin B -> ready again for remaining negative checks.
    assert invoke("collection-rights-decide", {
        "job_id": job_id, "asset_id": web_hero["asset_id"], "decision": "verified",
        "reason": "Verified by active B", "evidence_ref": "ev-b-1",
        "if_match": started["etag"], "request_key": "rights-b-verify",
    }, session_b)[0] == 200
    assert delivery.build_delivery_plan(job_dir, job_id, db_path=db, projects_root=worker.DATA_DIR)["status"] == "ready"

    # (c) Mock curation is ALWAYS blocked on checkpoint jobs (even if allow_fixture=True).
    curation_path = job_dir / "curation.json"
    orig_curation = curation_path.read_bytes()
    mocked = json.loads(orig_curation)
    mocked["ai_envelope"]["mock"] = True
    curation_path.write_text(json.dumps(mocked), encoding="utf-8")
    assert delivery.build_delivery_plan(job_dir, job_id, allow_fixture=True, db_path=db, projects_root=worker.DATA_DIR)["status"] == "blocked_non_real_curation"
    curation_path.write_bytes(orig_curation)

    # (d) Unsafe/unverified MIME on a .media file is never released.
    assert delivery._safe_delivery_filename(web_hero["asset_id"], job_dir / web_hero["local_path"], "application/octet-stream", web_hero["sha256"]) is None
    assert delivery._safe_delivery_filename(web_hero["asset_id"], job_dir / web_hero["local_path"], "image/svg+xml", web_hero["sha256"]) is None

    # (e) Tampering a downloaded .media file on disk breaks integrity and blocks delivery.
    media_file = job_dir / web_hero["local_path"]
    orig_media = media_file.read_bytes()
    media_file.write_bytes(orig_media + b"tampered")
    tampered_plan = delivery.build_delivery_plan(job_dir, job_id, db_path=db, projects_root=worker.DATA_DIR)
    assert tampered_plan["status"] == "blocked_unverified_collection"
    media_file.write_bytes(orig_media)

    # (f) Publishing a new revision (revision 2) invalidates revision 1 approval, Start AI, AND rights decisions.
    worker.store(worker.fetch(job_id), collection_base=1)
    assert delivery.build_delivery_plan(job_dir, job_id, db_path=db, projects_root=worker.DATA_DIR)["status"] == "blocked_unverified_collection"
    with sqlite3.connect(db) as con:
        rev2_sha = con.execute("SELECT manifest_sha FROM collection_revisions WHERE job=? AND revision=2", (job_id,)).fetchone()[0]
        assert ledger.verified_rights_for_revision(con, job_id, 2, rev2_sha) == {}


def test_provider_failure_records_honest_failed_state_scrubs_secrets_and_blocks_delivery(m4_env, monkeypatch):
    worker = m4_env["worker"]
    db = m4_env["db"]
    invoke = m4_env["invoke"]
    session_a = m4_env["session_a"]
    session_b = m4_env["session_b"]

    with closing(sqlite3.connect(db)) as con:
        ledger.migrate(con)
    monkeypatch.setenv("M4_COLLECTION_APPROVAL_ENABLED", "1")

    job_id = _create_real_checkpoint_job(m4_env, monkeypatch)
    job_dir = worker.DATA_DIR / job_id
    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    by_url = {a.get("source_url"): a for a in manifest["assets"] if a.get("type") != "frame"}
    eligible = [by_url[f"{APPROVED_CDN}/web-hero.jpg"], by_url[f"{APPROVED_CDN}/web-clip.mp4"], by_url[f"{APPROVED_CDN}/fb-photo-1.jpg"]]

    panel = invoke("collection-panel", {"job_id": job_id}, session_b)[1]
    approved = invoke("collection-approve", {
        "job_id": job_id, "if_match": panel["etag"], "request_key": "app-fail", "reason": "Reviewed",
    }, session_b)[1]
    started = invoke("collection-start", {
        "job_id": job_id, "if_match": approved["etag"], "request_key": "start-fail",
    }, session_a)[1]

    monkeypatch.setenv("AI_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AI_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    monkeypatch.setenv("AI_ENDPOINT", GROQ_ACCEPTANCE_ENDPOINT)
    monkeypatch.setenv("AI_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("AI_MIN_REQUEST_SPACING_S", "0")
    monkeypatch.setattr(
        worker.app.state,
        "ai_provider_factory",
        lambda cfg: OpenAICompatibleProvider(cfg, sleep=lambda _: None, transport=_build_fake_groq_transport([], fail_http_500=True)),
        raising=False,
    )
    consent = {a["asset_id"]: {"source_url": a["source_url"], "sha256": a["sha256"]} for a in eligible}
    st, fail_resp = invoke("collection-execute", {
        "job_id": job_id, "if_match": started["etag"], "processing_consent": consent,
    }, session_b)
    assert st == 502 and fail_resp["status"] == "failed"
    assert FAKE_GROQ_KEY not in json.dumps(fail_resp)
    assert not (job_dir / "curation.json").exists()
    stored_ai = worker.fetch(job_id)["ai_curation"]
    assert stored_ai["status"] == "failed"
    assert FAKE_GROQ_KEY not in json.dumps(stored_ai)

    # Attempt recorded as ambiguous -> duplicate execute blocked (409) and delivery blocked.
    with sqlite3.connect(db) as con:
        outcomes = con.execute("SELECT mode, outcome FROM ai_provider_attempts").fetchall()
        assert outcomes == [("A", "ambiguous")]
    assert invoke("collection-execute", {
        "job_id": job_id, "if_match": started["etag"], "processing_consent": consent,
    }, session_b)[0] == 409
    assert request(worker.app, f"/jobs/{job_id}/delivery-result", "POST", "test-admin",
                   (json.dumps({"authorization_confirmed": True, "uploads": []}).encode(),))[0] == 409
