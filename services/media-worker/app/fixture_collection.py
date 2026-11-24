"""Explicit worker entry point for recorded collection fixtures only.

Run with python -m app.fixture_collection RECORDING.json OUTPUT_ROOT.
All documents and binaries come from the recording. This module does no I/O
against source URLs and is not registered with the normal worker API.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path
from urllib.robotparser import RobotFileParser

from app.collection_checkpoint import (
    Attachment, CollectionCheckpoint, Download, InventoryPage, SourceItem, SourceStop,
)
from app.manifest import SCHEMA_VERSION, atomic_write_json, validate_against_disk
from app.publication import WINDOW_FIELDS, record_publication_review, window_bounds
from app.website_inventory import WebsiteInventory

SOURCES = ("website", "instagram", "facebook")
FIXTURE_USER_AGENT = "PetSiteResearchBot"


class RecordedInventory:
    """Translate recorded social pages into the shared inventory contract."""

    def __init__(self, pages):
        self.pages = {page["cursor"]: page for page in pages}
        if len(self.pages) != len(pages):
            raise ValueError("Duplicate recorded cursor")

    def page(self, cursor):
        record = self.pages.get(cursor)
        if record is None:
            raise ValueError("Continuation missing from recording")
        if record.get("stop"):
            raise SourceStop(record["stop"])
        if "next_cursor" not in record:
            raise ValueError("Recording must declare a continuation or explicit exhaustion")
        items = []
        for post in record["posts"]:
            attachments = tuple(
                Attachment(item["attachment_id"], item["source_url"], item["kind"],
                           item.get("publication_evidence"))
                for item in post["attachments"]
            )
            items.append(SourceItem(post["post_id"], post["source_url"], attachments,
                                    tuple(post["expected_attachment_ids"])))
        return InventoryPage(tuple(items), record["next_cursor"])


def _website_adapter(website):
    robots = RobotFileParser()
    robots.parse(website["robots"].splitlines())

    def permitted(url):
        return robots.can_fetch(FIXTURE_USER_AGENT, url)

    def fetch(url):
        document = website["documents"].get(url)
        if not permitted(url):
            raise SourceStop("access_blocked")
        if document is None:
            raise OSError("Document missing from recording")
        return document.get("final_url", url), document["body"].encode()

    return WebsiteInventory(
        website["url"], sitemaps=website["sitemaps"], fetch=fetch, permitted=permitted,
        document_limit=website["document_limit"], entry_limit=website["entry_limit"],
    ), permitted


def _recorded_download(files, url, offset, revision):
    entry = files.get(url)
    if entry is None:
        raise OSError("Attachment missing from recording")
    if entry.get("stop"):
        raise SourceStop(entry["stop"])
    data = base64.b64decode(entry["base64"], validate=True)
    matches = revision is not None and entry.get("revision") == revision
    offset = offset if matches else 0

    def chunks():
        if "interrupt_after" in entry:
            yield data[offset:offset + entry["interrupt_after"]]
            raise OSError("Recorded interruption")
        yield data[offset:]

    return Download(len(data), chunks(), offset, entry.get("revision"), entry.get("sha256"))


def _fixture_job(recording, output_root):
    raw = recording["job"]
    job_id = raw["job_id"]
    if recording.get("fixture_only") is not True or not re.fullmatch(r"fixture-[a-z0-9-]+", job_id):
        raise ValueError("Only marked fixture jobs are accepted")
    if window_bounds(raw) is None:
        raise ValueError("Fixture requires its original frozen window")
    job = {"job_id": job_id, **{field: raw[field] for field in WINDOW_FIELDS}}
    directory = output_root / job_id
    marker = directory / "fixture-scope.json"
    if directory.is_symlink() or (directory / "collection").is_symlink():
        raise ValueError("Fixture directory cannot be a symlink")
    if directory.exists():
        if marker.is_symlink() or not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != job:
            raise ValueError("Existing directory does not match this fixture job")
        if (directory / "manifest.json").exists():
            raise ValueError("Refusing to use a normal worker job directory")
    else:
        directory.mkdir(parents=True)
        atomic_write_json(marker, job)
    return directory, job


def run_fixture_collection(recording: dict, output_root: Path) -> Path:
    """Resume a fixture through the production checkpoint and date contracts."""
    directory, job = _fixture_job(recording, output_root)
    checkpoint = CollectionCheckpoint(directory, job)
    reports = {}
    for source in SOURCES:
        config = recording["sources"][source]
        permitted = None
        if source == "website":
            inventory, permitted = _website_adapter(config)
        else:
            inventory = RecordedInventory(config["pages"])

        def fetch(url, offset, revision):
            if permitted is not None and not permitted(url):
                raise SourceStop("access_blocked")
            return _recorded_download(config["files"], url, offset, revision)

        reports[source] = checkpoint.run_source(source, config["url"], inventory, fetch,
                                                  page_budget=config["page_budget"], transfer_budget=config["transfer_budget"],
                                                  byte_limit=config["byte_limit"])

    manifest = {"schema_version": SCHEMA_VERSION, "fixture_only": True, **job, "assets": [], "sources": {}}
    for source, report in reports.items():
        manifest["sources"][source] = {
            key: report[key] for key in ("items", "coverage", "coverage_percentage", "cursor", "stop_reason",
                                        "issues", "listing_exhausted")
        }
        items = {item["id"]: item for item in report["items"]}
        for asset in report["assets"]:
            manifest["assets"].append({
                **asset, "platform": source, "job_id": job["job_id"],
                "source_url": asset.get("url"), "source_page": items[asset["item"]]["url"],
                "type": asset.get("kind"), "sha256": asset["digest"], "bytes": asset["size"],
                "rights_status": "unverified",
            })
    record_publication_review(manifest, job)
    manifest["unknown_date_review"] = [key for report in reports.values() for key in report["unknown_date_review"]]
    if validate_against_disk(directory, manifest):
        raise ValueError("Fixture manifest failed file-integrity validation")
    target = checkpoint.root / "fixture-manifest.json"
    atomic_write_json(target, manifest)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("output_root", type=Path)
    args = parser.parse_args()
    print(run_fixture_collection(json.loads(args.recording.read_text(encoding="utf-8")), args.output_root))


if __name__ == "__main__":
    main()
