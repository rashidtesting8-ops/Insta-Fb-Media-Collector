"""Phase 7 orchestration tests (offline, deterministic).

All fixtures are clearly labeled as integration fixtures and must never be
released as genuine AI curation or customer-approved deliverables.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.orchestration import (
    MAX_POLL_ATTEMPTS,
    STAGES,
    TERMINAL_STAGES,
    derive_stage,
    find_idempotent_job,
    poll_decision,
    validate_intake,
)

FIXTURE = "FIXTURE-ONLY-PHASE7-ORCHESTRATION"


def _job(status="queued", **kw) -> dict:
    base = {"job_id": "job7", "status": status, "updated_at": "2026-01-01T00:00:00+00:00",
            "run_ai_curation": False, "sources": {}, "idempotency_key": None}
    base.update(kw)
    return base


def test_stages_list_matches_spec():
    assert "delivering" in STAGES
    assert TERMINAL_STAGES == {"awaiting_rights_approval", "completed", "partial_success", "blocked", "failed"}


def test_derive_stage_queued_collecting_preprocessing():
    assert derive_stage(_job("queued"))["stage"] == "queued"
    assert derive_stage(_job("running", pipeline_stage="collecting"))["stage"] == "collecting"
    assert derive_stage(_job("running", pipeline_stage="preprocessing"))["stage"] == "preprocessing"
    assert derive_stage(_job("running", pipeline_stage="ai_processing"))["stage"] == "ai_processing"
    assert derive_stage(_job("running"))["stage"] in ("collecting", "preprocessing")


def test_derive_stage_extraction_complete_branches():
    r = _job("extraction_complete", run_ai_curation=False)
    assert derive_stage(r)["stage"] == "completed"
    r = _job("extraction_complete", run_ai_curation=True, ai_curation={"status": "completed"})
    assert derive_stage(r)["stage"] == "awaiting_rights_approval"
    r = _job("extraction_complete", run_ai_curation=True, ai_curation={"status": "failed"})
    assert derive_stage(r)["stage"] == "partial_success"


def test_derive_stage_failures_and_interrupted():
    assert derive_stage(_job("failed", error="boom"))["stage"] == "failed"
    assert derive_stage(_job("partial_success", error="x"))["stage"] == "partial_success"
    assert derive_stage(_job("interrupted"))["stage"] == "blocked"


def test_derive_stage_delivery_overrides():
    j = _job("extraction_complete", run_ai_curation=True, ai_curation={"status": "completed"})
    d = {"status": "completed"}
    assert derive_stage(j, d)["stage"] == "completed"
    d = {"status": "blocked_rights_review"}
    assert derive_stage(j, d)["stage"] == "awaiting_rights_approval"
    d = {"status": "failed", "errors": ["x"]}
    assert derive_stage(j, d)["stage"] == "blocked"
    d = {"status": "uploading"}
    assert derive_stage(j, d)["stage"] == "delivering"


def test_derive_stage_terminal_flags():
    for st in ("completed", "failed", "blocked", "partial_success", "awaiting_rights_approval"):
        r = _job("extraction_complete", run_ai_curation=False) if st == "completed" else _job("failed")
        # force via delivery for blocked/completed checks
        pass
    assert derive_stage(_job("queued"))["terminal"] is False
    assert derive_stage(_job("failed"))["terminal"] is True


def test_validate_intake_valid_website_only():
    out = validate_intake({"website_url": "https://example.com/", "max_pages": 5})
    assert out["valid"] is True
    assert out["errors"] == []
    assert out["resume"] is False
    assert out["create"]["website_url"] == "https://example.com/"


def test_validate_intake_invalid_urls():
    out = validate_intake({"website_url": "ftp://example.com"})
    assert out["valid"] is False
    assert any("scheme" in e for e in out["errors"])
    out = validate_intake({"website_url": "not a url"})
    assert out["valid"] is False
    out = validate_intake({"website_url": "https://u:p@example.com"})
    assert out["valid"] is False
    assert any("credentials" in e for e in out["errors"])
    out = validate_intake({"instagram_url": "https://example.com/x"})
    assert out["valid"] is False  # website required
    assert any("website_url: required" in e for e in out["errors"])


def test_validate_intake_multi_source():
    out = validate_intake({
        "website_url": "https://a.test/",
        "instagram_url": "https://www.instagram.com/x/",
        "facebook_url": "https://www.facebook.com/x",
    })
    assert out["valid"] is True
    c = out["create"]
    assert c["instagram_url"].startswith("https://")
    assert c["facebook_url"].startswith("https://")


def test_validate_intake_resume_mode():
    out = validate_intake({"job_id": "a" * 32})
    assert out["valid"] is True
    assert out["resume"] is True
    assert out["job_id"] == "a" * 32
    assert out["create"] is None


def test_validate_intake_invalid_job_id():
    out = validate_intake({"job_id": "zzz"})
    assert out["valid"] is False


def test_validate_intake_preferences_clamped_and_invalid():
    out = validate_intake({"website_url": "https://x.test/", "max_pages": 0, "max_pages2": 1})
    # max_pages clamped to 1
    assert out["valid"] is True
    assert out["create"]["max_pages"] == 1
    out = validate_intake({"website_url": "https://x.test/", "max_pages": "abc"})
    assert out["valid"] is False


def test_validate_intake_idempotency_derived():
    a = validate_intake({"website_url": "https://x.test/"})
    b = validate_intake({"website_url": "https://x.test/"})
    assert a["idempotency_key"] == b["idempotency_key"]
    c = validate_intake({"website_url": "https://x.test/", "idempotency_key": "my-key"})
    assert c["idempotency_key"] == "my-key"


def test_find_idempotent_job_reuse_and_expiry():
    now = "2026-01-02T00:00:00+00:00"
    records = [
        {"job_id": "j1", "idempotency_key": "k", "created_at": "2026-11-21T00:00:00+00:00"},
        {"job_id": "j2", "idempotency_key": "other", "created_at": "2026-11-21T00:00:00+00:00"},
    ]
    assert find_idempotent_job(records, "k") == "j1"
    assert find_idempotent_job(records, "missing") is None
    old = [{"job_id": "j3", "idempotency_key": "k", "created_at": "2020-01-01T00:00:00+00:00"}]
    assert find_idempotent_job(old, "k") is None


def test_poll_decision():
    assert poll_decision(0, 5, terminal=False)["action"] == "wait"
    assert poll_decision(5, 5, terminal=False)["action"] == "timeout"
    assert poll_decision(0, 5, terminal=True)["action"] == "stop"


def _workflow_path() -> Path | None:
    here = Path(__file__).resolve()
    candidates = []
    for d in range(1, 5):
        if len(here.parents) > d:
            candidates.append(here.parents[d] / "n8n-media" / "07-pipeline-orchestration.json")
    candidates.append(Path("/srv/n8n-media/07-pipeline-orchestration.json"))
    candidates.append(Path("/data/n8n-workflows/07-pipeline-orchestration.json"))
    for p in candidates:
        if p.is_file():
            return p
    return None


def test_workflow_json_structure():
    path = _workflow_path()
    if path is None:
        pytest.skip("workflow file not present in this runtime layout")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("active") is False
    assert data.get("settings", {}).get("executionOrder") == "v1"
    names = {n["name"] for n in data["nodes"]}
    required = {
        "Pipeline Intake Webhook", "Validate Intake", "Intake Valid?",
        "Create Worker Job", "Poll Job Status", "Map Stage", "Terminal?",
        "Rights Gate", "Authorization Confirmed?", "Get Delivery Plan",
        "Plan Ready?", "Create Business Drive Folder", "Persist Delivery Ledger",
        "Respond Final Status", "Respond Invalid Input", "Status Webhook",
    }
    assert required <= names, f"missing nodes: {required - names}"
    # No secrets in workflow JSON.
    text = path.read_text(encoding="utf-8")
    for pat in (r"AI_API_KEY\s*[:=]", r"ghp_[A-Za-z0-9]", r"password\s*[:=]\s*['\"][^'\"]{6,}"):
        assert not re.search(pat, text, re.IGNORECASE), f"secret-like pattern found: {pat}"
    # Credential referenced by name only (placeholder id).
    assert "Media Worker API Token" in text or "GOOGLE_DRIVE_CREDENTIAL_ID" in text
    # Bounded polling constant present.
    assert str(MAX_POLL_ATTEMPTS) in text
    # Wait node present.
    assert any(n.get("type") == "n8n-nodes-base.wait" for n in data["nodes"])
    # Connections reference existing nodes.
    node_names = names
    for src, outs in data["connections"].items():
        assert src in node_names
        for branch in outs.get("main", []):
            for c in branch:
                assert c["node"] in node_names


def test_workflow_has_two_webhooks():
    path = _workflow_path()
    if path is None:
        pytest.skip("workflow file not present in this runtime layout")
    data = json.loads(path.read_text(encoding="utf-8"))
    webhooks = [n for n in data["nodes"] if n.get("type") == "n8n-nodes-base.webhook"]
    paths = { (n.get("parameters") or {}).get("path") for n in webhooks }
    assert {"pipeline-intake", "pipeline-status"} <= paths