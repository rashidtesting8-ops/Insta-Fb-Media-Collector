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
from dataclasses import asdict, dataclass, field
from pathlib import Path
import time
from typing import Callable, Iterable, Protocol

from app.publication import WINDOW_FIELDS, record_publication_review, window_bounds


@dataclass(frozen=True)
class Attachment:
    id: str
    url: str
    kind: str
    publication_evidence: dict | None = None
    identity_status: str = "derived"
    raw_date_evidence: dict | None = None


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
    caption: str | None = None


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
    size: int | None
    chunks: Iterable[bytes]
    offset: int = 0
    revision: str | None = None
    sha256: str | None = None
    close: Callable[[], None] | None = None
    mime: str | None = None


@dataclass
class GlobalBudget:
    """Shared multi-source ceiling across website, Instagram, and Facebook."""

    max_pages: int | None = None
    max_items: int | None = None
    max_transfers: int | None = None
    max_bytes: int | None = None
    max_time_seconds: int | None = None
    clock: Callable[[], float] = time.monotonic
    pages_used: int = 0
    items_used: int = 0
    transfers_used: int = 0
    bytes_used: int = 0
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        for name in ("max_pages", "max_items", "max_transfers", "max_bytes", "max_time_seconds"):
            val = getattr(self, name)
            if val is not None and (type(val) is not int or val < 1):
                raise ValueError(f"GlobalBudget.{name} must be a positive integer")
        self.started_at = self.clock()

    def time_exceeded(self) -> bool:
        return self.max_time_seconds is not None and (self.clock() - self.started_at) >= self.max_time_seconds

    def can_fetch_page(self) -> str | None:
        if self.time_exceeded():
            return "time_budget"
        if self.max_pages is not None and self.pages_used >= self.max_pages:
            return "global_budget"
        if self.max_items is not None and self.items_used >= self.max_items:
            return "global_budget"
        return None

    def record_page(self, new_items: int = 0) -> None:
        self.pages_used += 1
        self.items_used += max(0, int(new_items))

    def can_transfer(self) -> str | None:
        if self.time_exceeded():
            return "time_budget"
        if self.max_transfers is not None and self.transfers_used >= self.max_transfers:
            return "global_budget"
        if self.max_bytes is not None and self.bytes_used >= self.max_bytes:
            return "byte_budget"
        return None

    def remaining_bytes(self) -> int | None:
        if self.max_bytes is None:
            return None
        return max(0, self.max_bytes - self.bytes_used)

    def record_transfer(self, nbytes: int) -> None:
        self.transfers_used += 1
        self.bytes_used += max(0, int(nbytes))

    def snapshot(self) -> dict:
        return {
            "pages_used": self.pages_used,
            "items_used": self.items_used,
            "transfers_used": self.transfers_used,
            "bytes_used": self.bytes_used,
            "max_pages": self.max_pages,
            "max_items": self.max_items,
            "max_transfers": self.max_transfers,
            "max_bytes": self.max_bytes,
            "max_time_seconds": self.max_time_seconds,
        }


class SourceStop(Exception):
    """A sanitized adapter stop, such as rate limiting or access denial."""

    def __init__(self, reason: str):
        super().__init__(reason if reason in {
            "access_blocked", "restricted", "rate_limited", "document_cap", "request_budget", "time_budget",
            "public_inventory_unavailable", "truncated_listing", "unsupported_media",
            "recorded_bytes_unavailable", "listing_interrupted", "listing_cap", "invalid_continuation",
            "cutoff_reached", "unsupported_pagination", "extractor_failure", "byte_budget", "global_budget",
            "media_url_expired", "invalid_media_content",
        } else "source_stopped")


def _verify_media_magic(path: Path, mime: str | None) -> bool:
    """Validate file signature matches the declared image/video MIME type (blocks HTML/error pages)."""
    if not mime:
        return False
    clean = mime.split(";", 1)[0].strip().lower()
    try:
        with path.open("rb") as stream:
            head = stream.read(32)
    except OSError:
        return False
    if len(head) < 8:
        return False
    if clean in ("image/jpeg", "image/jpg"):
        return head.startswith(b"\xff\xd8\xff")
    if clean == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if clean == "image/gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if clean == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if clean == "image/avif":
        return head[4:8] == b"ftyp" and any(brand in head[8:24] for brand in (b"avif", b"avis", b"mif1"))
    if clean in ("video/mp4", "video/quicktime"):
        return head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free")
    if clean == "video/webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    return False


def _canonical_termination_reason(stop: str | None, exhausted: bool) -> str:
    if stop == "cutoff_reached":
        return "cutoff_reached_with_evidence"
    if exhausted or stop == "listing_exhausted":
        return "exhausted"
    if stop == "rate_limited":
        return "rate_limited"
    if stop in ("access_blocked", "restricted", "public_inventory_unavailable"):
        return "access_blocked"
    if stop in ("page_budget", "request_budget", "time_budget", "transfer_budget", "byte_budget", "global_budget", "listing_cap", "document_cap"):
        return "budget_reached"
    if stop in ("unsupported_pagination", "invalid_continuation", "pagination_loop"):
        return "unsupported_pagination"
    if stop in ("extractor_failure", "cursor_failure", "source_stopped", "listing_interrupted"):
        return "extractor_failure"
    return "in_progress"


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
            db.execute("""CREATE TABLE IF NOT EXISTS item_caption (
                source TEXT, id TEXT, caption TEXT, PRIMARY KEY (source,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS media (
                key TEXT PRIMARY KEY, source TEXT, item TEXT, native_id TEXT,
                metadata TEXT, status TEXT, size INTEGER, revision TEXT, expected TEXT, digest TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS media_mime (
                key TEXT PRIMARY KEY, mime TEXT)""")
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

    def collect(
        self,
        source: str,
        scope: str,
        inventory: Inventory,
        *,
        page_budget: int,
        item_limit: int | None = None,
        global_budget: GlobalBudget | None = None,
    ) -> None:
        if source not in ("website", "instagram", "facebook") or page_budget < 1:
            raise ValueError("Invalid source or page budget")
        if item_limit is not None and (type(item_limit) is not int or item_limit < 1):
            raise ValueError("Invalid item limit")
        for _ in range(page_budget):
            with self._transaction() as db:
                db.execute("INSERT OR IGNORE INTO source (name, scope) VALUES (?, ?)", (source, scope))
                state = db.execute("SELECT * FROM source WHERE name=?", (source,)).fetchone()
                if state["scope"] != scope:
                    raise ValueError("Checkpoint source scope changed")
                if state["exhausted"]:
                    db.execute("UPDATE source SET stop='listing_exhausted' WHERE name=?", (source,))
                    return
                if state["stop"] == "cutoff_reached":
                    return
                if global_budget is not None:
                    gb_stop = global_budget.can_fetch_page()
                    if gb_stop:
                        db.execute("UPDATE source SET stop=? WHERE name=?", (gb_stop, source))
                        return
                if item_limit is not None:
                    existing_count = db.execute(
                        "SELECT count(*) FROM media WHERE source=? AND status!='thumbnail'",
                        (source,),
                    ).fetchone()[0]
                    if existing_count >= item_limit:
                        db.execute("UPDATE source SET stop='listing_cap' WHERE name=?", (source,))
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

                newly_added_media = 0
                for item in page.items:
                    db.execute("INSERT OR IGNORE INTO item_identity VALUES (?, ?, ?)",
                               (source, item.id, item.identity_status))
                    if item.expected_count is not None:
                        db.execute("INSERT OR IGNORE INTO item_expectation VALUES (?,?,?)",
                                   (source, item.id, item.expected_count))
                    if item.shortcode:
                        db.execute("INSERT OR IGNORE INTO item_shortcode VALUES (?,?,?)",
                                   (source, item.id, item.shortcode))
                    if item.caption:
                        db.execute("INSERT OR IGNORE INTO item_caption VALUES (?,?,?)",
                                   (source, item.id, item.caption))
                    db.execute("""INSERT INTO item VALUES (?, ?, ?, ?)
                        ON CONFLICT(source,id) DO UPDATE SET status=excluded.status""",
                        (source, item.id, item.url, item.status))
                    for attachment in item.attachments:
                        key = _key(source, item.id, attachment.id)
                        new_meta = json.dumps(asdict(attachment))
                        new_status = "thumbnail" if attachment.kind == "thumbnail" else "pending"
                        existing_media = db.execute(
                            "SELECT metadata, status FROM media WHERE key=?", (key,)
                        ).fetchone()
                        if existing_media is None:
                            db.execute(
                                "INSERT INTO media (key,source,item,native_id,metadata,status) VALUES (?,?,?,?,?,?)",
                                (key, source, item.id, attachment.id, new_meta, new_status),
                            )
                            if new_status != "thumbnail":
                                newly_added_media += 1
                        elif existing_media["status"] in (
                            "missing_attachment", "pending", "media_url_expired", "download_interrupted",
                        ):
                            old_meta = json.loads(existing_media["metadata"] or "{}")
                            if old_meta.get("url") and old_meta.get("url") != attachment.url and existing_media["status"] == "download_interrupted":
                                (self.root / (key + ".part")).unlink(missing_ok=True)
                                db.execute(
                                    "UPDATE media SET metadata=?,status=?,revision=NULL,size=NULL,expected=NULL WHERE key=?",
                                    (new_meta, new_status, key),
                                )
                            else:
                                db.execute(
                                    "UPDATE media SET metadata=?,status=? WHERE key=?",
                                    (new_meta, new_status, key),
                                )
                    for missing in set(item.expected_ids) - {a.id for a in item.attachments}:
                        db.execute("""INSERT OR IGNORE INTO media (key,source,item,native_id,status)
                            VALUES (?,?,?,?,?)""", (_key(source, item.id, missing), source, item.id,
                                                      missing, "missing_attachment"))
                if global_budget is not None:
                    global_budget.record_page(newly_added_media)
                issues = list(dict.fromkeys(json.loads(state["issues"]) + list(page.issues)))
                advance_on_stop = page.stop_reason in (
                    "cutoff_reached", "unsupported_pagination", "listing_cap", "page_budget",
                )
                if not page.stop_reason or advance_on_stop:
                    db.execute("INSERT OR IGNORE INTO page VALUES (?,?)", (source, token))
                exhausted = page.next_cursor is None and not page.stop_reason
                if page.stop_reason:
                    next_cursor = page.next_cursor if advance_on_stop else cursor
                    stop_val = page.stop_reason
                else:
                    next_cursor = page.next_cursor
                    stop_val = "listing_exhausted" if exhausted else "page_budget"
                db.execute("UPDATE source SET cursor=?,exhausted=?,stop=?,issues=? WHERE name=?",
                           (next_cursor, int(exhausted), stop_val, json.dumps(issues), source))
                if exhausted or page.stop_reason:
                    return

    def download(
        self,
        key: str,
        fetch: Callable[[str, int, str | None], Download],
        *,
        byte_limit: int,
        validate_content_magic: bool = False,
    ) -> str:
        """Continue only against a validated revision. Restart at zero otherwise."""
        with closing(sqlite3.connect(self.root / "transfer-lock.sqlite")) as lock, lock:
            lock.execute("BEGIN IMMEDIATE")
            return self._download(
                key, fetch, byte_limit=byte_limit, validate_content_magic=validate_content_magic,
            )

    def run_source(
        self,
        source,
        scope,
        inventory,
        fetch,
        *,
        page_budget,
        transfer_budget,
        byte_limit,
        item_limit: int | None = None,
        source_byte_budget: int | None = None,
        global_budget: GlobalBudget | None = None,
        skip_out_of_window_downloads: bool = False,
        validate_content_magic: bool = False,
    ):
        if (any(type(value) is not int or value < 1 for value in (page_budget, byte_limit))
                or type(transfer_budget) is not int or transfer_budget < 0):
            raise ValueError("Invalid collection budgets")
        if source_byte_budget is not None and (type(source_byte_budget) is not int or source_byte_budget < 1):
            raise ValueError("Invalid source byte budget")
        self.collect(
            source,
            scope,
            inventory,
            page_budget=page_budget,
            item_limit=item_limit,
            global_budget=global_budget,
        )
        report = self.report(source)
        hard_stop_codes = (
            "access_blocked", "restricted", "rate_limited", "request_budget", "time_budget",
            "public_inventory_unavailable", "source_stopped", "invalid_continuation",
            "extractor_failure", "global_budget", "byte_budget",
        )
        if report["stop_reason"] in hard_stop_codes:
            return report
        attempted = 0
        source_bytes_used = 0
        stop = None
        for asset in report["assets"]:
            if asset.get("kind") not in ("image", "video"):
                continue
            if skip_out_of_window_downloads and asset.get("date_eligibility") == "out_of_window":
                continue
            if asset["status"] == "verified":
                path = self.root.parent / asset["local_path"]
                if path.is_file() and digest_file(path) == asset["digest"]:
                    continue
            if global_budget is not None:
                gb_stop = global_budget.can_transfer()
                if gb_stop:
                    stop = gb_stop
                    break
            if source_byte_budget is not None and source_bytes_used >= source_byte_budget:
                stop = "byte_budget"
                break
            if attempted == transfer_budget:
                stop = "transfer_budget"
                break
            effective_byte_limit = byte_limit
            if source_byte_budget is not None:
                effective_byte_limit = min(effective_byte_limit, source_byte_budget - source_bytes_used)
            if global_budget is not None and global_budget.remaining_bytes() is not None:
                effective_byte_limit = min(effective_byte_limit, global_budget.remaining_bytes())
            if effective_byte_limit < 1:
                stop = "byte_budget"
                break
            budget_constrained = effective_byte_limit < byte_limit
            attempted += 1
            status = self.download(
                asset["asset_id"],
                fetch,
                byte_limit=effective_byte_limit,
                validate_content_magic=validate_content_magic,
            )
            if status == "verified":
                final_path = self.root / (asset["asset_id"] + ".media")
                nbytes = final_path.stat().st_size if final_path.is_file() else 0
                source_bytes_used += nbytes
                if global_budget is not None:
                    global_budget.record_transfer(nbytes)
            elif status == "byte_limit_or_invalid_range" and budget_constrained:
                self._download_status(asset["asset_id"], "pending")
                stop = "byte_budget"
                break
            elif status in hard_stop_codes:
                stop = status
                break
        report = self.report(source)
        if stop:
            report["issues"] = list(dict.fromkeys([*report["issues"], stop]))
            report["coverage"] = "incomplete"
        return report

    def _download(self, key, fetch, *, byte_limit, validate_content_magic: bool = False):
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
        committed_digest = False
        try:
            response = fetch(url, offset, revision)
            if offset and (response.offset != offset or response.revision != revision
                           or response.size is None or response.size != row["size"]
                           or response.sha256 != row["expected"]):
                offset = 0
                if response.offset != 0:
                    if response.close:
                        response.close()
                    response = None
                    response = fetch(url, 0, None)
            if response.size is not None:
                if response.offset != offset or not 0 <= offset <= response.size <= byte_limit:
                    part.unlink(missing_ok=True)
                    self._download_status(key, "byte_limit_or_invalid_range")
                    return "byte_limit_or_invalid_range"
            else:
                if offset != 0 or response.offset != 0:
                    part.unlink(missing_ok=True)
                    self._download_status(key, "byte_limit_or_invalid_range")
                    return "byte_limit_or_invalid_range"
            if not offset:
                part.unlink(missing_ok=True)
            # Commit the transfer identity before writing a partial payload.
            with self._transaction() as db:
                db.execute("UPDATE media SET size=?,revision=?,expected=?,digest=NULL,status='downloading' WHERE key=?",
                           (response.size, response.revision, response.sha256, key))
                if response.mime:
                    db.execute("INSERT OR REPLACE INTO media_mime VALUES (?, ?)", (key, response.mime))
            with part.open("ab" if offset else "wb") as out:
                count = offset
                for chunk in response.chunks:
                    count += len(chunk)
                    if count > byte_limit:
                        out.close()
                        part.unlink(missing_ok=True)
                        self._download_status(key, "byte_limit_or_invalid_range")
                        return "byte_limit_or_invalid_range"
                    if response.size is not None and count > response.size:
                        raise ValueError("Declared file length exceeded")
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if response.size is not None and count != response.size:
                raise OSError("Incomplete transfer")
            if count == 0:
                part.unlink(missing_ok=True)
                self._download_status(key, "invalid_media_content")
                return "invalid_media_content"
            if validate_content_magic and not _verify_media_magic(part, response.mime):
                part.unlink(missing_ok=True)
                self._download_status(key, "invalid_media_content")
                return "invalid_media_content"
            digest = digest_file(part)
            if response.sha256 and digest != response.sha256:
                part.unlink()
                self._download_status(key, "checksum_mismatch")
                return "checksum_mismatch"
            # Store the digest before publishing the file. A restart can reconcile either side of the rename.
            with self._transaction() as db:
                db.execute("UPDATE media SET size=?,digest=?,status='committing' WHERE key=?", (count, digest, key))
            committed_digest = True
            os.replace(part, final)
            self._download_status(key, "verified")
            return "verified"
        except SourceStop as exc:
            if not offset and not committed_digest:
                part.unlink(missing_ok=True)
            self._download_status(key, str(exc))
            return str(exc)
        except (OSError, ValueError):
            if not committed_digest and (response is None or not response.revision):
                part.unlink(missing_ok=True)
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
            rows = [dict(row) for row in db.execute("""
                SELECT media.*, media_mime.mime AS mime
                FROM media LEFT JOIN media_mime USING (key)
                WHERE media.source=? ORDER BY media.key
            """, (source,))]
            items = [dict(row) for row in db.execute("""
                SELECT item.id,item.url,item.status,COALESCE(item_identity.status,'unknown') AS identity_status,
                       item_expectation.expected_count,item_shortcode.shortcode,item_caption.caption
                FROM item LEFT JOIN item_identity USING (source,id)
                LEFT JOIN item_expectation USING (source,id)
                LEFT JOIN item_shortcode USING (source,id)
                LEFT JOIN item_caption USING (source,id) WHERE item.source=? ORDER BY item.id
            """, (source,))]
        assets = []
        for row in rows:
            mime = row.pop("mime", None)
            metadata = json.loads(row.pop("metadata") or "{}")
            if metadata.get("raw_date_evidence") is None:
                metadata.pop("raw_date_evidence", None)
            evidence = metadata.get("publication_evidence")
            asset = {**row, "identity_status": "unknown", **metadata, "asset_id": row["key"],
                     "local_path": "collection/" + row["key"] + ".media" if row["status"] == "verified" else None,
                     "post_date": evidence["timestamp_utc"] if evidence else None,
                     "date_status": "verified" if evidence else "unknown_date",
                     "extraction_status": "downloaded" if row["status"] == "verified" else row["status"]}
            if mime:
                asset["mime"] = mime
            assets.append(asset)
        manifest = {"job_id": self.job_id, "assets": assets}
        record_publication_review(manifest, self.window)
        manifest["unknown_date_review"] = [
            asset["asset_id"] for asset in assets
            if asset.get("kind") in ("image", "video") and asset["date_status"] == "unknown_date"
        ]
        issues = json.loads(state.pop("issues"))
        terminal_ok = (bool(state["exhausted"]) and state["stop"] == "listing_exhausted") or state["stop"] == "cutoff_reached"
        incomplete = bool(issues or not terminal_ok or any(
            row["status"] not in ("verified", "thumbnail")
            and not (state["stop"] == "cutoff_reached" and asset.get("date_eligibility") == "out_of_window")
            for row, asset in zip(rows, assets)
        ))
        return {**manifest, "items": items, "issues": issues, "stop_reason": state["stop"],
                "termination_reason": _canonical_termination_reason(state["stop"], bool(state["exhausted"])),
                "cursor": state["cursor"], "listing_exhausted": bool(state["exhausted"]),
                "coverage": "incomplete" if incomplete else "unknown", "coverage_percentage": None}
