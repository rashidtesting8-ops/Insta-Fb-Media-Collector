"""Offline handoff of local extractor artifacts. There is no network fallback."""

import json

from app.collection_checkpoint import CollectionCheckpoint, InventoryPage, SourceStop
from app.fixture_collection import _recorded_download, _website_adapter
from app.public_collection import SOURCES, limit, publish_checkpoint_manifest
from app.social_normalizers import (
    PARSERS,
    build_source_item,
    gallery_posts as _gallery_posts,
    instaloader_posts as _instaloader_posts,
    media_record as _media,
    ytdlp_posts as _ytdlp_posts,
)

EXTRACTORS = {
    "instagram": {"gallery-dl", "instaloader"},
    "facebook": {"gallery-dl", "yt-dlp"},
}
STOP_CODES = {
    "login_required": "restricted", "challenge": "restricted", "access_denied": "access_blocked",
    "rate_limited": "rate_limited", "cap": "listing_cap", "interrupted": "listing_interrupted",
}


class RecordedSocialInventory:
    def __init__(self, source, url, recording):
        self.source, self.url, self.recording = source, url, recording
        self.extractor = recording.get("extractor")
        if self.extractor not in EXTRACTORS[source] or recording.get("profile_url") != url:
            raise ValueError("Unexpected recorded extractor or profile")
        pages = recording["pages"]
        self.pages = {page["cursor"]: page for page in pages}
        if len(pages) != len(self.pages):
            raise ValueError("Duplicate recorded cursor")

    def page(self, cursor):
        page = self.pages.get(cursor)
        if page is None:
            raise SourceStop("listing_interrupted")
        items = []
        issues = ["bounded_public_recording", "independent_inventory_unavailable"]
        for post in PARSERS[self.extractor](page["records"]):
            item = self._item(post, page.get("expected_children", {}), issues)
            if item is not None:
                items.append(item)
        if page.get("truncated"):
            issues.append("truncated_listing")
        stop = STOP_CODES.get(page.get("stop"), "source_stopped") if page.get("stop") else None
        if self.extractor == "gallery-dl" and any(
            isinstance(record, list) and record and record[0] == 6 for record in page["records"]
        ):
            issues.append("extractor_error")
            stop = stop or "source_stopped"
        continuation = page.get("continuation")
        if continuation is not None and not (
            isinstance(continuation, dict)
            and continuation.get("extractor") == self.extractor
            and continuation.get("profile_url") == self.url
            and continuation.get("provenance") == "recorded_extractor"
            and isinstance(continuation.get("token"), str) and continuation["token"]
        ):
            issues.append("invalid_continuation")
            stop = stop or "invalid_continuation"
        if page.get("truncated") and continuation is None:
            stop = stop or "truncated_listing"
        return InventoryPage(tuple(items), continuation["token"] if continuation and not stop else None,
                             tuple(dict.fromkeys(issues)), stop)

    def _item(self, post, expected, issues):
        return build_source_item(
            self.source,
            self.url,
            self.extractor,
            post,
            attribution=f"recorded {self.extractor} publication metadata",
            expected=expected,
            issues=issues,
        )


def collect_recorded_job(job_dir, job, configuration):
    if configuration.get("offline_only") is not True:
        raise ValueError("Recorded handoff requires an offline-only configuration")
    configs = configuration["sources"]
    for source in SOURCES:
        if configs[source]["url"] != job[source + "_url"]:
            raise ValueError("Recorded configuration does not match the submitted source URLs")
    checkpoint = CollectionCheckpoint(job_dir, job)
    reports = {}
    for source in SOURCES:
        config = configs[source]
        permitted = None
        if source == "website":
            inventory, permitted = _website_adapter(config)
            binding = None
        else:
            recording = config.get("recording")
            binding = config["extractor"]
            if binding not in EXTRACTORS[source]:
                raise ValueError("Unsupported recorded extractor")
            if recording is not None and binding != recording.get("extractor"):
                raise ValueError("Recorded extractor scope changed")
            inventory = RecordedSocialInventory(source, config["url"], recording) if recording is not None else None
        scope = json.dumps({"url": config["url"], "extractor": binding}, sort_keys=True)
        if inventory is None:
            reports[source] = checkpoint.hold_source(source, scope, "public_inventory_unavailable")
            continue

        def fetch(url, offset, revision):
            if permitted is not None and not permitted(url):
                raise SourceStop("access_blocked")
            if url not in config["files"]:
                raise SourceStop("recorded_bytes_unavailable")
            return _recorded_download(config["files"], url, offset, revision)

        reports[source] = checkpoint.run_source(
            source, scope, inventory, fetch, page_budget=limit(config, "page_budget"),
            transfer_budget=limit(config, "transfer_budget", minimum=0), byte_limit=limit(config, "byte_limit"),
        )
    return publish_checkpoint_manifest(job_dir, job, reports, mode="recorded_extractor")


__all__ = [
    "EXTRACTORS",
    "PARSERS",
    "RecordedSocialInventory",
    "STOP_CODES",
    "_gallery_posts",
    "_instaloader_posts",
    "_media",
    "_ytdlp_posts",
    "collect_recorded_job",
]
