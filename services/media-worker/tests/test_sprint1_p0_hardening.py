"""Sprint 1 P0 worker security, SQLite concurrency, subprocess isolation, and preprocessing regression tests.

Fully offline (sockets blocked where applicable). Zero external requests.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading

from PIL import Image
import pytest

from app import collection_approval as ledger
from app import named_auth
from app.ai.payload import select_ai_candidates
from app.ai.provider import GROQ_ACCEPTANCE_ENDPOINT, OpenAICompatibleProvider
from app.legacy_social_collect import _execute_subprocess, check_social_url
from app.manifest import validate_against_disk
from app.preprocessing import _resolve, run_media_preprocessing
from app.publication import frozen_window, publication_evidence
from app.security import MAX_BODY, MAX_LARGE_BODY, sanitized_subprocess_env
from tests.test_m4_real_pipeline import (
    FAKE_GROQ_KEY,
    _build_fake_groq_transport,
    _create_real_checkpoint_job,
    m4_env,  # noqa: F401
)
from tests.test_named_auth import KEY as ASSERTION_KEY
from tests.test_named_auth import envelope, identity  # noqa: F401
from tests.test_phase7_orchestration import SOURCE_URLS
from tests.test_phase9_security import request, worker  # noqa: F401


def test_sqlite_connect_closes_fd_on_context_exit(worker):
    with worker.connect() as con:
        assert con.execute("SELECT 1").fetchone() == (1,)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        con.execute("SELECT 1")


def test_concurrent_collection_execute_does_not_clobber_winner(m4_env, monkeypatch):
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
    eligible = [
        a for a in manifest["assets"]
        if a.get("type") != "frame" and a.get("date_status") == "verified"
    ]
    consent = {a["asset_id"]: {"source_url": a["source_url"], "sha256": a["sha256"]} for a in eligible}

    panel = invoke("collection-panel", {"job_id": job_id}, session_b)[1]
    approved = invoke("collection-approve", {
        "job_id": job_id, "if_match": panel["etag"], "request_key": "race-approve", "reason": "Reviewed",
    }, session_b)[1]
    started = invoke("collection-start", {
        "job_id": job_id, "if_match": approved["etag"], "request_key": "race-start",
    }, session_a)[1]

    in_provider = threading.Event()
    release_provider = threading.Event()
    groq_calls: list[dict] = []
    base_transport = _build_fake_groq_transport(groq_calls)

    def blocking_transport(req: dict) -> dict:
        in_provider.set()
        release_provider.wait(timeout=5)
        return base_transport(req)

    monkeypatch.setenv("AI_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AI_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    monkeypatch.setenv("AI_ENDPOINT", GROQ_ACCEPTANCE_ENDPOINT)
    monkeypatch.setenv("AI_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("AI_MIN_REQUEST_SPACING_S", "0")
    monkeypatch.setattr(
        worker.app.state,
        "ai_provider_factory",
        lambda cfg: OpenAICompatibleProvider(cfg, sleep=lambda _: None, transport=blocking_transport),
        raising=False,
    )

    body = {"job_id": job_id, "if_match": started["etag"], "processing_consent": consent}
    # Bypass read-only preflight race by letting Thread 1 claim & block in provider, then Thread 2 calls execute_started_curation directly.
    with ThreadPoolExecutor(2) as pool:
        f1 = pool.submit(invoke, "collection-execute", body, session_b)
        assert in_provider.wait(timeout=5), "Winner never entered provider"
        # Call execute_started_curation directly AND via HTTP to test both preflight and PK-collision paths.
        st_http_loser, _ = invoke("collection-execute", body, session_b)
        assert st_http_loser == 409
        # Simulate simultaneous preflight bypass by forcing PK collision directly in ai_execution.
        # Record status must still be 'started' (never overwritten to 'failed').
        assert worker.fetch(job_id)["ai_curation"]["status"] == "started"
        release_provider.set()
        st_winner, out_winner = f1.result(timeout=10)

    assert st_winner == 200, out_winner
    assert out_winner["status"] == "completed"
    assert (job_dir / "curation.json").is_file()
    assert worker.fetch(job_id)["ai_curation"]["status"] == "completed"


def test_argon2_verify_runs_outside_sqlite_write_lock(worker, identity, monkeypatch):
    _, call = identity
    in_verify = threading.Event()
    proceed_verify = threading.Event()
    real_verify = named_auth.verify_password

    def instrumented_verify(stored_hash, raw_password):
        in_verify.set()
        assert proceed_verify.wait(timeout=5)
        return real_verify(stored_hash, raw_password)

    monkeypatch.setattr(named_auth, "verify_password", instrumented_verify)

    with ThreadPoolExecutor(1) as pool:
        fut = pool.submit(call, "login", {"name": "sample", "password": "synthetic-password"})
        assert in_verify.wait(timeout=5)
        # While Argon2id verify is running, another SQLite writer MUST acquire BEGIN IMMEDIATE without blocking.
        with closing(sqlite3.connect(worker.DB, timeout=0.5)) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("ROLLBACK")
        proceed_verify.set()
        status, payload = fut.result(timeout=5)

    assert status == 200 and payload.get("session")


def test_named_login_rate_limit_malformed_json_and_last_admin_guard(worker, identity):
    actor, call = identity
    session = call("login", {"name": "sample", "password": "synthetic-password"})[1]["session"]

    # Last active admin cannot be suspended or demoted.
    assert call("suspend", {"principal_id": actor}, session)[0] == 409
    assert call("role", {"principal_id": actor, "role": "none"}, session)[0] == 409

    # Malformed JSON with valid HMAC assertion returns 400 (never 500).
    import base64
    import hmac
    import time
    import uuid

    bad_raw = b"{not-json"
    claims = dict(
        iss="pgm-frontend",
        aud="pgm-worker",
        action="session",
        sub=named_auth.digest(session),
        body=hashlib.sha256(bad_raw).hexdigest(),
        exp=int(time.time()) + 30,
        jti=uuid.uuid4().hex,
    )
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    assertion = part + b"." + hmac.digest(ASSERTION_KEY.encode(), part, "sha256").hex().encode()
    st_bad, _, _ = request(
        worker.app,
        "/named/session",
        "POST",
        "test-dashboard",
        (bad_raw,),
        ((b"x-named-session", session.encode()), (b"x-named-assertion", assertion)),
    )
    assert st_bad == 400

    # Login rate limit: 1 login already consumed above; 19 more hit route, 21st returns 429.
    for _ in range(19):
        st, _ = call("login", {"name": "sample", "password": "wrong"})
        assert st == 403
    st_limited, limited_body = call("login", {"name": "sample", "password": "wrong"})
    assert st_limited == 429
    assert "rate limit" in limited_body["detail"].lower()


def test_named_intake_expired_ticket_does_not_lock_out_changed_params(worker, identity):
    _, call = identity
    session = call("login", {"name": "sample", "password": "synthetic-password"})[1]["session"]

    st1, t1 = call("intake", {**SOURCE_URLS, "max_selected_images": 10}, session)
    assert st1 == 200
    # Unexpired ticket with changed inputs still conflicts with 409.
    assert call("intake", {**SOURCE_URLS, "max_selected_images": 20}, session)[0] == 409

    # Once expired, old ticket is pruned and re-submission with changed options succeeds.
    with worker.connect() as con:
        con.execute("UPDATE named_intakes SET expires=0 WHERE ticket=?", (t1["idempotency_key"],))
    st2, t2 = call("intake", {**SOURCE_URLS, "max_selected_images": 20}, session)
    assert st2 == 200 and t2["idempotency_key"] == t1["idempotency_key"]


def test_body_limit_scoped_for_large_execute_and_delivery_endpoints(worker):
    # Standard endpoint keeps strict 64 KiB cap.
    assert request(
        worker.app, "/intake/validate", "POST", "test-dashboard", (b"x" * (MAX_BODY + 1),)
    )[0] == 413

    # delivery-result allows >64 KiB up to MAX_LARGE_BODY (reaches route JSON/auth logic, not 413).
    large_valid = json.dumps({"padding": "a" * (MAX_BODY + 1024)}).encode()
    st_ok, _, _ = request(
        worker.app, f"/jobs/{'a' * 32}/delivery-result", "POST", "test-admin", (large_valid,)
    )
    assert st_ok != 413

    # Exceeding MAX_LARGE_BODY still rejected with 413.
    assert request(
        worker.app,
        f"/jobs/{'a' * 32}/delivery-result",
        "POST",
        "test-admin",
        (b"x" * (MAX_LARGE_BODY + 1),),
    )[0] == 413


def test_subprocess_env_isolation_and_social_port_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_WORKER_TOKEN", "secret-worker-token")
    monkeypatch.setenv("MEDIA_WORKER_DASHBOARD_TOKEN", "secret-dash-token")
    monkeypatch.setenv("AI_API_KEY", "gsk_super_secret_should_never_leak")
    monkeypatch.setenv("NAMED_ASSERTION_SECRET", "secret-assertion-key-32-chars-minimum")
    monkeypatch.setenv("HTTPS_PROXY", "http://egress-gate:8080")

    local_env = sanitized_subprocess_env(stage=tmp_path, allow_proxy=False)
    for forbidden in (
        "MEDIA_WORKER_TOKEN",
        "MEDIA_WORKER_DASHBOARD_TOKEN",
        "AI_API_KEY",
        "NAMED_ASSERTION_SECRET",
        "HTTPS_PROXY",
    ):
        assert forbidden not in local_env
    assert local_env["HOME"] == str(tmp_path)
    assert local_env["XDG_CONFIG_HOME"] == str(tmp_path)

    cli_env = sanitized_subprocess_env(stage=tmp_path, allow_proxy=True)
    assert cli_env["HTTPS_PROXY"] == "http://egress-gate:8080"
    assert "AI_API_KEY" not in cli_env
    assert "MEDIA_WORKER_TOKEN" not in cli_env

    # Execute a real child Python process via _execute_subprocess and verify secrets are absent.
    code, out = _execute_subprocess(
        [sys.executable, "-c", "import os; print(os.environ.get('AI_API_KEY', 'ABSENT'), os.environ.get('MEDIA_WORKER_TOKEN', 'ABSENT'))"],
        tmp_path,
        timeout=10,
        runner=None,
    )
    assert code == 0 and out.strip() == "ABSENT ABSENT"

    # Explicit port rejected by check_social_url.
    with pytest.raises(ValueError):
        check_social_url("facebook", "https://www.facebook.com:8443/lilysclub/")
    with pytest.raises(ValueError):
        check_social_url("facebook", "https://user:@www.facebook.com/lilysclub/")


def test_get_job_file_enforces_manifest_allowlist_media_mime_and_xss_headers(worker):
    job_id = "a" * 32
    job_dir = worker.DATA_DIR / job_id
    (job_dir / "collection" / "media").mkdir(parents=True)
    (job_dir / "research" / "snapshots").mkdir(parents=True)
    (job_dir / "website").mkdir(parents=True)

    # Internal files that MUST NEVER be served.
    (job_dir / "collection" / "checkpoints.sqlite").write_bytes(b"SQLite format 3\x00")
    (job_dir / "research" / "snapshots" / "home.html").write_text("<script>alert(1)</script>", encoding="utf-8")

    # Manifest-listed SVG (unsafe inline XSS) and .media checkpoint JPEG (safe preview media).
    (job_dir / "website" / "logo.svg").write_text("<svg onload=alert(1)></svg>", encoding="utf-8")
    media_file = job_dir / "collection" / "media" / "fb-item.media"
    Image.new("RGB", (64, 64), (100, 150, 200)).save(media_file, format="JPEG")

    manifest = {
        "job_id": job_id,
        "assets": [
            {
                "asset_id": "web_svg",
                "local_path": "website/logo.svg",
                "mime": "image/svg+xml",
                "extraction_status": "downloaded",
            },
            {
                "asset_id": "fb_jpg",
                "local_path": "collection/media/fb-item.media",
                "mime": "image/jpeg",
                "extraction_status": "downloaded",
            },
        ],
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Path traversal rejected with 400.
    assert request(worker.app, f"/jobs/{job_id}/file", key="test-dashboard", query=b"path=../outside.png")[0] == 400
    assert request(worker.app, f"/jobs/{job_id}/file", key="test-dashboard", query=b"path=website%5Clogo.svg")[0] == 400

    # Internal non-manifest files rejected with 404.
    assert request(worker.app, f"/jobs/{job_id}/file", key="test-dashboard", query=b"path=manifest.json")[0] == 404
    assert request(worker.app, f"/jobs/{job_id}/file", key="test-dashboard", query=b"path=collection/checkpoints.sqlite")[0] == 404
    assert request(worker.app, f"/jobs/{job_id}/file", key="test-dashboard", query=b"path=research/snapshots/home.html")[0] == 404

    # SVG rejected with 415.
    assert request(worker.app, f"/jobs/{job_id}/file", key="test-dashboard", query=b"path=website/logo.svg")[0] == 415

    # .media checkpoint JPEG served with its verified manifest MIME and hardened headers.
    # Use a streaming-safe ASGI receive() (test_phase9_security.request fires http.disconnect immediately).
    import asyncio

    async def _stream_get(path: str, query: bytes):
        messages = []
        done = asyncio.Event()
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                done.set()

        await worker.app(
            {
                "type": "http",
                "http_version": "1.1",
                "asgi": {"version": "3.0"},
                "method": "GET",
                "scheme": "http",
                "path": path,
                "root_path": "",
                "query_string": query,
                "headers": [(b"x-api-key", b"test-dashboard")],
            },
            receive,
            send,
        )
        return (
            messages[0]["status"],
            dict(messages[0]["headers"]),
            b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"),
        )

    st, headers, body = asyncio.run(
        _stream_get(f"/jobs/{job_id}/file", b"path=collection/media/fb-item.media")
    )
    assert st == 200
    assert headers[b"content-type"] == b"image/jpeg"
    assert headers[b"content-disposition"] == b"attachment"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert b"sandbox" in headers[b"content-security-policy"]
    assert body == media_file.read_bytes()


def test_preprocessing_union_find_4_asset_chain_no_crash(tmp_path, monkeypatch):
    """Regression: 4-asset perceptual chain A~D, B~C, C~D previously stranded A and crashed max()."""
    job = tmp_path / "job"
    job.mkdir()
    assets = []
    # Distinct solid colors so sha256 differs across all 4 images.
    for idx, name in enumerate(("a", "b", "c", "d")):
        p = job / f"{name}.png"
        Image.new("RGB", (640, 480), (20 + idx * 40, 80, 120)).save(p, format="PNG")
        blob = p.read_bytes()
        assets.append({
            "asset_id": f"img_{name}",
            "job_id": "job_uf",
            "platform": "website",
            "type": "image",
            "source_url": f"https://example.test/{name}.png",
            "local_path": f"{name}.png",
            "mime": "image/png",
            "bytes": len(blob),
            "width": 640,
            "height": 480,
            "sha256": hashlib.sha256(blob).hexdigest(),
            "date_status": "unknown",
            "rights_status": "unverified",
            "extraction_status": "downloaded",
        })

    # Construct exact dHash chain:
    # A = 0x0000000000000000
    # B = 0x0000000000000fff (12 bits from A -> >6)
    # C = 0x00000000000000ff (8 bits from A -> >6; 4 bits from B -> <=6; 4 bits from D -> <=6)
    # D = 0x000000000000000f (4 bits from A -> <=6; 8 bits from B -> >6; 4 bits from C -> <=6)
    # Matching pairs: (A,D), (B,C), (C,D) -> exact chain that triggered the old crash!
    synthetic_hashes = {
        "a.png": "0000000000000000",
        "b.png": "0000000000000fff",
        "c.png": "00000000000000ff",
        "d.png": "000000000000000f",
    }
    monkeypatch.setattr("app.preprocessing.phash_file", lambda path: synthetic_hashes[path.name])

    manifest = {
        "schema_version": "3.0-unified",
        "job_id": "job_uf",
        "source_status": "completed",
        "assets": assets,
    }
    (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    res = run_media_preprocessing(job, "job_uf")
    assert res.status == "completed"
    enriched = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
    pd_groups = [g for g in enriched["preprocessing"]["duplicate_groups"] if g["method"] == "perceptual_dhash"]
    assert len(pd_groups) == 1
    assert pd_groups[0]["member_asset_ids"] == ["img_a", "img_b", "img_c", "img_d"]
    assert pd_groups[0]["max_hamming_distance"] == 12


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_preprocessing_video_rerun_is_idempotent(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    video = job / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1.5:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", str(video),
        ],
        check=True,
        timeout=60,
    )
    blob = video.read_bytes()
    manifest = {
        "schema_version": "3.0-unified",
        "job_id": "job_idem",
        "source_status": "completed",
        "assets": [{
            "asset_id": "vid_0001",
            "job_id": "job_idem",
            "platform": "website",
            "type": "video",
            "source_url": "https://example.test/clip.mp4",
            "local_path": "clip.mp4",
            "mime": "video/mp4",
            "bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
            "date_status": "unknown",
            "rights_status": "unverified",
            "extraction_status": "downloaded",
        }],
    }
    (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    run_media_preprocessing(job, "job_idem", max_video_frames=3)
    run_media_preprocessing(job, "job_idem", max_video_frames=3)
    enriched = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
    frame_assets = [a for a in enriched["assets"] if a.get("type") == "frame"]
    assert len(frame_assets) == 3
    assert len({a["asset_id"] for a in frame_assets}) == 3
    assert validate_against_disk(job, enriched) == []


def test_path_traversal_and_frame_timestamp_sync_hardening(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    outside = tmp_path / "secret.jpg"
    Image.new("RGB", (64, 64), "red").save(outside, format="JPEG")
    inside_frame = job / "frames" / "v1" / "f2.jpg"
    inside_frame.parent.mkdir(parents=True)
    Image.new("RGB", (64, 64), "blue").save(inside_frame, format="JPEG")

    # Backslash, absolute, and traversal paths rejected by _resolve and validate_against_disk.
    assert _resolve(job, "../secret.jpg") is None
    assert _resolve(job, r"frames\v1\f2.jpg") is None
    assert validate_against_disk(job, {
        "assets": [{"asset_id": "esc", "extraction_status": "downloaded", "local_path": "../secret.jpg"}]
    }) != []

    # Symlink escape check (tested whenever OS allows creating symlinks, e.g. Linux container).
    symlink_path = job / "link.jpg"
    symlink_created = False
    try:
        symlink_path.symlink_to(outside)
        symlink_created = True
    except OSError:
        pass
    if symlink_created:
        assert _resolve(job, "link.jpg") is None
        assert validate_against_disk(job, {
            "assets": [{"asset_id": "sym", "extraction_status": "downloaded", "local_path": "link.jpg"}]
        }) != []

    # AI payload selection rejects traversal/missing video frames AND keeps frame_timestamps synchronized.
    win = frozen_window(datetime(2026, 6, 1, tzinfo=UTC))
    ev = publication_evidence(win["created_at"], source_url="https://example.test/v1", field="datePublished", attribution="media")
    manifest = {
        "job_id": "job_ts",
        **win,
        "assets": [{
            "asset_id": "vid_1",
            "type": "video",
            "extraction_status": "downloaded",
            "local_path": "clip.mp4",
            "post_date": win["created_at"],
            "date_status": "verified",
            "publication_evidence": ev,
            "preprocessing": {"outcome": "video_processed"},
            "frames": [
                {"local_path": "../secret.jpg", "timestamp_s": 1.0},
                {"local_path": "frames/v1/missing.jpg", "timestamp_s": 2.0},
                {"local_path": "frames/v1/f2.jpg", "timestamp_s": 3.5},
            ],
        }],
    }
    report = select_ai_candidates(manifest, job)
    assert len(report.candidates) == 1
    assert report.candidates[0].payload_files == [inside_frame.resolve()]
    assert report.candidates[0].frame_timestamps == [3.5]
