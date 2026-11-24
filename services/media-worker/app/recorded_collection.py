"""Offline handoff of local extractor artifacts. There is no network fallback."""

import json
from dataclasses import replace

from app.collection_checkpoint import Attachment, CollectionCheckpoint, InventoryPage, SourceItem, SourceStop
from app.fixture_collection import _recorded_download, _website_adapter
from app.legacy_social_collect import check_social_url
from app.public_collection import SOURCES, limit, publish_checkpoint_manifest
from app.publication import publication_evidence

EXTRACTORS = {
    "instagram": {"gallery-dl", "instaloader"},
    "facebook": {"gallery-dl", "yt-dlp"},
}
STOP_CODES = {
    "login_required": "restricted", "challenge": "restricted", "access_denied": "access_blocked",
    "rate_limited": "rate_limited", "cap": "listing_cap", "interrupted": "listing_interrupted",
}


def _media(media_id, url, kind, *dates):
    return {"id": media_id, "url": url, "kind": kind, "dates": dates}


def _gallery_posts(records):
    posts = {}
    for entry in records:
        if not isinstance(entry, list) or len(entry) != 3 or entry[0] != 3 or not isinstance(entry[2], dict):
            continue
        _, url, meta = entry
        post_url = meta.get("post_url") or meta.get("original_post_url")
        post_id = meta.get("post_id") or meta.get("sidecar_media_id")
        shortcode = meta.get("post_shortcode") or meta.get("sidecar_shortcode")
        post = posts.setdefault(post_id or shortcode or post_url or url, {
            "id": post_id or shortcode, "url": post_url, "shortcode": shortcode, "media": [],
        })
        ext = str(meta.get("extension", "")).lower().lstrip(".")
        if meta.get("media_type") == "thumbnail":
            kind = "thumbnail"
        elif ext in ("mp4", "webm", "mov"):
            kind = "video"
        elif ext in ("jpg", "jpeg", "png", "webp", "gif", "avif"):
            kind = "image"
        else:
            kind = "unsupported"
        # A generic gallery-dl date can come from "seen"; it is insufficient on its own.
        post["media"].append(_media(meta.get("media_id") or meta.get("attachment_id"), url, kind,
                                   ("post_date", meta.get("post_date")),
                                   ("taken_at_timestamp", meta.get("taken_at_timestamp"))))
    return posts.values()


def _instaloader_posts(records):
    for raw in records:
        node = raw.get("node", raw)
        code = node.get("shortcode")
        parent_time = node.get("taken_at_timestamp")
        edges = node.get("edge_sidecar_to_children", {}).get("edges")
        media = []
        for child in ([edge["node"] for edge in edges] if edges is not None else [node]):
            image_url = child.get("display_url")
            date = ("taken_at_timestamp", child.get("taken_at_timestamp", parent_time))
            if child.get("is_video"):
                if child.get("video_url"):
                    media.append(_media(child.get("id"), child["video_url"], "video", date))
                if image_url:
                    media.append(_media(None, image_url, "thumbnail", date))
            elif image_url:
                media.append(_media(child.get("id"), image_url, "image", date))
        yield {"id": node.get("id") or code, "shortcode": code,
               "url": f"https://www.instagram.com/p/{code}/" if code else None, "media": media}


def _ytdlp_posts(records):
    for document in records:
        entries = document["entries"] if document.get("_type") == "playlist" else [document]
        for item in entries:
            if not item:
                continue
            date = ("release_timestamp", item.get("release_timestamp"))
            media = []
            if item.get("url"):
                media.append(_media(item.get("id"), item["url"], "video", date))
            if item.get("thumbnail"):
                media.append(_media(None, item["thumbnail"], "thumbnail", date))
            yield {"id": item.get("id"), "url": item.get("webpage_url"), "media": media}


PARSERS = {"gallery-dl": _gallery_posts, "instaloader": _instaloader_posts, "yt-dlp": _ytdlp_posts}


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
        explicit_url = post.get("url")
        if explicit_url:
            check_social_url(self.source, explicit_url)
        native_id = post.get("id")
        derived = explicit_url or next((item["url"] for item in post["media"]), None)
        if not (native_id or derived):
            issues.append("unidentifiable_post")
            return None
        identity = str(native_id) if native_id else derived
        expectation = expected.get(identity, {})
        if expectation and not (
            expectation.get("provenance") == "recorded_extractor"
            and isinstance(expectation.get("reference"), str) and expectation["reference"].strip()
        ):
            issues.append("unverified_child_expectation")
            expectation = {}
        attachments = {}
        for child in post["media"]:
            kind = child["kind"]
            if kind == "unsupported":
                issues.append("unsupported_media")
            evidence = [
                entry for field, raw in child["dates"]
                if (entry := publication_evidence(
                    raw, source_url=explicit_url, field=field,
                    attribution=f"recorded {self.extractor} publication metadata",
                    unix_seconds=field in ("taken_at_timestamp", "release_timestamp"),
                ))
            ]
            date = evidence[0] if len({entry["timestamp_utc"] for entry in evidence}) == 1 else None
            child_id = child["id"] if kind != "thumbnail" else None
            fallback = ("thumbnail:" if kind == "thumbnail" else "") + child["url"]
            item = Attachment(str(child_id) if child_id else fallback, child["url"], kind, date,
                              "source_supplied" if child_id else "derived")
            previous = attachments.get(item.id)
            if previous and previous.publication_evidence != item.publication_evidence:
                item = replace(previous, publication_evidence=None)
                issues.append("ambiguous_publication_evidence")
            attachments[item.id] = item
        count = expectation.get("count")
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("Invalid recorded expected attachment count")
        if count is not None and len([a for a in attachments.values() if a.kind != "thumbnail"]) < count:
            issues.append("missing_child_count")
        return SourceItem(identity, explicit_url or self.url, tuple(attachments.values()),
                          tuple(map(str, expectation.get("ids", []))),
                          identity_status="source_supplied" if native_id else "derived",
                          expected_count=count, shortcode=post.get("shortcode"))


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
