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
