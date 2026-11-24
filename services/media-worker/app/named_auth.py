"""Stage 1 named identities. All privileged mutations run under BEGIN IMMEDIATE."""
import base64
import ctypes
import ctypes.util
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from functools import lru_cache

from fastapi import HTTPException

SESSION_SECONDS = 8 * 3600
SCHEMA = """
CREATE TABLE IF NOT EXISTS named_accounts (
 id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('admin','none')), active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS named_sessions (
 digest TEXT PRIMARY KEY, actor TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS named_bootstrap (singleton INTEGER PRIMARY KEY CHECK(singleton=1));
CREATE TABLE IF NOT EXISTS named_nonces (nonce TEXT PRIMARY KEY, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS named_intakes (
 ticket TEXT PRIMARY KEY, session TEXT NOT NULL, payload TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS named_audit (
 id INTEGER PRIMARY KEY, stamp INTEGER NOT NULL, actor TEXT NOT NULL,
 event TEXT NOT NULL, target TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS named_audit_immutable BEFORE UPDATE ON named_audit
 BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER IF NOT EXISTS named_audit_no_delete BEFORE DELETE ON named_audit
 BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
"""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def denied():
    return HTTPException(403, "Named authorization denied")


@lru_cache(maxsize=1)
def sodium():
    library = ctypes.CDLL(ctypes.util.find_library("sodium") or "libsodium.so.23")
    if library.sodium_init() < 0:
        raise RuntimeError("Password hashing unavailable")
    library.crypto_pwhash_str_alg.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulonglong, ctypes.c_ulonglong,
        ctypes.c_size_t, ctypes.c_int,
    ]
    library.crypto_pwhash_str_verify.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulonglong]
    return library


def password_bytes(password):
    if not isinstance(password, str) or not 1 <= len(password.encode()) <= 1024:
        raise denied()
    return password.encode()


def password_hash(password):
    raw = password_bytes(password)
    encoded = ctypes.create_string_buffer(128)
    if sodium().crypto_pwhash_str_alg(encoded, raw, len(raw), 3, 64 * 1024**2, 2):
        raise RuntimeError("Argon2id unavailable")
    return encoded.value.decode()


@lru_cache(maxsize=1)
def dummy_hash():
    return password_hash(secrets.token_urlsafe(32))


def create_account(con, name, password):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", name):
        raise HTTPException(400, "Invalid account name")
    principal = uuid.uuid4().hex
    try:
        con.execute("INSERT INTO named_accounts VALUES (?,?,?,'admin',1)",
                    (principal, name, password_hash(password)))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Account already exists") from None
    return principal


def audit(con, actor, event, target):
    con.execute("INSERT INTO named_audit(stamp,actor,event,target) VALUES(?,?,?,?)",
                (int(time.time()), actor, event, target))


def bootstrap(db, name, password, *, verified_operator):
    # Only a trusted local caller can provide the independently verified operator.
    if not verified_operator:
        raise denied()
    with closing(sqlite3.connect(db)) as con, con:
        con.executescript(SCHEMA)
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM named_bootstrap").fetchone() or con.execute(
                "SELECT 1 FROM named_accounts").fetchone():
            raise HTTPException(409, "Bootstrap already consumed")
        con.execute("INSERT INTO named_bootstrap VALUES (1)")
        actor = create_account(con, name, password)
        audit(con, verified_operator, "bootstrap", actor)
        return actor


def session_actor(con, session_hash):
    row = con.execute("""SELECT a.id,a.role FROM named_sessions s JOIN named_accounts a
        ON a.id=s.actor WHERE s.digest=? AND a.active=1 AND a.role='admin' AND s.expires>?""",
                      (session_hash, int(time.time()))).fetchone()
    if not row:
        raise denied()
    return row


def verify_assertion(header, action, raw, session):
    key = os.environ.get("NAMED_ASSERTION_SECRET", "")
    if len(key) < 32 or key in [os.getenv(k) for k in (
            "MEDIA_WORKER_TOKEN", "MEDIA_WORKER_DASHBOARD_TOKEN", "N8N_PIPELINE_TOKEN",
            "DASHBOARD_PASSWORD", "DASHBOARD_SESSION_SECRET")]:
        raise denied()
    try:
        encoded, mac = header.split(".")
        expected = hmac.digest(key.encode(), encoded.encode("ascii"), "sha256").hex()
        if not hmac.compare_digest(expected, mac):
            raise denied()
        claims = json.loads(base64.urlsafe_b64decode(encoded + "==="))
        now = int(time.time())
        if (claims["iss"] != "pgm-frontend" or claims["aud"] != "pgm-worker"
                or claims["action"] != action or claims["sub"] != digest(session)
                or claims["body"] != hashlib.sha256(raw).hexdigest()
                or not now < claims["exp"] <= now + 30
                or not re.fullmatch("[a-f0-9]{32}", claims["jti"])):
            raise denied()
        return claims
    except (TypeError, KeyError, ValueError, AttributeError):
        raise denied() from None


def intake_digest(data):
    return digest(json.dumps(data, sort_keys=True, separators=(",", ":")))


def verify_ticket(con, data):
    row = con.execute("SELECT session,payload FROM named_intakes WHERE ticket=? AND expires>?",
                      (data["idempotency_key"], int(time.time()))).fetchone()
    if not row or row[1] != intake_digest(data):
        raise denied()
    return session_actor(con, row[0])[0]


def verify_admin_action(db, action, raw, header, session):
    """Verify HMAC assertion, single-use replay nonce, and active named Admin session, then release DB lock."""
    claims = verify_assertion(header, action, raw, session)
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise denied()
    session_hash = digest(session)
    now = int(time.time())
    with closing(sqlite3.connect(db)) as con, con:
        con.executescript(SCHEMA)
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM named_nonces WHERE expires<=?", (now,))
        try:
            con.execute("INSERT INTO named_nonces VALUES (?,?)", (claims["jti"], claims["exp"]))
        except sqlite3.IntegrityError:
            raise denied() from None
        con.commit()
        con.execute("BEGIN IMMEDIATE")
        if claims["exp"] <= int(time.time()):
            raise denied()
        actor, role = session_actor(con, session_hash)
        if role != "admin":
            raise denied()
    return actor, body


def dispatch(db, action, raw, header, session, *, approval_root=None):
    claims = verify_assertion(header, action, raw, session)
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise denied()
    session_hash = digest(session)
    now = int(time.time())
    with closing(sqlite3.connect(db)) as con, con:
        con.executescript(SCHEMA)
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM named_nonces WHERE expires<=?", (now,))
        try:
            con.execute("INSERT INTO named_nonces VALUES (?,?)", (claims["jti"], claims["exp"]))
        except sqlite3.IntegrityError:
            raise denied() from None
        con.commit()
        con.execute("BEGIN IMMEDIATE")
        if claims["exp"] <= int(time.time()):
            raise denied()
        if action == "login":
            raw_password = password_bytes(body.get("password"))
            if not isinstance(body.get("name"), str):
                raise denied()
            account = con.execute("SELECT id,password FROM named_accounts WHERE name=? AND active=1 AND role='admin'",
                                  (body.get("name"),)).fetchone()
            stored_hash = account[1] if account else dummy_hash()
            if sodium().crypto_pwhash_str_verify(stored_hash.encode(), raw_password, len(raw_password)) or not account:
                raise denied()
            token = secrets.token_urlsafe(32)
            con.execute("DELETE FROM named_sessions WHERE digest=? OR expires<=?", (session_hash, now))
            con.execute("INSERT INTO named_sessions VALUES (?,?,?)",
                        (digest(token), account[0], now + SESSION_SECONDS))
            audit(con, account[0], "login", account[0])
            return {"session": token}
        actor, role = session_actor(con, session_hash)
        if action == "session":
            return {"principal_id": actor, "role": role}
        if action == "logout":
            con.execute("DELETE FROM named_sessions WHERE digest=?", (session_hash,))
            audit(con, actor, "logout", actor)
            return {"ok": True}
        if action == "intake":
            from app.orchestration import validate_intake
            validated = validate_intake(body)
            if not validated["valid"] or not validated["create"]:
                raise HTTPException(400, "Invalid intake")
            data = validated["create"]
            ticket = "named-" + hmac.digest(os.environ["NAMED_ASSERTION_SECRET"].encode(),
                                             (actor + "|" + data["idempotency_key"]).encode(), "sha256").hex()
            data["idempotency_key"] = ticket
            previous = con.execute("SELECT payload FROM named_intakes WHERE ticket=?", (ticket,)).fetchone()
            if previous and previous[0] != intake_digest(data):
                raise HTTPException(409, "Intake key already used for different inputs")
            con.execute("INSERT OR REPLACE INTO named_intakes VALUES (?,?,?,?)",
                        (ticket, session_hash, intake_digest(data), now + 60))
            return {"idempotency_key": ticket}
        if role != "admin":
            raise denied()
        if action.startswith("collection-"):
            from app.collection_approval import ACTIONS, handle
            if approval_root is None or action not in ACTIONS:
                raise HTTPException(404, "Approval adapter unavailable")
            return handle(con, approval_root, action, body, actor)
        if action == "create":
            target = create_account(con, body.get("name"), body.get("password"))
        else:
            target = body.get("principal_id")
            if not isinstance(target, str) or not con.execute(
                    "SELECT 1 FROM named_accounts WHERE id=?", (target,)).fetchone():
                raise denied()
            if action == "suspend":
                con.execute("UPDATE named_accounts SET active=0 WHERE id=?", (target,))
            elif action == "role" and body.get("role") in ("none", "admin"):
                con.execute("UPDATE named_accounts SET role=? WHERE id=?", (body["role"], target))
            elif action == "request-recovery":
                audit(con, actor, "recovery-requested", target)
                return {"status": "pending-authority"}
            else:
                raise denied()
            con.execute("DELETE FROM named_sessions WHERE actor=?", (target,))
        audit(con, actor, action + ("/" + body["role"] if action == "role" else ""), target)
        return {"principal_id": target}
