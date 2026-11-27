"""Durable Stage 3 authorization with real SQLite transactions and synthetic providers."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from PIL import Image

from app import ai_execution, collection_approval
from app.ai.pipeline import run_ai_curation, run_started_curation
from app.ai.provider import AIProviderConfig, MockVisionProvider, OpenAICompatibleProvider
from app.manifest import atomic_write_json
from app.publication import frozen_window, publication_evidence
from tests.test_named_auth import identity
from tests.test_phase9_security import worker


def ledger_setup(worker, identity, job, monkeypatch):
    actor, invoke = identity
    db = worker.DATA_DIR / "_system" / "jobs.sqlite"
    db.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(db)) as copied, worker.connect() as original:
        original.backup(copied)
        collection_approval.migrate(copied)
    monkeypatch.setattr(worker, "DB", db)
    monkeypatch.setattr(worker.app.state, "offline_approval_qa", True, raising=False)
    worker.store({**job, "submitter_principal_id": actor})
    worker.store(worker.fetch(job["job_id"]), collection_base=0)
    owner = invoke("login", {"name": "sample", "password": "synthetic-password"})[1]["session"]
    credentials = {"name": "synthetic-reviewer", "password": "disposable"}
    invoke("create", credentials, owner)
    reviewer = invoke("login", credentials)[1]["session"]

    def action(kind="review"):
        body = {"job_id": job["job_id"]}
        review = invoke("collection-review", body, reviewer)[1]
        if kind == "review":
            return review["etag"]
        body.update(if_match=review["etag"], request_key=uuid4().hex)
        if kind != "start":
            body["reason"] = "Offline test decision"
        status, result = invoke("collection-" + kind, body, reviewer)
        assert status == 200, result
        return result["etag"]
    return action


@pytest.fixture
def setup(worker, identity, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("Unexpected external connection"))
    job = "b" * 32
    folder = worker.DATA_DIR / job
    folder.mkdir()
    image = folder / "test.jpg"
    Image.new("RGB", (48, 48), "white").save(image)
    checksum = sha256(image.read_bytes()).hexdigest()
    window = frozen_window(datetime(2024, 6, 1, tzinfo=UTC))
    url = "https://fixture.test/image"
    asset = dict(asset_id="test", local_path=image.name, source_url=url, type="image", mime="image/jpeg",
                 extraction_status="downloaded", sha256=checksum, rights_status="unverified",
                 preprocessing={"outcome": "processed"}, date_status="verified", post_date=window["created_at"],
                 publication_evidence=publication_evidence(window["created_at"], source_url=url,
                                                          field="datePublished", attribution="media"))
    old = "2020-02-29T12:00:00+00:00"
    expired = {**asset, "asset_id": "expired", "post_date": old,
               "publication_evidence": publication_evidence(old, source_url=url, field="datePublished", attribution="media")}
    manifest = dict(job_id=job, assets=[asset, expired, {**asset, "asset_id": "undated", "date_status": "unknown_date"}], **window)
    atomic_write_json(folder / "manifest.json", manifest)
    action = ledger_setup(worker, identity, dict(job_id=job, status="partial_success", sources={}, **window), monkeypatch)
    return folder, job, {"test": (url, checksum)}, action


@pytest.mark.parametrize("state", ["pending", "approved", "denied", "revoked", "stale", "manifest", "subset",
                                   "cross-job", "no-consent", "historical"])
def test_holds_before_dispatch(setup, state, worker):
    folder, job, consent, action = setup
    tag = action()
    if state != "pending":
        tag = action("deny" if state == "denied" else "approve")
    if state not in ("pending", "approved", "denied"):
        tag = action("start")
    if state == "revoked":
        action("revoke")
    if state == "stale":
        worker.store(worker.fetch(job), collection_base=1)
    if state == "manifest":
        with (folder / "manifest.json").open("ab") as f:
            f.write(b" ")
    if state == "subset":
        (folder / "test.jpg").write_bytes(b"changed")
    if state == "historical":
        job_record = worker.fetch(job)
        del job_record["submitter_principal_id"]
        worker.store(job_record)
    with pytest.raises(ai_execution.InferenceHeld):
        run_started_curation(folder, "a"*32 if state == "cross-job" else job,
                             processing_consent={} if state == "no-consent" else consent, start_etag=tag,
                             provider=MockVisionProvider())
    with worker.connect() as con:
        assert con.execute("SELECT count(*) FROM ai_provider_attempts").fetchone() == (0,)
    assert not (folder / "curation.json").exists()


@pytest.mark.parametrize("revoke", ["none", "during-A", "between", "during-B", "before-publish", "ambiguous"])
def test_fenced_calls_and_publication(setup, worker, monkeypatch, revoke):
    folder, job, consent, action = setup
    action("approve")
    tag = action("start")
    calls = []

    class Simulated(MockVisionProvider):
        def complete(self, prompt, images, tracker):
            mode = "A" if images else "B"
            calls.append(mode)
            if images:
                assert {item.asset_id for item in images} == {"test"}
            if revoke == "during-" + mode:
                action("revoke")
            if revoke == "ambiguous":
                raise TimeoutError("simulated lost reply")
            return super().complete(prompt, images, tracker)

    if revoke == "between":
        from app.ai import pipeline
        build = pipeline.build_mode_b_prompt
        def revoke_then_build(*args, **kwargs):
            action("revoke")
            return build(*args, **kwargs)
        monkeypatch.setattr(pipeline, "build_mode_b_prompt", revoke_then_build)
    if revoke == "before-publish":
        from app.ai import pipeline
        truncate = pipeline._truncate_to_limits
        def revoke_then_truncate(*args, **kwargs):
            action("revoke")
            return truncate(*args, **kwargs)
        monkeypatch.setattr(pipeline, "_truncate_to_limits", revoke_then_truncate)
    def execute():
        return run_started_curation(folder, job, processing_consent=consent, start_etag=tag,
                                    config=AIProviderConfig(provider="mock"), provider=Simulated())
    if revoke == "none":
        result = execute()
        assert result.status == "completed" and result.mock
        assert result.mode_b["rights_review_required_ids"] == ["test"]
        assert calls == ["A", "B"]
        raw = (folder / "curation.json").read_bytes()
        with pytest.raises(ai_execution.InferenceHeld):
            execute()
        assert (folder / "curation.json").read_bytes() == raw
    else:
        try:
            result = execute()
            assert result.status == "failed"
        except ai_execution.InferenceHeld:
            pass
        assert calls == (["A", "B"] if revoke in ("during-B", "before-publish") else ["A"])
        assert not (folder / "curation.json").exists()
        with pytest.raises(ai_execution.InferenceHeld):
            execute()
    with worker.connect() as con:
        outcomes = con.execute("SELECT mode,outcome FROM ai_provider_attempts ORDER BY rowid").fetchall()
        assert len(outcomes) == len(calls)
        if revoke.startswith("during"):
            assert outcomes[-1][1] == "rejected"
        if revoke == "ambiguous":
            assert outcomes == [("A", "ambiguous")]


def test_concurrent_claim_and_crashed_reservation(setup, worker):
    folder, job, consent, action = setup
    action("approve")
    tag = action("start")
    def claim(_):
        try:
            with ai_execution.execution(folder, job, consent, tag):
                return True
        except ai_execution.InferenceHeld:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(claim, range(2))) == [False, True]
    with worker.connect() as con:
        nonce, = con.execute("SELECT claim FROM ai_execution_claims").fetchone()
        con.execute("INSERT INTO ai_provider_attempts VALUES (?, 'batch', 'A','synthetic','dispatching',NULL)", (nonce,))
    worker.store(worker.fetch(job), collection_base=1)
    action("approve")
    newer = action("start")
    with pytest.raises(ai_execution.InferenceHeld, match="reconciliation"):
        with ai_execution.execution(folder, job, consent, newer):
            pytest.fail("Restart dispatched an unresolved attempt")


def test_direct_provider_and_pipeline_have_no_authority(setup):
    folder, job, _, _ = setup
    from app.ai.provider import UsageTracker
    config = AIProviderConfig(provider="openai_compatible", max_retries=0)
    provider = OpenAICompatibleProvider(config, transport=lambda *_: pytest.fail("unclaimed dispatch"))
    with pytest.raises(ai_execution.InferenceHeld):
        provider.complete("fixture", [], UsageTracker(config))
    with pytest.raises(ai_execution.InferenceHeld):
        provider._http_post({})
    assert run_ai_curation(folder, job, provider=provider).status == "failed"


@pytest.mark.parametrize("revoke_at", ["A", "B"])
def test_bounded_transport_and_checkpoint(setup, worker, revoke_at):
    from app.ai.provider import ImagePayload, UsageTracker
    from app.qa_live import bounded_config
    import base64
    folder, job, consent, action = setup
    manifest = json.loads((folder / "manifest.json").read_bytes())
    manifest["assets"] = manifest["assets"][:1]
    atomic_write_json(folder / "manifest.json", manifest)
    worker.store(worker.fetch(job), collection_base=1)
    action("approve")
    tag = action("start")
    config = bounded_config("offline-test-value")
    calls = []

    def simulated_http(request):
        ai_execution.require_transport()
        content = request["body"]["messages"][0]["content"]
        images = [ImagePayload("test", "image/jpeg", base64.b64decode(p["image_url"]["url"].split(",")[1]))
                  for p in content[1:]]
        mode = "A" if images else "B"
        calls.append(mode)
        if mode == revoke_at:
            action("revoke")
        mock = MockVisionProvider().complete(content[0]["text"], images, UsageTracker(AIProviderConfig(provider="mock")))
        return {"choices": [{"message": {"content": mock.text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100}}

    with pytest.raises(ai_execution.InferenceHeld):
        run_started_curation(folder, job, processing_consent=consent, start_etag=tag,
                            config=config, provider=OpenAICompatibleProvider(config, transport=simulated_http))
    checkpoints = list((worker.DB.parent / "ai-checkpoints").glob("*/mode-a-validated.json"))
    if revoke_at == "B":
        assert calls == ["A", "B"] and len(checkpoints) == 1
        record = json.loads(checkpoints[0].read_bytes())
        checksum = record.pop("checkpoint_sha256")
        assert checksum == sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert record["job_id"] == job and record["asset_ids"] == ["test"] and record["schema_validation"] == "PASS"
        assert config.api_key not in checkpoints[0].read_text()
    else:
        assert calls == ["A"] and not checkpoints
    assert not (folder / "curation.json").exists()


@pytest.mark.parametrize("flags", [["--live"], ["--recover-mode-b", "previous.json", "--authorize-live-mode-b"]])
def test_standalone_cannot_start_external_attempt(monkeypatch, flags):
    import runpy
    import sys
    from pathlib import Path
    runner = Path(__file__).resolve().parents[1] / "scripts/groq_bounded_acceptance.py"
    monkeypatch.setattr(sys, "argv", [str(runner), *flags])
    with pytest.raises(ai_execution.InferenceHeld, match="execution claim required"):
        runpy.run_path(str(runner), run_name="__main__")


def test_fenced_503_overload_marks_rejected_and_retries_without_locking_job(setup, worker):
    """HTTP 503 Service Unavailable (e.g. Gemini high-demand spike) must be treated as a
    definitive non-billed rejection and retried under fenced execution, while HTTP 500
    continues to record `ambiguous` and lock reconciliation."""
    import base64
    from requests import HTTPError, Response
    from app.ai.provider import ImagePayload, UsageTracker

    folder, job, consent, action = setup
    action("approve")
    tag = action("start")
    config = AIProviderConfig(
        provider="openai_compatible",
        model="gemini-3.1-flash-lite",
        endpoint="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        api_key="offline-test-key",
        max_retries=2,
        min_request_spacing_s=0.0,
    )
    attempts_seen: list[str] = []

    def flaky_503_transport(request):
        ai_execution.require_transport()
        content = request["body"]["messages"][-1]["content"]
        images = [
            ImagePayload("test", "image/jpeg", base64.b64decode(p["image_url"]["url"].split(",")[1]))
            for p in content
            if isinstance(p, dict) and p.get("type") == "image_url"
        ]
        mode = "A" if images else "B"
        attempts_seen.append(mode)
        if len(attempts_seen) == 1:
            resp = Response()
            resp.status_code = 503
            resp._content = b'{"error":{"code":503,"status":"UNAVAILABLE","message":"high demand"}}'
            raise HTTPError("503 Service Unavailable", response=resp)
        mock = MockVisionProvider().complete(content[0]["text"], images, UsageTracker(AIProviderConfig(provider="mock")))
        return {
            "choices": [{"message": {"content": mock.text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 90},
        }

    result = run_started_curation(
        folder,
        job,
        processing_consent=consent,
        start_etag=tag,
        config=config,
        provider=OpenAICompatibleProvider(config, sleep=lambda _s: None, transport=flaky_503_transport),
    )
    assert result.status == "completed", result.errors
    assert attempts_seen == ["A", "A", "B"]
    assert (folder / "curation.json").is_file()
    with worker.connect() as con:
        outcomes = con.execute("SELECT mode, outcome FROM ai_provider_attempts ORDER BY rowid").fetchall()
    assert outcomes == [("A", "rejected"), ("A", "returned"), ("B", "returned")]


def test_resume_collection_blocked_while_ai_curation_active(setup, worker):
    """POST /jobs/{id}/resume-collection must fail closed (409) while AI curation is queued/started."""
    from fastapi import HTTPException
    _, job, _, _ = setup
    rec = worker.fetch(job)
    rec["collection_mode"] = "public_checkpoint"
    rec["status"] = "partial_success"
    rec["ai_curation"] = {"status": "started"}
    worker.store(rec)
    with pytest.raises(HTTPException) as exc_info:
        worker.resume_collection(job)
    assert exc_info.value.status_code == 409
    assert "AI curation is in progress" in str(exc_info.value.detail)


def test_audited_ai_reconcile_recovers_ambiguous_timeout_and_preserves_m4(setup, worker, monkeypatch):
    """Audited collection-ai-reconcile clears an ambiguous timeout onto rev+1, preserves immutable ledgers,
    keeps Two-Admin approval enforced on rev+1, and allows Drive release once the retry succeeds."""
    import base64
    from requests import Timeout
    from app import collection_approval
    from app.ai.pipeline import run_started_curation
    from app.ai.provider import AIProviderConfig, ImagePayload, MockVisionProvider, OpenAICompatibleProvider, UsageTracker

    folder, job, consent, action = setup
    action("approve")
    tag = action("start")
    monkeypatch.setenv("AI_ALLOWED_ENDPOINTS", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
    config = AIProviderConfig(
        provider="openai_compatible",
        model="gemini-3.1-flash-lite",
        endpoint="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        api_key="offline-test-key",
        max_retries=2,
        batch_size=3,
        min_request_spacing_s=0.0,
    )

    def timeout_transport(_req):
        ai_execution.require_transport()
        raise Timeout("Read timed out. (read timeout=120)")

    # 1. Simulate a network timeout during fenced execution -> records outcome='ambiguous'
    outcome1 = run_started_curation(
        folder,
        job,
        processing_consent=consent,
        start_etag=tag,
        config=config,
        provider=OpenAICompatibleProvider(config, sleep=lambda _s: None, transport=timeout_transport),
    )
    assert outcome1.status == "failed"
    assert not (folder / "curation.json").exists()
    rec = worker.fetch(job)
    rec["ai_curation"] = {"status": "failed", "warnings": outcome1.warnings, "errors": outcome1.errors}
    worker.store(rec)

    with worker.connect() as con:
        assert ai_execution.has_unresolved_attempt(con, job) is True
        # Grab Admin #1 (submitter) and Admin #2 (reviewer)
        submitter_id = rec["submitter_principal_id"]
        reviewer_id = con.execute(
            "SELECT id FROM named_accounts WHERE id!=? AND active=1 AND role='admin' LIMIT 1",
            (submitter_id,),
        ).fetchone()[0]
        panel1 = collection_approval.handle(con, worker.DATA_DIR, "collection-panel", {"job_id": job}, reviewer_id)
        assert panel1["ai_execution_claimed"] is True
        assert panel1["unresolved_ai_attempts"] == 1

        # 2. Audited reconcile publishes Revision 2 and records immutable ai_attempt_reconciliations row
        reconciled = collection_approval.handle(
            con,
            worker.DATA_DIR,
            "collection-ai-reconcile",
            {
                "job_id": job,
                "reason": "Gemini Batch 8 timed out after 120s; verified no curation written",
                "if_match": panel1["etag"],
                "request_key": "reconcile-rev1-timeout",
            },
            reviewer_id,
        )
        assert reconciled["revision"] == 2
        assert reconciled["state"] == "stale"
        assert reconciled["start_authorized"] is False
        assert ai_execution.has_unresolved_attempt(con, job) is False
        assert con.execute("SELECT COUNT(*) FROM ai_attempt_reconciliations WHERE job=?", (job,)).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM ai_provider_attempts WHERE outcome='ambiguous'").fetchone()[0] == 1
        # Immutability triggers prevent tampering with reconciliation history
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE ai_attempt_reconciliations SET reason='tampered'")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("DELETE FROM ai_attempt_reconciliations")

        # 3. Submitter STILL cannot self-approve Revision 2 (Two-Admin invariant preserved)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as self_exc:
            collection_approval.handle(
                con,
                worker.DATA_DIR,
                "collection-approve",
                {"job_id": job, "reason": "self", "if_match": reconciled["etag"], "request_key": "self-rev2"},
                submitter_id,
            )
        assert self_exc.value.status_code == 403

        # 4. Independent Admin #2 approves & starts Revision 2
        approved2 = collection_approval.handle(
            con,
            worker.DATA_DIR,
            "collection-approve",
            {"job_id": job, "reason": "Approved retry revision", "if_match": reconciled["etag"], "request_key": "appr-rev2"},
            reviewer_id,
        )
        started2 = collection_approval.handle(
            con,
            worker.DATA_DIR,
            "collection-start",
            {"job_id": job, "if_match": approved2["etag"], "request_key": "start-rev2"},
            reviewer_id,
        )
        con.commit()

    # 5. Retry AI execution succeeds on Revision 2 and publishes curation.json
    def ok_transport(req):
        ai_execution.require_transport()
        content = req["body"]["messages"][-1]["content"]
        images = [
            ImagePayload("test", "image/jpeg", base64.b64decode(p["image_url"]["url"].split(",")[1]))
            for p in content
            if isinstance(p, dict) and p.get("type") == "image_url"
        ]
        mock = MockVisionProvider().complete(content[0]["text"], images, UsageTracker(AIProviderConfig(provider="mock")))
        return {
            "choices": [{"message": {"content": mock.text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 110, "completion_tokens": 85},
        }

    outcome2 = run_started_curation(
        folder,
        job,
        processing_consent=consent,
        start_etag=started2["etag"],
        config=config,
        provider=OpenAICompatibleProvider(config, sleep=lambda _s: None, transport=ok_transport),
    )
    assert outcome2.status == "completed", outcome2.errors
    assert (folder / "curation.json").is_file()

    # 6. verify_checkpoint_release passes on Revision 2 (reconciled Rev 1 attempt no longer blocks Drive!)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    ok_release, release_reason, _, _ = collection_approval.verify_checkpoint_release(
        worker.DB, worker.DATA_DIR, job, manifest
    )
    assert ok_release is True, release_reason
    assert release_reason == "ok"

    # 7. Reconciliation is rejected (409) once curation.json exists
    with worker.connect() as con:
        latest_rev = collection_approval.handle(con, worker.DATA_DIR, "collection-review", {"job_id": job}, reviewer_id)
        with pytest.raises(HTTPException) as post_exc:
            collection_approval.handle(
                con,
                worker.DATA_DIR,
                "collection-ai-reconcile",
                {"job_id": job, "reason": "too late", "if_match": latest_rev["etag"], "request_key": "late-rec"},
                reviewer_id,
            )
        assert post_exc.value.status_code == 409


def test_orchestration_derive_stage_handles_failed_interrupted_and_partial():
    from app.orchestration import derive_stage
    failed = derive_stage({"status": "partial_success", "ai_curation": {"status": "failed"}})
    assert failed["stage"] == "partial_success"
    assert "reconcile" in failed["detail"].lower()

    partial = derive_stage({"status": "partial_success", "ai_curation": {"status": "partial", "mock": False}})
    assert partial["stage"] == "awaiting_rights_approval"
