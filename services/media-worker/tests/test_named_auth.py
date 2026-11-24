import base64
import hashlib
import hmac
import json
import sqlite3
import time
import uuid

import pytest
from fastapi import HTTPException

from app import named_auth as auth
from tests.test_phase7_orchestration import SOURCE_URLS
from tests.test_phase9_security import request, worker

KEY = "disposable-assertion-key-" * 2


def envelope(action, body, session="", **overrides):
    raw = json.dumps(body).encode()
    claims = dict(iss="pgm-frontend", aud="pgm-worker", action=action, sub=auth.digest(session),
                  body=hashlib.sha256(raw).hexdigest(), exp=int(time.time()) + 30, jti=uuid.uuid4().hex)
    claims.update(overrides)
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    assertion = part + b"." + hmac.digest(KEY.encode(), part, "sha256").hex().encode()
    return raw, ((b"x-named-session", session.encode()), (b"x-named-assertion", assertion))


@pytest.fixture
def identity(worker, monkeypatch):
    monkeypatch.setenv("NAMED_ASSERTION_SECRET", KEY)
    actor = auth.bootstrap(worker.DB, "sample", "synthetic-password", verified_operator="test-fixture")

    def invoke(action, body, session="", **overrides):
        raw, headers = envelope(action, body, session, **overrides)
        status, _, data = request(worker.app, "/named/" + action, "POST", "test-dashboard", (raw,), headers)
        return status, json.loads(data)

    return actor, invoke


def test_bootstrap_and_history(worker, identity):
    with pytest.raises(HTTPException) as repeat:
        auth.bootstrap(worker.DB, "another", "password", verified_operator="test-fixture")
    assert repeat.value.status_code == 409
    with worker.connect() as con:
        assert con.execute("SELECT password FROM named_accounts").fetchone()[0].startswith("$argon2id$")
        assert con.execute("SELECT actor,event FROM named_audit").fetchall() == [("test-fixture", "bootstrap")]
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("DELETE FROM named_audit")
    assert worker.fetch("a"*32) == {"job_id": "a"*32}


def test_named_session_and_management(worker, identity):
    actor, call = identity
    credentials = {"name": "sample", "password": "synthetic-password"}
    assert call("login", {**credentials, "password": "wrong"})[0] == 403
    old = call("login", credentials, "fixed")[1]["session"]
    session = call("login", credentials, old)[1]["session"]
    assert old != session and call("session", {}, old)[0] == 403
    assert call("session", {}, "fixed")[0] == 403
    assert call("session", {}, session)[1]["principal_id"] == actor
    child = {"name": "sample2", "password": "example-password"}
    target = call("create", child, session)[1]["principal_id"]
    second = call("login", child)[1]["session"]
    assert call("request-recovery", {"principal_id": target}, session)[1]["status"] == "pending-authority"
    assert call("role", {"principal_id": target, "role": "none"}, session)[0] == 200
    assert call("session", {}, second)[0] == 403
    assert call("login", child)[0] == 403
    assert call("create", {**child, "role": "admin"}, second)[0] == 403
    assert call("role", {"principal_id": target, "role": "admin"}, session)[0] == 200
    second = call("login", child)[1]["session"]
    assert call("suspend", {"principal_id": target}, session)[0] == 200
    assert call("session", {}, second)[0] == call("login", child)[0] == 403
    assert call("logout", {}, session)[0] == 200
    assert call("session", {}, session)[0] == 403


def test_signed_scope_and_replay(worker, identity):
    _, call = identity
    credentials = {"name": "sample", "password": "synthetic-password"}
    for changes in ({"aud": "other"}, {"iss": "n8n"}, {"sub": "forged"}, {"exp": 1}):
        assert call("login", credentials, **changes)[0] == 403
    raw, headers = envelope("login", credentials)
    def invoke(headers, raw=raw):
        return request(worker.app, "/named/login", "POST", "test-admin", (raw,), headers)[0]
    assert invoke(()) == 403
    assert invoke(((b"x-named-assertion", headers[1][1] + b"x"),)) == 403
    assert invoke(headers, b"{}") == 403
    assert invoke(headers) == 200
    assert invoke(headers) == 403


def test_named_job_binding(worker, identity, monkeypatch):
    actor, call = identity
    session = call("login", {"name": "sample", "password": "synthetic-password"})[1]["session"]
    ticket = call("intake", SOURCE_URLS, session)[1]["idempotency_key"]
    payload = {**SOURCE_URLS, "idempotency_key": ticket, "submitter_principal_id": "forged"}
    monkeypatch.setattr(worker.executor, "submit", lambda *args: worker.slots.release())
    def submit(data):
        status, _, raw = request(worker.app, "/jobs", "POST", "test-admin", (json.dumps(data).encode(),))
        return status, json.loads(raw)
    assert submit({**payload, "run_ai_curation": True})[0] == 403
    status, result = submit(payload)
    assert status == 202 and worker.fetch(result["job_id"])["submitter_principal_id"] == actor
    assert submit(payload)[1]["job_id"] == result["job_id"]
    assert submit({**payload, "idempotency_key": "named-forged"})[0] == 403
    ordinary = submit({**SOURCE_URLS, "submitter_principal_id": actor})[1]["job_id"]
    assert "submitter_principal_id" not in worker.fetch(ordinary)
    with worker.connect() as con:
        con.execute("UPDATE named_sessions SET expires=0")
    assert submit(payload)[0] == 403


def test_cli_requires_disposable_mode(monkeypatch):
    import sys
    from scripts import named_bootstrap

    monkeypatch.setattr(sys, "argv", ["named_bootstrap"])
    with pytest.raises(SystemExit) as error:
        named_bootstrap.main()
    assert error.value.code == 2
    monkeypatch.setattr(sys, "argv", ["named_bootstrap", "--disposable-test"])
    monkeypatch.setattr("builtins.input", lambda _: "demo")
    monkeypatch.setattr(named_bootstrap.getpass, "getpass", lambda _: "only-in-qa")
    named_bootstrap.main()


def test_simultaneous_bootstrap_and_rollback(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db = tmp_path / "disposable.sqlite"
    with pytest.raises(HTTPException):
        auth.bootstrap(db, "bad name", "sample", verified_operator="test-only")

    def attempt(name):
        try:
            auth.bootstrap(db, name, "fixture-password", verified_operator="test-only")
            return 201
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(2) as executor:
        assert sorted(executor.map(attempt, ["first", "second"])) == [201, 409]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM named_accounts").fetchone() == (1,)
        assert con.execute("SELECT count(*) FROM named_audit").fetchone() == (1,)
