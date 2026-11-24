"""Opt-in collection checkpoint engine for offline transports.

The existing worker does not invoke this engine automatically. Adapters receive
opaque cursors and must supply normalized source records and validated bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

from app.publication import WINDOW_FIELDS, record_publication_review, window_bounds


@dataclass(frozen=True)
class Attachment:
    id: str
    url: str
    kind: str
    publication_evidence: dict | None = None
    identity_status: str = "derived"


@dataclass(frozen=True)
class SourceItem:
    id: str
    url: str
    attachments: tuple[Attachment, ...] = ()
    expected_ids: tuple[str, ...] = ()
    status: str = "discovered"
    identity_status: str = "derived"
    expected_count: int | None = None
    shortcode: str | None = None


@dataclass(frozen=True)
class InventoryPage:
    items: tuple[SourceItem, ...]
    next_cursor: str | None = None
    issues: tuple[str, ...] = ()
    stop_reason: str | None = None


class Inventory(Protocol):
    def page(self, cursor: str | None) -> InventoryPage: ...


@dataclass(frozen=True)
class Download:
    size: int
    chunks: Iterable[bytes]
    offset: int = 0
    revision: str | None = None
    sha256: str | None = None
    close: Callable[[], None] | None = None


class SourceStop(Exception):
    """A sanitized adapter stop, such as rate limiting or access denial."""

    def __init__(self, reason: str):
        super().__init__(reason if reason in {
            "access_blocked", "restricted", "rate_limited", "document_cap", "request_budget", "time_budget",
            "public_inventory_unavailable", "truncated_listing", "unsupported_media",
            "recorded_bytes_unavailable", "listing_interrupted", "listing_cap", "invalid_continuation",
        } else "source_stopped")


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _key(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


class CollectionCheckpoint:
    def __init__(self, job_dir: Path, job: dict):
        if not job_dir.is_dir() or window_bounds(job) is None or job_dir.name != job.get("job_id"):
            raise ValueError("Checkpoint requires the existing job ID and frozen window")
        self.root = job_dir / "collection"
        self.root.mkdir(parents=True, exist_ok=True)
        self.window = {key: job[key] for key in WINDOW_FIELDS}
        self.job_id = job["job_id"]
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS job (id INTEGER PRIMARY KEY, scope TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS source (
                name TEXT PRIMARY KEY, scope TEXT NOT NULL, cursor TEXT,
                exhausted INTEGER NOT NULL DEFAULT 0, stop TEXT, issues TEXT NOT NULL DEFAULT '[]')""")
            db.execute("""CREATE TABLE IF NOT EXISTS page (
                source TEXT, cursor TEXT, PRIMARY KEY (source, cursor))""")
            db.execute("""CREATE TABLE IF NOT EXISTS item (
                source TEXT, id TEXT, url TEXT, status TEXT, PRIMARY KEY (source, id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS item_identity (
                source TEXT, id TEXT, status TEXT, PRIMARY KEY (source,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS item_expectation (
                source TEXT, id TEXT, expected_count INTEGER, PRIMARY KEY (source,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS item_shortcode (
                source TEXT, id TEXT, shortcode TEXT, PRIMARY KEY (source,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS media (
                key TEXT PRIMARY KEY, source TEXT, item TEXT, native_id TEXT,
                metadata TEXT, status TEXT, size INTEGER, revision TEXT, expected TEXT, digest TEXT)""")
            scope = json.dumps({"job_id": self.job_id, **self.window}, sort_keys=True)
            db.execute("INSERT OR IGNORE INTO job VALUES (1, ?)", (scope,))
            if db.execute("SELECT scope FROM job WHERE id=1").fetchone()[0] != scope:
                raise ValueError("Checkpoint does not match the original job window")

    @contextmanager
    def _transaction(self):
        with closing(sqlite3.connect(self.root / "checkpoints.sqlite")) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    def hold_source(self, source, scope, reason):
        reason = str(SourceStop(reason))
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO source (name, scope) VALUES (?, ?)", (source, scope))
            if db.execute("SELECT scope FROM source WHERE name=?", (source,)).fetchone()[0] != scope:
                raise ValueError("Checkpoint source scope changed")
            db.execute("UPDATE source SET stop=? WHERE name=?", (reason, source))
        return self.report(source)

    def collect(self, source: str, scope: str, inventory: Inventory, *, page_budget: int) -> None:
        if source not in ("website", "instagram", "facebook") or page_budget < 1:
            raise ValueError("Invalid source or page budget")
        for _ in range(page_budget):
            with self._transaction() as db:
                db.execute("INSERT OR IGNORE INTO source (name, scope) VALUES (?, ?)", (source, scope))
                state = db.execute("SELECT * FROM source WHERE name=?", (source,)).fetchone()
                if state["scope"] != scope:
                    raise ValueError("Checkpoint source scope changed")
                if state["exhausted"]:
                    db.execute("UPDATE source SET stop='listing_exhausted' WHERE name=?", (source,))
                    return
                cursor = state["cursor"]
                token = json.dumps(cursor)
                if db.execute("SELECT 1 FROM page WHERE source=? AND cursor=?", (source, token)).fetchone():
                    db.execute("UPDATE source SET stop='pagination_loop' WHERE name=?", (source,))
                    return
                try:
                    page = inventory.page(cursor)
                except SourceStop as exc:
                    db.execute("UPDATE source SET stop=? WHERE name=?", (str(exc), source))
                    return
                except (OSError, ValueError):
                    db.execute("UPDATE source SET stop='cursor_failure' WHERE name=?", (source,))
                    return

                for item in page.items:
                    db.execute("INSERT OR IGNORE INTO item_identity VALUES (?, ?, ?)",
                               (source, item.id, item.identity_status))
                    if item.expected_count is not None:
                        db.execute("INSERT OR IGNORE INTO item_expectation VALUES (?,?,?)",
                                   (source, item.id, item.expected_count))
                    if item.shortcode:
                        db.execute("INSERT OR IGNORE INTO item_shortcode VALUES (?,?,?)",
                                   (source, item.id, item.shortcode))
                    db.execute("""INSERT INTO item VALUES (?, ?, ?, ?)
                        ON CONFLICT(source,id) DO UPDATE SET status=excluded.status""",
                        (source, item.id, item.url, item.status))
                    for attachment in item.attachments:
                        key = _key(source, item.id, attachment.id)
                        db.execute("""INSERT INTO media (key,source,item,native_id,metadata,status)
                            VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE
                            SET metadata=excluded.metadata,status=excluded.status
                            WHERE media.status='missing_attachment'""",
                            (key, source, item.id, attachment.id, json.dumps(asdict(attachment)),
                             "thumbnail" if attachment.kind == "thumbnail" else "pending"))
                    for missing in set(item.expected_ids) - {a.id for a in item.attachments}:
                        db.execute("""INSERT OR IGNORE INTO media (key,source,item,native_id,status)
                            VALUES (?,?,?,?,?)""", (_key(source, item.id, missing), source, item.id,
                                                      missing, "missing_attachment"))
                issues = list(dict.fromkeys(json.loads(state["issues"]) + list(page.issues)))
                if not page.stop_reason:
                    db.execute("INSERT INTO page VALUES (?,?)", (source, token))
                exhausted = page.next_cursor is None and not page.stop_reason
                next_cursor = cursor if page.stop_reason else page.next_cursor
                db.execute("UPDATE source SET cursor=?,exhausted=?,stop=?,issues=? WHERE name=?",
                           (next_cursor, exhausted, page.stop_reason or ("listing_exhausted" if exhausted else "page_budget"),
                            json.dumps(issues), source))
                if exhausted:
                    return

    def download(self, key: str, fetch: Callable[[str, int, str | None], Download], *, byte_limit: int) -> str:
        """Continue only against a validated revision. Restart at zero otherwise."""
        with closing(sqlite3.connect(self.root / "transfer-lock.sqlite")) as lock, lock:
            lock.execute("BEGIN IMMEDIATE")
            return self._download(key, fetch, byte_limit=byte_limit)

    def run_source(self, source, scope, inventory, fetch, *, page_budget, transfer_budget, byte_limit):
        if (any(type(value) is not int or value < 1 for value in (page_budget, byte_limit))
                or type(transfer_budget) is not int or transfer_budget < 0):
            raise ValueError("Invalid collection budgets")
        self.collect(source, scope, inventory, page_budget=page_budget)
        report = self.report(source)
        stop_codes = ("access_blocked", "restricted", "rate_limited", "request_budget", "time_budget",
                      "public_inventory_unavailable", "source_stopped", "invalid_continuation")
        if report["stop_reason"] in stop_codes:
            return report
        attempted = 0
        stop = None
        for asset in report["assets"]:
            if asset.get("kind") not in ("image", "video"):
                continue
            if asset["status"] == "verified":
                path = self.root.parent / asset["local_path"]
                if path.is_file() and digest_file(path) == asset["digest"]:
                    continue
            if attempted == transfer_budget:
                stop = "transfer_budget"
                break
            attempted += 1
            status = self.download(asset["asset_id"], fetch, byte_limit=byte_limit)
            if status in stop_codes:
                stop = status
                break
        report = self.report(source)
        if stop:
            report["issues"] = list(dict.fromkeys([*report["issues"], stop]))
            report["coverage"] = "incomplete"
        return report

    def _download(self, key, fetch, *, byte_limit):
        with self._transaction() as db:
            row = db.execute("SELECT * FROM media WHERE key=?", (key,)).fetchone()
            if row is None or not row["metadata"] or row["status"] == "thumbnail":
                raise ValueError("No downloadable attachment")
            url = json.loads(row["metadata"])["url"]
            part = self.root / (key + ".part")
            final = self.root / (key + ".media")
            if row["digest"] and final.is_file() and digest_file(final) == row["digest"]:
                db.execute("UPDATE media SET status='verified' WHERE key=?", (key,))
                return "verified"
            if row["digest"] and part.is_file() and digest_file(part) == row["digest"]:
                os.replace(part, final)
                db.execute("UPDATE media SET status='verified' WHERE key=?", (key,))
                return "verified"
            offset = part.stat().st_size if part.is_file() and row["revision"] else 0
            revision = row["revision"] if offset else None
        response = None
        try:
            response = fetch(url, offset, revision)
            if offset and (response.offset != offset or response.revision != revision
                           or response.size != row["size"] or response.sha256 != row["expected"]):
                offset = 0
                if response.offset != 0:
                    if response.close:
                        response.close()
                    response = None
                    response = fetch(url, 0, None)
            if response.offset != offset or not 0 <= offset <= response.size <= byte_limit:
                self._download_status(key, "byte_limit_or_invalid_range")
                return "byte_limit_or_invalid_range"
            if not offset:
                part.unlink(missing_ok=True)
            # Commit the transfer identity before writing a partial payload.
            with self._transaction() as db:
                db.execute("UPDATE media SET size=?,revision=?,expected=?,digest=NULL,status='downloading' WHERE key=?",
                           (response.size, response.revision, response.sha256, key))
            with part.open("ab" if offset else "wb") as out:
                count = offset
                for chunk in response.chunks:
                    count += len(chunk)
                    if count > response.size:
                        raise ValueError("Declared file length exceeded")
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if count != response.size:
                raise OSError("Incomplete transfer")
            digest = digest_file(part)
            if response.sha256 and digest != response.sha256:
                part.unlink()
                self._download_status(key, "checksum_mismatch")
                return "checksum_mismatch"
            # Store the digest before publishing the file. A restart can reconcile either side of the rename.
            with self._transaction() as db:
                db.execute("UPDATE media SET digest=?,status='committing' WHERE key=?", (digest, key))
            os.replace(part, final)
            self._download_status(key, "verified")
            return "verified"
        except SourceStop as exc:
            self._download_status(key, str(exc))
            return str(exc)
        except (OSError, ValueError):
            self._download_status(key, "download_interrupted")
            return "download_interrupted"
        finally:
            if response is not None and response.close:
                response.close()

    def _download_status(self, key: str, status: str) -> None:
        with self._transaction() as db:
            db.execute("UPDATE media SET status=? WHERE key=?", (status, key))

    def report(self, source: str) -> dict:
        with self._transaction() as db:
            state = dict(db.execute("SELECT * FROM source WHERE name=?", (source,)).fetchone())
            rows = [dict(row) for row in db.execute("SELECT * FROM media WHERE source=? ORDER BY key", (source,))]
            items = [dict(row) for row in db.execute("""
                SELECT item.id,item.url,item.status,COALESCE(item_identity.status,'unknown') AS identity_status,
                       item_expectation.expected_count,item_shortcode.shortcode
                FROM item LEFT JOIN item_identity USING (source,id)
                LEFT JOIN item_expectation USING (source,id)
                LEFT JOIN item_shortcode USING (source,id) WHERE item.source=? ORDER BY item.id
            """, (source,))]
        assets = []
        for row in rows:
            metadata = json.loads(row.pop("metadata") or "{}")
            evidence = metadata.get("publication_evidence")
            assets.append({**row, "identity_status": "unknown", **metadata, "asset_id": row["key"],
                           "local_path": "collection/" + row["key"] + ".media" if row["status"] == "verified" else None,
                           "post_date": evidence["timestamp_utc"] if evidence else None,
                           "date_status": "verified" if evidence else "unknown_date",
                           "extraction_status": "downloaded" if row["status"] == "verified" else row["status"]})
        manifest = {"job_id": self.job_id, "assets": assets}
        record_publication_review(manifest, self.window)
        manifest["unknown_date_review"] = [
            asset["asset_id"] for asset in assets
            if asset.get("kind") in ("image", "video") and asset["date_status"] == "unknown_date"
        ]
        issues = json.loads(state.pop("issues"))
        incomplete = bool(issues or not state["exhausted"] or state["stop"] != "listing_exhausted" or any(
            row["status"] not in ("verified", "thumbnail") for row in rows
        ))
        return {**manifest, "items": items, "issues": issues, "stop_reason": state["stop"],
                "cursor": state["cursor"], "listing_exhausted": bool(state["exhausted"]),
                "coverage": "incomplete" if incomplete else "unknown", "coverage_percentage": None}
