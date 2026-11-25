"""Worker-owned execution fencing. No HTTP routes and no implicit database migration."""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi import HTTPException
from requests import HTTPError


class InferenceHeld(ValueError):
    def __init__(self, message: str, *, claimed: bool = False, claim_nonce: str | None = None) -> None:
        super().__init__(message)
        self.claimed = claimed
        self.claim_nonce = claim_nonce


SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_execution_claims(
 job TEXT NOT NULL, revision INTEGER NOT NULL, decision INTEGER NOT NULL,
 claim TEXT NOT NULL UNIQUE, etag TEXT NOT NULL, created TEXT NOT NULL,
 PRIMARY KEY(job,revision));
CREATE TABLE IF NOT EXISTS ai_provider_attempts(
 claim TEXT NOT NULL, logical_request TEXT NOT NULL, mode TEXT NOT NULL, reserved TEXT NOT NULL,
 outcome TEXT NOT NULL CHECK(outcome IN ('dispatching','returned','rejected','ambiguous')),
 finished TEXT, PRIMARY KEY(claim,logical_request));
"""

_current = ContextVar("worker_ai_execution", default=None)
_dispatching = ContextVar("worker_ai_dispatching", default=False)


def now():
    return datetime.now(UTC).isoformat()


def require(ok, message):
    if not ok:
        raise InferenceHeld(message)


@dataclass
class Execution:
    db: Path
    root: Path
    job: str
    nonce: str
    tag: str = ""
    staging: Path | None = None
    eligible: frozenset = frozenset()
    manifest_sha: str = ""
    subset_sha: str = ""
    revision: int = 0

    @contextmanager
    def transaction(self):
        # mode=rw deliberately cannot create a missing job database.
        try:
            with closing(sqlite3.connect(self.db.as_uri() + "?mode=rw", uri=True, timeout=10)) as con, con:
                con.execute("PRAGMA busy_timeout=5000;")
                con.execute("BEGIN IMMEDIATE")
                yield con
        except sqlite3.Error as exc:
            raise InferenceHeld("Execution ledger unavailable or previously claimed") from exc

    def check(self, con):
        from app.collection_approval import handle
        row = con.execute("""SELECT s.actor,d.actor,r.submitter,d.sequence FROM collection_decisions d
            JOIN collection_ai_starts s ON s.job=d.job AND s.decision=d.sequence AND s.revision=d.revision
            JOIN collection_revisions r ON r.job=d.job AND r.revision=d.revision
            WHERE d.job=? AND d.action='approve' ORDER BY d.sequence DESC LIMIT 1""", (self.job,)).fetchone()
        require(row and row[2] and row[1] != row[2], "Independent approval and Start AI required")
        try:
            review = handle(con, self.root, "collection-review", {"job_id": self.job}, row[0])
        except HTTPException as exc:
            raise InferenceHeld("Current named authorization required") from exc
        require(review["start_authorized"] and (not self.tag or review["etag"] == self.tag),
                "Approval, revision or Start AI is stale")
        if self.tag:
            require(con.execute("SELECT 1 FROM ai_execution_claims WHERE claim=? AND job=? AND etag=?",
                                (self.nonce, self.job, self.tag)).fetchone(), "Execution claim mismatch")
        return review, row[3]


@contextmanager
def execution(job_dir, job, processing_consent, start_etag):
    """Trusted worker call only. Consent maps asset ID to (source URL, original SHA-256)."""
    root = Path(os.getenv("PROJECTS_DIR", "./storage/jobs")).resolve()
    folder = Path(job_dir).resolve()
    require(folder == root / job and folder.parent == root and folder.name == job, "Wrong job storage")
    require(not (folder / "curation.json").exists(), "Existing curation preserved")
    run = Execution(root / "_system" / "jobs.sqlite", root, job, uuid4().hex)
    with run.transaction() as con:
        review, decision = run.check(con)
        require(review["etag"] == start_etag, "Requested Start AI does not match this job and revision")
        require(not con.execute("""SELECT 1 FROM ai_provider_attempts a JOIN ai_execution_claims e
                ON a.claim=e.claim WHERE e.job=? AND a.outcome IN ('dispatching','ambiguous')""",
                                (job,)).fetchone(), "Prior request requires reconciliation")
        raw, subset = con.execute("SELECT manifest,subset FROM collection_revisions WHERE job=? AND revision=?",
                                  (job, review["revision"])).fetchone()
        require(sha256(raw).hexdigest() == review["manifest_sha256"] and
                sha256(subset.encode()).hexdigest() == review["eligible_subset_sha256"],
                "Stored approval snapshot integrity mismatch")
        manifest = json.loads(raw)
        selected = json.loads(subset)
        ids = {c["asset_id"] for c in selected["candidates"]} | set(selected["inherited"])
        assets = {a["asset_id"]: a for a in manifest["assets"]}
        require(ids and all(assets[i].get("source_url") and assets[i].get("sha256") and
                            processing_consent.get(i) == (assets[i]["source_url"], assets[i]["sha256"])
                            for i in ids), "Separate asset processing consent required")
        run.tag = review["etag"]
        run.eligible = frozenset(ids)
        run.manifest_sha = review["manifest_sha256"]
        run.subset_sha = review["eligible_subset_sha256"]
        run.revision = review["revision"]
        con.execute("INSERT INTO ai_execution_claims VALUES (?,?,?,?,?,?)",
                    (job, review["revision"], decision, run.nonce, run.tag, now()))

    try:
        with TemporaryDirectory(prefix="pgm-ai-", dir=run.db.parent) as tmp:
            run.staging = Path(tmp)
            (run.staging / "manifest.json").write_bytes(raw)
            for candidate in selected["candidates"]:
                for rel, expected in candidate["files"]:
                    source = (folder / rel).resolve()
                    target = (run.staging / rel).resolve()
                    require(source.is_relative_to(folder) and target.is_relative_to(run.staging), "Unsafe payload file")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    with target.open("rb") as stream:
                        require(file_digest(stream, "sha256").hexdigest() == expected, "Payload checksum changed")
            token = _current.set(run)
            try:
                yield run.staging
            finally:
                _current.reset(token)
    except InferenceHeld as exc:
        exc.claimed = True
        exc.claim_nonce = run.nonce
        raise


def require_pipeline(folder, job):
    run = _current.get()
    require(run and run.job == job and run.staging == Path(folder).resolve(), "Current job execution claim required")


def check_inputs(raw, selected_ids=None):
    run = _current.get()
    require(run and sha256(raw).hexdigest() == run.manifest_sha, "Execution manifest mismatch")
    if selected_ids is not None:
        require(set(selected_ids) <= run.eligible, "Selection outside authorized subset")


def retry_limit(configured):
    return 0 if _current.get() is not None else configured


def checkpoint_analysis(parsed, ids, model, secret, usage):
    """Preserve only a validated bounded MODE A response, while authorization is current."""
    run = _current.get()
    require(run, "Current job execution claim required")
    from app.ai.provider import scrub_secrets
    from app.manifest import atomic_write_json
    analysis = parsed.model_dump()
    encoded = json.dumps(analysis, sort_keys=True, ensure_ascii=False)
    require((not secret or secret not in encoded) and scrub_secrets(encoded) == encoded,
            "Sensitive MODE A checkpoint rejected")
    with publication():
        target = run.db.parent / "ai-checkpoints" / run.nonce / "mode-a-validated.json"
        require(not target.exists(), "Existing MODE A checkpoint preserved")
        record = {"job_id": run.job, "claim": run.nonce, "start_etag": run.tag, "manifest_sha256": run.manifest_sha,
                  "revision": run.revision, "eligible_subset_sha256": run.subset_sha,
                  "asset_ids": sorted(ids), "model": model, "schema_validation": "PASS", "usage": usage,
                  "analysis": analysis}
        record["checkpoint_sha256"] = sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        atomic_write_json(target, record)


@contextmanager
def publication():
    run = _current.get()
    require(run, "Current job execution claim required")
    with run.transaction() as con:
        run.check(con)
        yield


def require_transport():
    require(_dispatching.get(), "Reserved provider attempt required")
    with publication():
        pass


@contextmanager
def attempt(mode, ids, reserve=None):
    run = _current.get()
    require(run, "Current job execution claim required")
    require(mode in ("A", "B") and set(ids) <= run.eligible, "Request outside authorized subset")
    logical = sha256(json.dumps([mode, sorted(set(ids))]).encode()).hexdigest()
    with run.transaction() as con:
        run.check(con)
        require(not con.execute("""SELECT 1 FROM ai_provider_attempts WHERE claim=? AND
            (outcome!='returned' OR logical_request=? OR mode='B')""", (run.nonce, logical)).fetchone(),
                "Previous attempt requires reconciliation or was already sent")
        if mode == "B":
            require(con.execute("SELECT 1 FROM ai_provider_attempts WHERE claim=? AND mode='A' AND outcome='returned'",
                                (run.nonce,)).fetchone(), "Validated MODE A execution required")
        reservation = reserve() if reserve else None
        con.execute("INSERT INTO ai_provider_attempts VALUES (?,?,?,?,?,NULL)",
                    (run.nonce, logical, mode, now(), "dispatching"))
    token = _dispatching.set(True)
    outcome = "ambiguous"
    try:
        yield reservation
        outcome = "rejected"
        with publication():
            pass
        outcome = "returned"
    except HTTPError as exc:
        if exc.response is not None and 400 <= exc.response.status_code < 500 and exc.response.status_code != 408:
            outcome = "rejected"
        raise
    except InferenceHeld:
        outcome = "rejected"
        raise
    finally:
        _dispatching.reset(token)
        with run.transaction() as con:
            con.execute("UPDATE ai_provider_attempts SET outcome=?,finished=? WHERE claim=? AND logical_request=?",
                        (outcome, now(), run.nonce, logical))
