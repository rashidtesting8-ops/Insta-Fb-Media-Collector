"""Shared pure normalizers for open-source social extractor metadata.

Reused by both offline recorded handoff (`app.recorded_collection`) and
bounded discovery-first CLI extraction (`app.social_discovery`).
Never performs network I/O, never invents publication timestamps, and never
claims full historical coverage.
"""

from __future__ import annotations

from dataclasses import replace
import json
import re
from typing import Any

from app.collection_checkpoint import Attachment, SourceItem
from app.legacy_social_collect import check_social_url
from app.publication import publication_evidence

IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "webp", "gif", "avif"})
VIDEO_EXTS = frozenset({"mp4", "webm", "mov"})
_SHORTCODE_RE = re.compile(r"^[\w-]+$")


def media_record(media_id: Any, url: str, kind: str, *dates: tuple[str, Any]) -> dict[str, Any]:
    return {"id": media_id, "url": url, "kind": kind, "dates": dates}


def parse_gallery_json(raw_output: str) -> list[Any]:
    """Parse gallery-dl --dump-json stdout (JSON array or line-delimited entries)."""
    text = (raw_output or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        entries: list[Any] = []
        for line in text.splitlines():
            item = line.strip()
            if not item or not (item.startswith("[") or item.startswith("{")):
                continue
            try:
                loaded = json.loads(item)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, list) and loaded and isinstance(loaded[0], list):
                entries.extend(loaded)
            else:
                entries.append(loaded)
        return entries
    if isinstance(parsed, list):
        if parsed and isinstance(parsed[0], int):
            return [parsed]
        return parsed
    return [parsed] if isinstance(parsed, dict) else []


def gallery_error_codes(records: list[Any]) -> list[str]:
    """Return sorted numeric gallery-dl envelope error codes (e.g. -1, 4, 6)."""
    codes: set[str] = set()
    for entry in records:
        if isinstance(entry, list) and entry and isinstance(entry[0], int) and entry[0] in (-1, 4, 6):
            codes.add(str(entry[0]))
    return sorted(codes)


def gallery_posts(records: list[Any]) -> list[dict[str, Any]]:
    """Normalize gallery-dl --dump-json records into post dicts with child media."""
    posts: dict[str, dict[str, Any]] = {}
    for entry in records:
        if not isinstance(entry, list) or len(entry) != 3 or entry[0] != 3 or not isinstance(entry[2], dict):
            continue
        _, url, meta = entry
        if not isinstance(url, str) or not url:
            continue
        post_url = meta.get("post_url") or meta.get("original_post_url")
        post_id = meta.get("post_id") or meta.get("sidecar_media_id")
        shortcode = meta.get("post_shortcode") or meta.get("sidecar_shortcode")
        if not post_url and isinstance(shortcode, str) and _SHORTCODE_RE.fullmatch(shortcode):
            if str(meta.get("category") or "").lower() == "instagram":
                post_url = f"https://www.instagram.com/p/{shortcode}/"
        key = str(post_id or shortcode or post_url or url)
        post = posts.setdefault(
            key,
            {"id": post_id or shortcode, "url": post_url, "shortcode": shortcode, "media": []},
        )
        ext = str(meta.get("extension", "")).lower().lstrip(".")
        if meta.get("media_type") == "thumbnail":
            kind = "thumbnail"
        elif ext in VIDEO_EXTS:
            kind = "video"
        elif ext in IMAGE_EXTS:
            kind = "image"
        else:
            kind = "unsupported"
        # A generic gallery-dl "date" can come from capture/"seen"; require attributable post fields.
        post["media"].append(
            media_record(
                meta.get("media_id") or meta.get("attachment_id"),
                url,
                kind,
                ("post_date", meta.get("post_date")),
                ("taken_at_timestamp", meta.get("taken_at_timestamp")),
            )
        )
    return list(posts.values())


def instaloader_posts(records: list[Any]) -> list[dict[str, Any]]:
    """Normalize Instaloader post/sidecar JSON records (including carousels & videos)."""
    out: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        meta_hdr = raw.get("instaloader")
        if isinstance(meta_hdr, dict) and meta_hdr.get("node_type") not in (None, "Post"):
            continue
        node = raw.get("node") if isinstance(raw.get("node"), dict) else raw
        code = node.get("shortcode")
        parent_time = node.get("taken_at_timestamp")
        parent_date_utc = node.get("date_utc")
        edges = (node.get("edge_sidecar_to_children") or {}).get("edges")
        children = [edge["node"] for edge in edges if isinstance(edge, dict) and isinstance(edge.get("node"), dict)] if isinstance(edges, list) else [node]
        media: list[dict[str, Any]] = []
        for child in children:
            image_url = child.get("display_url")
            dates = [
                ("taken_at_timestamp", child.get("taken_at_timestamp", parent_time)),
                ("date_utc", child.get("date_utc", parent_date_utc)),
            ]
            if child.get("is_video"):
                if child.get("video_url"):
                    media.append(media_record(child.get("id"), child["video_url"], "video", *dates))
                if image_url:
                    media.append(media_record(None, image_url, "thumbnail", *dates))
            elif image_url:
                media.append(media_record(child.get("id"), image_url, "image", *dates))
        post_url = f"https://www.instagram.com/p/{code}/" if isinstance(code, str) and _SHORTCODE_RE.fullmatch(code) else None
        out.append({"id": node.get("id") or code, "shortcode": code, "url": post_url, "media": media})
    return out


def ytdlp_posts(records: list[Any]) -> list[dict[str, Any]]:
    """Normalize yt-dlp --dump-single-json video/playlist metadata records."""
    out: list[dict[str, Any]] = []
    for document in records:
        if not isinstance(document, dict):
            continue
        entries = document["entries"] if document.get("_type") == "playlist" and isinstance(document.get("entries"), list) else [document]
        for item in entries:
            if not isinstance(item, dict) or not item:
                continue
            date = ("release_timestamp", item.get("release_timestamp"))
            media: list[dict[str, Any]] = []
            if isinstance(item.get("url"), str) and item["url"]:
                media.append(media_record(item.get("id"), item["url"], "video", date))
            if isinstance(item.get("thumbnail"), str) and item["thumbnail"]:
                media.append(media_record(None, item["thumbnail"], "thumbnail", date))
            out.append({"id": item.get("id"), "url": item.get("webpage_url"), "media": media})
    return out


PARSERS = {
    "gallery-dl": gallery_posts,
    "instaloader": instaloader_posts,
    "yt-dlp": ytdlp_posts,
}


def build_source_item(
    platform: str,
    profile_url: str,
    extractor: str,
    post: dict[str, Any],
    *,
    attribution: str,
    expected: dict[str, Any] | None = None,
    issues: list[str] | None = None,
) -> SourceItem | None:
    """Convert one normalized post dict into a checkpoint SourceItem with strict date evidence."""
    issue_list = issues if issues is not None else []
    explicit_url = post.get("url")
    if explicit_url:
        check_social_url(platform, explicit_url)
    native_id = post.get("id")
    derived = explicit_url or next((item["url"] for item in post.get("media", []) if item.get("url")), None)
    if not (native_id or derived):
        issue_list.append("unidentifiable_post")
        return None
    identity = str(native_id) if native_id else str(derived)
    expectation = (expected or {}).get(identity, {})
    if expectation and not (
        expectation.get("provenance") == "recorded_extractor"
        and isinstance(expectation.get("reference"), str)
        and expectation["reference"].strip()
    ):
        issue_list.append("unverified_child_expectation")
        expectation = {}

    attachments: dict[str, Attachment] = {}
    for child in post.get("media", []):
        kind = child["kind"]
        if kind == "unsupported":
            issue_list.append("unsupported_media")
        evidence = [
            entry
            for field, raw in child.get("dates", ())
            if (
                entry := publication_evidence(
                    raw,
                    source_url=explicit_url,
                    field=field,
                    attribution=attribution,
                    unix_seconds=field in ("taken_at_timestamp", "release_timestamp"),
                )
            )
        ]
        date = evidence[0] if len({entry["timestamp_utc"] for entry in evidence}) == 1 else None
        if len(evidence) > 1 and date is None:
            issue_list.append("ambiguous_publication_evidence")
        child_id = child["id"] if kind != "thumbnail" else None
        fallback = ("thumbnail:" if kind == "thumbnail" else "") + child["url"]
        item = Attachment(
            str(child_id) if child_id else fallback,
            child["url"],
            kind,
            date,
            "source_supplied" if child_id else "derived",
        )
        previous = attachments.get(item.id)
        if previous and previous.publication_evidence != item.publication_evidence:
            item = replace(previous, publication_evidence=None)
            issue_list.append("ambiguous_publication_evidence")
        attachments[item.id] = item

    count = expectation.get("count")
    if count is not None and (type(count) is not int or count < 0):
        raise ValueError("Invalid recorded expected attachment count")
    if count is not None and len([a for a in attachments.values() if a.kind != "thumbnail"]) < count:
        issue_list.append("missing_child_count")
    return SourceItem(
        identity,
        explicit_url or profile_url,
        tuple(attachments.values()),
        tuple(map(str, expectation.get("ids", []))),
        identity_status="source_supplied" if native_id else "derived",
        expected_count=count,
        shortcode=post.get("shortcode"),
    )
