"""Cross-platform readiness, disk-pressure, and alert probe for Media Worker, n8n, and Dashboard.

Checks /ready, /healthz/readiness, /api/ready, and optional storage disk pressure
with bounded timeouts. Returns exit code 0 when all enabled targets report healthy,
or 1 when any target or disk threshold is degraded/unreachable. Optionally posts a
sanitized failure notification (zero secrets/PII) to an HTTPS alert webhook.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
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
    req = Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "pgm-readiness-probe/1.1"})
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


def check_disk_pressure(disk_path: str | Path, *, max_disk_pct: float = 80.0) -> dict[str, Any]:
    target = Path(disk_path)
    if not target.exists():
        return {"path": str(target), "ok": False, "error": "path_not_found", "max_disk_pct": max_disk_pct}
    usage = shutil.disk_usage(target)
    used_pct = round((usage.used / usage.total) * 100.0, 1) if usage.total else 100.0
    free_gb = round(usage.free / (1024**3), 2)
    total_gb = round(usage.total / (1024**3), 2)
    ok = used_pct < max_disk_pct
    return {
        "path": str(target),
        "ok": ok,
        "used_pct": used_pct,
        "free_gb": free_gb,
        "total_gb": total_gb,
        "max_disk_pct": max_disk_pct,
        **({} if ok else {"error": "disk_threshold_exceeded"}),
    }


def _valid_alert_webhook(url: str) -> bool:
    parsed = urlsplit(url.strip())
    if parsed.username or parsed.password or not parsed.hostname:
        return False
    if parsed.scheme == "https":
        return True
    # Permit loopback http strictly for local automated unit tests / host-local alert relays.
    return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")


def send_alert_webhook(webhook_url: str, report: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    if not _valid_alert_webhook(webhook_url):
        return {"sent": False, "error": "invalid_alert_webhook_url"}
    payload = json.dumps({
        "service": "atelier-media-readiness",
        "overall": report.get("overall"),
        "checked_at_utc": report.get("checked_at_utc"),
        "failed_checks": [c["service"] for c in report.get("checks", []) if not c.get("ok")],
        "disk": report.get("disk"),
        "text": (
            f"[ALERT] Atelier Media readiness {report.get('overall')} at {report.get('checked_at_utc')}: "
            f"failed={[c['service'] for c in report.get('checks', []) if not c.get('ok')]}"
            + (f", disk_used_pct={report['disk'].get('used_pct')}%" if isinstance(report.get("disk"), dict) else "")
        ),
    }).encode("utf-8")
    req = Request(
        webhook_url.strip(),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "pgm-readiness-probe/1.1"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme validated above
            return {"sent": 200 <= int(resp.status) < 300, "http_status": int(resp.status)}
    except HTTPError as exc:
        return {"sent": False, "http_status": int(exc.code), "error": f"http_{exc.code}"}
    except (URLError, TimeoutError, OSError) as exc:
        return {"sent": False, "error": str(getattr(exc, "reason", exc))[:160]}


def run_probes(
    targets: list[tuple[str, str]],
    *,
    timeout: float = 5.0,
    disk_path: str | Path | None = None,
    max_disk_pct: float = 80.0,
    alert_webhook: str = "",
) -> dict[str, Any]:
    results = [_probe_one(name, url, timeout) for name, url in targets if url.strip()]
    disk_report = check_disk_pressure(disk_path, max_disk_pct=max_disk_pct) if disk_path and str(disk_path).strip() else None
    checks_ok = bool(results) and all(item["ok"] for item in results)
    disk_ok = True if disk_report is None else bool(disk_report.get("ok"))
    all_ok = checks_ok and disk_ok
    report: dict[str, Any] = {
        "ok": all_ok,
        "overall": "ok" if all_ok else "degraded",
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "checks": results,
    }
    if disk_report is not None:
        report["disk"] = disk_report
    if not all_ok and alert_webhook.strip():
        report["alert"] = send_alert_webhook(alert_webhook, report, timeout=timeout)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check readiness endpoints and disk pressure for worker, n8n, and dashboard.")
    parser.add_argument("--worker-url", default="http://127.0.0.1:8788/ready", help="Worker /ready URL (empty to skip).")
    parser.add_argument("--n8n-url", default="http://127.0.0.1:5679/healthz/readiness", help="n8n readiness URL (empty to skip).")
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:3000/api/ready", help="Dashboard /api/ready URL (empty to skip).")
    parser.add_argument("--disk-path", default="", help="Optional storage directory to monitor for disk pressure.")
    parser.add_argument("--max-disk-pct", type=float, default=80.0, help="Degrade readiness when disk usage >= this percent.")
    parser.add_argument("--alert-webhook", default="", help="Optional HTTPS webhook to notify ONLY when readiness is degraded.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-endpoint timeout in seconds.")
    args = parser.parse_args(argv)

    report = run_probes(
        [
            ("worker", args.worker_url),
            ("n8n", args.n8n_url),
            ("dashboard", args.dashboard_url),
        ],
        timeout=max(0.5, float(args.timeout)),
        disk_path=args.disk_path or None,
        max_disk_pct=max(10.0, min(99.0, float(args.max_disk_pct))),
        alert_webhook=args.alert_webhook,
    )
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
