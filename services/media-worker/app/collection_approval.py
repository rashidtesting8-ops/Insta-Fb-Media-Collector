"""Offline M4 ledger. Call mutations inside the caller's SQLite write transaction."""
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

from app.ai.payload import select_ai_candidates
from app.publication import WINDOW_FIELDS, window_bounds

ACTIONS = {
    "collection-panel",
    "collection-review",
    "collection-approve",
    "collection-deny",
    "collection-revoke",
    "collection-start",
    "collection-rights-review",
    "collection-rights-decide",
    "collection-website-policy",
}
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
CREATE TABLE IF NOT EXISTS collection_rights_decisions (
 job TEXT NOT NULL, revision INTEGER NOT NULL, sequence INTEGER NOT NULL,
 asset_id TEXT NOT NULL, asset_sha TEXT NOT NULL, manifest_sha TEXT NOT NULL,
 actor TEXT NOT NULL, actor_epoch INTEGER NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('verified','rejected','revoked')),
 reason TEXT NOT NULL, evidence_ref TEXT NOT NULL, utc TEXT NOT NULL,
 request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 PRIMARY KEY(job,sequence), UNIQUE(job,request_key),
 FOREIGN KEY(job,revision) REFERENCES collection_revisions(job,revision));
"""


def migrate(con):
    """Explicit additive, idempotent migration. Production startup never calls this function."""
    triggers = []
    for table, key in (
        ("collection_revisions", "revision"),
        ("collection_decisions", "sequence"),
        ("collection_ai_starts", "decision"),
        ("collection_rights_decisions", "sequence"),
    ):
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
    include_undated_website = bool(record.get("include_undated_website"))
    candidates = select_ai_candidates(
        manifest, folder, include_undated_website=include_undated_website
    )
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
    subset_doc = {"candidates": subset, "inherited": candidates.inherited}
    if include_undated_website:
        subset_doc["include_undated_website"] = True
    subset_text = canonical(subset_doc)
    inventory = canonical(record.get("sources", {}))
    return raw, subset_text, inventory


def accept(con, root, record, expected_revision, *, precomputed_snapshot=None):
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
    raw, subset, inventory = precomputed_snapshot if precomputed_snapshot is not None else snapshot(root, record)
    revision = previous[0] + 1 if previous else 1
    con.execute("INSERT INTO collection_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (job, revision, *binding, sha(raw), sha(subset.encode()), raw, subset, inventory, utc()))


def epoch(con, actor):
    account = con.execute("SELECT active,role FROM named_accounts WHERE id=?", (actor,)).fetchone()
    if account != (1, "admin"):
        return None
    return con.execute("""SELECT coalesce(max(id),0) FROM named_audit WHERE target=?
        AND (event='suspend' OR event LIKE 'role/%')""", (actor,)).fetchone()[0]


def _matching_precomputed_snapshot(precomputed_snapshot, payload_raw, root, job):
    if precomputed_snapshot is None:
        return None
    expected_payload, expected_stat, snap = precomputed_snapshot
    if expected_payload != payload_raw:
        return None
    try:
        manifest_path = Path(root).resolve() / job / "manifest.json"
        st = manifest_path.stat()
        if (st.st_mtime_ns, st.st_size) == expected_stat:
            return snap
    except OSError:
        return None
    return None


def review_state(con, root, job, *, precomputed_snapshot=None):
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
            snap = _matching_precomputed_snapshot(precomputed_snapshot, row[0], root, job)
            raw, subset, sources = snap if snap is not None else snapshot(root, record)
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


def _rights_table_exists(con) -> bool:
    return bool(
        con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_rights_decisions'").fetchone()
    )


def verified_rights_for_revision(con, job: str, revision: int, manifest_sha: str) -> dict[str, dict]:
    """Return latest active-admin 'verified' rights decisions bound strictly to (job, revision, manifest_sha)."""
    if not _rights_table_exists(con):
        return {}
    rows = con.execute(
        """SELECT sequence, asset_id, asset_sha, actor, actor_epoch, decision, reason, evidence_ref, utc
           FROM collection_rights_decisions
           WHERE job=? AND revision=? AND manifest_sha=?
           ORDER BY sequence ASC""",
        (job, revision, manifest_sha),
    ).fetchall()
    latest: dict[str, dict] = {}
    for seq, asset_id, asset_sha, actor, actor_epoch, decision, reason, evidence_ref, stamp in rows:
        latest[asset_id] = {
            "sequence": seq,
            "asset_id": asset_id,
            "asset_sha": asset_sha,
            "actor": actor,
            "actor_epoch": actor_epoch,
            "decision": decision,
            "reason": reason,
            "evidence_ref": evidence_ref,
            "utc": stamp,
        }
    cleared: dict[str, dict] = {}
    for asset_id, entry in latest.items():
        if entry["decision"] == "verified" and epoch(con, entry["actor"]) == entry["actor_epoch"]:
            cleared[asset_id] = entry
    return cleared


def _rights_review_payload(con, result: dict) -> dict:
    job, rev, manifest_sha = result["job_id"], result["revision"], result["manifest_sha256"]
    history: list[dict] = []
    if _rights_table_exists(con):
        rows = con.execute(
            """SELECT sequence, revision, asset_id, asset_sha, manifest_sha, actor, decision, reason, evidence_ref, utc
               FROM collection_rights_decisions WHERE job=? ORDER BY sequence ASC""",
            (job,),
        ).fetchall()
        history = [
            dict(zip(
                ("sequence", "revision", "asset_id", "asset_sha", "manifest_sha", "actor", "decision", "reason", "evidence_ref", "utc"),
                row,
            ))
            for row in rows
        ]
    cleared = verified_rights_for_revision(con, job, rev, manifest_sha)
    return {
        "job_id": job,
        "revision": rev,
        "manifest_sha256": manifest_sha,
        "state": result["state"],
        "etag": result["etag"],
        "verified_asset_ids": sorted(cleared.keys()),
        "verified_rights": cleared,
        "history": history,
    }


def verify_checkpoint_release(
    db_path: Path, root: Path, job: str, manifest: dict
) -> tuple[bool, str, dict[str, dict], set[str]]:
    """Verify ALL M4 release gates for a public_checkpoint/recorded_extractor delivery plan."""
    import sqlite3
    from contextlib import closing

    required_tables = {
        "collection_revisions",
        "collection_decisions",
        "collection_ai_starts",
        "collection_rights_decisions",
        "ai_execution_claims",
        "ai_provider_attempts",
    }
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as con:
            con.execute("PRAGMA busy_timeout=5000;")
            existing = {
                row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if not required_tables <= existing:
                return False, "m4_ledger_not_migrated", {}, set()
            result, submitter, current, sequence, decision, start = review_state(con, root, job)
            if not current or result["state"] != "approved" or not result["start_authorized"]:
                return False, "collection_approval_or_start_invalid", {}, set()
            if not decision or not start or not submitter or decision[3] == submitter:
                return False, "independent_approval_required", {}, set()
            rev = result["revision"]
            manifest_sha = result["manifest_sha256"]
            claim_row = con.execute(
                "SELECT claim FROM ai_execution_claims WHERE job=? AND revision=? AND decision=? AND etag=?",
                (job, rev, sequence, result["etag"]),
            ).fetchone()
            if not claim_row:
                return False, "ai_execution_not_claimed", {}, set()
            claim = claim_row[0]
            unresolved = con.execute(
                """SELECT 1 FROM ai_provider_attempts a
                   JOIN ai_execution_claims e ON a.claim=e.claim
                   WHERE e.job=? AND a.outcome IN ('dispatching','ambiguous')""",
                (job,),
            ).fetchone()
            if unresolved:
                return False, "ai_attempt_unresolved", {}, set()
            mode_b_ok = con.execute(
                "SELECT 1 FROM ai_provider_attempts WHERE claim=? AND mode='B' AND outcome='returned'",
                (claim,),
            ).fetchone()
            if not mode_b_ok:
                return False, "ai_curation_attempt_unverified", {}, set()
            stored = con.execute(
                "SELECT manifest, subset FROM collection_revisions WHERE job=? AND revision=?",
                (job, rev),
            ).fetchone()
            if not stored or sha(stored[0]) != manifest_sha or sha(stored[1].encode()) != result["eligible_subset_sha256"]:
                return False, "manifest_revision_integrity_mismatch", {}, set()
            if manifest.get("job_id") != job:
                return False, "manifest_job_mismatch", {}, set()
            subset_doc = json.loads(stored[1])
            eligible_ids = {c["asset_id"] for c in subset_doc.get("candidates", [])} | set(subset_doc.get("inherited", {}))
            cleared = verified_rights_for_revision(con, job, rev, manifest_sha)
            return True, "ok", cleared, eligible_ids
    except (sqlite3.Error, HTTPException, OSError, ValueError):
        return False, "m4_release_verification_failed", {}, set()


def handle(con, root, operation, body, actor, *, precomputed_snapshot=None):
    """Trusted caller has verified the Stage 1 assertion/session in this write transaction."""
    action = operation.removeprefix("collection-")
    if action == "rights-review":
        fields = {"job_id"}
    elif action == "rights-decide":
        fields = {"job_id", "asset_id", "decision", "reason", "evidence_ref", "if_match", "request_key"}
    elif action == "website-policy":
        fields = {"job_id", "include_undated_website", "reason", "if_match", "request_key"}
    else:
        fields = {"job_id"} if action in ("review", "panel") else {"job_id", "if_match", "request_key"}
        if action in ("approve", "deny", "revoke"):
            fields.add("reason")
    if action == "website-policy":
        if (
            set(body) != fields
            or not isinstance(body.get("include_undated_website"), bool)
            or not all(isinstance(body[k], str) and body[k].strip() for k in fields - {"include_undated_website"})
        ):
            raise HTTPException(400, "Invalid website policy fields")
    elif set(body) != fields or not all(isinstance(v, str) and v.strip() for v in body.values()):
        raise HTTPException(400, "Invalid approval fields")
    actor_epoch = epoch(con, actor)
    if actor_epoch is None:
        raise HTTPException(403, "Named Admin required")
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_revisions'").fetchone():
        raise HTTPException(503, "M4 collection ledger is not migrated")
    job = body["job_id"]
    result, submitter, current, sequence, decision, start = review_state(
        con, root, job, precomputed_snapshot=precomputed_snapshot
    )
    tag, rev, state = result['etag'], result['revision'], result['state']
    if action == 'panel':
        from app.approval_panel import panel
        return panel(con, result, actor, submitter, current, start)
    if action == "review":
        return result
    if action == "rights-review":
        return _rights_review_payload(con, result)
    key = body["request_key"]
    if len(key) > 128:
        raise HTTPException(400, "Invalid request key")
    fingerprint = sha(canonical([operation, actor, body]).encode())
    if action == "website-policy":
        if not current or body["if_match"] != tag:
            raise HTTPException(412, "Collection or decision changed")
        if not submitter or not con.execute("SELECT 1 FROM named_accounts WHERE id=?", (submitter,)).fetchone():
            raise HTTPException(403, "Verified submitter required")
        row = con.execute("SELECT payload FROM jobs WHERE id=?", (job,)).fetchone()
        if not row:
            raise HTTPException(404, "Unknown job")
        record = json.loads(row[0])
        include_flag = bool(body["include_undated_website"])
        updated = {**record, "include_undated_website": include_flag, "updated_at": utc()}
        accept(con, root, updated, rev)
        con.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(updated), job))
        from app.named_auth import audit
        audit(con, actor, "website-policy/" + ("include" if include_flag else "exclude"), job)
        return handle(con, root, "collection-review", {"job_id": job}, actor)
    if action == "rights-decide":
        if not _rights_table_exists(con):
            raise HTTPException(503, "Rights ledger not migrated")
        if body["decision"] not in ("verified", "rejected", "revoked"):
            raise HTTPException(400, "Invalid rights decision")
        replay = con.execute(
            "SELECT fingerprint FROM collection_rights_decisions WHERE job=? AND request_key=?",
            (job, key),
        ).fetchone()
        if replay:
            if replay[0] != fingerprint:
                raise conflict("Request key reused")
            return {**_rights_review_payload(con, result), "idempotent_replay": True}
        if not current or body["if_match"] != tag:
            raise HTTPException(412, "Collection or decision changed")
        if not submitter or not con.execute("SELECT 1 FROM named_accounts WHERE id=?", (submitter,)).fetchone():
            raise HTTPException(403, "Verified submitter required")
        raw_manifest = con.execute(
            "SELECT manifest FROM collection_revisions WHERE job=? AND revision=?",
            (job, rev),
        ).fetchone()[0]
        manifest_doc = json.loads(raw_manifest)
        asset = next((a for a in manifest_doc.get("assets", []) if a.get("asset_id") == body["asset_id"]), None)
        if (
            asset is None
            or asset.get("type") == "frame"
            or asset.get("extraction_status") != "downloaded"
            or not asset.get("sha256")
        ):
            raise conflict("Asset ineligible for rights verification")
        next_seq = con.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM collection_rights_decisions WHERE job=?",
            (job,),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO collection_rights_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job,
                rev,
                next_seq,
                body["asset_id"],
                str(asset["sha256"]),
                result["manifest_sha256"],
                actor,
                actor_epoch,
                body["decision"],
                body["reason"].strip(),
                body["evidence_ref"].strip(),
                utc(),
                key,
                fingerprint,
            ),
        )
        return _rights_review_payload(con, result)
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
    return handle(con, root, "collection-review", {"job_id": job}, actor, precomputed_snapshot=precomputed_snapshot)


if __name__ == "__main__":
    import argparse
    import sqlite3
    from contextlib import closing

    parser = argparse.ArgumentParser(description="Explicit additive M4 collection approval & rights ledger migration.")
    parser.add_argument("--migrate-db", type=Path, required=True, help="Path to existing jobs.sqlite database")
    args = parser.parse_args()
    if not args.migrate_db.is_file():
        raise SystemExit(f"Refusing to create missing database: {args.migrate_db}")
    with closing(sqlite3.connect(args.migrate_db, timeout=10)) as con:
        migrate(con)
    print(f"M4_MIGRATION_OK {args.migrate_db}")
