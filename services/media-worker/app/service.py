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
import hmac
import json
import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Pet Grooming Media Worker", version="0.1.0-phase1")

executor = ThreadPoolExecutor(max_workers=2)
slots = BoundedSemaphore(4)
lock = Lock()

DATA_DIR = Path(os.environ.get("PROJECTS_DIR", "./storage/jobs"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "_system" / "jobs.sqlite"
DB.parent.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    con.execute(
        "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    return con


def store(record: dict[str, Any]) -> None:
    with lock, connect() as con:
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
    website_url: str = Field(min_length=1, description="Required pet grooming business website URL")
    instagram_url: str | None = Field(default=None, description="Optional public Instagram URL")
    facebook_url: str | None = Field(default=None, description="Optional public Facebook Page URL")
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



@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


_MEDIA_WORKER_TOKEN = os.environ.get("MEDIA_WORKER_TOKEN", "").strip()


@app.get("/status")
def status(request: Request) -> object:
    """Authenticated worker status (used by the n8n Phase 1 probe workflow).

    Fails closed when MEDIA_WORKER_TOKEN is unset or mismatched; constant-time
    comparison adapted from the legacy `src/engine/auth.py`.
    """
    supplied = request.headers.get("x-api-key", "").strip()
    if not _MEDIA_WORKER_TOKEN or not hmac.compare_digest(supplied, _MEDIA_WORKER_TOKEN):
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized: MEDIA_WORKER_TOKEN required"},
        )
    return {
        "ok": True,
        "service": "media-worker",
        "phase": 1,
        "auth": "verified",
        "token_configured": True,
    }


@app.post("/jobs", status_code=202)
def create_job(req: CreateJobRequest) -> dict[str, Any]:
    if not slots.acquire(blocking=False):
        raise HTTPException(429, "Worker busy; retry later")
    if req.idempotency_key:
        try:
            from app.orchestration import find_idempotent_job

            existing_id = find_idempotent_job(_all_job_records(), str(req.idempotency_key))
        except Exception:  # noqa: BLE001
            existing_id = None
        if existing_id:
            slots.release()
            rec = fetch(existing_id)
            return {
                "job_id": existing_id,
                "status": (rec or {}).get("status", "queued"),
                "idempotent_reuse": True,
            }
    job_id = uuid.uuid4().hex
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": "queued",
        "website_url": req.website_url,
        "instagram_url": req.instagram_url,
        "facebook_url": req.facebook_url,
        "lookback_days": req.lookback_days,
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
        "updated_at": now_iso,
        "sources": {
            "website": {"status": "queued"},
            "instagram": {"status": "queued" if req.instagram_url else "not_requested"},
            "facebook": {"status": "queued" if req.facebook_url else "not_requested"},
        },
    }
    store(payload)
    try:
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
        sources: dict[str, Any] = dict(payload.get("sources") or {})
        sources["website"] = {"status": "running"}
        for platform in ("instagram", "facebook"):
            sources[platform] = {
                "status": "running" if payload.get(f"{platform}_url") else "not_requested"
            }
        store({**payload, "status": "running", "sources": sources, "pipeline_stage": "collecting", "updated_at": _now()})

        job_dir = Path(DATA_DIR) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

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
        if social_sections or website_doc is None:
            try:
                merged = merge_social_into_manifest(
                    website_doc,
                    social_sections,
                    job_id=job_id,
                    lookback_days=int(payload.get("lookback_days") or 365),
                    permission_confirmed=bool(payload.get("social_permission_confirmed")),
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

                cutoff = (
                    _dt.datetime.now(_dt.timezone.utc)
                    - _dt.timedelta(days=int(payload.get("lookback_days") or 365))
                ).date().isoformat()
                pre = run_media_preprocessing(job_dir, job_id, cutoff_date=cutoff)
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

        if payload.get("run_ai_curation") and manifest_file.is_file():
            store({**payload, "status": "running", "sources": {**sources, "website": website_status}, "pipeline_stage": "ai_processing", "updated_at": _now()})
        # --- Phase 5 AI curation (optional; isolated; honest failure) ------
        ai_record: dict[str, Any] = {"status": "not_requested"}
        if payload.get("run_ai_curation") and manifest_file.is_file():
            try:
                from app.ai.pipeline import run_ai_curation as run_ai
                from app.ai.provider import AIProviderConfig

                ai_config = AIProviderConfig.from_env()
                ai = run_ai(
                    job_dir,
                    job_id,
                    business_context=payload.get("business_context") or {},
                    max_images=int(payload.get("max_selected_images") or 30),
                    max_videos=int(payload.get("max_selected_videos") or 10),
                    batch_size=max(1, ai_config.batch_size),
                )
                ai_record = ai.as_dict()
                ai_record["curation_path"] = str(job_dir / "curation.json")
            except Exception as exc:  # noqa: BLE001 — AI failure is recorded, extraction preserved
                ai_record = {"status": "failed", "error": str(exc)[:240]}
        elif payload.get("run_ai_curation"):
            ai_record = {"status": "failed", "error": "no manifest available for AI curation"}

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
        store(record)
    except Exception as exc:  # noqa: BLE001 — record failure and keep worker up
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


@app.get("/jobs/{job_id}/file")
def get_job_file(job_id: str, path: str) -> Any:
    from fastapi.responses import FileResponse

    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    job_dir = (Path(DATA_DIR) / job_id).resolve()
    if not job_dir.is_dir():
        raise HTTPException(404, "Job directory missing")
    # Strict traversal guard: only files inside the job directory.
    candidate = (job_dir / (path or "")).resolve()
    try:
        candidate.relative_to(job_dir)
    except ValueError:
        raise HTTPException(400, "Invalid path")
    if not candidate.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(candidate)




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

@app.get("/jobs/{job_id}/delivery-plan")
def get_delivery_plan(job_id: str, business_name: str = "") -> dict[str, Any]:
    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    from app.delivery import build_delivery_plan

    return build_delivery_plan(Path(DATA_DIR) / job_id, job_id, business_name)


@app.post("/jobs/{job_id}/delivery-result")
def post_delivery_result(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    from app.delivery import finalize_delivery, json_load
    from app.manifest import atomic_write_json

    job_dir = Path(DATA_DIR) / job_id
    plan = {}
    if (job_dir / "delivery.json").is_file():
        try:
            plan = json_load(job_dir / "delivery.json")
        except (OSError, json.JSONDecodeError):
            plan = {}
    if not plan.get("assets"):
        try:
            from app.delivery import build_delivery_plan

            plan = build_delivery_plan(job_dir, job_id, str(payload.get("business_name") or ""), allow_fixture=False)
        except Exception:  # noqa: BLE001
            plan = plan or {}
    result = finalize_delivery(
        job_dir,
        plan,
        list(payload.get("uploads") or []),
        authorization_confirmed=bool(payload.get("authorization_confirmed")),
    )
    if payload.get("drive_folder_id") is not None:
        result["drive_folder_id"] = payload.get("drive_folder_id")
    if payload.get("drive_folder_link") is not None:
        result["drive_folder_link"] = payload.get("drive_folder_link")
    atomic_write_json(job_dir / "delivery.json", result)
    return result


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
