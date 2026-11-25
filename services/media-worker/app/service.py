"""Phase 3 media worker API.

Endpoints:
  GET  /health                              -> {"ok": true}
  GET  /status                              -> authenticated worker status (X-API-Key)
  POST /jobs                                -> {"job_id": "<uuid>", "status": "queued"}
  GET  /jobs/{job_id}                       -> job record (status, per-source fields)
  GET  /jobs/{job_id}/manifest              -> unified multi-source manifest

Website extraction is live (Phase 2). Instagram/Facebook collection is live
(Phase 3): gallery-dl primary with Instaloader/yt-dlp fallbacks, gated behind
the operator's `social_permission_confirmed` acknowledgement, with honest
per-source statuses and a unified manifest. No AI or Google Drive yet.
"""

from __future__ import annotations

import datetime as _dt
import json
import mimetypes
import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from threading import BoundedSemaphore, Lock
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from app.publication import frozen_window, window_bounds
from app.security import WorkerSecurity

_SAFE_PREVIEW_MIMES = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/avif",
    "video/mp4",
    "video/webm",
    "video/quicktime",
})

app = FastAPI(title="Pet Grooming Media Worker", version="0.9.0", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(WorkerSecurity)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, error: RequestValidationError) -> JSONResponse:
    return JSONResponse({"detail": [
        {"loc": item["loc"], "type": item["type"], "msg": item["msg"]}
        for item in error.errors()
    ]}, status_code=422)

executor = ThreadPoolExecutor(max_workers=2)
slots = BoundedSemaphore(4)
lock = Lock()
submission_lock = Lock()

DATA_DIR = Path(os.environ.get("PROJECTS_DIR", "./storage/jobs"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "_system" / "jobs.sqlite"
DB.parent.mkdir(parents=True, exist_ok=True)


class ClosingConnection(sqlite3.Connection):
    """SQLite connection that commits/rolls back and always closes its FD on context exit."""

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            return super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10, factory=ClosingConnection)
    con.execute("PRAGMA busy_timeout=5000;")
    mode_row = con.execute("PRAGMA journal_mode;").fetchone()
    if not mode_row or str(mode_row[0]).lower() != "wal":
        with lock:
            mode_row = con.execute("PRAGMA journal_mode;").fetchone()
            if not mode_row or str(mode_row[0]).lower() != "wal":
                con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute(
        "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    return con


def store(record: dict[str, Any], *, collection_base: int | None = None) -> None:
    prepared_snapshot = None
    if collection_base is not None:
        from app.collection_approval import accept, snapshot
        try:
            prepared_snapshot = snapshot(DATA_DIR, record)
        except (HTTPException, OSError, ValueError, KeyError, TypeError):
            prepared_snapshot = None
    with lock, connect() as con:
        if collection_base is not None:
            from app.collection_approval import accept
            con.execute("BEGIN IMMEDIATE")
            accept(con, DATA_DIR, record, collection_base, precomputed_snapshot=prepared_snapshot)
        con.execute(
            "INSERT OR REPLACE INTO jobs (id, payload) VALUES (?,?)",
            (record["job_id"], json.dumps(record)),
        )


def fetch(job_id: str) -> dict[str, Any] | None:
    with lock, connect() as con:
        row = con.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _all_job_records() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT payload FROM jobs").fetchall()
    out: list[dict[str, Any]] = []
    for (blob,) in rows:
        try:
            out.append(json.loads(blob))
        except (TypeError, json.JSONDecodeError):
            continue
    return out


# Recover jobs left running/queued across a restart (fail-closed: mark interrupted).
with connect() as _db:
    for _id, _blob in _db.execute("SELECT id, payload FROM jobs").fetchall():
        _old = json.loads(_blob)
        if _old.get("status") in ("running", "queued"):
            _old.update(
                status="interrupted",
                error="Service restarted; resubmit to retry",
            )
            _db.execute(
                "UPDATE jobs SET payload=? WHERE id=?",
                (json.dumps(_old), _id),
            )


class CreateJobRequest(BaseModel):
    website_url: str = Field(min_length=1, max_length=2048, description="Required business website URL")
    instagram_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Optional public Instagram URL (parked by default in V2.1)",
    )
    facebook_url: str = Field(min_length=1, max_length=2048, description="Required public Facebook URL")
    lookback_days: int = Field(default=365, ge=1, le=366)
    max_selected_images: int = Field(default=30, ge=1, le=200)
    max_selected_videos: int = Field(default=10, ge=1, le=100)
    user_selection_notes: str | None = None
    max_pages: int = Field(default=10, ge=1, le=25, description="Website crawl page cap (low-volume Phase 2 default)")
    max_media: int = Field(default=30, ge=1, le=80, description="Website media download cap")
    social_permission_confirmed: bool = Field(
        default=False,
        description=(
            "Operator acknowledges the intended collection and use of social media are "
            "permitted. This acknowledgement is NOT platform authorization and NOT a "
            "copyright licence; it never bypasses login walls or rate limits."
        ),
    )
    max_social_files: int = Field(default=20, ge=1, le=60, description="Per-platform social media download cap")
    run_ai_curation: bool = Field(
        default=False,
        description=(
            "Run the Phase 5 AI Visual Director (MODE A + MODE B) after preprocessing. "
            "Requires AI provider configuration (AI_PROVIDER env). When false or when "
            "the provider is unavailable, extraction results are preserved unchanged."
        ),
    )
    business_context: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Operator-supplied verified business context for curation: "
            "business_name, business_type, services, location, brand_notes, "
            "visual_preferences. Never fabricated by the pipeline."
        ),
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description="Optional client-supplied idempotency key for duplicate webhook protection.",
    )

    @field_validator("website_url", "instagram_url", "facebook_url")
    @classmethod
    def validate_url(cls, value: str | None, info) -> str | None:
        from app.orchestration import _check_url

        is_required = info.field_name != "instagram_url"
        errors: list[str] = []
        normalized = _check_url(value, info.field_name, errors, required=is_required)
        if errors:
            raise ValueError(errors[0])
        return normalized



@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/ready")
def ready() -> JSONResponse:
    try:
        from contextlib import closing
        with closing(sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
            connection.execute("SELECT id FROM jobs LIMIT 1").fetchone()
    except (ValueError, sqlite3.Error):
        return JSONResponse({"ok": False}, status_code=503)
    return JSONResponse({"ok": True})


@app.get("/status")
def status() -> object:
    """Authenticated worker status used by the n8n health probe and dashboard settings."""
    from app.ai.provider import AIProviderConfig, build_provider

    cfg = AIProviderConfig.from_env()
    provider_obj, provider_status = build_provider(cfg)
    ai_configured = provider_obj is not None and provider_status in ("ready", "mock")
    collection_engine = (
        "historical"
        if (
            os.environ.get("PUBLIC_COLLECTION_CHECKPOINTS") == "1"
            or os.environ.get("COLLECTION_ENGINE", "").strip().lower() == "historical"
        )
        else "legacy"
    )
    return {
        "ok": True,
        "service": "media-worker",
        "phase": 9,
        "auth": "verified",
        "token_configured": True,
        "ai_configured": ai_configured,
        "ai_provider": cfg.provider or None,
        "ai_model": cfg.model or None,
        "m4_approval_enabled": m4_approval_enabled(),
        "collection_engine": collection_engine,
    }


def m4_approval_enabled() -> bool:
    """Explicit default-OFF activation gate for M4 approval, rights ledger, and Start AI execution."""
    return bool(
        getattr(app.state, "offline_approval_qa", False)
        or os.environ.get("M4_COLLECTION_APPROVAL_ENABLED") == "1"
    )


def execute_started_curation(
    body: dict[str, Any],
    actor: str,
    *,
    provider: Any = None,
    config: Any = None,
) -> Any:
    """Execute Start AI curation for an approved immutable revision through the configured provider."""
    import re
    from app.ai.pipeline import run_started_curation
    from app.ai.provider import AIProviderConfig, build_provider, scrub_secrets
    from app.ai_execution import InferenceHeld
    from app.collection_approval import review_state

    if (
        not isinstance(body, dict)
        or not {"job_id", "if_match", "processing_consent"} <= set(body)
        or not set(body) <= {"job_id", "if_match", "processing_consent", "async"}
    ):
        raise HTTPException(400, "Expected job_id, if_match, and processing_consent")
    run_async = body.get("async", False)
    if not isinstance(run_async, bool):
        raise HTTPException(400, "Field async must be boolean")
    job_id = body["job_id"]
    start_etag = body["if_match"]
    raw_consent = body["processing_consent"]
    if (
        not isinstance(job_id, str)
        or not re.fullmatch(r"[a-f0-9]{32}", job_id)
        or not isinstance(start_etag, str)
        or not start_etag.strip()
        or not isinstance(raw_consent, dict)
        or not raw_consent
    ):
        raise HTTPException(400, "Invalid collection execution fields")
    consent_map: dict[str, tuple[str, str]] = {}
    for aid, entry in raw_consent.items():
        if (
            not isinstance(aid, str)
            or not aid.strip()
            or not isinstance(entry, dict)
            or set(entry) != {"source_url", "sha256"}
            or not isinstance(entry["source_url"], str)
            or not entry["source_url"].strip()
            or not isinstance(entry["sha256"], str)
            or not entry["sha256"].strip()
        ):
            raise HTTPException(400, "Each processing_consent entry requires source_url and sha256")
        consent_map[aid] = (entry["source_url"], entry["sha256"])

    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    job_dir = Path(DATA_DIR) / job_id
    if (job_dir / "curation.json").exists():
        raise HTTPException(409, "Existing curation preserved")

    # Read-only preflight before mutating job status or building provider;
    # ai_execution.execution() remains the sole atomic claim gate inside BEGIN IMMEDIATE.
    with connect() as con:
        required = ("collection_revisions", "collection_decisions", "collection_ai_starts", "ai_execution_claims")
        present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not set(required) <= present:
            raise HTTPException(503, "M4 collection ledger is not migrated")
        state, submitter, current, _seq, decision, start = review_state(con, DATA_DIR, job_id)
        if not current or state["state"] != "approved" or not state["start_authorized"] or not decision or not start or decision[3] == submitter:
            raise HTTPException(409, "Independent approval and active Start AI required")
        if state["etag"] != start_etag:
            raise HTTPException(412, "Requested Start AI does not match current approved revision")
        rev = state["revision"]
        if con.execute("SELECT 1 FROM ai_execution_claims WHERE job=? AND revision=?", (job_id, rev)).fetchone():
            raise HTTPException(409, "Execution already claimed for this revision")

    cfg = config or AIProviderConfig.from_env()
    if cfg.provider == "mock" and not getattr(app.state, "offline_approval_qa", False):
        raise HTTPException(409, "Mock AI provider is not permitted in production mode")

    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    previous_ai = rec.get("ai_curation")
    if provider is None:
        factory = getattr(app.state, "ai_provider_factory", None)
        if callable(factory):
            provider = factory(cfg)
        else:
            provider, status = build_provider(cfg)
            if provider is None:
                store({
                    **rec,
                    "ai_curation": {
                        "status": status,
                        "provider": cfg.provider,
                        "model": cfg.model,
                        "actor": actor,
                        "start_etag": start_etag,
                        "updated_at": now,
                    },
                    "updated_at": now,
                })
                raise HTTPException(503, f"AI provider {status}")

    def _run_curation_sync() -> Any:
        claimed_by_self = False

        def _mark_started() -> None:
            nonlocal claimed_by_self
            claimed_by_self = True
            started_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
            latest_rec = fetch(job_id) or rec
            store({
                **latest_rec,
                "ai_curation": {
                    "status": "started",
                    "provider": getattr(provider, "name", cfg.provider),
                    "model": cfg.model or getattr(provider, "model", ""),
                    "actor": actor,
                    "start_etag": start_etag,
                    "started_at": started_at,
                },
                "updated_at": started_at,
            })

        try:
            outcome = run_started_curation(
                job_dir,
                job_id,
                processing_consent=consent_map,
                start_etag=start_etag,
                on_claimed=_mark_started,
                config=cfg,
                provider=provider,
                business_context=rec.get("business_context"),
                user_selection_notes=rec.get("user_selection_notes"),
                include_undated_website=bool(rec.get("include_undated_website")),
                max_images=int(rec.get("max_selected_images") or 30),
                max_videos=int(rec.get("max_selected_videos") or 10),
                batch_size=cfg.batch_size,
                max_images_per_request=cfg.max_images_per_request,
            )
        except InferenceHeld as exc:
            if exc.claimed or claimed_by_self:
                finished = _dt.datetime.now(_dt.timezone.utc).isoformat()
                latest = fetch(job_id) or rec
                store({
                    **latest,
                    "ai_curation": {
                        "status": "failed",
                        "held": True,
                        "error": scrub_secrets(str(exc))[:240],
                        "actor": actor,
                        "start_etag": start_etag,
                        "finished_at": finished,
                    },
                    "updated_at": finished,
                })
            # If this caller never won the claim (e.g. concurrent loser or bad consent),
            # leave the job record untouched so another caller's started/completed state is never clobbered.
            raise HTTPException(409, str(exc)) from None
        except Exception as exc:  # noqa: BLE001 — fail honestly without leaking secrets
            safe_err = scrub_secrets(str(exc))[:240]
            if claimed_by_self:
                finished = _dt.datetime.now(_dt.timezone.utc).isoformat()
                latest = fetch(job_id) or rec
                store({
                    **latest,
                    "ai_curation": {
                        "status": "failed",
                        "provider": getattr(provider, "name", cfg.provider),
                        "model": cfg.model or getattr(provider, "model", ""),
                        "actor": actor,
                        "start_etag": start_etag,
                        "errors": [safe_err],
                        "finished_at": finished,
                    },
                    "updated_at": finished,
                })
            return JSONResponse({"job_id": job_id, "status": "failed", "errors": [safe_err]}, status_code=502)

        finished = _dt.datetime.now(_dt.timezone.utc).isoformat()
        latest = fetch(job_id) or rec
        store({
            **latest,
            "ai_curation": {
                **outcome.as_dict(),
                "actor": actor,
                "start_etag": start_etag,
                "finished_at": finished,
            },
            "updated_at": finished,
        })
        payload_out = {"job_id": job_id, **outcome.as_dict()}
        if outcome.status not in ("completed", "partial"):
            return JSONResponse(payload_out, status_code=502)
        return payload_out

    if run_async:
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "Worker busy; retry later")

        def _bg_worker() -> None:
            try:
                _run_curation_sync()
            except HTTPException:
                pass
            finally:
                slots.release()

        try:
            executor.submit(_bg_worker)
        except Exception:
            slots.release()
            raise
        return JSONResponse(
            {"job_id": job_id, "status": "queued", "async": True, "start_etag": start_etag},
            status_code=202,
        )

    return _run_curation_sync()


@app.post("/named/{action}")
async def named_action(action: str, request: Request):
    from app.named_auth import dispatch, is_local_bootstrap_peer, verify_admin_action
    from starlette.concurrency import run_in_threadpool

    if action in ("bootstrap", "bootstrap-status"):
        client_host = request.client.host if request.client else ""
        if not is_local_bootstrap_peer(client_host, request.headers):
            raise HTTPException(403, "Browser bootstrap requires local loopback access")

    enabled = m4_approval_enabled()
    if action == "collection-resume":
        if not enabled:
            raise HTTPException(404, "Approval adapter unavailable")
        _actor, body = await run_in_threadpool(
            verify_admin_action,
            DB,
            action,
            await request.body(),
            request.headers.get("x-named-assertion", ""),
            request.headers.get("x-named-session", ""),
        )
        import re as _re
        jid = body.get("job_id") if isinstance(body, dict) else None
        if set(body) != {"job_id"} or not isinstance(jid, str) or not _re.fullmatch(r"[a-f0-9]{32}", jid):
            raise HTTPException(400, "Valid 32-hex job_id required")
        out = await run_in_threadpool(resume_collection, jid)
        return JSONResponse(out, status_code=202)
    if action == "collection-execute":
        if not enabled:
            raise HTTPException(404, "Approval adapter unavailable")
        actor, body = await run_in_threadpool(
            verify_admin_action,
            DB,
            action,
            await request.body(),
            request.headers.get("x-named-assertion", ""),
            request.headers.get("x-named-session", ""),
        )
        return await run_in_threadpool(execute_started_curation, body, actor)
    if action.startswith("collection-") and not enabled:
        raise HTTPException(404, "Approval adapter unavailable")
    return await run_in_threadpool(
        dispatch,
        DB,
        action,
        await request.body(),
        request.headers.get("x-named-assertion", ""),
        request.headers.get("x-named-session", ""),
        approval_root=DATA_DIR if enabled else None,
    )


@app.post("/jobs", status_code=202)
def create_job(req: CreateJobRequest) -> dict[str, Any]:
    with submission_lock:
        return register_job(req)


def register_job(req: CreateJobRequest) -> dict[str, Any]:
    named = (req.idempotency_key or "").startswith("named-")
    if named:
        from app.named_auth import verify_ticket
        with connect() as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='named_intakes'").fetchone():
                raise HTTPException(403, "Named authorization denied")
            submitter = verify_ticket(con, req.model_dump())
    if req.idempotency_key:
        from app.orchestration import find_idempotent_job

        existing_id = find_idempotent_job(_all_job_records(), req.idempotency_key)
        if existing_id:
            rec = fetch(existing_id)
            if named and (rec or {}).get("submitter_principal_id") != submitter:
                raise HTTPException(403, "Submitter mismatch")
            if (rec and rec.get("collection_mode") == "recorded_extractor" and rec.get("status") == "interrupted"
                    and os.environ.get("RECORDED_EXTRACTOR_HANDOFF") == "1"):
                keys = ("website_url", "instagram_url", "facebook_url")
                if any(rec.get(key) != getattr(req, key) for key in keys):
                    raise HTTPException(409, "Recorded retry must preserve the original source URLs")
                if not slots.acquire(blocking=False):
                    raise HTTPException(429, "Worker busy; retry later")
                queued = {**rec, "status": "queued"}
                try:
                    store(queued)
                    executor.submit(_process_job, existing_id, queued)
                except Exception:
                    slots.release()
                    store(rec)
                    raise
                return {"job_id": existing_id, "status": "queued", "idempotent_reuse": True}
            return {
                "job_id": existing_id,
                "status": (rec or {}).get("status", "queued"),
                "idempotent_reuse": True,
            }
    if not slots.acquire(blocking=False):
        raise HTTPException(429, "Worker busy; retry later")
    job_id = uuid.uuid4().hex
    window = frozen_window(_dt.datetime.now(_dt.timezone.utc))
    now_iso = window["created_at"]
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": "queued",
        "website_url": req.website_url,
        "instagram_url": req.instagram_url,
        "facebook_url": req.facebook_url,
        "lookback_days": 365,
        "max_selected_images": req.max_selected_images,
        "max_selected_videos": req.max_selected_videos,
        "user_selection_notes": req.user_selection_notes,
        "max_pages": req.max_pages,
        "max_media": req.max_media,
        "social_permission_confirmed": req.social_permission_confirmed,
        "max_social_files": req.max_social_files,
        "run_ai_curation": req.run_ai_curation,
        "business_context": req.business_context,
        "idempotency_key": req.idempotency_key,
        **window,
        "updated_at": now_iso,
        "sources": {
            "website": {"status": "queued"},
            "instagram": {"status": "queued" if req.instagram_url else "not_requested"},
            "facebook": {"status": "queued" if req.facebook_url else "not_requested"},
        },
    }
    if os.environ.get("RECORDED_EXTRACTOR_HANDOFF") == "1":
        payload["collection_mode"] = "recorded_extractor"
    elif (
        os.environ.get("PUBLIC_COLLECTION_CHECKPOINTS") == "1"
        or os.environ.get("COLLECTION_ENGINE", "").strip().lower() == "historical"
    ):
        payload["collection_mode"] = "public_checkpoint"
    try:
        if named:
            with lock, connect() as con:
                con.execute("BEGIN IMMEDIATE")
                payload["submitter_principal_id"] = verify_ticket(con, req.model_dump())
                con.execute("INSERT INTO jobs VALUES (?,?)", (job_id, json.dumps(payload)))
        else:
            store(payload)
        executor.submit(_process_job, job_id, payload)
    except Exception:
        slots.release()
        raise
    return {"job_id": job_id, "status": "queued"}


def _process_job(job_id: str, payload: dict[str, Any]) -> None:
    """Phase 3: website extraction + permitted social collection.

    Each source is isolated: a social failure never destroys website results,
    and no source is ever marked successful without verifiable evidence.
    Social collection only runs when the operator acknowledged permitted use
    (social_permission_confirmed); that acknowledgement is not platform
    authorization and never bypasses access restrictions.
    """
    from app.collector import run_website_extraction
    from app.manifest import merge_social_into_manifest, validate_against_disk, write_manifest
    from app.social_adapter import (
        SocialExtractionResult,
        run_social_extraction,
        section_from_result,
    )

    def _now() -> str:
        return _dt.datetime.now(_dt.timezone.utc).isoformat()

    try:
        collection_base = None
        if m4_approval_enabled():
            with connect() as con:
                has_ledger = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_revisions'"
                ).fetchone()
                if not has_ledger:
                    raise RuntimeError(
                        "M4 collection ledger is not migrated; run explicit migration before enabling M4_COLLECTION_APPROVAL_ENABLED"
                    )
                collection_base = con.execute(
                    "SELECT coalesce(max(revision),0) FROM collection_revisions WHERE job=?",
                    (job_id,),
                ).fetchone()[0]
        sources: dict[str, Any] = dict(payload.get("sources") or {})
        sources["website"] = {"status": "running"}
        for platform in ("instagram", "facebook"):
            sources[platform] = {
                "status": "running" if payload.get(f"{platform}_url") else "not_requested"
            }
        store({**payload, "status": "running", "sources": sources, "pipeline_stage": "collecting", "updated_at": _now()})

        job_dir = Path(DATA_DIR) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        mode = payload.get("collection_mode")
        if mode in ("public_checkpoint", "recorded_extractor"):
            from app.public_collection import (
                COLLECTION_HOLD,
                build_multitenant_collection_config,
                collect_public_job,
            )

            if mode == "recorded_extractor":
                from app.recorded_collection import collect_recorded_job

                if os.environ.get("RECORDED_EXTRACTOR_HANDOFF") != "1":
                    raise ValueError("Checkpoint collection mode is disabled")
                config_path = os.environ.get("RECORDED_EXTRACTOR_CONFIG")
                if not config_path:
                    raise ValueError("Checkpoint collection requires an explicit source configuration")
                source_cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
                manifest = collect_recorded_job(job_dir, payload, source_cfg)
            else:
                checkpoint_enabled = (
                    os.environ.get("PUBLIC_COLLECTION_CHECKPOINTS") == "1"
                    or os.environ.get("COLLECTION_ENGINE", "").strip().lower() == "historical"
                )
                if not checkpoint_enabled:
                    raise ValueError("Checkpoint collection mode is disabled")
                config_path = os.environ.get("PUBLIC_COLLECTION_CONFIG")
                if config_path:
                    source_cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
                else:
                    source_cfg = build_multitenant_collection_config(payload)
                manifest = collect_public_job(job_dir, payload, source_cfg)
            store({
                **payload, "status": "partial_success", "sources": manifest["sources"],
                "collection_gate": COLLECTION_HOLD, "ai_curation": {"status": COLLECTION_HOLD},
                "preprocessing": manifest.get("preprocessing") or {"status": "not_run"},
                "manifest_path": str(job_dir / "manifest.json"),
                "error": "Public inventory is unverified and held for per-job owner review.",
                "updated_at": _now(),
            }, collection_base=collection_base)
            return

        # --- Website source -------------------------------------------------
        website_status: dict[str, Any] = {}
        try:
            result = run_website_extraction(
                job_id,
                job_dir,
                str(payload["website_url"]),
                max_pages=int(payload.get("max_pages") or 10),
                max_media=int(payload.get("max_media") or 30),
            )
            website_status = {
                "status": result.status,
                "pages_crawled": result.pages_crawled,
                "downloaded": result.assets_downloaded,
                "duplicates": result.duplicates,
                "failed": result.failed,
                "skipped": result.skipped,
                "bytes": result.bytes_downloaded,
            }
            if result.issues:
                website_status["issues"] = result.issues[:20]
            if result.notes:
                website_status["notes"] = result.notes[:10]
        except Exception as exc:  # noqa: BLE001 — isolate website failures
            website_status = {"status": "failed", "error": str(exc)[:240]}

        # --- Social sources (isolated; permission-gated) ---------------------
        social_sections: list[tuple[str, dict[str, Any]]] = []
        for platform in ("instagram", "facebook"):
            url = payload.get(f"{platform}_url")
            if not url:
                sources[platform] = {"status": "not_requested"}
                continue
            try:
                social_result = run_social_extraction(
                    job_id,
                    job_dir,
                    platform,
                    str(url),
                    max_files=int(payload.get("max_social_files") or 20),
                    lookback_days=int(payload.get("lookback_days") or 365),
                    permission_confirmed=bool(payload.get("social_permission_confirmed")),
                    window_start_utc=payload.get("window_start_utc"),
                )
            except Exception as exc:  # noqa: BLE001 — isolate social failures
                social_result = SocialExtractionResult(
                    job_id=job_id,
                    platform=platform,
                    url=str(url),
                    status="failed",
                    reason=f"adapter_error: {exc}"[:240],
                )
            section = section_from_result(social_result)
            social_sections.append((platform, section))
            sources[platform] = {k: v for k, v in section.items() if k != "items"}
            # Incremental progress so the operator sees per-source status early.
            store(
                {
                    **payload,
                    "status": "running",
                    "sources": {**sources, "website": website_status},
                    "updated_at": _now(),
                }
            )

        # --- Unified manifest ------------------------------------------------
        website_doc: dict[str, Any] | None = None
        manifest_file = job_dir / "manifest.json"
        if manifest_file.is_file():
            try:
                website_doc = json.loads(manifest_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                website_doc = None
        if social_sections or website_doc is None or window_bounds(payload):
            try:
                merged = merge_social_into_manifest(
                    website_doc,
                    social_sections,
                    job_id=job_id,
                    lookback_days=int(payload.get("lookback_days") or 365),
                    permission_confirmed=bool(payload.get("social_permission_confirmed")),
                    collection_job=payload,
                )
                # Final guarantee: every manifest asset that claims a file must
                # exist on disk with a matching hash (Phase 3 acceptance #5).
                problems = validate_against_disk(job_dir, merged)
                if problems:
                    issues = list((merged.get("coverage") or {}).get("issues") or [])
                    issues = issues[:50] + [
                        {"reason": "manifest_validation", "detail": p} for p in problems[:20]
                    ]
                    merged.setdefault("coverage", {})["issues"] = issues
                write_manifest(job_dir, merged)
                if problems:
                    sources["manifest"] = {
                        "status": "partial_success",
                        "problems": problems[:20],
                    }
            except Exception as exc:  # noqa: BLE001 — manifest failure is recorded
                sources["manifest"] = {"status": "failed", "error": str(exc)[:240]}

        store({**payload, "status": "running", "sources": {**sources, "website": website_status}, "pipeline_stage": "preprocessing", "updated_at": _now()})
        # --- Phase 4 media preprocessing (isolated; partial-failure safe) --
        preprocessing_record: dict[str, Any] = {"status": "not_run"}
        if manifest_file.is_file():
            try:
                from app.preprocessing import run_media_preprocessing

                pre = run_media_preprocessing(job_dir, job_id)
                preprocessing_record = {
                    "status": pre.status,
                    "images": {
                        "analyzed": pre.images_analyzed,
                        "unsupported_format": pre.images_unsupported,
                        "problem": pre.images_problem,
                    },
                    "videos": {
                        "probed": pre.videos_probed,
                        "frames_extracted": pre.videos_frames,
                        "problem": pre.videos_problem,
                    },
                    "duplicate_groups": {
                        "exact": pre.exact_duplicate_groups,
                        "perceptual": pre.perceptual_duplicate_groups,
                    },
                    "warnings": pre.warnings[:20],
                }
            except Exception as exc:  # noqa: BLE001 — preprocessing failure is recorded
                preprocessing_record = {"status": "failed", "error": str(exc)[:240]}

        # Intake can request review. Only post-collection dispatch consumes a named Start AI.
        ai_record = {"status": "awaiting_collection_approval" if payload.get("run_ai_curation") else "not_requested"}

        # --- Honest overall status -------------------------------------------
        # NOTE: compute from the SAME merged dict the record stores, otherwise a
        # stale "running" placeholder in `sources` can override the real result.
        final_sources = {**sources, "website": website_status}
        attempted = [
            s.get("status")
            for s in final_sources.values()
            if isinstance(s, dict)
            and s.get("status")
            in (
                "completed",
                "partial_success",
                "failed",
                "invalid_url",
                "restricted",
                "rate_limited",
                "inaccessible",
            )
        ]
        if not attempted:
            job_status = "failed"
        elif all(s == "completed" for s in attempted):
            job_status = "extraction_complete"
        elif any(s in ("completed", "partial_success") for s in attempted):
            job_status = "partial_success"
        else:
            job_status = "failed"

        record = {
            **payload,
            "status": job_status,
            "sources": final_sources,
            "preprocessing": preprocessing_record,
            "ai_curation": ai_record,
            "updated_at": _now(),
        }
        if manifest_file.is_file():
            record["manifest_path"] = str(manifest_file)
        if job_status == "failed":
            record["error"] = "Extraction failed for every attempted source; see sources for reasons."
        elif (
            preprocessing_record.get("status") == "failed"
            and job_status == "extraction_complete"
        ):
            # honest downgrade: collection succeeded, post-processing failed
            record["status"] = job_status = "partial_success"
            record["error"] = "Media preprocessing failed; see preprocessing.error."
        elif (
            ai_record.get("status") in ("failed", "provider_unavailable", "disabled")
            and job_status == "extraction_complete"
        ):
            # AI was REQUESTED and could not run: extraction is preserved and
            # the job is honestly NOT marked fully complete.
            record["status"] = job_status = "partial_success"
            record["error"] = "AI curation failed or provider unavailable; extraction preserved."
        store(record, collection_base=collection_base if manifest_file.is_file() else None)
    except Exception as exc:  # noqa: BLE001 — record failure and keep worker up
        if isinstance(exc, HTTPException) and exc.status_code == 412:
            return  # A newer collection publication already won the transaction.
        store(
            {
                **payload,
                "status": "failed",
                "error": str(exc)[:240],
                "sources": {
                    **(payload.get("sources") or {}),
                    "website": {"status": "failed", "error": str(exc)[:240]},
                },
                "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }
        )
    finally:
        slots.release()


@app.get("/jobs")
def list_jobs() -> dict[str, Any]:
    records = _all_job_records()
    items = []
    for rec in records:
        if not isinstance(rec, dict) or not rec.get("job_id"):
            continue
        items.append(
            {
                "job_id": rec.get("job_id"),
                "status": rec.get("status"),
                "website_url": rec.get("website_url"),
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at"),
            }
        )
    items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {"jobs": items, "total": len(items)}


def _manifest_preview_allowlist(job_dir: Path) -> dict[str, str | None]:
    """Return {relative_posix_path: recorded_mime} for manifest-declared media files only."""
    manifest_file = job_dir / "manifest.json"
    if not manifest_file.is_file():
        return {}
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    allowed: dict[str, str | None] = {}
    for asset in manifest.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        if asset.get("extraction_status") in ("downloaded", "duplicate", "extracted_frame"):
            rel = asset.get("local_path")
            if isinstance(rel, str) and rel:
                mime = asset.get("mime")
                clean_mime = str(mime).split(";", 1)[0].strip().lower() if isinstance(mime, str) else None
                allowed[rel] = clean_mime
        for frame in asset.get("frames") or []:
            if isinstance(frame, dict):
                f_rel = frame.get("local_path")
                if isinstance(f_rel, str) and f_rel:
                    allowed[f_rel] = "image/jpeg"
    return allowed


@app.get("/jobs/{job_id}/file")
def get_job_file(job_id: str, path: str) -> Any:
    from fastapi.responses import FileResponse

    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    job_dir = (Path(DATA_DIR) / job_id).resolve()
    if not job_dir.is_relative_to(DATA_DIR.resolve()):
        raise HTTPException(400, "Invalid job directory")
    if not job_dir.is_dir():
        raise HTTPException(404, "Job directory missing")
    # Strict traversal guard FIRST (before manifest lookup): reject absolute, backslash, drive, or .. escapes.
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or PureWindowsPath(path).drive
        or Path(path).is_absolute()
        or ".." in Path(path).parts
    ):
        raise HTTPException(400, "Invalid path")
    candidate = (job_dir / path).resolve()
    try:
        rel_posix = candidate.relative_to(job_dir).as_posix()
    except ValueError:
        raise HTTPException(400, "Invalid path")
    # Manifest media allowlist: never serve internal SQLite DBs, snapshots, manifests, or unindexed files.
    allowed = _manifest_preview_allowlist(job_dir)
    if rel_posix not in allowed:
        raise HTTPException(404, "File not found")
    if not candidate.is_file():
        raise HTTPException(404, "File not found")
    recorded_mime = allowed[rel_posix]
    if recorded_mime == "image/jpg":
        recorded_mime = "image/jpeg"
    guessed_mime, _ = mimetypes.guess_type(candidate.name)
    safe_mime = recorded_mime if recorded_mime in _SAFE_PREVIEW_MIMES else (
        guessed_mime if guessed_mime in _SAFE_PREVIEW_MIMES else None
    )
    if safe_mime is None:
        raise HTTPException(415, "Unsupported media type")
    return FileResponse(
        candidate,
        media_type=safe_mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    record = fetch(job_id)
    if record is None:
        raise HTTPException(404, "Unknown job")
    return record




@app.post("/intake/validate")
def validate_intake_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    from app.orchestration import validate_intake

    return validate_intake(body or {})


@app.get("/jobs/{job_id}/pipeline-status")
def get_pipeline_status(job_id: str) -> dict[str, Any]:
    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    from app.orchestration import derive_stage

    delivery = None
    job_dir = Path(DATA_DIR) / job_id
    delivery_path = job_dir / "delivery.json"
    if delivery_path.is_file():
        try:
            delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            delivery = None
    return derive_stage(rec, delivery)


@app.post("/jobs/{job_id}/orchestration")
def post_orchestration(job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    from app.orchestration import STAGES

    stage = str((body or {}).get("stage") or "").strip()
    if stage not in STAGES:
        raise HTTPException(400, "Unknown stage: " + stage)
    detail = str((body or {}).get("detail") or "")[:500]
    job_dir = Path(DATA_DIR) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    orch = rec.get("orchestration") if isinstance(rec.get("orchestration"), dict) else {}
    history = list(orch.get("history") or [])
    history.append({"stage": stage, "detail": detail, "at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
    history = history[-20:]
    orch = {"stage": stage, "detail": detail, "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "history": history}
    rec["orchestration"] = orch
    rec["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    store(rec)
    return {"ok": True, "job_id": job_id, "stage": stage}

@app.post("/jobs/{job_id}/resume-collection", status_code=202)
def resume_collection(job_id: str) -> dict[str, Any]:
    """Resume an existing public_checkpoint job from its persisted CollectionCheckpoint cursor."""
    with submission_lock:
        rec = fetch(job_id)
        if rec is None:
            raise HTTPException(404, "Unknown job")
        if rec.get("collection_mode") != "public_checkpoint":
            raise HTTPException(409, "Only checkpoint-backed jobs can resume collection")
        if rec.get("status") in ("queued", "running"):
            raise HTTPException(409, "Job is already running")
        checkpoint_enabled = (
            os.environ.get("PUBLIC_COLLECTION_CHECKPOINTS") == "1"
            or os.environ.get("COLLECTION_ENGINE", "").strip().lower() == "historical"
        )
        if not checkpoint_enabled:
            raise HTTPException(503, "Checkpoint collection mode is disabled")
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "Worker busy; retry later")
        resumed = {
            **rec,
            "status": "queued",
            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        try:
            store(resumed)
            executor.submit(_process_job, job_id, resumed)
        except Exception:
            slots.release()
            store(rec)
            raise
        return {"job_id": job_id, "status": "queued", "resumed": True}


@app.get("/jobs/{job_id}/delivery-plan")
def get_delivery_plan(job_id: str, business_name: str = "") -> dict[str, Any]:
    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    from app.delivery import build_delivery_plan

    is_checkpoint = rec.get("collection_mode") in ("public_checkpoint", "recorded_extractor")
    if is_checkpoint and not m4_approval_enabled():
        return {"status": "blocked_unverified_collection", "job_id": job_id, "assets": []}
    use_m4_db = is_checkpoint or m4_approval_enabled()
    return build_delivery_plan(
        Path(DATA_DIR) / job_id,
        job_id,
        business_name,
        db_path=DB if use_m4_db else None,
        projects_root=DATA_DIR if use_m4_db else None,
    )


@app.post("/jobs/{job_id}/delivery-result")
def post_delivery_result(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    if payload.get("public_sharing") is True or payload.get("visibility") not in (None, "private"):
        raise HTTPException(403, "Private delivery destination required; public sharing is prohibited")
    from app.delivery import build_delivery_plan, finalize_delivery

    is_checkpoint = rec.get("collection_mode") in ("public_checkpoint", "recorded_extractor")
    if is_checkpoint and not m4_approval_enabled():
        raise HTTPException(409, "blocked_unverified_collection")
    use_m4_db = is_checkpoint or m4_approval_enabled()
    job_dir = Path(DATA_DIR) / job_id
    plan = build_delivery_plan(
        job_dir,
        job_id,
        str(payload.get("business_name") or ""),
        db_path=DB if use_m4_db else None,
        projects_root=DATA_DIR if use_m4_db else None,
    )
    if plan["status"] != "ready":
        raise HTTPException(409, plan["status"])
    if payload.get("authorization_confirmed") is not True:
        raise HTTPException(403, "Explicit rights approval required")
    return finalize_delivery(
        job_dir,
        plan,
        list(payload.get("uploads") or []),
        authorization_confirmed=True,
        drive_folder_id=payload.get("drive_folder_id"),
        drive_folder_link=payload.get("drive_folder_link"),
        public_sharing=False,
        visibility="private",
        report_uploads=list(payload.get("report_uploads") or []) if isinstance(payload.get("report_uploads"), list) else None,
        subfolders=payload.get("subfolders") if isinstance(payload.get("subfolders"), dict) else None,
    )


@app.get("/jobs/{job_id}/delivery")
def get_delivery(job_id: str) -> dict[str, Any]:
    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    from app.delivery import json_load

    path = Path(DATA_DIR) / job_id / "delivery.json"
    if path.is_file():
        return json_load(path)
    return {"job_id": job_id, "status": "not_started", "note": "No delivery recorded for this job."}


@app.get("/jobs/{job_id}/curation")
def get_curation(job_id: str) -> dict[str, Any]:
    """Serve the Phase 5 curation result (MODE B envelope) for a job."""
    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    curation_file = Path(DATA_DIR) / job_id / "curation.json"
    if curation_file.is_file():
        try:
            return json.loads(curation_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(500, f"Curation unreadable: {exc}") from exc
    return {
        "job_id": job_id,
        "status": rec.get("ai_curation", {}).get("status", "not_requested")
        if isinstance(rec.get("ai_curation"), dict)
        else "not_requested",
        "note": "No AI curation was produced for this job (not requested, provider "
        "unavailable, or analysis failed). See the job record's ai_curation field.",
    }


@app.get("/jobs/{job_id}/manifest")
def get_manifest(job_id: str) -> dict[str, Any]:
    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    manifest_file = Path(DATA_DIR) / job_id / "manifest.json"
    if manifest_file.is_file():
        try:
            return json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(500, f"Manifest unreadable: {exc}") from exc
    return {
        "job_id": job_id,
        "status": rec.get("status"),
        "assets": [],
        "note": "No manifest generated for this job yet (pre-Phase-2 job, or extraction not finished).",
    }


@app.get("/qa/collection/{job_id}/poll")
def qa_collection_poll(job_id: str):
    from app.collection_approval import review_state
    if not getattr(app.state, "offline_approval_qa", False):
        raise HTTPException(404, "QA adapter unavailable")
    with connect() as con:
        con.execute("BEGIN")
        state = review_state(con, DATA_DIR, job_id)[0]
        claimed = con.execute("SELECT 1 FROM ai_execution_claims WHERE job=? AND revision=?",
                              (job_id, state["revision"])).fetchone()
    return {"job_id": job_id, "state": state["state"], "etag": state["etag"],
            "start_authorized": state["start_authorized"], "claimed": bool(claimed)}


@app.post("/qa/collection/{job_id}/resume")
def qa_collection_resume(job_id: str, body: dict):
    from app.ai.pipeline import run_started_curation
    from app.ai.provider import AIProviderConfig, MockVisionProvider
    from app.ai_execution import InferenceHeld
    if not getattr(app.state, "offline_approval_qa", False):
        raise HTTPException(404, "QA adapter unavailable")
    if set(body) != {"etag"} or not isinstance(body["etag"], str):
        raise HTTPException(400, "Expected the observed Start AI etag")
    consent = getattr(app.state, "offline_processing_consent", {}).get(job_id, {})
    try:
        outcome = run_started_curation(DATA_DIR / job_id, job_id, processing_consent=consent,
                                      start_etag=body["etag"], provider=MockVisionProvider(),
                                      config=AIProviderConfig(provider="mock"))
    except InferenceHeld:
        raise HTTPException(409, "Execution held by worker") from None
    return {"job_id": job_id, "status": outcome.status, "mock": True}
