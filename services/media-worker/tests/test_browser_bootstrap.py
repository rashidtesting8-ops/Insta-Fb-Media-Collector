"""Targeted security tests for one-time loopback browser Named Admin bootstrap."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import sys

from fastapi import HTTPException
import pytest

from app import named_auth as auth
from tests.test_named_auth import KEY, envelope
from tests.test_phase9_security import worker  # noqa: F401


def _asgi_post(app, path, raw, headers=(), *, key="test-dashboard", client=("127.0.0.1", 51234)):
    messages = []
    incoming = [{"type": "http.request", "body": raw, "more_body": False}]
    request_headers = [(b"content-type", b"application/json"), *list(headers)]
    if key:
        request_headers.append((b"x-api-key", key.encode()))

    async def send(message):
        messages.append(message)

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "scheme": "http",
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": request_headers,
    }
    if client is not None:
        scope["client"] = client
    asyncio.run(app(scope, receive, send))
    return messages[0]["status"], dict(messages[0]["headers"]), b"".join(m.get("body", b"") for m in messages)


def _call_bootstrap(worker_mod, action, body, *, client=("127.0.0.1", 51234), extra_headers=()):
    raw, signed_headers = envelope(action, body)
    status, _, data = _asgi_post(
        worker_mod.app,
        f"/named/{action}",
        raw,
        (*signed_headers, *extra_headers),
        client=client,
    )
    return status, json.loads(data)


def test_successful_one_time_browser_setup_and_permanent_lock(worker, monkeypatch):
    monkeypatch.setenv("NAMED_ASSERTION_SECRET", KEY)
    # Before token is issued, setup is unavailable.
    status, data = _call_bootstrap(worker, "bootstrap-status", {})
    assert status == 200 and data == {"available": False}

    issued = auth.issue_setup_token(worker.DB, verified_operator="ahsan-rashid", ttl_seconds=600)
    token = issued["token"]
    assert len(token) >= 32

    # Only SHA-256 digest is stored in SQLite, never the plaintext setup token.
    with worker.connect() as con:
        stored_digest = con.execute("SELECT digest FROM named_setup_tokens WHERE singleton=1").fetchone()[0]
        assert stored_digest == auth.digest(token)
        assert token not in stored_digest

    status, data = _call_bootstrap(worker, "bootstrap-status", {})
    assert status == 200 and data == {"available": True}

    secret_pw = "CorrectHorseBatteryStaple!99"
    status, data = _call_bootstrap(
        worker,
        "bootstrap",
        {
            "setup_token": token,
            "name": "ahsan-rashid",
            "password": secret_pw,
            "confirm_password": secret_pw,
        },
    )
    assert status == 200
    principal_id = data["principal_id"]
    assert len(principal_id) == 32
    # Never echo plaintext password or setup token in response.
    serialized = json.dumps(data)
    assert secret_pw not in serialized and token not in serialized

    # Verify Argon2id storage, bootstrap lock, token invalidation, audit log, and preserved jobs.
    with worker.connect() as con:
        acct = con.execute("SELECT id, name, password, role, active FROM named_accounts").fetchone()
        assert acct[0] == principal_id
        assert acct[1] == "ahsan-rashid"
        assert acct[2].startswith("$argon2id$")
        assert secret_pw not in acct[2]
        assert acct[3] == "admin" and acct[4] == 1
        assert con.execute("SELECT count(*) FROM named_bootstrap").fetchone() == (1,)
        assert con.execute("SELECT count(*) FROM named_setup_tokens").fetchone() == (0,)
        audits = con.execute("SELECT actor, event, target FROM named_audit ORDER BY id").fetchall()
        assert audits == [
            ("ahsan-rashid", "setup-token-issued", "singleton"),
            ("ahsan-rashid", "bootstrap", principal_id),
        ]
        # Preserved pre-existing historical job row.
        assert con.execute("SELECT count(*) FROM jobs").fetchone() == (1,)

    # Setup status is now permanently unavailable.
    status, data = _call_bootstrap(worker, "bootstrap-status", {})
    assert status == 200 and data == {"available": False}

    # Repeated bootstrap with same token is permanently rejected (409).
    status, data = _call_bootstrap(
        worker,
        "bootstrap",
        {
            "setup_token": token,
            "name": "second-admin",
            "password": secret_pw,
            "confirm_password": secret_pw,
        },
    )
    assert status == 409

    # Issuing a new setup token after consumption is permanently rejected (409).
    with pytest.raises(HTTPException) as exc:
        auth.issue_setup_token(worker.DB, verified_operator="ahsan-rashid")
    assert exc.value.status_code == 409

    # Created Admin #1 can immediately authenticate via existing /named/login flow.
    status, login_data = _call_bootstrap(
        worker,
        "login",
        {"name": "ahsan-rashid", "password": secret_pw},
    )
    assert status == 200 and len(login_data["session"]) >= 32


def test_invalid_missing_and_expired_setup_token_rejected(worker, monkeypatch):
    monkeypatch.setenv("NAMED_ASSERTION_SECRET", KEY)
    issued = auth.issue_setup_token(worker.DB, verified_operator="ahsan-rashid", ttl_seconds=600)
    secret_pw = "ValidLongAdminPassword!2026"

    for bad_token in ("", "short", "wrong-token-" + "x" * 32, issued["token"] + "z"):
        status, data = _call_bootstrap(
            worker,
            "bootstrap",
            {
                "setup_token": bad_token,
                "name": "ahsan-rashid",
                "password": secret_pw,
                "confirm_password": secret_pw,
            },
        )
        assert status == 403
        assert secret_pw not in json.dumps(data)

    # Expire the token in DB and verify rejection.
    with worker.connect() as con:
        con.execute("UPDATE named_setup_tokens SET expires=1")
    status, _ = _call_bootstrap(worker, "bootstrap-status", {})
    assert status == 200
    status, data = _call_bootstrap(
        worker,
        "bootstrap",
        {
            "setup_token": issued["token"],
            "name": "ahsan-rashid",
            "password": secret_pw,
            "confirm_password": secret_pw,
        },
    )
    assert status == 403
    assert issued["token"] not in json.dumps(data)


def test_weak_password_and_mismatch_rejected_without_consuming_lock_or_leaking(worker, monkeypatch):
    monkeypatch.setenv("NAMED_ASSERTION_SECRET", KEY)
    token = auth.issue_setup_token(worker.DB, verified_operator="ahsan-rashid")["token"]

    # Weak password (< 12 chars)
    weak = "TooShort1!"
    status, data = _call_bootstrap(
        worker,
        "bootstrap",
        {"setup_token": token, "name": "ahsan-rashid", "password": weak, "confirm_password": weak},
    )
    assert status == 400
    assert weak not in json.dumps(data)

    # Mismatched password confirmation
    pw1 = "VeryStrongPassword!One123"
    pw2 = "VeryStrongPassword!Two456"
    status, data = _call_bootstrap(
        worker,
        "bootstrap",
        {"setup_token": token, "name": "ahsan-rashid", "password": pw1, "confirm_password": pw2},
    )
    assert status == 400
    assert pw1 not in json.dumps(data) and pw2 not in json.dumps(data)

    # Neither failure created an account or consumed the bootstrap lock/token.
    with worker.connect() as con:
        assert con.execute("SELECT count(*) FROM named_accounts").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM named_bootstrap").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM named_setup_tokens").fetchone() == (1,)


def test_concurrent_browser_bootstrap_attempts_are_atomic_single_use(tmp_path):
    db = tmp_path / "concurrent.sqlite"
    token = auth.issue_setup_token(db, verified_operator="ahsan-rashid")["token"]
    pw = "ConcurrentBootstrapSecret!2026"

    def attempt(idx):
        try:
            auth.browser_bootstrap(
                db,
                setup_token=token,
                name=f"admin-{idx}",
                password=pw,
                confirm_password=pw,
            )
            return 201
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = sorted(pool.map(attempt, range(4)))
    assert statuses[0] == 201
    assert all(code in (403, 409) for code in statuses[1:])

    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM named_accounts").fetchone() == (1,)
        assert con.execute("SELECT count(*) FROM named_bootstrap").fetchone() == (1,)
        assert con.execute("SELECT count(*) FROM named_setup_tokens").fetchone() == (0,)


def test_remote_and_forwarded_bootstrap_requests_rejected(worker, monkeypatch):
    monkeypatch.setenv("NAMED_ASSERTION_SECRET", KEY)
    monkeypatch.setattr(auth.os.path, "exists", lambda _p: False)  # simulate non-Docker strict loopback
    token = auth.issue_setup_token(worker.DB, verified_operator="ahsan-rashid")["token"]
    payload = {
        "setup_token": token,
        "name": "ahsan-rashid",
        "password": "LoopbackOnlySecret!2026",
        "confirm_password": "LoopbackOnlySecret!2026",
    }

    # Public remote IP, private LAN IP, and missing client all rejected.
    for bad_client in (("203.0.113.10", 44321), ("192.168.1.50", 44321), None):
        status, _ = _call_bootstrap(worker, "bootstrap", payload, client=bad_client)
        assert status == 403
        status, _ = _call_bootstrap(worker, "bootstrap-status", {}, client=bad_client)
        assert status == 403

    # Reverse-proxy forwarding headers rejected even from 127.0.0.1.
    for hdr_name in (b"x-forwarded-for", b"x-real-ip", b"forwarded"):
        status, _ = _call_bootstrap(
            worker,
            "bootstrap",
            payload,
            client=("127.0.0.1", 51234),
            extra_headers=((hdr_name, b"203.0.113.10"),),
        )
        assert status == 403

    # IPv6 loopback ::1 is accepted.
    status, data = _call_bootstrap(worker, "bootstrap-status", {}, client=("::1", 51234))
    assert status == 200 and data == {"available": True}


def test_cli_issue_browser_token_outputs_url_and_token_without_password(tmp_path, monkeypatch, capsys):
    from scripts import named_bootstrap

    db = tmp_path / "jobs.sqlite"
    sqlite3.connect(db).close()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "named_bootstrap.py",
            "--issue-browser-token",
            "--db",
            str(db),
            "--verified-operator",
            "ahsan-rashid",
        ],
    )
    named_bootstrap.main()
    out = capsys.readouterr().out
    assert "BROWSER_SETUP_URL=http://localhost:3000/setup-admin" in out
    assert "BROWSER_SETUP_TOKEN=" in out
    assert "BROWSER_SETUP_EXPIRES_UTC=" in out
