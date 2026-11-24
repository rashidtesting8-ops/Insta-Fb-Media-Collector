"""Offline M4 ledger. Call mutations inside the caller's SQLite write transaction."""
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

from app.ai.payload import select_ai_candidates
from app.publication import WINDOW_FIELDS, window_bounds

ACTIONS = {"collection-panel", "collection-review", "collection-approve", "collection-deny", "collection-revoke", "collection-start"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_revisions (
 job TEXT NOT NULL, revision INTEGER NOT NULL, created_utc TEXT NOT NULL,
 window_start_utc TEXT NOT NULL, window_end_utc TEXT NOT NULL, submitter TEXT,
 manifest_sha TEXT NOT NULL, subset_sha TEXT NOT NULL, manifest BLOB NOT NULL,
 subset TEXT NOT NULL, inventory TEXT NOT NULL, recorded_utc TEXT NOT NULL,
 PRIMARY KEY(job,revision));
CREATE TABLE IF NOT EXISTS collection_decisions (
 job TEXT NOT NULL, revision INTEGER NOT NULL, sequence INTEGER NOT NULL,
 actor TEXT NOT NULL, actor_epoch INTEGER NOT NULL, action TEXT NOT NULL
 CHECK(action IN ('approve','deny','revoke')), reason TEXT NOT NULL, utc TEXT NOT NULL,
 request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 PRIMARY KEY(job,sequence), UNIQUE(job,request_key),
 FOREIGN KEY(job,revision) REFERENCES collection_revisions(job,revision));
CREATE TABLE IF NOT EXISTS collection_ai_starts (
 job TEXT NOT NULL, revision INTEGER NOT NULL, decision INTEGER NOT NULL,
 actor TEXT NOT NULL, actor_epoch INTEGER NOT NULL, utc TEXT NOT NULL, request_key TEXT NOT NULL,
 fingerprint TEXT NOT NULL, PRIMARY KEY(job,decision), UNIQUE(job,request_key),
 FOREIGN KEY(job,decision) REFERENCES collection_decisions(job,sequence),
 FOREIGN KEY(job,revision) REFERENCES collection_revisions(job,revision));
"""


def migrate(con):
    """Explicit additive migration. Production startup never calls this function."""
    triggers = []
    for table, key in (("collection_revisions", "revision"), ("collection_decisions", "sequence"),
                       ("collection_ai_starts", "decision")):
        for operation in ("UPDATE", "DELETE"):
            triggers.append(f"""CREATE TRIGGER IF NOT EXISTS {table}_{operation} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;""")
        collision = f"{key}=NEW.{key}"
        if table != "collection_revisions":
            collision += " OR request_key=NEW.request_key"
        triggers.append(f"""CREATE TRIGGER IF NOT EXISTS {table}_replace BEFORE INSERT ON {table}
            WHEN EXISTS(SELECT 1 FROM {table} WHERE job=NEW.job AND ({collision}))
            BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;""")
    from app.ai_execution import SCHEMA as execution_schema
    con.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + execution_schema + "\n".join(triggers) + "\nCOMMIT;")


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def utc():
    return datetime.now(UTC).isoformat()


def conflict(message):
    return HTTPException(409, message)


def snapshot(root, record):
    job = record["job_id"]
    if not re.fullmatch("[a-f0-9]{32}", job):
        raise conflict("Invalid job identifier")
    folder = Path(root).resolve() / job
    path = folder / "manifest.json"
    if folder.resolve() != folder or path.resolve() != path or not path.is_file():
        raise conflict("Manifest unavailable")
    raw = path.read_bytes()
    manifest = json.loads(raw)
    bounds = window_bounds(record)
    if not bounds or window_bounds(manifest) != bounds or manifest.get("job_id") != job:
        raise conflict("Manifest identity or frozen window mismatch")
    assets = manifest["assets"]
    if len({a["asset_id"] for a in assets}) != len(assets):
        raise conflict("Ambiguous asset identity")
    for asset in assets:
        for item in [asset, *(asset.get("frames") or [])]:
            rel = item.get("local_path")
            if rel and (Path(rel).is_absolute() or not (folder / rel).resolve().is_relative_to(folder)):
                raise conflict("Unsafe asset path")
    candidates = select_ai_candidates(manifest, folder)
    subset = []
    for candidate in candidates.candidates:
        files = []
        for file in candidate.payload_files:
            with file.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            files.append([file.relative_to(folder).as_posix(), checksum])
        if candidate.kind == "image" and candidate.asset.get("sha256") != files[0][1]:
            raise conflict("Eligible image checksum mismatch")
        subset.append({"asset_id": candidate.asset["asset_id"], "files": files})
    subset_text = canonical({"candidates": subset, "inherited": candidates.inherited})
    inventory = canonical(record.get("sources", {}))
    return raw, subset_text, inventory


def accept(con, root, record, expected_revision):
    """Each accepted collection publication is a revision, including identical re-collections."""
    job = record["job_id"]
    previous = con.execute("""SELECT revision,created_utc,window_start_utc,window_end_utc,submitter
        FROM collection_revisions WHERE job=? ORDER BY revision DESC LIMIT 1""", (job,)).fetchone()
    binding = tuple(record.get(field) for field in WINDOW_FIELDS) + (record.get("submitter_principal_id"),)
    stored = con.execute("SELECT payload FROM jobs WHERE id=?", (job,)).fetchone()
    original = json.loads(stored[0]) if stored else {}
    if tuple(original.get(field) for field in (*WINDOW_FIELDS, "submitter_principal_id")) != binding:
        raise conflict("Job creation binding mismatch")
    if (previous[0] if previous else 0) != expected_revision:
        raise HTTPException(412, "Concurrent collection publication")
    if previous and previous[1:] != binding:
        raise conflict("Original window or submitter changed")
    raw, subset, inventory = snapshot(root, record)
    revision = previous[0] + 1 if previous else 1
    con.execute("INSERT INTO collection_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (job, revision, *binding, sha(raw), sha(subset.encode()), raw, subset, inventory, utc()))


def epoch(con, actor):
    account = con.execute("SELECT active,role FROM named_accounts WHERE id=?", (actor,)).fetchone()
    if account != (1, "admin"):
        return None
    return con.execute("""SELECT coalesce(max(id),0) FROM named_audit WHERE target=?
        AND (event='suspend' OR event LIKE 'role/%')""", (actor,)).fetchone()[0]


def review_state(con, root, job):
    row = con.execute("SELECT payload FROM jobs WHERE id=?", (job,)).fetchone()
    if not row:
        raise HTTPException(404, "Unknown job")
    record = json.loads(row[0])
    head = con.execute("""SELECT revision,manifest_sha,subset_sha,inventory,submitter
        FROM collection_revisions WHERE job=? ORDER BY revision DESC LIMIT 1""", (job,)).fetchone()
    if not head:
        raise conflict("No accepted collection revision")
    rev, manifest_sha, subset_sha, inventory, submitter = head
    current = False
    if record["status"] in ("partial_success", "extraction_complete") and record.get("submitter_principal_id") == submitter:
        try:
            raw, subset, sources = snapshot(root, record)
            current = (sha(raw), sha(subset.encode()), sources) == (manifest_sha, subset_sha, inventory)
        except (OSError, ValueError, KeyError, TypeError, HTTPException):
            pass
    decision = con.execute("""SELECT sequence,revision,action,actor,actor_epoch FROM collection_decisions
        WHERE job=? ORDER BY sequence DESC LIMIT 1""", (job,)).fetchone()
    sequence = decision[0] if decision else 0
    start = con.execute("SELECT actor,actor_epoch FROM collection_ai_starts WHERE job=? AND decision=?",
                        (job, sequence)).fetchone()
    state = "pending"
    if decision:
        state = {"approve": "approved", "deny": "denied", "revoke": "revoked"}[decision[2]]
        if decision[1] != rev or (state == "approved" and epoch(con, decision[3]) != decision[4]):
            state = "stale"
    if not current:
        state = "stale"
    started = bool(state == "approved" and start and epoch(con, start[0]) == start[1])
    tag = sha(canonical([job, rev, manifest_sha, subset_sha, sequence, bool(start), state, started]).encode())
    result = dict(job_id=job, revision=rev, manifest_sha256=manifest_sha, eligible_subset_sha256=subset_sha,
                  state=state, start_authorized=started, etag=tag)
    return result, submitter, current, sequence, decision, start


def handle(con, root, operation, body, actor):
    """Trusted caller has verified the Stage 1 assertion/session in this write transaction."""
    action = operation.removeprefix("collection-")
    fields = {"job_id"} if action in ("review", "panel") else {"job_id", "if_match", "request_key"}
    if action in ("approve", "deny", "revoke"):
        fields.add("reason")
    if set(body) != fields or not all(isinstance(v, str) and v.strip() for v in body.values()):
        raise HTTPException(400, "Invalid approval fields")
    actor_epoch = epoch(con, actor)
    if actor_epoch is None:
        raise HTTPException(403, "Named Admin required")
    job = body["job_id"]
    result, submitter, current, sequence, decision, start = review_state(con, root, job)
    tag, rev, state = result['etag'], result['revision'], result['state']
    if action == 'panel':
        from app.approval_panel import panel
        return panel(con, result, actor, submitter, current, start)
    if action == "review":
        return result
    key = body["request_key"]
    if len(key) > 128:
        raise HTTPException(400, "Invalid request key")
    fingerprint = sha(canonical([operation, actor, body]).encode())
    replay = con.execute("""SELECT fingerprint FROM collection_decisions WHERE job=? AND request_key=?
        UNION ALL SELECT fingerprint FROM collection_ai_starts WHERE job=? AND request_key=?""",
                         (job, key, job, key)).fetchone()
    if replay:
        if replay[0] != fingerprint:
            raise conflict("Request key reused")
        return {**result, "idempotent_replay": True}
    if not current or body["if_match"] != tag:
        raise HTTPException(412, "Collection or decision changed")
    if not submitter or not con.execute("SELECT 1 FROM named_accounts WHERE id=?", (submitter,)).fetchone():
        raise HTTPException(403, "Verified submitter required")
    if action == "start":
        if state != "approved" or decision[3] == submitter or start:
            raise conflict("Independent approval and unused start required")
        con.execute("INSERT INTO collection_ai_starts VALUES (?,?,?,?,?,?,?,?)",
                    (job, rev, sequence, actor, actor_epoch, utc(), key, fingerprint))
    else:
        if action == "approve" and actor == submitter:
            raise HTTPException(403, "Independent approval required")
        if action == "revoke" and state != "approved":
            raise conflict("Only current approval can be revoked")
        if (action, state) in (("approve", "approved"), ("deny", "denied")):
            raise conflict("Decision already recorded")
        con.execute("INSERT INTO collection_decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (job, rev, sequence + 1, actor, actor_epoch, action, body["reason"].strip(), utc(), key, fingerprint))
    return handle(con, root, "collection-review", {"job_id": job}, actor)
