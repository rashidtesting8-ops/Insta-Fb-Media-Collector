"""Offline regression tests for the M5 Stage 2C.8 disposable QA worker harness."""

from datetime import UTC, datetime
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import socket
import uuid

from fastapi import HTTPException
from PIL import Image
import pytest

from app import delivery, named_auth, orchestration
from app.publication import frozen_window, publication_evidence
from tests.test_phase9_security import request, worker

QA_WORKER_PATH = Path(__file__).resolve().parents[3] / "n8n-media" / "qa-fixtures" / "worker.py"
SPEC = importlib.util.spec_from_file_location("pgm_qa_fixture_worker", QA_WORKER_PATH)
QA_WORKER = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(QA_WORKER)

ASSERTION_SECRET = "qa-harness-test-assertion-key-" * 2


def _signed_headers(action, body, session=""):
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    import base64
    import hmac
    import time

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
    mac = hmac.digest(ASSERTION_SECRET.encode("utf-8"), encoded, "sha256").hex()
    return raw, ((b"x-named-session", session.encode()), (b"x-named-assertion", encoded + b"." + mac.encode()))


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("QA fixture test attempted network I/O"))


def test_provision_qa_admins_bootstraps_once_and_creates_distinct_admin_b(worker, monkeypatch):
    monkeypatch.setenv("NAMED_ASSERTION_SECRET", ASSERTION_SECRET)
    principal_a, principal_b = QA_WORKER.provision_qa_admins(
        worker.DB,
        assertion_secret=ASSERTION_SECRET,
        password_a="qa-synthetic-pass-a",
        password_b="qa-synthetic-pass-b",
    )
    assert principal_a != principal_b

    # Bootstrap singleton must remain consumed after provisioning.
    with pytest.raises(HTTPException) as exc:
        named_auth.bootstrap(worker.DB, "qa-admin-c", "qa-synthetic-pass-c", verified_operator="qa-fixture")
    assert exc.value.status_code == 409

    # Bootstrap login session must be logged out cleanly; both admins can authenticate independently.
    with worker.connect() as con:
        assert con.execute("SELECT count(*) FROM named_sessions").fetchone() == (0,)
        names = sorted(r[0] for r in con.execute("SELECT name FROM named_accounts WHERE active=1 AND role='admin'"))
        assert names == ["qa-admin-a", "qa-admin-b"]


def test_enable_disposable_m4_qa_refuses_non_disposable_root(worker, tmp_path):
    unrelated = tmp_path / "other-root"
    unrelated.mkdir()
    with pytest.raises(RuntimeError, match="outside disposable storage"):
        QA_WORKER.enable_disposable_m4_qa(worker, unrelated)


def test_m4_qa_worker_approval_start_ai_manifest_integrity_and_rights_filter(worker, monkeypatch):
    monkeypatch.setenv("NAMED_ASSERTION_SECRET", ASSERTION_SECRET)
    # Match real service.py / worker.py layout: DATA_DIR/_system/jobs.sqlite (required by ai_execution.execution).
    system_db = worker.DATA_DIR / "_system" / "jobs.sqlite"
    system_db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(worker, "DB", system_db)
    QA_WORKER.enable_disposable_m4_qa(worker, worker.DATA_DIR.parent)
    principal_a, _ = QA_WORKER.provision_qa_admins(
        worker.DB,
        assertion_secret=ASSERTION_SECRET,
        password_a="qa-synthetic-pass-a",
        password_b="qa-synthetic-pass-b",
    )

    def named_http(action, body, session=""):
        raw, headers = _signed_headers(action, body, session)
        status, _, data = request(worker.app, "/named/" + action, "POST", "test-dashboard", (raw,), headers)
        return status, json.loads(data)

    status_a, login_a = named_http("login", {"name": "qa-admin-a", "password": "qa-synthetic-pass-a"})
    status_b, login_b = named_http("login", {"name": "qa-admin-b", "password": "qa-synthetic-pass-b"})
    assert status_a == 200 and status_b == 200
    session_a, session_b = login_a["session"], login_b["session"]

    # Create job with Admin A attribution and two fixture assets (approved.jpg + review.jpg).
    job_id = "c" * 32
    job_dir = worker.DATA_DIR / job_id
    (job_dir / "website").mkdir(parents=True)
    window = frozen_window(datetime(2026, 6, 1, tzinfo=UTC))

    assets = []
    for name, color in (("approved.jpg", "#ebc168"), ("review.jpg", "#135e86")):
        img_path = job_dir / "website" / name
        Image.new("RGB", (320, 256), color).save(img_path, format="JPEG", quality=90)
        digest = sha256(img_path.read_bytes()).hexdigest()
        source_url = f"http://127.0.0.1:8790/partial/{name}"
        assets.append({
            "asset_id": f"web_{name.split('.')[0]}",
            "job_id": job_id,
            "platform": "website",
            "type": "image",
            "source_url": source_url,
            "filename": name,
            "local_path": f"website/{name}",
            "mime": "image/jpeg",
            "bytes": img_path.stat().st_size,
            "sha256": digest,
            "post_date": window["created_at"],
            "date_status": "verified",
            "publication_evidence": publication_evidence(
                window["created_at"], source_url=source_url, field="datePublished", attribution="media",
            ),
            "rights_status": "unverified",
            "extraction_status": "downloaded",
            "preprocessing": {"outcome": "processed"},
        })

    manifest = {"job_id": job_id, "website_url": "http://127.0.0.1:8790/partial/", "assets": assets, **window}
    (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Mark explicitly verified synthetic fixture asset BEFORE revision snapshot acceptance.
    verified = QA_WORKER.apply_fixture_rights(job_dir, guarded=False)
    assert verified == ["web_approved"]

    record = {
        "job_id": job_id,
        "status": "extraction_complete",
        "website_url": "http://127.0.0.1:8790/partial/",
        "run_ai_curation": True,
        "ai_curation": {"status": "awaiting_collection_approval"},
        "submitter_principal_id": principal_a,
        "sources": {"website": {"status": "completed"}},
        **window,
    }
    worker.store(record)
    worker.store(record, collection_base=0)
    consent = QA_WORKER.record_fixture_processing_consent(worker, job_dir, job_id)
    assert set(consent) == {"web_approved", "web_review"}

    # Extraction-complete + awaiting_collection_approval must map to stage='blocked'.
    stage = orchestration.derive_stage(worker.fetch(job_id))
    assert stage["job_status"] == "extraction_complete"
    assert stage["ai_status"] == "awaiting_collection_approval"
    assert stage["stage"] == "blocked"

    # Admin A cannot self-approve.
    st, panel_a = named_http("collection-panel", {"job_id": job_id}, session_a)
    assert st == 200 and panel_a["state"] == "pending" and panel_a["permissions"]["approve"] is False
    st, _ = named_http(
        "collection-approve",
        {"job_id": job_id, "if_match": panel_a["etag"], "request_key": "self-approve", "reason": "Denied self-approval"},
        session_a,
    )
    assert st == 403

    # Admin B approves; resume must remain blocked until independent Start AI.
    st, panel_b = named_http("collection-panel", {"job_id": job_id}, session_b)
    assert st == 200 and panel_b["permissions"]["approve"] is True
    manifest_sha_before = panel_b["manifest_sha256"]
    st, approved = named_http(
        "collection-approve",
        {"job_id": job_id, "if_match": panel_b["etag"], "request_key": "admin-b-approve", "reason": "Reviewed gaps"},
        session_b,
    )
    assert st == 200 and approved["state"] == "approved" and approved["start_authorized"] is False

    poll_status, _, poll_body = request(worker.app, f"/qa/collection/{job_id}/poll", "GET", "test-admin")
    assert poll_status == 200
    poll_data = json.loads(poll_body)
    assert poll_data["state"] == "approved" and poll_data["start_authorized"] is False

    resume_before_start, _, _ = request(
        worker.app, f"/qa/collection/{job_id}/resume", "POST", "test-admin",
        (json.dumps({"etag": poll_data["etag"]}).encode(),),
    )
    assert resume_before_start == 409
    assert not (job_dir / "curation.json").exists()

    # Separate authorized Start AI unlocks /qa/collection/{id}/resume.
    st, started = named_http(
        "collection-start",
        {"job_id": job_id, "if_match": approved["etag"], "request_key": "admin-a-start"},
        session_a,
    )
    assert st == 200 and started["start_authorized"] is True

    poll_status, _, poll_body = request(worker.app, f"/qa/collection/{job_id}/poll", "GET", "test-admin")
    poll_data = json.loads(poll_body)
    assert poll_status == 200 and poll_data["start_authorized"] is True and poll_data["claimed"] is False

    resume_status, _, resume_body = request(
        worker.app, f"/qa/collection/{job_id}/resume", "POST", "test-admin",
        (json.dumps({"etag": poll_data["etag"]}).encode(),),
    )
    assert resume_status == 200
    assert json.loads(resume_body) == {"job_id": job_id, "status": "completed", "mock": True}
    assert (job_dir / "curation.json").is_file()

    # Manifest SHA-256 and approval state must remain valid (never mutated/stale).
    st, final_panel = named_http("collection-panel", {"job_id": job_id}, session_b)
    assert st == 200 and final_panel["state"] == "approved"
    assert final_panel["manifest_sha256"] == manifest_sha_before

    # Rights filter must allow ONLY approved.jpg and exclude review.jpg.
    plan = delivery.build_delivery_plan(job_dir, job_id, allow_fixture=True)
    assert plan["status"] == "ready"
    assert [item["asset_id"] for item in plan["pending_uploads"]] == ["web_approved"]
    assert plan["pending_rights_ids"] == ["web_review"]


def test_worker_diagnostics_redacts_secrets_and_raises_on_write_failure(tmp_path):
    secret = "ephemeral-worker-secret-0123456789"
    QA_WORKER._diag(
        "unit_check",
        {
            "password": "plain-password",
            "cookie": "__Host-pgm_named=xyz987",
            "note": f"contains {secret}",
            "safe": "extraction_complete",
        },
        root=tmp_path,
        run_id="run-001",
        strict=True,
        extra_secrets=(secret,),
    )
    log_file = tmp_path / "run-001" / "worker.jsonl"
    raw = log_file.read_text(encoding="utf-8")
    parsed = json.loads(raw.strip())
    assert parsed["password"] == "[REDACTED]"
    assert parsed["cookie"] == "[REDACTED]"
    assert parsed["note"] == "[REDACTED]"
    assert parsed["safe"] == "extraction_complete"
    assert secret not in raw and "plain-password" not in raw and "xyz987" not in raw

    occupied = tmp_path / "file-blocking-dir"
    occupied.write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="QA diagnostics write failed"):
        QA_WORKER._diag("fail_check", {}, root=occupied, run_id="run-fail", strict=True)
