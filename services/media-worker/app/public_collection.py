"""Opt-in public collection. No account access or unguarded extractor subprocess."""

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from requests import HTTPError

from app.collection_checkpoint import Attachment, CollectionCheckpoint, Download, InventoryPage, SourceItem, SourceStop
from app.legacy_collector import MEDIA_TYPES, build_collector_session, response_get, robots_allowed
from app.legacy_social_collect import check_social_url
from app.manifest import SCHEMA_VERSION, validate_against_disk, write_manifest
from app.publication import WINDOW_FIELDS, publication_evidence, record_publication_review
from app.website_inventory import WebsiteInventory

SOURCES = ("website", "instagram", "facebook")
COLLECTION_HOLD = "blocked_unverified_collection"


def origin(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None:
        raise ValueError("Expected a credential-free public HTTP(S) origin")
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def limit(config, key, *, minimum=1):
    value = config.get(key)
    if type(value) is not int or value < minimum:
        raise ValueError("Public collection requires an explicit valid " + key)
    return value


def check_response(response):
    if response.status_code == 429:
        raise SourceStop("rate_limited")
    if response.status_code in (401, 403, 407, 451) or response.headers.get("cf-mitigated") == "challenge":
        raise SourceStop("restricted")


class PublicHTTP:
    def __init__(self, source_url, config, session, held_origins=None):
        self.origin = origin(source_url)
        self.media_origins = {origin(url) for url in config["media_origins"]}
        self.session = session
        self.robots = {}
        self.held_origins = held_origins if held_origins is not None else {}
        self.remaining = limit(config, "request_budget")
        self.deadline = time.monotonic() + limit(config, "time_budget_seconds")

    def _clock(self):
        if time.monotonic() >= self.deadline:
            raise SourceStop("time_budget")

    def _network_policy(self, url, allowed):
        self._clock()
        current = origin(url)
        if current not in allowed:
            raise SourceStop("access_blocked")
        if current in self.held_origins:
            raise SourceStop(self.held_origins[current])
        if self.remaining == 0:
            raise SourceStop("request_budget")
        self.remaining -= 1
        self.session.cookies.clear()

    def _robots(self, url):
        current = origin(url)
        if current in self.held_origins:
            raise SourceStop(self.held_origins[current])
        return robots_allowed(self.session, url, self.robots,
                              url_policy=lambda hop: self._network_policy(hop, {current}),
                              response_policy=self._check_response)

    def _check_response(self, response):
        try:
            check_response(response)
        except SourceStop as exc:
            self.held_origins[origin(response.url)] = str(exc)
            raise

    def page_permitted(self, url):
        return origin(url) == self.origin and self._robots(url)

    def media_permitted(self, url):
        # Discovery checks origin only. Robots rules are checked for each actual download hop.
        return origin(url) in self.media_origins

    def _get(self, url, allowed, headers=None):
        def check_hop(hop):
            path = urlsplit(hop).path.lower().rstrip("/")
            if path in ("/login", "/login.php", "/accounts/login", "/checkpoint", "/challenge"):
                raise SourceStop("restricted")
            if origin(hop) not in allowed or not self._robots(hop):
                raise SourceStop("access_blocked")
            self._network_policy(hop, allowed)

        response = response_get(self.session, url, stream=True, url_policy=check_hop, headers=headers)
        try:
            self._check_response(response)
            response.raise_for_status()
            return response
        except BaseException:
            response.close()
            raise

    def document(self, url, byte_limit):
        with self._get(url, {self.origin}) as response:
            body = bytearray()
            for chunk in response.iter_content(65536):
                self._clock()
                if len(body) + len(chunk) > byte_limit:
                    raise SourceStop("document_cap")
                body.extend(chunk)
            return response.url, bytes(body)

    def download(self, url, offset, revision):
        headers = {"Accept-Encoding": "identity"}
        if offset and revision:
            headers.update(Range=f"bytes={offset}-", **{"If-Range": revision})
        try:
            response = self._get(url, self.media_origins, headers)
        except HTTPError as exc:
            if offset and exc.response is not None and exc.response.status_code == 416:
                return self.download(url, 0, None)
            raise
        try:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in MEDIA_TYPES or content_type == "image/svg+xml":
                raise SourceStop("unsupported_media")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Unsupported encoded response")
            size = int(response.headers["Content-Length"])
            etag = response.headers.get("ETag", "")
            strong = etag if re.fullmatch(r'"[^"\r\n]+"', etag) else None
            start = 0
            if response.status_code == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match:
                    raise ValueError("Invalid Content-Range")
                start, end, total = map(int, match.groups())
                if not (offset and start == offset and size > 0 and start + size == end + 1 == total and strong):
                    raise ValueError("Unverified resume range")
                size = total
            elif response.status_code != 200:
                raise ValueError("Unexpected download response")

            def chunks():
                for chunk in response.iter_content(65536):
                    self._clock()
                    yield chunk

            return Download(size, chunks(), start, strong, close=response.close)
        except (KeyError, ValueError) as exc:
            response.close()
            raise ValueError("Incomplete or invalid HTTP download metadata") from exc
        except BaseException:
            response.close()
            raise


class PublicSocialSnapshot:
    """Normalize provenance from one bounded, externally supplied public extract.

    No pagination token is synthesized and no extractor is executed here.
    """

    def __init__(self, platform, profile, snapshot, consent):
        self.platform, self.profile, self.snapshot, self.consent = platform, profile, snapshot, consent

    def check_access(self):
        if not self.consent:
            raise SourceStop("access_blocked")
        if self.snapshot is None:
            raise SourceStop("public_inventory_unavailable")
        if self.snapshot.get("profile_url") != self.profile:
            raise SourceStop("access_blocked")
        if self.snapshot.get("stop"):
            reason = self.snapshot["stop"]
            if reason in ("login_required", "private_account", "challenge", "checkpoint_required"):
                reason = "restricted"
            raise SourceStop(reason)

    def page(self, cursor):
        if cursor is not None:
            raise ValueError("Public extractor snapshot provides no continuation")
        self.check_access()
        items = []
        for post in self._posts():
            attributable = post.get("source_url")
            post_url = check_social_url(self.platform, attributable or self.profile)
            supplied_id = post.get("post_id")
            attachments = []
            for media in post["attachments"]:
                native_id = media.get("attachment_id")
                ref = media["source_url"]
                evidence = publication_evidence(
                    media.get("published_at") if attributable else None, source_url=post_url, field="published_at",
                    attribution="attributable public extractor attachment record",
                )
                kind = media["kind"]
                if kind not in ("image", "video", "thumbnail"):
                    raise ValueError("Unsupported public attachment kind")
                attachments.append(Attachment(str(native_id) if native_id else ref, ref, kind, evidence,
                                              "source_supplied" if native_id else "derived"))
            derived = attributable or next((item.url for item in attachments), None)
            if supplied_id or derived:
                items.append(SourceItem(str(supplied_id) if supplied_id else derived, post_url, tuple(attachments),
                                        tuple(map(str, post.get("expected_attachment_ids", []))),
                                        identity_status="source_supplied" if supplied_id else
                                        ("derived" if attributable else "unknown")))
        return InventoryPage(tuple(items), issues=("truncated_listing",) if self.snapshot.get("truncated")
                             else ("bounded_public_snapshot",))

    def _posts(self):
        if "posts" in self.snapshot:
            return self.snapshot["posts"]
        # gallery-dl URL records contain a direct media URL and its own metadata.
        posts = {}
        for record in self.snapshot["records"]:
            if not isinstance(record, list) or len(record) != 3 or record[0] != 3 or not isinstance(record[2], dict):
                continue
            _, media_url, meta = record
            post_url = meta.get("post_url") or meta.get("original_post_url")
            if not isinstance(media_url, str):
                continue
            post_id = meta.get("post_id") or meta.get("post_shortcode")
            post = posts.setdefault(post_id or post_url or media_url,
                                    {"source_url": post_url, "post_id": post_id, "attachments": []})
            extension = str(meta.get("extension", "")).lower().lstrip(".")
            if meta.get("media_type") == "thumbnail":
                kind = "thumbnail"
            elif extension in ("mp4", "webm", "mov"):
                kind = "video"
            elif extension in ("jpg", "jpeg", "png", "webp", "gif", "avif"):
                kind = "image"
            else:
                continue
            post["attachments"].append({
                "source_url": media_url, "attachment_id": meta.get("media_id") or meta.get("attachment_id"),
                "kind": kind, "published_at": meta.get("post_date") or meta.get("date"),
            })
        return posts.values()


def collect_public_job(
    job_dir: Path,
    job: dict,
    configuration: dict,
    *,
    session_factory=build_collector_session,
    social_runner=None,
    clock=time.monotonic,
):
    """Run the checkpoint path for an explicitly scoped job. Curation is held."""
    if configuration.get("offline_only") or configuration.get("fixture_only"):
        raise ValueError("Offline recordings cannot use the public HTTP path")
    configs = configuration["sources"]
    for source in SOURCES:
        if configs[source]["url"] != job[source + "_url"]:
            raise ValueError("Public collection configuration does not match the submitted source URLs")
    state = CollectionCheckpoint(job_dir, job)
    reports = {}
    tools_by_source = {}
    held_origins = {}
    for source in SOURCES:
        config = configs[source]
        url = config["url"]
        with session_factory() as session:
            transport = PublicHTTP(url, config, session, held_origins=held_origins)
            if source == "website":
                document_limit = limit(config, "document_limit")
                inventory = WebsiteInventory(
                    url, sitemaps=config["sitemaps"], fetch=lambda url: transport.document(url, document_limit),
                    permitted=transport.page_permitted, media_permitted=transport.media_permitted,
                    document_limit=document_limit, entry_limit=limit(config, "entry_limit"),
                )
            elif config.get("snapshot") is not None or config.get("discovery_mode") != "open_source_cli":
                inventory = PublicSocialSnapshot(source, url, config.get("snapshot"),
                                                 job.get("social_permission_confirmed") is True)
            else:
                from app.social_discovery import OpenSourceSocialInventory

                inventory = OpenSourceSocialInventory(
                    source,
                    url,
                    permission_confirmed=job.get("social_permission_confirmed") is True,
                    window_start_utc=job.get("window_start_utc"),
                    item_limit=limit(config, "entry_limit"),
                    request_budget=limit(config, "request_budget"),
                    time_budget_seconds=limit(config, "time_budget_seconds"),
                    allow_fallbacks=bool(config.get("allow_fallbacks", True)),
                    runner=social_runner,
                    clock=clock,
                )
            scope = json.dumps({"url": url, "media_origins": sorted(transport.media_origins),
                                "sitemaps": config.get("sitemaps", []),
                                **({"discovery_mode": config["discovery_mode"]} if config.get("discovery_mode") else {})},
                               sort_keys=True)
            try:
                if hasattr(inventory, "check_access"):
                    inventory.check_access()
                report = state.run_source(source, scope, inventory, transport.download,
                                          page_budget=limit(config, "page_budget"),
                                          transfer_budget=limit(config, "transfer_budget", minimum=0),
                                          byte_limit=limit(config, "byte_limit"))
            except SourceStop as exc:
                report = state.hold_source(source, scope, str(exc))
            if getattr(inventory, "tools_executed", None):
                tools_by_source[source] = list(inventory.tools_executed)
        reports[source] = report
    return publish_checkpoint_manifest(
        job_dir, job, reports, mode="public_checkpoint", tools_by_source=tools_by_source,
    )


def publish_checkpoint_manifest(job_dir, job, reports, *, mode, tools_by_source=None):
    manifest = {"job_id": job["job_id"], "schema_version": SCHEMA_VERSION,
                **{field: job[field] for field in WINDOW_FIELDS},
                "collection_mode": mode, "collection_gate": COLLECTION_HOLD,
                "sources": {}, "assets": []}
    by_digest = {}
    for source, report in reports.items():
        manifest["sources"][source] = {
            key: report[key] for key in ("items", "coverage", "coverage_percentage", "cursor",
                                        "stop_reason", "issues", "listing_exhausted")
        }
        if tools_by_source and source in tools_by_source:
            manifest["sources"][source]["tools_executed"] = tools_by_source[source]
        if mode == "recorded_extractor":
            manifest["sources"][source].update(discovery_status=report["coverage"], coverage="unknown")
        items = {item["id"]: item for item in report["items"]}
        for asset in report["assets"]:
            normalized = {
                **asset, "platform": source, "job_id": job["job_id"],
                "source_url": asset.get("url"), "source_page": items[asset["item"]]["url"],
                "type": asset.get("kind"), "sha256": asset["digest"], "bytes": asset["size"],
                "rights_status": "unverified",
            }
            if items[asset["item"]].get("shortcode") is not None:
                normalized["source_shortcode"] = items[asset["item"]]["shortcode"]
            if asset["status"] == "verified" and asset.get("digest"):
                by_digest.setdefault(asset["digest"], []).append(asset["asset_id"])
            if mode == "recorded_extractor":
                normalized["source_shortcode"] = items[asset["item"]]["shortcode"]
                status = asset["status"]
                normalized["collection_outcome"] = (
                    "downloaded" if status == "verified" else
                    "skipped" if status in ("thumbnail", "recorded_bytes_unavailable") or asset.get("kind") == "unsupported" else
                    "blocked" if status in ("restricted", "access_blocked", "rate_limited") else
                    "discovered" if status == "pending" else "failed"
                )
            manifest["assets"].append(normalized)
    record_publication_review(manifest, job)
    manifest["unknown_date_review"] = list(dict.fromkeys(
        [*manifest["unknown_date_review"], *(key for report in reports.values() for key in report["unknown_date_review"])]
    ))
    duplicate_groups = [{"sha256": digest, "asset_ids": ids} for digest, ids in by_digest.items() if len(ids) > 1]
    if mode == "recorded_extractor" or duplicate_groups:
        manifest["duplicate_provenance_groups"] = duplicate_groups
    if validate_against_disk(job_dir, manifest):
        raise ValueError("Public collection manifest failed file-integrity validation")
    write_manifest(job_dir, manifest)
    return manifest
