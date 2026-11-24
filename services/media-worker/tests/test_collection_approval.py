"""Stage 2 acceptance using signed Stage 1 requests and disposable job storage."""
import hashlib
import json
import sqlite3
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app import collection_approval as ledger
from app.publication import frozen_window, publication_evidence
from tests.test_named_auth import envelope, identity
from tests.test_phase9_security import request, worker

JOB = "b" * 32


@pytest.fixture(autouse=True)
def zero_external_operations(monkeypatch):
    from app.ai import pipeline
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        pytest.fail("Offline approval attempted inference or an outbound connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(pipeline, "run_ai_curation", forbidden)
    yield
    assert not attempts


@pytest.fixture
def qa(worker, identity, monkeypatch):
    actor, invoke = identity
    monkeypatch.setattr(worker.app.state, "offline_approval_qa", True, raising=False)
    with worker.connect() as con:
        ledger.migrate(con)
    owner = invoke("login", {"name": "sample", "password": "synthetic-password"})[1]["session"]
    cred = {"name": "reviewer", "password": "test-only"}
    invoke("create", cred, owner)
    reviewer = invoke("login", cred)[1]["session"]
    record = dict(job_id=JOB, status="partial_success", submitter_principal_id=actor,
                  sources={"website": {"coverage": "unknown"}},
                  **frozen_window(datetime(2024, 6, 1, tzinfo=UTC)))
    worker.store(record)
    folder = worker.DATA_DIR / JOB
    folder.mkdir()
    (folder / "image.jpg").write_bytes(b"fixture")
    date = record["window_end_utc"]
    asset = dict(asset_id="image", local_path="image.jpg", type="image", extraction_status="downloaded",
                 post_date=date, date_status="verified", preprocessing={"outcome": "processed"},
                 sha256=hashlib.sha256(b"fixture").hexdigest(),
                 publication_evidence=publication_evidence(date, source_url="https://example.test/image",
                                                           field="datePublished", attribution="media"))
    manifest = {**record, "assets": [asset]}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    worker.store(record, collection_base=0)

    def call(action="review", session=None, tag=None, key=None, **more):
        body = {"job_id": JOB, **more}
        if action != "review":
            body.update(if_match=tag, request_key=key or uuid.uuid4().hex)
        return invoke("collection-" + action, body, session or reviewer)

    return call, record, owner, reviewer, folder


@pytest.mark.parametrize("starter", ["reviewer", "submitter"])
def test_decision_start_revoke(worker, qa, starter):
    call, _, owner, reviewer, _ = qa
    pending = call()[1]
    assert pending["state"] == "pending"
    assert call("start", tag=pending["etag"])[0] == 409
    approve = dict(tag=pending["etag"], key="approval", reason="All source gaps reviewed")
    assert call("approve", session=owner, **approve)[0] == 403
    assert call("approve", **approve, actor="forged")[0] == 400
    assert call("approve", **{**approve, "reason": "  "})[0] == 400
    ok, approved = call("approve", **approve)
    assert ok == 200 and approved["state"] == "approved" and not approved["start_authorized"]
    assert call("approve", **approve)[1]["idempotent_replay"]
    assert call("deny", **approve)[0] == 409
    start = dict(tag=approved["etag"], key="start", session=owner if starter == "submitter" else reviewer)
    ok, started = call("start", **start)
    assert ok == 200 and started["start_authorized"]
    assert call("start", **start)[1]["idempotent_replay"]
    assert call("start", tag=started["etag"])[0] == 409
    revoked = call("revoke", tag=started["etag"], reason="Inventory needs review")[1]
    assert revoked["state"] == "revoked" and not revoked["start_authorized"]
    assert call("start", tag=revoked["etag"])[0] == 409
    denied = call("deny", tag=revoked["etag"], reason="Unresolved inventory gaps")[1]
    assert denied["state"] == "denied"
    with worker.connect() as con:
        assert con.execute("SELECT count(*) FROM collection_ai_starts").fetchone()[0] == 1
        for table in ("collection_revisions", "collection_decisions", "collection_ai_starts"):
            for statement in (f"DELETE FROM {table}", f"UPDATE {table} SET job=job",
                              f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
                with pytest.raises(sqlite3.IntegrityError):
                    con.execute(statement)


def test_stale_snapshot_and_recollection(worker, qa):
    call, record, _, _, folder = qa
    before = call()[1]
    approved = call("approve", tag=before["etag"], reason="Reviewed")[1]
    call("start", tag=approved["etag"])
    worker.store(record, collection_base=1)
    unchanged = call()[1]
    assert unchanged["revision"] == 2 and unchanged["manifest_sha256"] == before["manifest_sha256"]
    assert unchanged["state"] == "stale" and not unchanged["start_authorized"]
    assert call("approve", tag=before["etag"], reason="Reviewed")[0] == 412
    path = folder / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["assets"][0]["date_status"] = "unknown_date"
    path.write_text(json.dumps(manifest))
    assert call("approve", tag=unchanged["etag"], reason="Reviewed")[0] == 412
    worker.store({**record, "sources": {"facebook": {"missing": 1}}}, collection_base=2)
    after = call()[1]
    assert after["revision"] == 3 and after["eligible_subset_sha256"] != before["eligible_subset_sha256"]
    with worker.connect() as con:
        raw, subset = con.execute("SELECT manifest,subset FROM collection_revisions WHERE revision=1").fetchone()
        assert hashlib.sha256(raw).hexdigest() == before["manifest_sha256"]
        assert json.loads(subset)["candidates"][0]["asset_id"] == "image"
        assert con.execute("SELECT count(*) FROM collection_revisions").fetchone() == (3,)


def test_races(worker, qa):
    call, record, _, _, _ = qa
    tag = call()[1]["etag"]
    with ThreadPoolExecutor(2) as pool:
        decisions = list(pool.map(lambda a: call(a, tag=tag, reason="Reviewed")[0], ("approve", "deny")))
    assert sorted(decisions) == [200, 412]

    def publish(_):
        try:
            worker.store(record, collection_base=1)
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(publish, range(2))) == [200, 412]
    assert call()[1]["state"] == "stale"


def test_historical_and_default_isolation(worker, qa, identity, monkeypatch):
    call, record, _, reviewer, folder = qa
    old = "c" * 32
    history = {**record, "job_id": old}
    del history["submitter_principal_id"]
    historical_dir = worker.DATA_DIR / old
    historical_dir.mkdir()
    (historical_dir / "manifest.json").write_text(json.dumps({**history, "assets": []}))
    worker.store(history)
    worker.store(history, collection_base=0)
    tag = call(job_id=old)[1]["etag"]
    assert call("approve", job_id=old, tag=tag, reason="Reviewed")[0] == 403
    assert call("approve", job_id=JOB, tag=tag, reason="Reviewed")[0] == 412
    assert worker.fetch(old) == history
    assert request(worker.app, "/named/collection-review", "POST", "test-admin", (b"{}",))[0] == 403
    monkeypatch.setattr(worker.app.state, "offline_approval_qa", False)
    assert call()[0] == 404


def test_assertion_replay_and_account_suspension(worker, qa, identity):
    call, _, owner, reviewer, _ = qa
    raw, headers = envelope("collection-approve", {
        "job_id": JOB, "request_key": "signed-once", "if_match": call()[1]["etag"], "reason": "Reviewed"
    }, reviewer)
    def send():
        return request(worker.app, "/named/collection-approve", "POST", "test-dashboard", (raw,), headers)[0]
    assert send() == 200
    assert send() == 403
    active = call("start", tag=call()[1]["etag"], session=owner)[1]
    assert active["start_authorized"]
    _, invoke = identity
    reviewer_id = invoke("session", {}, reviewer)[1]["principal_id"]
    assert invoke("suspend", {"principal_id": reviewer_id}, owner)[0] == 200
    assert call(session=reviewer)[0] == 403
    result = call(session=owner)[1]
    assert result["state"] == "stale" and not result["start_authorized"]


def test_disposable_backup_restore(worker, identity, tmp_path):
    with worker.connect() as original, sqlite3.connect(tmp_path / "before.sqlite") as before:
        original.backup(before)
        jobs = before.execute("SELECT * FROM jobs").fetchall()
        users = before.execute("SELECT id FROM named_accounts").fetchall()
    with sqlite3.connect(tmp_path / "migrated.sqlite") as migrated, sqlite3.connect(tmp_path / "before.sqlite") as before:
        before.backup(migrated)
        ledger.migrate(migrated)
        ledger.migrate(migrated)
        assert migrated.execute("SELECT * FROM jobs").fetchall() == jobs
        assert migrated.execute("SELECT id FROM named_accounts").fetchall() == users
        before.backup(migrated)
        assert migrated.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert not migrated.execute("SELECT 1 FROM sqlite_master WHERE name='collection_revisions'").fetchone()
        assert migrated.execute("SELECT * FROM jobs").fetchall() == jobs


def test_old_approval_survives_restore_and_migration(worker, qa, monkeypatch, tmp_path):
    call, _, _, _, folder = qa
    monkeypatch.setattr(ledger, "utc", lambda: "2000-01-01T00:00:00+00:00")
    pending = call()[1]
    approved = call("approve", tag=pending["etag"], reason="Reviewed documented gaps")[1]
    restored = tmp_path / "restored.sqlite"
    with worker.connect() as original, sqlite3.connect(restored) as copy:
        original.backup(copy)
        ledger.migrate(copy)
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert copy.execute("PRAGMA foreign_key_check").fetchall() == []
        row = copy.execute("""SELECT d.actor,d.action,d.reason,d.utc,r.job,r.revision,r.manifest_sha,r.subset_sha
                              FROM collection_decisions d JOIN collection_revisions r USING(job,revision)""").fetchone()
        assert row[0] and row[1:] == ("approve", "Reviewed documented gaps", "2000-01-01T00:00:00+00:00",
                                       JOB, 1, approved["manifest_sha256"], approved["eligible_subset_sha256"])
    monkeypatch.setattr(worker, "DB", restored)
    assert call()[1] == approved
    assert not (folder / "curation.json").exists() and not (folder / "delivery.json").exists()


def test_worker_publication_hook_and_collection_decision_race(worker, qa, monkeypatch):
    call, record, _, _, folder = qa
    from app import public_collection
    config = worker.DATA_DIR / "synthetic-config.json"
    config.write_text("{}")
    monkeypatch.setenv("PUBLIC_COLLECTION_CHECKPOINTS", "1")
    monkeypatch.setenv("PUBLIC_COLLECTION_CONFIG", str(config))
    monkeypatch.setattr(public_collection, "collect_public_job",
                        lambda *args: json.loads((folder / "manifest.json").read_bytes()))
    before = call()[1]
    assert worker.slots.acquire(blocking=False)
    with ThreadPoolExecutor(2) as pool:
        publication = pool.submit(worker._process_job, JOB, {**record, "collection_mode": "public_checkpoint"})
        decision = pool.submit(call, "approve", tag=before["etag"], reason="Reviewed")
        assert decision.result()[0] in (200, 412)
        publication.result()
    latest = call()[1]
    assert latest["revision"] == 2 and latest["state"] in ("pending", "stale")
    assert not latest["start_authorized"]
    assert call("start", tag=latest["etag"])[0] == 409
