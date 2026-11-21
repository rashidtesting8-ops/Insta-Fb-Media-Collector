"""Phase 7 orchestration helpers: intake validation, worker-status reconciliation,
idempotent job lookup and polling decisions.

n8n drives the business process through HTTP endpoints; this module keeps the
status reconciliation and validation rules single-sourced and unit-testable
offline. It never performs crawling, AI, delivery or external I/O.
"""

from __future__ import annotations

import datetime as _dt
import re
import urllib.parse
from hashlib import sha256
from typing import Any, Iterable, Mapping

# Phase 7 pipeline stages (must match the n8n workflow's routing).
STAGES: tuple[str, ...] = (
    "queued",
    "collecting",
    "preprocessing",
    "ai_processing",
    "awaiting_rights_approval",
    "delivering",
    "completed",
    "partial_success",
    "blocked",
    "failed",
)

TERMINAL_STAGES: frozenset[str] = frozenset(
    {"awaiting_rights_approval", "completed", "partial_success", "blocked", "failed"}
)

MAX_POLL_ATTEMPTS: int = 120
POLL_INTERVAL_SECONDS: int = 10


_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*$", re.IGNORECASE)
_HEX32 = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)


def _as_int(value: Any, default: int, lo: int, hi: int) -> int | str:
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "invalid"
    return max(lo, min(hi, n))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off", ""):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _check_url(value: Any, field: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(f"{field}: must be a string URL")
        return None
    v = value.strip()
    if not v:
        return None
    if len(v) > 2048:
        errors.append(f"{field}: URL too long")
        return None
    try:
        parsed = urllib.parse.urlsplit(v)
    except ValueError:
        errors.append(f"{field}: malformed URL")
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        errors.append(f"{field}: URL scheme must be http or https")
        return None
    if not parsed.hostname:
        errors.append(f"{field}: URL must include a hostname")
        return None
    if parsed.username is not None or parsed.password is not None:
        errors.append(f"{field}: URLs must not embed credentials")
        return None
    # Normalize: drop fragments, keep path/query.
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
    )


def validate_intake(payload: Any) -> dict[str, Any]:
    """Validate + normalize an intake body. Returns ``{valid, errors, ...}``.

    Never raises; returns a structured result for n8n and tests.
    """
    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return {"valid": False, "errors": ["body must be a JSON object"], "resume": False}

    job_id_raw = payload.get("job_id")
    resume = False
    job_id: str | None = None
    if job_id_raw is not None and str(job_id_raw).strip() != "":
        jid = str(job_id_raw).strip()
        if not _HEX32.match(jid):
            errors.append("job_id: must be a 32-character hex job id")
        else:
            job_id = jid
            resume = True

    website_url = _check_url(payload.get("website_url"), "website_url", errors)
    instagram_url = _check_url(payload.get("instagram_url"), "instagram_url", errors)
    facebook_url = _check_url(payload.get("facebook_url"), "facebook_url", errors)

    if not resume and not website_url:
        errors.append("website_url: required (or provide job_id to resume)")

    lookback_days = _as_int(payload.get("lookback_days", 365), 365, 1, 366)
    if lookback_days == "invalid":
        errors.append("lookback_days: must be an integer 1..366")
        lookback_days = 365
    max_pages = _as_int(payload.get("max_pages", 10), 10, 1, 25)
    if max_pages == "invalid":
        errors.append("max_pages: must be an integer 1..25")
        max_pages = 10
    max_media = _as_int(payload.get("max_media", 30), 30, 1, 80)
    if max_media == "invalid":
        errors.append("max_media: must be an integer 1..80")
        max_media = 30
    max_selected_images = _as_int(payload.get("max_selected_images", 30), 30, 1, 200)
    if max_selected_images == "invalid":
        errors.append("max_selected_images: must be an integer 1..200")
        max_selected_images = 30
    max_selected_videos = _as_int(payload.get("max_selected_videos", 10), 10, 1, 100)
    if max_selected_videos == "invalid":
        errors.append("max_selected_videos: must be an integer 1..100")
        max_selected_videos = 10
    max_social_files = _as_int(payload.get("max_social_files", 20), 20, 1, 60)
    if max_social_files == "invalid":
        errors.append("max_social_files: must be an integer 1..60")
        max_social_files = 20

    social_permission_confirmed = _as_bool(payload.get("social_permission_confirmed"), False)
    run_ai_curation = _as_bool(payload.get("run_ai_curation"), False)
    authorization_confirmed = payload.get("authorization_confirmed") is True

    business_name = payload.get("business_name", "")
    if not isinstance(business_name, str):
        errors.append("business_name: must be a string")
        business_name = ""
    business_name = business_name.strip()[:120]

    business_context = payload.get("business_context")
    if business_context is not None and not isinstance(business_context, Mapping):
        errors.append("business_context: must be an object")
        business_context = None
    if isinstance(business_context, Mapping):
        business_context = dict(business_context)

    user_selection_notes = payload.get("user_selection_notes")
    if user_selection_notes is not None and not isinstance(user_selection_notes, str):
        errors.append("user_selection_notes: must be a string")
        user_selection_notes = None
    if isinstance(user_selection_notes, str):
        user_selection_notes = user_selection_notes[:2000]

    idempotency_key = payload.get("idempotency_key")
    if idempotency_key is None or str(idempotency_key).strip() == "":
        seed = f"{website_url or ''}|{instagram_url or ''}|{facebook_url or ''}|{business_name}"
        idempotency_key = "auto-" + sha256(seed.encode()).hexdigest()
    else:
        idempotency_key = str(idempotency_key).strip()
        if len(idempotency_key) > 128:
            errors.append("idempotency_key: too long (max 128)")
            idempotency_key = idempotency_key[:128]

    valid = not errors
    result: dict[str, Any] = {
        "valid": valid,
        "errors": errors,
        "resume": resume,
        "job_id": job_id,
        "idempotency_key": idempotency_key,
        "authorization_confirmed": authorization_confirmed,
        "business_name": business_name,
    }
    if valid and not resume:
        result["create"] = {
            "website_url": website_url,
            "instagram_url": instagram_url,
            "facebook_url": facebook_url,
            "lookback_days": lookback_days,
            "max_pages": max_pages,
            "max_media": max_media,
            "max_selected_images": max_selected_images,
            "max_selected_videos": max_selected_videos,
            "max_social_files": max_social_files,
            "social_permission_confirmed": social_permission_confirmed,
            "run_ai_curation": run_ai_curation,
            "business_context": business_context,
            "user_selection_notes": user_selection_notes,
            "idempotency_key": idempotency_key,
        }
    else:
        result["create"] = None
    return result


def find_idempotent_job(records: Iterable[Mapping[str, Any]], key: str) -> str | None:
    """Return an existing job id for ``key`` if present and fresh (< 48h)."""
    if not key:
        return None
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48)
    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        if str(rec.get("idempotency_key") or "") != key:
            continue
        created = rec.get("created_at")
        try:
            created_dt = _dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            created_dt = None
        if created_dt is not None and created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=_dt.timezone.utc)
        if created_dt is not None and created_dt < cutoff:
            continue
        jid = rec.get("job_id")
        if isinstance(jid, str) and jid:
            return jid
    return None


def derive_stage(job: Mapping[str, Any], delivery: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Reconcile a worker job record (+ optional delivery ledger) into a Phase 7 stage."""
    if not isinstance(job, Mapping):
        return {"stage": "failed", "terminal": True, "job_status": None,
                "detail": "invalid job record", "delivery_status": None}

    job_status = job.get("status")
    run_ai = bool(job.get("run_ai_curation"))
    ai_record = job.get("ai_curation") if isinstance(job.get("ai_curation"), Mapping) else {}
    ai_status = (ai_record or {}).get("status") if isinstance(ai_record, Mapping) else None
    delivery_status = None
    if isinstance(delivery, Mapping):
        delivery_status = delivery.get("status")

    stage = "failed"
    detail = ""

    if job_status == "queued":
        stage, detail = "queued", "Job queued for worker."
    elif job_status == "running":
        ps = job.get("pipeline_stage")
        if ps in ("collecting", "preprocessing", "ai_processing"):
            stage, detail = ps, f"Worker running: {ps}."
        else:
            # Infer from source statuses if no explicit pipeline_stage.
            sources = job.get("sources") or {}
            if any(isinstance(v, Mapping) and v.get("status") in ("queued", "running")
                   for v in sources.values()):
                stage, detail = "collecting", "Worker collecting media."
            else:
                stage, detail = "preprocessing", "Worker preprocessing media."
    elif job_status == "interrupted":
        stage, detail = "blocked", "Worker interrupted (restart); resubmit to retry."
    elif job_status == "failed":
        stage, detail = "failed", str(job.get("error") or "Extraction failed for all attempted sources.")
    elif job_status == "partial_success":
        stage, detail = "partial_success", str(job.get("error") or "Partial extraction success.")
    elif job_status == "extraction_complete":
        if run_ai:
            if ai_status == "completed":
                stage, detail = "awaiting_rights_approval", "AI curation completed; awaiting rights approval."
            else:
                stage, detail = "partial_success", "AI curation requested but not completed."
        else:
            stage, detail = "completed", "Extraction complete; AI curation not requested."
    else:
        stage, detail = "blocked", f"Unknown worker status: {job_status!r}."

    # Delivery overrides (only meaningful once a curation/delivery ledger exists).
    if isinstance(delivery, Mapping):
        if delivery_status == "completed":
            stage, detail = "completed", "Drive delivery completed."
        elif delivery_status == "blocked_rights_review":
            stage, detail = "awaiting_rights_approval", "Rights approval required before delivery."
        elif delivery_status == "partial_success":
            stage, detail = "partial_success", "Partial Drive delivery success."
        elif delivery_status == "failed":
            stage, detail = "blocked", "Drive delivery failed; upstream results preserved."
        elif delivery_status == "uploading":
            stage, detail = "delivering", "Drive delivery in progress."

    # Orchestration marker can surface "delivering" even without a finalized ledger.
    orch = job.get("orchestration")
    if isinstance(orch, Mapping) and orch.get("stage") == "delivering" and delivery_status not in (
        "completed", "partial_success", "failed",
    ):
        stage, detail = "delivering", str(orch.get("detail") or "Drive delivery in progress.")

    terminal = stage in TERMINAL_STAGES
    return {
        "job_id": job.get("job_id"),
        "stage": stage,
        "terminal": terminal,
        "job_status": job_status,
        "detail": detail,
        "ai_status": ai_status,
        "delivery_status": delivery_status,
        "updated_at": job.get("updated_at"),
    }


def poll_decision(attempt: int, max_attempts: int, terminal: bool) -> dict[str, Any]:
    """Return the next polling action for an n8n loop."""
    if terminal:
        return {"action": "stop", "reason": "terminal"}
    if attempt >= max_attempts:
        return {"action": "timeout", "reason": "polling_timeout", "attempt": attempt}
    return {"action": "wait", "reason": "in_progress", "attempt": attempt}
