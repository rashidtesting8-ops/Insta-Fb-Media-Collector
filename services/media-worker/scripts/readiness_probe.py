"""Cross-platform readiness probe for Media Worker, n8n, and Dashboard.

Checks /ready, /healthz/readiness, and /api/ready with bounded timeouts and
returns exit code 0 when all enabled targets report healthy, or 1 when any
target is degraded or unreachable. Intended for cron, Windows Task Scheduler,
or container health/uptime monitors.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def _probe_one(name: str, url: str, timeout: float) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"service": name, "url": url, "ok": False, "error": "invalid_url"}
    started = time.monotonic()
    req = Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "pgm-readiness-probe/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme validated above
            status = int(resp.status)
            body = resp.read(4096).decode("utf-8", errors="replace").strip()
            latency_ms = round((time.monotonic() - started) * 1000.0, 1)
            ok = 200 <= status < 300
            detail: Any = body[:200]
            try:
                parsed_json = json.loads(body)
                if isinstance(parsed_json, dict):
                    if parsed_json.get("status") in ("error", "degraded", "not_ready", "unhealthy"):
                        ok = False
                    if parsed_json.get("ready") is False or parsed_json.get("ok") is False:
                        ok = False
                    detail = {k: parsed_json[k] for k in ("status", "ready", "ok", "collection_engine") if k in parsed_json} or "ok"
            except json.JSONDecodeError:
                pass
            return {
                "service": name,
                "url": url,
                "ok": ok,
                "http_status": status,
                "latency_ms": latency_ms,
                "detail": detail,
            }
    except HTTPError as exc:
        return {
            "service": name,
            "url": url,
            "ok": False,
            "http_status": int(exc.code),
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            "error": f"http_{exc.code}",
        }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "service": name,
            "url": url,
            "ok": False,
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            "error": str(getattr(exc, "reason", exc))[:160],
        }


def run_probes(targets: list[tuple[str, str]], *, timeout: float = 5.0) -> dict[str, Any]:
    results = [_probe_one(name, url, timeout) for name, url in targets if url.strip()]
    all_ok = bool(results) and all(item["ok"] for item in results)
    return {
        "ok": all_ok,
        "overall": "ok" if all_ok else "degraded",
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "checks": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check readiness endpoints for worker, n8n, and dashboard.")
    parser.add_argument("--worker-url", default="http://127.0.0.1:8788/ready", help="Worker /ready URL (empty to skip).")
    parser.add_argument("--n8n-url", default="http://127.0.0.1:5679/healthz/readiness", help="n8n readiness URL (empty to skip).")
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:3000/api/ready", help="Dashboard /api/ready URL (empty to skip).")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-endpoint timeout in seconds.")
    args = parser.parse_args(argv)

    report = run_probes(
        [
            ("worker", args.worker_url),
            ("n8n", args.n8n_url),
            ("dashboard", args.dashboard_url),
        ],
        timeout=max(0.5, float(args.timeout)),
    )
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
