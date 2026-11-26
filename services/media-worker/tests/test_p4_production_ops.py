"""Milestone P4 production VPS deployment & operations hardening tests (fully offline)."""

from __future__ import annotations

from collections import namedtuple
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading

import pytest

_HERE = Path(__file__).resolve()
WORKER_DIR = _HERE.parents[1]
REPO_ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else None


def _load_script(name: str):
    script = WORKER_DIR / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _valid_prod_env(media_dir: Path) -> dict[str, str]:
    # Ephemeral runtime-generated values so no high-entropy literals live in source control.
    return {
        "STAGING_HOST": "salon.ateliermedia.io",
        "DASHBOARD_ORIGIN": "https://salon.ateliermedia.io",
        "ACME_EMAIL": "ops@ateliermedia.io",
        "CADDYFILE_PATH": "./Caddyfile.prod",
        "N8N_IMAGE": "n8nio/n8n:2.48.7",
        "CADDY_IMAGE": "caddy:2.8-alpine",
        "STAGING_MEDIA_DIR": str(media_dir),
        "COLLECTION_ENGINE": "historical",
        "PUBLIC_COLLECTION_CHECKPOINTS": "1",
        "M4_COLLECTION_APPROVAL_ENABLED": "1",
        "MEDIA_WORKER_TOKEN": secrets.token_hex(24),
        "MEDIA_WORKER_DASHBOARD_TOKEN": secrets.token_hex(24),
        "NAMED_ASSERTION_SECRET": secrets.token_hex(24),
        "DASHBOARD_PASSWORD": f"Studio!{secrets.token_hex(8)}#",
        "DASHBOARD_SESSION_SECRET": secrets.token_hex(24),
        "N8N_PIPELINE_TOKEN": secrets.token_hex(24),
        "N8N_ADMIN_WEBHOOK_TOKEN": secrets.token_hex(24),
        "N8N_ENCRYPTION_KEY": secrets.token_hex(24),
        "GOOGLE_DRIVE_READY": "1",
        "AI_TIER_PRESET": "dev",
        "AI_PROVIDER": "openai_compatible",
        "AI_MODEL": "qwen/qwen3.8-27b",
        "AI_ENDPOINT": "https://api.groq.com/openai/v1/chat/completions",
        "AI_ALLOWED_ENDPOINTS": "https://api.groq.com/openai/v1/chat/completions",
        "AI_API_KEY": secrets.token_hex(24),
        "AI_MAX_COST_USD_PER_JOB": "1.50",
    }


def _write_env_file(path: Path, values: dict[str, str], mode: int = 0o600) -> None:
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, mode)


def test_p4_prod_preflight_passes_valid_env_and_never_leaks_secrets(tmp_path, capsys):
    mod = _load_script("prod_preflight")
    media_dir = tmp_path / "prod-jobs"
    media_dir.mkdir()
    env_vals = _valid_prod_env(media_dir)
    env_file = tmp_path / ".env.prod"
    _write_env_file(env_file, env_vals, mode=0o600)

    exit_code = mod.main(["--env-file", str(env_file), "--check-host", "--max-disk-pct", "99"])
    raw_out = capsys.readouterr().out
    report = json.loads(raw_out)

    assert exit_code == 0, report
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["ai_tier_preset"] == "dev"
    assert report["host_checks"]["disk"]["used_pct"] < 99.0
    # Guarantee no secret value ever leaks into stdout/logs.
    for key in (*mod.REQUIRED_SECRET_KEYS, "AI_API_KEY"):
        assert env_vals[key] not in raw_out


def test_p4_prod_preflight_fails_closed_on_misconfigs_and_reused_secrets(tmp_path):
    mod = _load_script("prod_preflight")
    media_dir = tmp_path / "prod-jobs"
    media_dir.mkdir()

    # 1. Reused secret across MEDIA_WORKER_TOKEN and MEDIA_WORKER_DASHBOARD_TOKEN
    bad = _valid_prod_env(media_dir)
    bad["MEDIA_WORKER_DASHBOARD_TOKEN"] = bad["MEDIA_WORKER_TOKEN"]
    bad["DASHBOARD_ORIGIN"] = "http://salon.ateliermedia.io/"
    bad["CADDYFILE_PATH"] = "./Caddyfile"
    bad["M4_COLLECTION_APPROVAL_ENABLED"] = "0"
    bad["AI_ALLOWED_ENDPOINTS"] = "*"
    bad["N8N_IMAGE"] = "n8nio/n8n:latest"
    res = mod.validate_prod_env(bad)
    assert res["ok"] is False
    joined = "\n".join(res["errors"])
    assert "must not reuse" in joined
    assert "canonical https://" in joined
    assert "Caddyfile.prod" in joined
    assert "M4_COLLECTION_APPROVAL_ENABLED" in joined
    assert "allowlist" in joined
    assert "pinned version tag" in joined

    # 2. Loose 0644 permissions fail --check-host on POSIX
    if os.name == "posix":
        loose_file = tmp_path / ".env.loose"
        _write_env_file(loose_file, _valid_prod_env(media_dir), mode=0o644)
        res_loose = mod.validate_prod_env(mod.parse_env_file(loose_file), env_file_path=loose_file, check_host=True)
        assert res_loose["ok"] is False
        assert any("chmod 600" in e for e in res_loose["errors"])


def test_p4_compose_preset_passthrough_and_prod_override_contracts():
    if REPO_ROOT is None or not (REPO_ROOT / "deploy" / "compose.prod.yml").is_file():
        pytest.skip("repository root deploy/ manifests not present in container-only layout")

    # 1. Compose files must NOT hardcode numeric defaults that clobber AI_TIER_PRESET=dev.
    preset_vars = (
        "AI_MODE_A_MAX_OUTPUT_TOKENS",
        "AI_MODE_B_MAX_OUTPUT_TOKENS",
        "AI_BATCH_SIZE",
        "AI_MAX_IMAGES_PER_REQUEST",
        "AI_MIN_REQUEST_SPACING_S",
        "AI_MAX_REQUESTS_PER_JOB",
        "AI_MAX_COST_USD_PER_JOB",
    )
    for rel in ("compose.media.yml", "deploy/compose.staging.yml"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for var in preset_vars:
            assert f"${{{var}:-}}" in text, f"{rel} must leave {var} fallback empty so AI_TIER_PRESET works"
        assert "INSTALL_PLAYWRIGHT" in text

    # 2. deploy/compose.prod.yml enforces public 80/443 on Caddy only + fail-closed prod secrets.
    prod_compose = (REPO_ROOT / "deploy" / "compose.prod.yml").read_text(encoding="utf-8")
    for required in (
        "name: pgm-media-prod",
        '"80:80"',
        '"443:443"',
        "./Caddyfile.prod:/etc/caddy/Caddyfile:ro",
        "NAMED_ASSERTION_SECRET: ${NAMED_ASSERTION_SECRET:?",
        "N8N_ADMIN_WEBHOOK_TOKEN: ${N8N_ADMIN_WEBHOOK_TOKEN:?",
        "ACME_EMAIL: ${ACME_EMAIL:?",
        "AI_API_KEY: ${AI_API_KEY:?",
    ):
        assert required in prod_compose, f"missing {required} in deploy/compose.prod.yml"

    # 3. Template, systemd units, Dockerfile, and VPS bootstrap script exist & pass syntax checks.
    assert (REPO_ROOT / "deploy" / ".env.prod.example").is_file()
    for unit in ("pgm-backup.service", "pgm-backup.timer", "pgm-readiness.service", "pgm-readiness.timer"):
        assert (REPO_ROOT / "deploy" / "systemd" / unit).is_file()

    dockerfile = (WORKER_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "scripts/prod_preflight.py" in dockerfile

    bootstrap = REPO_ROOT / "deploy" / "vps-bootstrap.sh"
    assert bootstrap.is_file()
    assert "\r\n" not in bootstrap.read_text(encoding="utf-8"), "vps-bootstrap.sh must use LF line endings"
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(bootstrap)], check=True)


def test_p4_readiness_probe_disk_pressure_and_sanitized_alert_webhook(tmp_path, monkeypatch, capsys):
    mod = _load_script("readiness_probe")
    alerts_received: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path in ("/ready", "/healthz/readiness", "/api/ready"):
                self.send_response(200)
                body = b'{"ok":true,"status":"ok"}'
            else:
                self.send_response(503)
                body = b'{"ok":false,"status":"degraded"}'
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            alerts_received.append(json.loads(raw))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"accepted":true}')

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        hook = f"{base}/alert-hook"

        # 1. Healthy services + healthy disk -> exit 0, zero alerts fired.
        code_ok = mod.main([
            "--worker-url", f"{base}/ready",
            "--n8n-url", f"{base}/healthz/readiness",
            "--dashboard-url", f"{base}/api/ready",
            "--disk-path", str(tmp_path),
            "--max-disk-pct", "99",
            "--alert-webhook", hook,
            "--timeout", "2",
        ])
        out_ok = json.loads(capsys.readouterr().out)
        assert code_ok == 0
        assert out_ok["ok"] is True
        assert out_ok["disk"]["ok"] is True
        assert len(alerts_received) == 0

        # 2. Simulated 88% disk usage vs 80% max -> exit 1 + 1 sanitized alert fired!
        Usage = namedtuple("usage", "total used free")
        monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(100 * 1024**3, 88 * 1024**3, 12 * 1024**3))
        code_disk = mod.main([
            "--worker-url", f"{base}/ready",
            "--n8n-url", "",
            "--dashboard-url", "",
            "--disk-path", str(tmp_path),
            "--max-disk-pct", "80",
            "--alert-webhook", hook,
            "--timeout", "2",
        ])
        out_disk = json.loads(capsys.readouterr().out)
        assert code_disk == 1
        assert out_disk["overall"] == "degraded"
        assert out_disk["disk"]["error"] == "disk_threshold_exceeded"
        assert out_disk["alert"]["sent"] is True
        assert len(alerts_received) == 1
        assert alerts_received[0]["disk"]["used_pct"] == 88.0

        # 3. Non-HTTPS remote alert webhook is rejected safely.
        bad_hook = mod.send_alert_webhook("http://insecure.example.test/hook", out_disk)
        assert bad_hook["sent"] is False
        assert bad_hook["error"] == "invalid_alert_webhook_url"
    finally:
        server.shutdown()
        server.server_close()
