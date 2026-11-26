"""Sprint 5 DevOps / container / CI hygiene regression tests (fully offline)."""

from __future__ import annotations

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
WORKER_DIR = _HERE.parents[1]
REPO_ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else None


def test_compose_media_parameterized_and_hardened():
    if REPO_ROOT is None or not (REPO_ROOT / "compose.media.yml").is_file():
        pytest.skip("repository root compose.media.yml not present in container-only layout")
    compose_path = REPO_ROOT / "compose.media.yml"
    text = compose_path.read_text(encoding="utf-8")
    assert "D:/" not in text, "compose.media.yml must not hardcode host drive paths"
    assert "${MEDIA_STORAGE_DIR:-./storage/jobs}" in text
    assert "no-new-privileges:true" in text
    assert "N8N_BLOCK_ENV_ACCESS_IN_NODE=true" in text
    assert "COLLECTION_ENGINE=${COLLECTION_ENGINE:-historical}" in text
    assert "M4_COLLECTION_APPROVAL_ENABLED=${M4_COLLECTION_APPROVAL_ENABLED:-1}" in text
    assert "NAMED_ASSERTION_SECRET=" in text
    assert "AI_MAX_IMAGES_PER_REQUEST=" in text
    assert "AI_VISUAL_DIRECTOR_V2.md:/srv/docs/AI_VISUAL_DIRECTOR_V2.md:ro" in text
    assert 'profiles: ["frontend"]' in text


def test_worker_dockerfile_and_pyproject_reproducible():
    dockerfile_path = WORKER_DIR / "Dockerfile"
    if dockerfile_path.is_file():
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
        assert "libsodium23" in dockerfile
        assert "ffmpeg" in dockerfile
        assert "USER appuser" in dockerfile
        assert "/ready" in dockerfile
        assert "pyproject.toml" in dockerfile
        assert "scripts/sqlite_backup.py" in dockerfile
        assert "scripts/readiness_probe.py" in dockerfile

    pyproject = (WORKER_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert 'cache_dir = "/tmp/pytest-cache"' in pyproject


def test_ci_workflow_covers_all_layers_and_secret_scan():
    if REPO_ROOT is None or not (REPO_ROOT / ".github" / "workflows" / "tests.yml").is_file():
        pytest.skip("repository root CI workflow not present in container-only layout")
    ci_path = REPO_ROOT / ".github" / "workflows" / "tests.yml"
    text = ci_path.read_text(encoding="utf-8")
    assert "n8n-workflows:" in text
    assert "sprint4-workflows.test.mjs" in text
    assert "libsodium23" in text
    assert "compose-validate:" in text
    assert "secret-scan:" in text
    assert "gitleaks" in text


def test_staging_compose_and_prod_caddyfile_hardened():
    if REPO_ROOT is None or not (REPO_ROOT / "deploy" / "compose.staging.yml").is_file():
        pytest.skip("repository root deploy/ manifests not present in container-only layout")
    staging = (REPO_ROOT / "deploy" / "compose.staging.yml").read_text(encoding="utf-8")
    for required in (
        "AI_ALLOWED_ENDPOINTS:",
        "AI_MODE_A_MAX_OUTPUT_TOKENS:",
        "AI_MODE_B_MAX_OUTPUT_TOKENS:",
        "AI_MAX_IMAGES_PER_REQUEST:",
        "AI_MIN_REQUEST_SPACING_S:",
        "AI_MAX_COST_USD_PER_JOB:",
        "AI_PRICE_IN_PER_1K:",
        "AI_PRICE_OUT_PER_1K:",
        "CADDYFILE_PATH:-./Caddyfile",
        "condition: service_healthy",
    ):
        assert required in staging, f"missing {required} in deploy/compose.staging.yml"

    caddy_prod = (REPO_ROOT / "deploy" / "Caddyfile.prod").read_text(encoding="utf-8")
    assert "tls internal" not in caddy_prod
    assert "Strict-Transport-Security" in caddy_prod
    assert "X-Content-Type-Options" in caddy_prod
    assert "X-Frame-Options" in caddy_prod
    assert "/setup-admin" in caddy_prod
    assert "/api/auth/setup-admin" in caddy_prod
    assert "respond @bootstrap 404" in caddy_prod
    assert "@media_upload path /api/jobs/*/media-upload" in caddy_prod
    assert "max_size 105MB" in caddy_prod


def test_sqlite_backup_captures_wal_verifies_sha256_and_prunes(tmp_path):
    import importlib.util
    import sqlite3
    import time

    script = WORKER_DIR / "scripts" / "sqlite_backup.py"
    spec = importlib.util.spec_from_file_location("sqlite_backup", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db_path = tmp_path / "jobs.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('job_wal_1', 'completed')")
    conn.commit()
    # Keep connection open so committed row remains in -wal without checkpointing.
    try:
        out_dir = tmp_path / "backups"
        first = mod.create_backup(db_path, out_dir, keep=2)
        assert first["ok"] is True
        assert first["integrity_check"] == "ok"
        assert first["table_counts"] == {"jobs": 1}
        first_file = Path(first["backup_file"])
        assert first_file.is_file()
        assert Path(first["sha256_file"]).is_file()

        verified = mod.verify_backup(first_file)
        assert verified["ok"] is True
        assert verified["sha256_verified"] is True
        assert verified["table_counts"] == {"jobs": 1}

        # Create 2 more backups to confirm keep=2 prunes the oldest backup + sidecar.
        time.sleep(0.02)
        conn.execute("INSERT INTO jobs VALUES ('job_wal_2', 'completed')")
        conn.commit()
        second = mod.create_backup(db_path, out_dir, keep=2)
        time.sleep(0.02)
        third = mod.create_backup(db_path, out_dir, keep=2)
        assert third["table_counts"] == {"jobs": 2}
        assert len(list(out_dir.glob("jobs.sqlite.*.bak"))) == 2
        assert len(list(out_dir.glob("jobs.sqlite.*.bak.sha256"))) == 2
        assert not first_file.exists()

        # Tamper with a backup byte and verify SHA-256 check fails closed.
        second_file = Path(second["backup_file"])
        raw = bytearray(second_file.read_bytes())
        raw[-1] ^= 0xFF
        second_file.write_bytes(bytes(raw))
        tampered = mod.verify_backup(second_file)
        assert tampered["ok"] is False
        assert tampered["error"] == "sha256 mismatch"
    finally:
        conn.close()


def test_readiness_probe_reports_ok_and_degraded_on_loopback():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import importlib.util
    import json
    import threading

    script = WORKER_DIR / "scripts" / "readiness_probe.py"
    spec = importlib.util.spec_from_file_location("readiness_probe", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/ready":
                body = json.dumps({"status": "ok", "ready": True}).encode()
                self.send_response(200)
            elif self.path == "/healthz/readiness":
                body = b'{"status":"ok"}'
                self.send_response(200)
            elif self.path == "/api/ready":
                body = b'{"ok":true}'
                self.send_response(200)
            else:
                body = b'{"ok":false,"status":"degraded"}'
                self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        ok_code = mod.main([
            "--worker-url", f"{base}/ready",
            "--n8n-url", f"{base}/healthz/readiness",
            "--dashboard-url", f"{base}/api/ready",
            "--timeout", "2",
        ])
        assert ok_code == 0

        fail_code = mod.main([
            "--worker-url", f"{base}/ready",
            "--n8n-url", f"{base}/degraded",
            "--dashboard-url", "",
            "--timeout", "2",
        ])
        assert fail_code == 1
    finally:
        server.shutdown()
        server.server_close()
