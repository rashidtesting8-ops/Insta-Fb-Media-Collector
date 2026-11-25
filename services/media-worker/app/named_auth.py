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
import threading
import time
import uuid
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

SESSION_SECONDS = 8 * 3600
MIN_BOOTSTRAP_PASSWORD_LEN = 12
SETUP_TOKEN_MIN_TTL_S = 60
SETUP_TOKEN_MAX_TTL_S = 3600
SETUP_TOKEN_DEFAULT_TTL_S = 900
_ARGON2_SLOTS = threading.BoundedSemaphore(4)
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_TABLES = frozenset({
    "named_accounts",
    "named_sessions",
    "named_bootstrap",
    "named_setup_tokens",
    "named_nonces",
    "named_intakes",
    "named_audit",
})
SCHEMA = """
CREATE TABLE IF NOT EXISTS named_accounts (
 id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('admin','none')), active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS named_sessions (
 digest TEXT PRIMARY KEY, actor TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS named_bootstrap (singleton INTEGER PRIMARY KEY CHECK(singleton=1));
CREATE TABLE IF NOT EXISTS named_setup_tokens (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 digest TEXT NOT NULL,
 verified_operator TEXT NOT NULL,
 expires INTEGER NOT NULL);
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


def _connect(db):
    con = sqlite3.connect(db, timeout=10)
    con.execute("PRAGMA busy_timeout=5000;")
    mode_row = con.execute("PRAGMA journal_mode;").fetchone()
    if not mode_row or str(mode_row[0]).lower() != "wal":
        with _SCHEMA_LOCK:
            mode_row = con.execute("PRAGMA journal_mode;").fetchone()
            if not mode_row or str(mode_row[0]).lower() != "wal":
                con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con


def _ensure_schema(con):
    existing = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if not _SCHEMA_TABLES <= existing:
        with _SCHEMA_LOCK:
            existing = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if not _SCHEMA_TABLES <= existing:
                con.executescript(SCHEMA)


def _parse_json_body(raw):
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON body") from None
    if not isinstance(body, dict):
        raise denied()
    return body


def password_hash(password):
    raw = password_bytes(password)
    encoded = ctypes.create_string_buffer(128)
    with _ARGON2_SLOTS:
        if sodium().crypto_pwhash_str_alg(encoded, raw, len(raw), 3, 64 * 1024**2, 2):
            raise RuntimeError("Argon2id unavailable")
    return encoded.value.decode()


def verify_password(stored_hash, raw_password):
    with _ARGON2_SLOTS:
        return sodium().crypto_pwhash_str_verify(stored_hash.encode(), raw_password, len(raw_password)) == 0


@lru_cache(maxsize=1)
def dummy_hash():
    return password_hash(secrets.token_urlsafe(32))


def validate_account_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", name):
        raise HTTPException(400, "Invalid account name")
    return name


def create_account(con, name, password, *, precomputed_hash=None):
    validate_account_name(name)
    hashed = precomputed_hash if precomputed_hash is not None else password_hash(password)
    principal = uuid.uuid4().hex
    try:
        con.execute("INSERT INTO named_accounts VALUES (?,?,?,'admin',1)",
                    (principal, name, hashed))
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
    validate_account_name(name)
    password_bytes(password)
    with closing(_connect(db)) as con:
        _ensure_schema(con)
        if con.execute("SELECT 1 FROM named_bootstrap").fetchone() or con.execute(
                "SELECT 1 FROM named_accounts").fetchone():
            raise HTTPException(409, "Bootstrap already consumed")
    # Run memory-hard Argon2id outside SQLite's write lock.
    hashed = password_hash(password)
    with closing(_connect(db)) as con, con:
        _ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM named_bootstrap").fetchone() or con.execute(
                "SELECT 1 FROM named_accounts").fetchone():
            raise HTTPException(409, "Bootstrap already consumed")
        con.execute("INSERT INTO named_bootstrap VALUES (1)")
        con.execute("DELETE FROM named_setup_tokens")
        actor = create_account(con, name, password, precomputed_hash=hashed)
        audit(con, verified_operator, "bootstrap", actor)
        return actor


def _validate_operator_id(verified_operator):
    if not isinstance(verified_operator, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", verified_operator):
        raise HTTPException(400, "Invalid verified operator identifier")
    return verified_operator


def issue_setup_token(db, *, verified_operator, ttl_seconds=SETUP_TOKEN_DEFAULT_TTL_S):
    """Mint a single-use cryptographically random browser setup capability.

    Only succeeds while zero Named Admins exist and bootstrap is unconsumed.
    Stores only the SHA-256 digest in SQLite; never logs or persists plaintext.
    """
    operator = _validate_operator_id(verified_operator)
    if not isinstance(ttl_seconds, int) or not (SETUP_TOKEN_MIN_TTL_S <= ttl_seconds <= SETUP_TOKEN_MAX_TTL_S):
        raise HTTPException(400, f"TTL must be between {SETUP_TOKEN_MIN_TTL_S} and {SETUP_TOKEN_MAX_TTL_S} seconds")
    raw_token = secrets.token_urlsafe(32)
    token_digest = digest(raw_token)
    now = int(time.time())
    expires = now + ttl_seconds
    with closing(_connect(db)) as con, con:
        _ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM named_bootstrap").fetchone() or con.execute(
                "SELECT 1 FROM named_accounts").fetchone():
            raise HTTPException(409, "Bootstrap already consumed")
        con.execute(
            "INSERT OR REPLACE INTO named_setup_tokens (singleton, digest, verified_operator, expires) VALUES (1,?,?,?)",
            (token_digest, operator, expires),
        )
        audit(con, operator, "setup-token-issued", "singleton")
    return {"token": raw_token, "expires_at": expires, "ttl_seconds": ttl_seconds}


def bootstrap_status(db):
    """Return whether browser one-time setup is currently available (non-secret booleans only)."""
    now = int(time.time())
    with closing(_connect(db)) as con:
        existing = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "named_bootstrap" in existing and con.execute("SELECT 1 FROM named_bootstrap").fetchone():
            return {"available": False}
        if "named_accounts" in existing and con.execute("SELECT 1 FROM named_accounts").fetchone():
            return {"available": False}
        if "named_setup_tokens" not in existing:
            return {"available": False}
        row = con.execute("SELECT expires FROM named_setup_tokens WHERE singleton=1").fetchone()
        return {"available": bool(row and int(row[0]) > now)}


def browser_bootstrap(db, *, setup_token, name, password, confirm_password):
    """Atomically create Admin #1 using a single-use setup token, reusing Argon2id and audit ledger."""
    if not isinstance(setup_token, str) or not (16 <= len(setup_token) <= 256):
        raise HTTPException(403, "Invalid or expired setup token")
    now = int(time.time())
    presented_digest = digest(setup_token)

    # Pre-check bootstrap lock and setup capability before password validation/hashing
    # so unauthenticated callers cannot trigger Argon2id work or probe rules.
    with closing(_connect(db)) as con:
        _ensure_schema(con)
        if con.execute("SELECT 1 FROM named_bootstrap").fetchone() or con.execute(
                "SELECT 1 FROM named_accounts").fetchone():
            raise HTTPException(409, "Bootstrap already consumed")
        token_row = con.execute(
            "SELECT digest, verified_operator, expires FROM named_setup_tokens WHERE singleton=1"
        ).fetchone()
    if (
        not token_row
        or int(token_row[2]) <= now
        or not hmac.compare_digest(str(token_row[0]), presented_digest)
    ):
        raise HTTPException(403, "Invalid or expired setup token")

    validate_account_name(name)
    if not isinstance(password, str) or not isinstance(confirm_password, str):
        raise HTTPException(400, "Password and confirmation are required")
    if len(password) < MIN_BOOTSTRAP_PASSWORD_LEN:
        raise HTTPException(400, f"Password must be at least {MIN_BOOTSTRAP_PASSWORD_LEN} characters")
    pw_bytes = password_bytes(password)
    confirm_bytes = password_bytes(confirm_password)
    if not hmac.compare_digest(pw_bytes, confirm_bytes):
        raise HTTPException(400, "Password confirmation does not match")

    # Run memory-hard Argon2id strictly outside SQLite's write lock.
    hashed = password_hash(password)

    with closing(_connect(db)) as con, con:
        _ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        now_commit = int(time.time())
        if con.execute("SELECT 1 FROM named_bootstrap").fetchone() or con.execute(
                "SELECT 1 FROM named_accounts").fetchone():
            raise HTTPException(409, "Bootstrap already consumed")
        token_row = con.execute(
            "SELECT digest, verified_operator, expires FROM named_setup_tokens WHERE singleton=1"
        ).fetchone()
        if (
            not token_row
            or int(token_row[2]) <= now_commit
            or not hmac.compare_digest(str(token_row[0]), presented_digest)
        ):
            raise HTTPException(403, "Invalid or expired setup token")
        verified_operator = str(token_row[1])
        con.execute("INSERT INTO named_bootstrap VALUES (1)")
        con.execute("DELETE FROM named_setup_tokens")
        actor = create_account(con, name, password, precomputed_hash=hashed)
        audit(con, verified_operator, "bootstrap", actor)
        return actor


def is_local_bootstrap_peer(client_host, headers):
    """Fail-closed loopback verification for browser bootstrap endpoints.

    Rejects any request carrying proxy forwarding headers. Outside Docker, only
    loopback client IPs (127.0.0.1 / ::1) are permitted. Inside Docker (where
    compose.media.yml publishes ports strictly on 127.0.0.1), permits loopback
    or RFC1918 local bridge gateway peers only when zero forwarding headers exist.
    """
    import ipaddress

    for header in ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "true-client-ip"):
        if headers.get(header):
            return False
    if not client_host:
        return False
    host = str(client_host).strip()
    if host == "testclient":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if os.path.exists("/.dockerenv") and ip.is_private and not ip.is_multicast and not ip.is_unspecified:
        return True
    return False


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


def _consume_nonce(con, claims, now):
    con.execute("BEGIN IMMEDIATE")
    con.execute("DELETE FROM named_nonces WHERE expires<=?", (now,))
    try:
        con.execute("INSERT INTO named_nonces VALUES (?,?)", (claims["jti"], claims["exp"]))
    except sqlite3.IntegrityError:
        raise denied() from None
    con.commit()


def _precompute_collection_snapshot(db, approval_root, body):
    """Hash manifest/payload files outside BEGIN IMMEDIATE for collection-* actions."""
    job_id = body.get("job_id")
    if not approval_root or not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return None
    try:
        with closing(_connect(db)) as con:
            row = con.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        record = json.loads(row[0])
        if record.get("status") not in ("partial_success", "extraction_complete"):
            return None
        manifest_path = Path(approval_root).resolve() / job_id / "manifest.json"
        if not manifest_path.is_file():
            return None
        st = manifest_path.stat()
        from app.collection_approval import snapshot
        snap = snapshot(approval_root, record)
        return (row[0], (st.st_mtime_ns, st.st_size), snap)
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, HTTPException):
        return None


def verify_admin_action(db, action, raw, header, session):
    """Verify HMAC assertion, single-use replay nonce, and active named Admin session, then release DB lock."""
    claims = verify_assertion(header, action, raw, session)
    body = _parse_json_body(raw)
    session_hash = digest(session)
    now = int(time.time())
    with closing(_connect(db)) as con, con:
        _ensure_schema(con)
        _consume_nonce(con, claims, now)
        if claims["exp"] <= int(time.time()):
            raise denied()
        actor, role = session_actor(con, session_hash)
        if role != "admin":
            raise denied()
    return actor, body


def dispatch(db, action, raw, header, session, *, approval_root=None):
    claims = verify_assertion(header, action, raw, session)
    body = _parse_json_body(raw)
    session_hash = digest(session)
    now = int(time.time())

    # Consume replay nonce in a minimal write transaction first.
    with closing(_connect(db)) as con, con:
        _ensure_schema(con)
        _consume_nonce(con, claims, now)

    if claims["exp"] <= int(time.time()):
        raise denied()

    if action == "bootstrap-status":
        if body:
            raise HTTPException(400, "Unexpected payload")
        return bootstrap_status(db)

    if action == "bootstrap":
        allowed_keys = {"setup_token", "name", "password", "confirm_password"}
        if set(body) != allowed_keys:
            raise HTTPException(400, "Invalid bootstrap request")
        actor = browser_bootstrap(
            db,
            setup_token=body.get("setup_token"),
            name=body.get("name"),
            password=body.get("password"),
            confirm_password=body.get("confirm_password"),
        )
        return {"principal_id": actor}

    if action == "login":
        raw_password = password_bytes(body.get("password"))
        if not isinstance(body.get("name"), str):
            raise denied()
        with closing(_connect(db)) as con:
            account = con.execute(
                "SELECT id,password FROM named_accounts WHERE name=? AND active=1 AND role='admin'",
                (body.get("name"),),
            ).fetchone()
        stored_hash = account[1] if account else dummy_hash()
        # Run memory-hard Argon2id verify strictly outside any SQLite transaction/lock.
        verified = verify_password(stored_hash, raw_password)
        if not verified or not account:
            raise denied()
        with closing(_connect(db)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            if claims["exp"] <= int(time.time()):
                raise denied()
            # Re-verify account status atomically after Argon2id verify (prevents suspension TOCTOU).
            still_active = con.execute(
                "SELECT 1 FROM named_accounts WHERE id=? AND active=1 AND role='admin'",
                (account[0],),
            ).fetchone()
            if not still_active:
                raise denied()
            token = secrets.token_urlsafe(32)
            con.execute("DELETE FROM named_sessions WHERE digest=? OR expires<=?", (session_hash, now))
            con.execute("INSERT INTO named_sessions VALUES (?,?,?)",
                        (digest(token), account[0], now + SESSION_SECONDS))
            audit(con, account[0], "login", account[0])
            return {"session": token}

    precomputed_hash = None
    if action == "create":
        with closing(_connect(db)) as con:
            _actor, _role = session_actor(con, session_hash)
            if _role != "admin":
                raise denied()
        validate_account_name(body.get("name"))
        # Run Argon2id hashing outside SQLite's write lock.
        precomputed_hash = password_hash(body.get("password"))

    precomputed_snapshot = None
    if action.startswith("collection-") and approval_root is not None:
        precomputed_snapshot = _precompute_collection_snapshot(db, approval_root, body)

    with closing(_connect(db)) as con, con:
        con.execute("BEGIN IMMEDIATE")
        if claims["exp"] <= int(time.time()):
            raise denied()
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
            con.execute("DELETE FROM named_intakes WHERE expires<=?", (now,))
            previous = con.execute(
                "SELECT payload FROM named_intakes WHERE ticket=? AND expires>?",
                (ticket, now),
            ).fetchone()
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
            return handle(con, approval_root, action, body, actor, precomputed_snapshot=precomputed_snapshot)
        if action == "create":
            target = create_account(con, body.get("name"), body.get("password"), precomputed_hash=precomputed_hash)
        else:
            target = body.get("principal_id")
            if not isinstance(target, str) or not con.execute(
                    "SELECT 1 FROM named_accounts WHERE id=?", (target,)).fetchone():
                raise denied()
            if action == "suspend":
                remaining = con.execute(
                    "SELECT count(*) FROM named_accounts WHERE active=1 AND role='admin' AND id!=?",
                    (target,),
                ).fetchone()[0]
                if remaining < 1:
                    raise HTTPException(409, "Cannot suspend the last active admin")
                con.execute("UPDATE named_accounts SET active=0 WHERE id=?", (target,))
            elif action == "role" and body.get("role") in ("none", "admin"):
                if body["role"] == "none":
                    remaining = con.execute(
                        "SELECT count(*) FROM named_accounts WHERE active=1 AND role='admin' AND id!=?",
                        (target,),
                    ).fetchone()[0]
                    if remaining < 1:
                        raise HTTPException(409, "Cannot demote the last active admin")
                con.execute("UPDATE named_accounts SET role=? WHERE id=?", (body["role"], target))
            elif action == "request-recovery":
                audit(con, actor, "recovery-requested", target)
                return {"status": "pending-authority"}
            else:
                raise denied()
            con.execute("DELETE FROM named_sessions WHERE actor=?", (target,))
        audit(con, actor, action + ("/" + body["role"] if action == "role" else ""), target)
        return {"principal_id": target}
