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
from app.publication import GALLERY_DL_FACEBOOK_UTC_FIELD, publication_evidence

IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "webp", "gif", "avif"})
VIDEO_EXTS = frozenset({"mp4", "webm", "mov"})
_SHORTCODE_RE = re.compile(r"^[\w-]+$")
_FB_NUMERIC_ID_RE = re.compile(r"^\d{6,30}$")
_FB_SET_ID_RE = re.compile(r"^[A-Za-z0-9._-]{2,120}$")
_FB_ATTRIBUTABLE_SUBCATEGORIES = frozenset({"photos", "photo", "set", "albums", "video"})


def media_record(
    media_id: Any,
    url: str,
    kind: str,
    *dates: tuple[str, Any],
    raw_date: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"id": media_id, "url": url, "kind": kind, "dates": dates}
    if raw_date is not None:
        record["raw_date"] = raw_date
    return record


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


def _valid_fb_numeric_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text if _FB_NUMERIC_ID_RE.fullmatch(text) else None


def _valid_fb_set_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _FB_SET_ID_RE.fullmatch(text) else None


def _sanitize_caption(value: Any, *, max_len: int = 500) -> str | None:
    """Store bounded inert caption text as untrusted metadata (never executable)."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:max_len]


def gallery_posts(records: list[Any]) -> list[dict[str, Any]]:
    """Normalize gallery-dl --resolve-json/--dump-json records into post dicts with child media."""
    posts: dict[str, dict[str, Any]] = {}
    for entry in records:
        if not isinstance(entry, list) or len(entry) != 3 or entry[0] != 3 or not isinstance(entry[2], dict):
            continue
        _, url, meta = entry
        if not isinstance(url, str) or not url:
            continue
        category = str(meta.get("category") or "").lower()
        subcategory = str(meta.get("subcategory") or "").lower()
        ext = str(meta.get("extension", "")).lower().lstrip(".")
        if meta.get("media_type") == "thumbnail":
            kind = "thumbnail"
        elif ext in VIDEO_EXTS:
            kind = "video"
        elif ext in IMAGE_EXTS:
            kind = "image"
        else:
            kind = "unsupported"

        fb_id = _valid_fb_numeric_id(meta.get("id")) if category == "facebook" else None
        fb_set_id = _valid_fb_set_id(meta.get("set_id")) if category == "facebook" else None
        fb_attributable = category == "facebook" and subcategory in _FB_ATTRIBUTABLE_SUBCATEGORIES and fb_id is not None

        post_url = meta.get("post_url") or meta.get("original_post_url")
        post_id = meta.get("post_id") or meta.get("sidecar_media_id")
        shortcode = meta.get("post_shortcode") or meta.get("sidecar_shortcode")
        media_id = meta.get("media_id") or meta.get("attachment_id")

        if category == "instagram" and not post_url and isinstance(shortcode, str) and _SHORTCODE_RE.fullmatch(shortcode):
            post_url = f"https://www.instagram.com/p/{shortcode}/"
        elif category == "facebook" and fb_id is not None and subcategory != "avatar":
            post_id = post_id or fb_id
            media_id = media_id or fb_id
            if not post_url and fb_attributable:
                if subcategory == "video" or kind == "video" or str(meta.get("type") or "").lower() == "video":
                    post_url = f"https://www.facebook.com/watch/?v={fb_id}"
                elif fb_set_id:
                    post_url = f"https://www.facebook.com/photo/?fbid={fb_id}&set={fb_set_id}"
                else:
                    post_url = f"https://www.facebook.com/photo/?fbid={fb_id}"

        key = str(post_id or shortcode or post_url or url)
        caption = _sanitize_caption(meta.get("caption") or meta.get("description"))
        pinned = bool(
            meta.get("pinned")
            or meta.get("timeline_pinned_user_ids")
            or meta.get("clips_tab_pinned_user_ids")
        )
        next_photo_id = _valid_fb_numeric_id(meta.get("next_photo_id")) if category == "facebook" else None
        post = posts.setdefault(
            key,
            {
                "id": post_id or shortcode,
                "url": post_url,
                "shortcode": shortcode,
                "caption": caption,
                "pinned": pinned,
                "fb_set_id": fb_set_id,
                "fb_next_photo_id": next_photo_id,
                "fb_next_is_video": bool(meta.get("_next_video")) if category == "facebook" else False,
                "media": [],
            },
        )
        if pinned:
            post["pinned"] = True
        if fb_set_id and not post.get("fb_set_id"):
            post["fb_set_id"] = fb_set_id
        if next_photo_id and not post.get("fb_next_photo_id"):
            post["fb_next_photo_id"] = next_photo_id
        if caption and not post.get("caption"):
            post["caption"] = caption
        # Preserve strict attributable fields across platforms; only accept gallery-dl Facebook "date"
        # when category/subcategory/id trace directly to facebook.py publish_time/created_time (gmtime UTC).
        dates: list[tuple[str, Any]] = [
            ("post_date", meta.get("post_date")),
            ("taken_at_timestamp", meta.get("taken_at_timestamp")),
        ]
        raw_date: dict[str, Any] | None = None
        raw_fb_date = meta.get("date")
        if category == "facebook" and raw_fb_date is not None:
            if fb_attributable:
                dates.append((GALLERY_DL_FACEBOOK_UTC_FIELD, raw_fb_date))
                raw_date = {
                    "field": "date",
                    "raw_value": str(raw_fb_date),
                    "subcategory": subcategory,
                    "reason": "unverified_timezone_or_format",
                }
            else:
                raw_date = {
                    "field": "date",
                    "raw_value": str(raw_fb_date),
                    "subcategory": subcategory or "unknown",
                    "reason": "unverified_timezone_or_provenance",
                }
        post["media"].append(media_record(media_id, url, kind, *dates, raw_date=raw_date))
    return list(posts.values())


def gallery_album_queues(records: list[Any]) -> list[dict[str, str]]:
    """Extract validated Facebook album URLs emitted by gallery-dl FacebookAlbumsExtractor (Message.Queue=6)."""
    albums: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for entry in records:
        if not isinstance(entry, list) or len(entry) != 3 or entry[0] != 6 or not isinstance(entry[2], dict):
            continue
        _, url, meta = entry
        if not isinstance(url, str) or not url or url in seen_urls:
            continue
        try:
            check_social_url("facebook", url)
        except Exception:
            continue
        set_id = _valid_fb_set_id(meta.get("set_id") or meta.get("id")) or ""
        title = _sanitize_caption(meta.get("title"), max_len=120) or ""
        seen_urls.add(url)
        albums.append({"url": url, "set_id": set_id, "title": title})
    return albums


def instaloader_posts(records: list[Any]) -> list[dict[str, Any]]:
    """Normalize Instaloader post/sidecar JSON records (including carousels, Reels & pinned posts)."""
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
        pinned = bool(
            node.get("is_pinned")
            or node.get("pinned_for_users")
            or node.get("timeline_pinned_user_ids")
            or node.get("clips_tab_pinned_user_ids")
        )
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
        cap_edges = (node.get("edge_media_to_caption") or {}).get("edges")
        caption = None
        if isinstance(cap_edges, list) and cap_edges and isinstance(cap_edges[0], dict):
            caption = _sanitize_caption((cap_edges[0].get("node") or {}).get("text"))
        segment = "reel" if str(node.get("product_type") or "").lower() == "clips" else "p"
        post_url = f"https://www.instagram.com/{segment}/{code}/" if isinstance(code, str) and _SHORTCODE_RE.fullmatch(code) else None
        out.append({
            "id": node.get("id") or code,
            "shortcode": code,
            "url": post_url,
            "caption": caption,
            "pinned": pinned,
            "media": media,
        })
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
            caption = _sanitize_caption(item.get("title") or item.get("description"))
            out.append({"id": item.get("id"), "url": item.get("webpage_url"), "caption": caption, "media": media})
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
                    attribution=(
                        "gallery-dl facebook publish_time/created_time (gmtime UTC)"
                        if field == GALLERY_DL_FACEBOOK_UTC_FIELD
                        else attribution
                    ),
                    unix_seconds=field in ("taken_at_timestamp", "release_timestamp"),
                    gallery_dl_facebook_gmtime_utc=field == GALLERY_DL_FACEBOOK_UTC_FIELD,
                )
            )
        ]
        date = evidence[0] if len({entry["timestamp_utc"] for entry in evidence}) == 1 else None
        if len(evidence) > 1 and date is None:
            issue_list.append("ambiguous_publication_evidence")
        child_id = child["id"] if kind != "thumbnail" else None
        fallback = ("thumbnail:" if kind == "thumbnail" else "") + child["url"]
        raw_date_evidence = child.get("raw_date") if date is None else None
        item = Attachment(
            str(child_id) if child_id else fallback,
            child["url"],
            kind,
            date,
            "source_supplied" if child_id else "derived",
            raw_date_evidence=raw_date_evidence,
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
        caption=post.get("caption"),
    )
