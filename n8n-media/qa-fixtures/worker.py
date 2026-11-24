"""Disposable worker fixture. Never mount a real jobs directory into this process."""

import base64
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
import hmac
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
from secrets import compare_digest
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from urllib.parse import urlsplit
import uuid

from fastapi import FastAPI, Header, HTTPException
from PIL import Image, ImageDraw
import requests
import uvicorn

_SENSITIVE_KEY_RE = re.compile(
    r"password|secret|token|cookie|authorization|assertion|api[_-]?key|session|credential",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(__Host-)?pgm_(named|session)=|\bBearer\s+\S+|\bBasic\s+\S+|"
    r"x-api-key|x-named-assertion|x-named-session|x-pipeline-key|"
    r"gsk_[A-Za-z0-9]{8,}|ya29\.[A-Za-z0-9._-]{8,}|sk-[A-Za-z0-9]{8,}",
    re.IGNORECASE,
)
_SENSITIVE_ENV_KEYS = (
    "PGM_QA_NAMED_ASSERTION_SECRET", "PGM_QA_NAMED_PASSWORD_A", "PGM_QA_NAMED_PASSWORD_B",
    "PGM_QA_WORKER_TOKEN", "PGM_QA_DASHBOARD_TOKEN", "PGM_QA_ADAPTER_TOKEN", "PGM_QA_DRIVE_TOKEN",
    "NAMED_ASSERTION_SECRET", "MEDIA_WORKER_TOKEN", "MEDIA_WORKER_DASHBOARD_TOKEN", "AI_API_KEY",
)

SCENARIOS = ("success", "partial", "no-eligible", "upload-failure", "oauth-expiry", "ambiguous", "oauth-rejected")
ORIGIN = "http://127.0.0.1:8790"
FIXTURE_SHA = "e5c5dd3e2d67e9e532e9de2fb5fa9a8f33a444df5ac285cc9fea2bc2bae38162"


def _runtime_secrets(extra_secrets=()):
    values = [os.environ[k] for k in _SENSITIVE_ENV_KEYS if len(os.environ.get(k, "")) >= 8]
    values.extend(s for s in extra_secrets if isinstance(s, str) and len(s) >= 8)
    return values


def sanitize_diag(value, *, key="", extra_secrets=()):
    if key and _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    secrets_list = _runtime_secrets(extra_secrets)
    if isinstance(value, str):
        if _SENSITIVE_VALUE_RE.search(value) or any(secret in value for secret in secrets_list):
            return "[REDACTED]"
        return value
    if isinstance(value, list):
        return [sanitize_diag(item, extra_secrets=extra_secrets) for item in value]
    if isinstance(value, tuple):
        return [sanitize_diag(item, extra_secrets=extra_secrets) for item in value]
    if isinstance(value, dict):
        return {str(k): sanitize_diag(v, key=str(k), extra_secrets=extra_secrets) for k, v in value.items()}
    return value


def _diag(event, data=None, *, root=None, run_id=None, strict=False, extra_secrets=()):
    diag_root = Path(root or os.environ.get("PGM_QA_DIAGNOSTICS_ROOT") or "/qa-diagnostics")
    diag_run = run_id if run_id is not None else os.environ.get("PGM_QA_RUN_ID", "")
    target_dir = diag_root / diag_run if diag_run else diag_root
    path = target_dir / os.environ.get("PGM_QA_DIAGNOSTICS_FILE", "worker.jsonl")
    payload = sanitize_diag({"t": time.time(), "component": "worker", "event": event, **(data or {})},
                            extra_secrets=extra_secrets)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, default=str) + "\n")
    except OSError as exc:
        print(f"QA diagnostics write failed ({path}): {exc}", file=sys.stderr, flush=True)
        if strict:
            raise RuntimeError(f"QA diagnostics write failed ({path}): {exc}") from exc


def signed_named_dispatch(db, action, body, *, assertion_secret, session=""):
    from app import named_auth

    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    claims = {
        "iss": "pgm-frontend",
        "aud": "pgm-worker",
        "action": action,
        "sub": named_auth.digest(session),
        "body": sha256(raw).hexdigest(),
        "exp": int(time.time()) + 25,
        "jti": uuid.uuid4().hex,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode("utf-8")).rstrip(b"=")
    mac = hmac.digest(assertion_secret.encode("utf-8"), encoded, "sha256").hex()
    header = encoded.decode("ascii") + "." + mac
    return named_auth.dispatch(db, action, raw, header, session)


def provision_qa_admins(db, *, assertion_secret, password_a, password_b):
    """Bootstrap Admin A once, then create distinct Admin B via the authorized create action."""
    from app import named_auth

    if len(assertion_secret) < 32:
        raise ValueError("QA NAMED_ASSERTION_SECRET must be at least 32 characters")
    principal_a = named_auth.bootstrap(db, "qa-admin-a", password_a, verified_operator="qa-fixture")
    login = signed_named_dispatch(
        db, "login", {"name": "qa-admin-a", "password": password_a}, assertion_secret=assertion_secret,
    )
    session_a = login["session"]
    try:
        created = signed_named_dispatch(
            db, "create", {"name": "qa-admin-b", "password": password_b},
            assertion_secret=assertion_secret, session=session_a,
        )
        principal_b = created["principal_id"]
    finally:
        signed_named_dispatch(db, "logout", {}, assertion_secret=assertion_secret, session=session_a)
    if not principal_b or principal_b == principal_a:
        raise RuntimeError("QA admins must have distinct principal identities")
    return principal_a, principal_b


def enable_disposable_m4_qa(service_mod, disposable_root):
    """Enable offline M4 QA mode and run additive schema migration ONLY in disposable storage."""
    from app import collection_approval

    root_path = Path(disposable_root).resolve()
    db_path = Path(service_mod.DB).resolve()
    data_path = Path(service_mod.DATA_DIR).resolve()
    if not db_path.is_relative_to(root_path) or not data_path.is_relative_to(root_path):
        raise RuntimeError("Refusing to initialize QA approval ledger outside disposable storage")
    with service_mod.connect() as con:
        collection_approval.migrate(con)
    service_mod.app.state.offline_approval_qa = True
    service_mod.app.state.offline_processing_consent = {}


def apply_fixture_rights(job_dir, *, guarded=False):
    """Mark explicitly rights-verified synthetic fixture assets BEFORE snapshot acceptance."""
    from app.manifest import write_manifest

    manifest_path = Path(job_dir) / "manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verified_ids = []
    for asset in manifest.get("assets", []):
        source = urlsplit(asset.get("source_url") or "")
        is_approved = (
            source.netloc == "127.0.0.1:8790"
            and (
                source.path.endswith("/approved.jpg")
                or (guarded and source.path == "/guarded/synthetic-pup-bath.jpg" and asset.get("sha256") == FIXTURE_SHA)
            )
        )
        if is_approved:
            asset["rights_status"] = "verified"
            asset["rights_evidence"] = "mock-only synthetic fixture approval; not valid for real delivery"
            verified_ids.append(asset.get("asset_id"))
    if verified_ids:
        write_manifest(job_dir, manifest)
    return verified_ids


def record_fixture_processing_consent(service_mod, job_dir, job_id):
    """Record explicit per-asset mock external-processing consent without mutating manifest.json."""
    manifest_path = Path(job_dir) / "manifest.json"
    if not manifest_path.is_file():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    consent = {}
    for asset in manifest.get("assets", []):
        source_url = asset.get("source_url") or ""
        sha = asset.get("sha256") or ""
        asset_id = asset.get("asset_id") or ""
        if (asset.get("extraction_status") == "downloaded"
                and urlsplit(source_url).netloc == "127.0.0.1:8790"
                and asset_id and sha):
            consent[asset_id] = (source_url, sha)
    store_map = getattr(service_mod.app.state, "offline_processing_consent", None)
    if store_map is None:
        store_map = {}
        service_mod.app.state.offline_processing_consent = store_map
    store_map[job_id] = consent
    return consent


class _QaDiagnosticMiddleware:
    """Observe sanitized M4/QA HTTP transitions without altering production routes."""

    def __init__(self, app, *, strict=False):
        self.app = app
        self.strict = strict

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        watched = (
            path.startswith("/qa/collection/")
            or path.startswith("/named/collection-")
            or path.endswith("/delivery-plan")
            or path.endswith("/delivery-result")
        )
        if not watched:
            await self.app(scope, receive, send)
            return

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                parts = [p for p in path.split("/") if p]
                job_id = parts[2] if len(parts) >= 3 and parts[0] == "qa" else (
                    parts[1] if len(parts) >= 2 and parts[0] == "jobs" else None
                )
                _diag("worker_http_transition", {
                    "method": method, "path": path, "job_id": job_id, "status": message["status"],
                }, strict=self.strict)
            await send(message)

        await self.app(scope, receive, wrapped_send)


def fixture_page(names, published):
    media = [{"@type": "ImageObject", "contentUrl": name, "datePublished": published} for name in names]
    images = "".join(f'<img src="{name}" alt="Synthetic pet grooming study">' for name in names)
    return f'<html><title>Offline Fixture Salon</title><script type="application/ld+json">{json.dumps(media)}</script><body>{images}</body></html>'


def make_site(root, published):
    (root / "robots.txt").write_text("User-agent: *\nAllow: /\n")
    for scenario in SCENARIOS:
        folder = root / scenario
        folder.mkdir()
        names = ["review.jpg"] if scenario == "no-eligible" else ["approved.jpg"]
        if scenario == "partial":
            names.append("review.jpg")
        for name in names:
            approved = name == "approved.jpg"
            image = Image.new("RGB", (320, 256), "#ebc168" if approved else "#135e86")
            draw = ImageDraw.Draw(image)
            for index in range(12):
                x = 18 + index * 24
                if approved:
                    draw.rectangle((x, 12 + index * 9, x + 12, 180), fill="#572734")
                else:
                    draw.ellipse((x, 135 - index * 7, x + 71, 229 - index * 6), fill="#e3faaa")
            image.save(folder / name, format="JPEG", quality=90)
        (folder / "index.html").write_text(fixture_page(names, published))


def main():
    with TemporaryDirectory(prefix="pgm-qa-worker-") as work:
        root = Path(work)
        web = root / "website"
        web.mkdir()
        guarded = os.getenv("PGM_QA_LOOPBACK") == "1"
        strict_diag = not guarded
        if guarded and not os.environ.get("PGM_QA_DIAGNOSTICS_ROOT"):
            os.environ["PGM_QA_DIAGNOSTICS_ROOT"] = str(root / "diagnostics")
        _diag("worker_boot", {"version": "qa-fixture-m4-instrumented", "guarded": guarded}, strict=strict_diag)

        published = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        if guarded:
            fixture = Path("/repo/storage/media-delivery-preflight-20261123/synthetic-pup-bath.jpg").read_bytes()
            if sha256(fixture).hexdigest() != FIXTURE_SHA:
                raise ValueError("The locally generated QA JPEG changed")
            (web / "robots.txt").write_text("User-agent: *\nAllow: /\n")
            image_path = web / "guarded"
            image_path.mkdir()
            (image_path / "synthetic-pup-bath.jpg").write_bytes(fixture)
            (image_path / "index.html").write_text(fixture_page(["synthetic-pup-bath.jpg"], published))
        else:
            make_site(web, published)

        admin = os.environ["PGM_QA_WORKER_TOKEN"] if guarded else "fixture-admin"
        dashboard = os.environ["PGM_QA_DASHBOARD_TOKEN"] if guarded else "fixture-dashboard"
        named_key = os.environ.get("PGM_QA_NAMED_ASSERTION_SECRET") or secrets.token_hex(32)
        pw_a = os.environ.get("PGM_QA_NAMED_PASSWORD_A") or secrets.token_hex(16)
        pw_b = os.environ.get("PGM_QA_NAMED_PASSWORD_B") or secrets.token_hex(16)
        os.environ.update(
            PROJECTS_DIR=str(root / "jobs"), AI_PROVIDER="mock", AI_API_KEY="",
            MEDIA_WORKER_TOKEN=admin, MEDIA_WORKER_DASHBOARD_TOKEN=dashboard,
            NAMED_ASSERTION_SECRET=named_key,
        )
        from app import collector, delivery, service
        from app.ai import pipeline
        from app.ai.provider import AIProviderConfig

        if not guarded:
            enable_disposable_m4_qa(service, root)
            _diag("worker_m4_qa_initialized", {"offline_approval_qa": True}, strict=strict_diag)
            _diag("worker_named_bootstrap_start", {}, strict=strict_diag)
            principal_a, principal_b = provision_qa_admins(
                service.DB, assertion_secret=named_key, password_a=pw_a, password_b=pw_b,
            )
            _diag("worker_named_bootstrap_done", {
                "admins": ["qa-admin-a", "qa-admin-b"], "distinct": principal_a != principal_b,
            }, strict=strict_diag)

        crawl = collector.run_website_extraction
        curate = pipeline.run_ai_curation
        build_plan = delivery.build_delivery_plan

        def crawl_fixture(job_id, job_dir, website_url, *args, **kwargs):
            scenarios = ("guarded",) if guarded else SCENARIOS
            if website_url not in {f"{ORIGIN}/{scenario}/" for scenario in scenarios}:
                raise ValueError("Unexpected QA fixture URL")
            result = crawl(job_id, job_dir, website_url, *args, allow_private=True, **kwargs)
            verified_ids = apply_fixture_rights(job_dir, guarded=guarded)
            _diag("worker_fixture_rights_prepared", {
                "job_id": job_id, "verified_asset_ids": verified_ids,
            }, strict=strict_diag)
            return result

        def curate_guarded_fixture(job_dir, job_id, **kwargs):
            apply_fixture_rights(job_dir, guarded=True)
            kwargs["config"] = AIProviderConfig(
                provider="mock", max_retries=0, max_requests_per_job=2,
                max_cost_usd_per_job=0.25, mode_a_max_output_tokens=1200,
                mode_b_max_output_tokens=900, batch_size=1,
            )
            return curate(job_dir, job_id, **kwargs)

        _original_store = service.store

        def _store_logged(record, **kwargs):
            job_id = record.get("job_id")
            if (not guarded and kwargs.get("collection_base") is not None
                    and record.get("status") in ("extraction_complete", "partial_success") and job_id):
                consent = record_fixture_processing_consent(service, Path(service.DATA_DIR) / job_id, job_id)
                _diag("worker_processing_consent_recorded", {
                    "job_id": job_id, "asset_ids": sorted(consent),
                }, strict=strict_diag)
            _diag("worker_job_stored", {
                "job_id": job_id,
                "status": record.get("status"),
                "pipeline_stage": record.get("pipeline_stage"),
                "ai_curation": (record.get("ai_curation") or {}).get("status"),
                "collection_base": kwargs.get("collection_base"),
            }, strict=strict_diag)
            return _original_store(record, **kwargs)

        service.store = _store_logged
        collector.run_website_extraction = crawl_fixture
        # Only guarded loopback mode wraps run_ai_curation; M4 QA uses unpatched run_started_curation fencing.
        if guarded:
            pipeline.run_ai_curation = curate_guarded_fixture
        # This fixture process alone can send mock-selected bytes to the fake Drive.
        delivery.build_delivery_plan = partial(build_plan, allow_fixture=True)
        app = service.app if guarded else _QaDiagnosticMiddleware(service.app, strict=strict_diag)
        if guarded:
            adapter_token = os.environ["PGM_QA_ADAPTER_TOKEN"]
            drive_token = os.environ["PGM_QA_DRIVE_TOKEN"]
            if min(map(len, (admin, dashboard, adapter_token, drive_token))) < 32:
                raise ValueError("Isolated fixture credentials are required")
            from app.qa_live import GuardedJobRequest, bounded_config

            config = bounded_config("fixture-token-with-no-groq-access")
            assert (config.mode_a_max_output_tokens, config.mode_b_max_output_tokens,
                    config.max_cost_usd_per_job, config.max_requests_per_job, config.max_retries) == (1200, 900, 0.25, 2, 0)

            app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=service.app.router.lifespan_context)

            @app.post("/guarded-job")
            def guarded_fixture(body: GuardedJobRequest, x_qa_adapter_token: str = Header(default="")):
                if not compare_digest(x_qa_adapter_token.encode(), adapter_token.encode()):
                    raise HTTPException(401, "Fixture adapter authorization required")
                job = service.fetch(body.job_id)
                if not job or job.get("website_url") != ORIGIN + "/guarded/" or job.get("status") != "extraction_complete":
                    raise HTTPException(403, "Job outside fixture scope")
                directory = service.DATA_DIR / body.job_id
                manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                items = manifest.get("assets", [])
                if len(items) != 1 or items[0].get("sha256") != FIXTURE_SHA or manifest.get("job_id") != body.job_id:
                    raise HTTPException(409, "Fixture job and asset hash mismatch")
                try:
                    with (directory / "fixture-adapter-intent").open("x"):
                        pass
                except FileExistsError:
                    raise HTTPException(409, "Fixture job already attempted") from None
                # The worker and published mock artifacts stay explicitly marked mock.
                analysis = curate_guarded_fixture(directory, body.job_id)
                outcome = analysis.as_dict()
                if outcome.get("mock") is not True or outcome.get("status") != "completed":
                    raise HTTPException(409, "Fixture curation did not complete as mock")
                service.store({**job, "run_ai_curation": True, "ai_curation": outcome})
                plan = delivery.build_delivery_plan(directory, body.job_id)
                eligible = [item for item in plan["pending_uploads"]
                            if item["rights_status"] == "verified" and item["rights_review_required"] is False]
                if len(eligible) != 1 or eligible[0]["sha256"] != FIXTURE_SHA:
                    raise HTTPException(409, "Fixture asset is not the approved simulation candidate")
                response = requests.post("http://127.0.0.1:8899/deliver",
                                         json={"job_id": body.job_id, "upload_keys": [eligible[0]["upload_key"]]},
                                         headers={"x-qa-drive-token": drive_token}, timeout=15, allow_redirects=False)
                response.raise_for_status()
                receipt = response.json()
                if receipt.get("job_id") != body.job_id:
                    raise HTTPException(409, "Fixture receipt belongs to another job")
                return {**receipt, "mode": "fixture", "status": "verified"}

            app.mount("/", service.app)
        server = ThreadingHTTPServer(("127.0.0.1", 8790), partial(SimpleHTTPRequestHandler, directory=str(web)))
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            uvicorn.run(app, host="127.0.0.1" if guarded else "0.0.0.0",
                        port=8877 if guarded else 8787, access_log=False)
        finally:
            server.shutdown()


if __name__ == "__main__":
    main()
