"""Production environment & host security pre-flight validator (fail-closed).

Audits `.env.prod` (and optional host storage/disk state) BEFORE deploying or
restarting public HTTPS production containers. Never prints raw secret values.

Usage:
    python scripts/prod_preflight.py --env-file .env.prod
    python scripts/prod_preflight.py --env-file .env.prod --check-host --max-disk-pct 80
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from typing import Any
from urllib.parse import urlsplit

REQUIRED_SECRET_KEYS: tuple[str, ...] = (
    "MEDIA_WORKER_TOKEN",
    "MEDIA_WORKER_DASHBOARD_TOKEN",
    "NAMED_ASSERTION_SECRET",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_SESSION_SECRET",
    "N8N_PIPELINE_TOKEN",
    "N8N_ADMIN_WEBHOOK_TOKEN",
    "N8N_ENCRYPTION_KEY",
)

MIN_SECRET_LENGTH = 32
MIN_PASSWORD_LENGTH = 16
_PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "placeholder",
    "replace-with",
    "replace_with",
    "your-",
    "your_",
    "example",
    "test-key",
    "local-dev",
    "localhost",
    "dummy",
    "todo",
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_ALLOWED_TIER_PRESETS = {"", "on_demand", "dev"}


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv file without executing shell syntax."""
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            parsed[key] = value
    return parsed


def _looks_placeholder(value: str) -> bool:
    low = value.strip().lower()
    if not low:
        return True
    if any(marker in low for marker in _PLACEHOLDER_MARKERS):
        return True
    # Reject trivial repeated-character strings like "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    if len(set(low)) < 6:
        return True
    return False


def validate_prod_env(
    env: dict[str, str],
    *,
    env_file_path: Path | None = None,
    check_host: bool = False,
    max_disk_pct: float = 80.0,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    def get(key: str) -> str:
        return str(env.get(key) or "").strip()

    # 1. Domain, HTTPS origin, ACME email, and production Caddyfile
    host = get("STAGING_HOST").lower()
    origin = get("DASHBOARD_ORIGIN")
    email = get("ACME_EMAIL")
    caddyfile = get("CADDYFILE_PATH")

    if not host or host in _LOOPBACK_HOSTS or not _HOST_RE.match(host) or host.endswith(".yourdomain.com") or host.endswith(".example.com"):
        errors.append("STAGING_HOST must be a real public DNS hostname (no scheme, port, loopback, or example domain).")

    parsed_origin = urlsplit(origin)
    if (
        parsed_origin.scheme != "https"
        or not parsed_origin.hostname
        or parsed_origin.port is not None
        or parsed_origin.path not in ("", "/")
        or origin.endswith("/")
    ):
        errors.append("DASHBOARD_ORIGIN must be a canonical https://<hostname> URL with no trailing slash or port.")
    elif host and parsed_origin.hostname.lower() != host:
        errors.append("DASHBOARD_ORIGIN hostname must match STAGING_HOST exactly.")

    if not _EMAIL_RE.match(email) or email.lower().endswith(("@yourdomain.com", "@example.com", "@example.test")):
        errors.append("ACME_EMAIL must be a real operator email address for Let's Encrypt TLS notices.")

    if not caddyfile.replace("\\", "/").endswith("Caddyfile.prod"):
        errors.append("CADDYFILE_PATH must point to ./Caddyfile.prod for production HTTPS.")

    # 2. Pinned images & storage path
    for img_key in ("N8N_IMAGE", "CADDY_IMAGE"):
        img = get(img_key)
        if not img or ":" not in img or img.endswith(":latest"):
            errors.append(f"{img_key} must specify a pinned version tag (never empty or :latest).")

    media_dir = get("STAGING_MEDIA_DIR")
    if not media_dir or not media_dir.startswith("/"):
        errors.append("STAGING_MEDIA_DIR must be an absolute isolated Linux host path (e.g. /srv/pgm-media-prod/jobs).")

    # 3. Core engine & M4 mandatory separation invariants
    if get("COLLECTION_ENGINE") != "historical":
        errors.append("COLLECTION_ENGINE must be set to 'historical'.")
    if get("PUBLIC_COLLECTION_CHECKPOINTS") != "1":
        errors.append("PUBLIC_COLLECTION_CHECKPOINTS must be '1'.")
    if get("M4_COLLECTION_APPROVAL_ENABLED") != "1":
        errors.append("M4_COLLECTION_APPROVAL_ENABLED must be '1' (two-admin separation is mandatory in production).")

    # 4. Secret strength & pairwise uniqueness (never emit secret values)
    seen_secrets: dict[str, str] = {}
    for key in REQUIRED_SECRET_KEYS:
        val = get(key)
        min_len = MIN_PASSWORD_LENGTH if key == "DASHBOARD_PASSWORD" else MIN_SECRET_LENGTH
        if len(val) < min_len:
            errors.append(f"{key} must be at least {min_len} characters long.")
            continue
        if _looks_placeholder(val):
            errors.append(f"{key} appears to be a placeholder or low-entropy string; generate via `openssl rand -hex 32`.")
            continue
        if val in seen_secrets:
            errors.append(f"{key} must not reuse the same secret value as {seen_secrets[val]}.")
        else:
            seen_secrets[val] = key

    # 5. Groq Vision AI configuration & budget guardrails
    ai_key = get("AI_API_KEY")
    if len(ai_key) < 20 or _looks_placeholder(ai_key):
        errors.append("AI_API_KEY must be a valid production Groq API key.")

    ai_provider = get("AI_PROVIDER") or "openai_compatible"
    if ai_provider != "openai_compatible":
        errors.append("AI_PROVIDER must be 'openai_compatible' for production Groq Vision execution.")

    ai_endpoint = get("AI_ENDPOINT")
    allowed_raw = get("AI_ALLOWED_ENDPOINTS")
    allowed_endpoints = [u.strip() for u in allowed_raw.split(",") if u.strip()]
    if not ai_endpoint.startswith("https://"):
        errors.append("AI_ENDPOINT must be an https:// URL.")
    if not allowed_endpoints or any(u == "*" or not u.startswith("https://") for u in allowed_endpoints):
        errors.append("AI_ALLOWED_ENDPOINTS must be an explicit comma-separated https:// allowlist (no wildcards).")
    elif ai_endpoint and ai_endpoint not in allowed_endpoints:
        errors.append("AI_ENDPOINT must be explicitly listed inside AI_ALLOWED_ENDPOINTS.")

    tier_preset = get("AI_TIER_PRESET").lower()
    if tier_preset not in _ALLOWED_TIER_PRESETS:
        errors.append("AI_TIER_PRESET must be empty, 'on_demand', or 'dev'.")
    elif tier_preset in ("", "on_demand"):
        warnings.append("AI_TIER_PRESET is 'on_demand' (bounded pilot throughput); switch to 'dev' once Groq Developer Tier is enabled.")

    max_cost_raw = get("AI_MAX_COST_USD_PER_JOB")
    if max_cost_raw:
        try:
            cost = float(max_cost_raw)
            if cost <= 0 or cost > 25.0:
                errors.append("AI_MAX_COST_USD_PER_JOB must be between 0 and 25.0 USD.")
        except ValueError:
            errors.append("AI_MAX_COST_USD_PER_JOB must be a valid decimal number.")

    if get("GOOGLE_DRIVE_READY") != "1":
        warnings.append("GOOGLE_DRIVE_READY is not '1'; verify production Google Drive OAuth in n8n before enabling live client delivery.")

    if get("PLAYWRIGHT_FALLBACK_ENABLED") == "1" and get("INSTALL_PLAYWRIGHT") != "1":
        warnings.append("PLAYWRIGHT_FALLBACK_ENABLED=1 is set without INSTALL_PLAYWRIGHT=1; Chromium will not be installed in the collector image.")

    # 6. Optional host checks (permissions, writable media dir, disk capacity)
    host_info: dict[str, Any] | None = None
    if check_host:
        host_info = {}
        if env_file_path is not None and os.name == "posix":
            mode = stat.S_IMODE(env_file_path.stat().st_mode)
            host_info["env_file_mode"] = oct(mode)
            if mode & 0o077:
                errors.append(f"env file permissions are too open ({oct(mode)}); run `chmod 600` on the env file.")
        if media_dir:
            mdir = Path(media_dir)
            if not mdir.is_dir():
                errors.append(f"STAGING_MEDIA_DIR does not exist on host: {media_dir}")
            elif not os.access(mdir, os.W_OK):
                errors.append(f"STAGING_MEDIA_DIR is not writable: {media_dir}")
            else:
                usage = shutil.disk_usage(mdir)
                used_pct = round((usage.used / usage.total) * 100.0, 1) if usage.total else 100.0
                free_gb = round(usage.free / (1024**3), 2)
                host_info["disk"] = {
                    "path": str(mdir),
                    "used_pct": used_pct,
                    "free_gb": free_gb,
                    "max_disk_pct": max_disk_pct,
                }
                if used_pct >= max_disk_pct:
                    errors.append(f"Disk usage on {media_dir} is {used_pct}% (threshold {max_disk_pct}%).")

    return {
        "ok": len(errors) == 0,
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "host": host or None,
        "ai_tier_preset": tier_preset or "on_demand",
        "secrets_checked": len(REQUIRED_SECRET_KEYS),
        "errors": errors,
        "warnings": warnings,
        **({"host_checks": host_info} if host_info is not None else {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate production .env.prod before VPS deployment.")
    parser.add_argument("--env-file", type=Path, required=True, help="Path to host-only .env.prod file.")
    parser.add_argument("--check-host", action="store_true", help="Also verify 0600 file perms, writable storage dir, and disk capacity.")
    parser.add_argument("--max-disk-pct", type=float, default=80.0, help="Fail --check-host if disk usage >= this percentage.")
    args = parser.parse_args(argv)

    if not args.env_file.is_file():
        print(json.dumps({"ok": False, "errors": [f"Env file not found: {args.env_file}"]}, indent=2))
        return 1

    env = parse_env_file(args.env_file)
    report = validate_prod_env(
        env,
        env_file_path=args.env_file,
        check_host=args.check_host,
        max_disk_pct=max(10.0, min(99.0, float(args.max_disk_pct))),
    )
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
