"""Opt-in public collection. No account access or unguarded extractor subprocess."""

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from requests import HTTPError

from app.browser_renderer import render_page_html

from app.collection_checkpoint import (
    Attachment, CollectionCheckpoint, Download, GlobalBudget, InventoryPage, SourceItem, SourceStop,
)
from app.legacy_collector import MEDIA_TYPES, build_collector_session, response_get, robots_allowed
from app.legacy_social_collect import check_social_url
from app.manifest import SCHEMA_VERSION, validate_against_disk, write_manifest
from app.publication import WINDOW_FIELDS, publication_evidence, record_publication_review
from app.website_inventory import WebsiteInventory

SOURCES = ("website", "instagram", "facebook")
COLLECTION_HOLD = "blocked_unverified_collection"
_ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)

# Vetted website-builder & managed image CDNs used by pet grooming businesses
# (Wix, Squarespace, Shopify, Webflow, WordPress Jetpack/Cloudinary, GoDaddy, BunnyCDN).
# Every hop still enforces HTTPS-443, SafeHTTPAdapter DNS/SSRF checks, robots.txt, and magic-byte validation.
WEBSITE_MEDIA_HOST_SUFFIXES = (
    ".wixstatic.com",
    ".wixmp.com",
    ".squarespace-cdn.com",
    ".squarespace.com",
    ".shopify.com",
    ".shopifycdn.net",
    ".website-files.com",
    ".webflow.com",
    ".wp.com",
    ".cloudinary.com",
    ".wsimg.com",
    ".godaddysites.com",
    ".secureserver.net",
    ".ctfassets.net",
    ".sanity.io",
    ".b-cdn.net",
)


def origin(url):
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("Expected a credential-free public HTTP(S) origin")
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _origin_str(url: str) -> str:
    scheme, host, port = origin(url)
    default_port = 443 if scheme == "https" else 80
    netloc = host if port == default_port else f"{host}:{port}"
    return f"{scheme}://{netloc}"


def _website_media_origins(website_url: str) -> list[str]:
    scheme, host, port = origin(website_url)
    default_port = 443 if scheme == "https" else 80
    hosts = [host]
    if host.startswith("www.") and len(host) > 4:
        hosts.append(host[4:])
    elif "." in host and not host.startswith("www."):
        hosts.append("www." + host)
    out: list[str] = []
    for h in hosts:
        netloc = h if port == default_port else f"{h}:{port}"
        candidate = f"{scheme}://{netloc}"
        if candidate not in out:
            out.append(candidate)
    return out


def build_multitenant_collection_config(job: dict) -> dict:
    """Build a bounded, multi-tenant V2.1 historical collection config from any submitted job."""
    website_url = str(job["website_url"])
    facebook_url = job.get("facebook_url")
    instagram_url = job.get("instagram_url")
    social_confirmed = job.get("social_permission_confirmed") is True

    web_pages = max(1, min(int(job.get("max_pages") or 10), 25))
    web_media = max(1, min(int(job.get("max_media") or 30), 80))
    social_files = max(1, min(int(job.get("max_social_files") or 20), 60))

    sources: dict[str, dict] = {
        "website": {
            "url": website_url,
            "sitemaps": [],
            "discover_robots_sitemaps": True,
            "probe_default_sitemaps": os.getenv("WEBSITE_PROBE_DEFAULT_SITEMAPS") == "1",
            "js_fallback_enabled": os.getenv("PLAYWRIGHT_FALLBACK_ENABLED") == "1",
            "max_js_pages": max(1, min(int(os.getenv("PLAYWRIGHT_MAX_PAGES_PER_JOB", "2") or "2"), 4)),
            "media_origins": _website_media_origins(website_url),
            "media_host_suffixes": list(WEBSITE_MEDIA_HOST_SUFFIXES),
            "download_authorized": True,
            "page_budget": web_pages,
            "entry_limit": web_media,
            "transfer_budget": web_media,
            "request_budget": max(32, web_pages * 4 + web_media * 2),
            "document_limit": 2 * 1024 * 1024,
            "byte_limit": 25 * 1024 * 1024,
            "source_byte_budget": 150 * 1024 * 1024,
            "time_budget_seconds": 120,
            "validate_content_magic": True,
        },
    }
    if facebook_url:
        sources["facebook"] = {
            "url": str(facebook_url),
            "discovery_mode": "open_source_cli",
            "download_authorized": social_confirmed,
            "media_origins": [_origin_str(str(facebook_url))],
            "media_host_suffixes": [".fbcdn.net", ".fbsbx.com"],
            "allow_fallbacks": True,
            "page_budget": 6,
            "cli_request_budget": 8,
            "batch_size": 10,
            "entry_limit": social_files,
            "transfer_budget": social_files if social_confirmed else 0,
            "request_budget": max(32, social_files * 2 + 16),
            "byte_limit": 25 * 1024 * 1024,
            "source_byte_budget": 150 * 1024 * 1024,
            "time_budget_seconds": 180,
            "include_albums": True,
            "album_limit": 3,
            "skip_out_of_window_downloads": True,
            "validate_content_magic": True,
        }
    if instagram_url:
        sources["instagram"] = {
            "url": str(instagram_url),
            "discovery_mode": "open_source_cli",
            "download_authorized": social_confirmed,
            "media_origins": [_origin_str(str(instagram_url))],
            "media_host_suffixes": [".cdninstagram.com", ".fbcdn.net"],
            "allow_fallbacks": True,
            "page_budget": 4,
            "cli_request_budget": 4,
            "batch_size": 10,
            "entry_limit": social_files,
            "transfer_budget": social_files if social_confirmed else 0,
            "request_budget": max(32, social_files * 2 + 16),
            "byte_limit": 25 * 1024 * 1024,
            "source_byte_budget": 150 * 1024 * 1024,
            "time_budget_seconds": 120,
            "include_reels": True,
            "skip_out_of_window_downloads": True,
            "validate_content_magic": True,
        }

    return {
        "collection_profile": "historical",
        "run_preprocessing": True,
        "global_budgets": {
            "max_pages": 40,
            "max_items": 200,
            "max_transfers": 120,
            "max_bytes": 250 * 1024 * 1024,
            "max_time_seconds": 300,
        },
        "sources": sources,
    }


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
        self.media_host_suffixes = tuple(
            s.lower()
            for s in (config.get("media_host_suffixes") or ())
            if isinstance(s, str) and s.startswith(".") and len(s) >= 4 and "/" not in s
        )
        self.session = session
        self.robots = {}
        self.held_origins = held_origins if held_origins is not None else {}
        self.diagnostic_issues: list[str] = []
        self.blocked_url_reasons: dict[str, str] = {}
        self.remaining = limit(config, "request_budget")
        self.deadline = time.monotonic() + limit(config, "time_budget_seconds")

    def _clock(self):
        if time.monotonic() >= self.deadline:
            raise SourceStop("time_budget")

    def _origin_matches(self, current, allowed, *, allow_media_suffixes=False):
        if current in allowed:
            return True
        if allow_media_suffixes and self.media_host_suffixes:
            scheme, host, port = current
            low_host = host.lower().rstrip(".")
            if scheme == "https" and port == 443 and any(
                low_host.endswith(suffix) and len(low_host) > len(suffix)
                for suffix in self.media_host_suffixes
            ):
                return True
        return False

    def _network_policy(self, url, allowed, *, allow_media_suffixes=False):
        self._clock()
        current = origin(url)
        if not self._origin_matches(current, allowed, allow_media_suffixes=allow_media_suffixes):
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
        # Discovery checks origin/suffix only. Robots rules are checked for each actual download hop.
        return self._origin_matches(origin(url), self.media_origins, allow_media_suffixes=True)

    def _record_block(self, url: str, reason: str) -> None:
        self.blocked_url_reasons[url] = reason
        if reason not in self.diagnostic_issues:
            self.diagnostic_issues.append(reason)

    def _get(self, url, allowed, headers=None, *, allow_media_suffixes=False):
        def check_hop(hop):
            path = urlsplit(hop).path.lower().rstrip("/")
            if path in ("/login", "/login.php", "/accounts/login", "/checkpoint", "/challenge"):
                self._record_block(url, "login_redirect")
                raise SourceStop("restricted")
            if not self._origin_matches(origin(hop), allowed, allow_media_suffixes=allow_media_suffixes):
                self._record_block(url, "origin_not_allowed")
                raise SourceStop("access_blocked")
            if not self._robots(hop):
                self._record_block(url, "robots_disallowed")
                raise SourceStop("access_blocked")
            self._network_policy(hop, allowed, allow_media_suffixes=allow_media_suffixes)

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

    def discover_robots_sitemaps(self, *, max_sitemaps: int = 8, probe_defaults: bool = False) -> list[str]:
        """Extract same-origin Sitemap: declarations from /robots.txt within request/time budgets.

        When `probe_defaults=True` and robots.txt declares no sitemaps, safely checks common
        same-origin sitemap endpoints (`/sitemap.xml`, `/sitemap_index.xml`) used by Squarespace,
        Wix, Shopify, Webflow, and WordPress while respecting robots.txt and budgets.
        """
        scheme, host, port = self.origin
        default_port = 443 if scheme == "https" else 80
        netloc = host if port == default_port else f"{host}:{port}"
        robots_url = f"{scheme}://{netloc}/robots.txt"
        found: list[str] = []
        try:
            _, raw = self.document(robots_url, 262_144)
            for match in _ROBOTS_SITEMAP_RE.finditer(raw.decode("utf-8", errors="replace")):
                candidate = match.group(1).strip()
                try:
                    if origin(candidate) == self.origin and candidate not in found:
                        found.append(candidate)
                except ValueError:
                    continue
                if len(found) >= max_sitemaps:
                    break
        except (SourceStop, HTTPError, OSError, ValueError):
            pass

        if not found and probe_defaults:
            was_held = self.origin in self.held_origins
            for suffix in ("/sitemap.xml", "/sitemap_index.xml"):
                candidate = f"{scheme}://{netloc}{suffix}"
                try:
                    if not self.page_permitted(candidate):
                        continue
                    final_sm, sm_raw = self.document(candidate, 262_144)
                    head = sm_raw[:2048].lower()
                    if b"<urlset" in head or b"<sitemapindex" in head:
                        if origin(final_sm) == self.origin and final_sm not in found:
                            found.append(final_sm)
                            break
                except SourceStop as exc:
                    # A 403 on a speculative /sitemap.xml probe must not hold the main website origin.
                    if str(exc) == "restricted" and not was_held and self.held_origins.get(self.origin) == "restricted":
                        self.held_origins.pop(self.origin, None)
                    if str(exc) in ("rate_limited", "time_budget", "request_budget"):
                        break
                    continue
                except (HTTPError, OSError, ValueError):
                    continue
        return found

    def download(self, url, offset, revision):
        headers = {"Accept-Encoding": "identity"}
        if offset and revision:
            headers.update(Range=f"bytes={offset}-", **{"If-Range": revision})
        try:
            response = self._get(url, self.media_origins, headers, allow_media_suffixes=True)
        except HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if offset and status_code == 416:
                return self.download(url, 0, None)
            if status_code in (404, 410):
                raise SourceStop("media_url_expired") from None
            raise
        try:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in MEDIA_TYPES or content_type == "image/svg+xml":
                raise SourceStop("unsupported_media")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Unsupported encoded response")
            raw_len = response.headers.get("Content-Length")
            size = int(raw_len) if raw_len is not None else None
            etag = response.headers.get("ETag", "")
            strong = etag if re.fullmatch(r'"[^"\r\n]+"', etag) else None
            start = 0
            if response.status_code == 206:
                if size is None:
                    raise ValueError("Partial response missing Content-Length")
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

            return Download(size, chunks(), start, strong, close=response.close, mime=content_type)
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
    js_renderer=None,
    clock=time.monotonic,
    run_preprocessing: bool = False,
):
    """Run the checkpoint path for an explicitly scoped job. Curation is held."""
    if configuration.get("offline_only") or configuration.get("fixture_only"):
        raise ValueError("Offline recordings cannot use the public HTTP path")
    raw_profile = configuration.get("collection_profile")
    if raw_profile is None:
        profile = "historical" if configuration.get("historical_mode") is True else "pilot"
    else:
        profile = str(raw_profile).strip().lower()
    if profile not in ("pilot", "historical"):
        raise ValueError("collection_profile must be 'pilot' or 'historical'")
    is_historical = profile == "historical"

    global_budget = None
    gb_cfg = configuration.get("global_budgets")
    if isinstance(gb_cfg, dict) and gb_cfg:
        global_budget = GlobalBudget(
            max_pages=gb_cfg.get("max_pages"),
            max_items=gb_cfg.get("max_items"),
            max_transfers=gb_cfg.get("max_transfers"),
            max_bytes=gb_cfg.get("max_bytes"),
            max_time_seconds=gb_cfg.get("max_time_seconds"),
            clock=clock,
        )

    configs = configuration["sources"]
    active_sources: list[str] = []
    for source in SOURCES:
        submitted_url = job.get(source + "_url")
        if source == "instagram" and not submitted_url:
            continue
        if source not in configs or configs[source].get("url") != submitted_url:
            raise ValueError("Public collection configuration does not match the submitted source URLs")
        active_sources.append(source)
    state = CollectionCheckpoint(job_dir, job)
    reports = {}
    tools_by_source = {}
    held_origins = {}
    for source in SOURCES:
        if source not in active_sources:
            reports[source] = {
                "status": "not_requested",
                "coverage": "not_requested",
                "coverage_percentage": None,
                "items": [],
                "assets": [],
                "unknown_date_review": [],
                "issues": [],
                "stop_reason": None,
                "termination_reason": None,
                "listing_exhausted": False,
            }
            continue
        config = configs[source]
        url = config["url"]
        with session_factory() as session:
            transport = PublicHTTP(url, config, session, held_origins=held_origins)
            if source == "website":
                document_limit = limit(config, "document_limit")
                robots_sitemaps = (
                    transport.discover_robots_sitemaps(
                        probe_defaults=bool(config.get("probe_default_sitemaps", False)),
                    )
                    if is_historical and config.get("discover_robots_sitemaps", True)
                    else []
                )
                active_js_renderer = js_renderer
                if active_js_renderer is None and is_historical and config.get("js_fallback_enabled") is True:
                    active_js_renderer = lambda target: render_page_html(  # noqa: E731
                        target,
                        robots_check=transport.page_permitted,
                    )
                inventory = WebsiteInventory(
                    url,
                    sitemaps=config["sitemaps"],
                    fetch=lambda target: transport.document(target, document_limit),
                    permitted=transport.page_permitted,
                    media_permitted=transport.media_permitted,
                    document_limit=document_limit,
                    entry_limit=limit(config, "entry_limit"),
                    historical_mode=is_historical,
                    robots_sitemaps=robots_sitemaps,
                    js_renderer=active_js_renderer,
                    max_js_pages=int(config.get("max_js_pages") or 2),
                )
            elif config.get("snapshot") is not None or config.get("discovery_mode") != "open_source_cli":
                inventory = PublicSocialSnapshot(source, url, config.get("snapshot"),
                                                 job.get("social_permission_confirmed") is True)
            else:
                from app.social_discovery import OpenSourceSocialInventory

                cli_budget = limit(config, "cli_request_budget") if "cli_request_budget" in config else limit(config, "request_budget")
                inventory = OpenSourceSocialInventory(
                    source,
                    url,
                    permission_confirmed=job.get("social_permission_confirmed") is True,
                    window_start_utc=job.get("window_start_utc"),
                    item_limit=limit(config, "entry_limit"),
                    request_budget=cli_budget,
                    time_budget_seconds=limit(config, "time_budget_seconds"),
                    allow_fallbacks=bool(config.get("allow_fallbacks", True)),
                    runner=social_runner,
                    clock=clock,
                    historical_mode=is_historical,
                    batch_size=limit(config, "batch_size") if "batch_size" in config else None,
                    cutoff_consecutive_older=limit(config, "cutoff_consecutive_older") if "cutoff_consecutive_older" in config else 3,
                    include_reels=bool(config.get("include_reels", is_historical)),
                    include_albums=bool(config.get("include_albums", is_historical)),
                    album_limit=limit(config, "album_limit") if "album_limit" in config else 5,
                    resume_dir=(job_dir / "collection" / "instaloader-resume") if is_historical else None,
                )
            scope_doc = {
                "url": url,
                "media_origins": sorted(transport.media_origins),
                "sitemaps": config.get("sitemaps", []),
                **({"media_host_suffixes": list(transport.media_host_suffixes)} if transport.media_host_suffixes else {}),
                **({"discovery_mode": config["discovery_mode"]} if config.get("discovery_mode") else {}),
                **({"collection_profile": profile} if is_historical else {}),
            }
            scope = json.dumps(scope_doc, sort_keys=True)
            requested_transfer = limit(config, "transfer_budget", minimum=0)
            download_blocked = (
                requested_transfer > 0
                and (
                    (source != "website" and config.get("discovery_mode") == "open_source_cli" and config.get("download_authorized") is not True)
                    or (config.get("download_authorized") is False)
                )
            )
            effective_transfer = 0 if download_blocked else requested_transfer
            source_item_limit = limit(config, "max_discovered_items") if "max_discovered_items" in config else (
                limit(config, "entry_limit") if is_historical and source != "website" and "entry_limit" in config else None
            )
            source_byte_budget = limit(config, "source_byte_budget") if "source_byte_budget" in config else None
            skip_old = bool(config.get("skip_out_of_window_downloads", is_historical))
            validate_magic = bool(config.get("validate_content_magic", is_historical))
            try:
                if hasattr(inventory, "check_access"):
                    inventory.check_access()
                report = state.run_source(
                    source,
                    scope,
                    inventory,
                    transport.download,
                    page_budget=limit(config, "page_budget"),
                    transfer_budget=effective_transfer,
                    byte_limit=limit(config, "byte_limit"),
                    item_limit=source_item_limit,
                    source_byte_budget=source_byte_budget,
                    global_budget=global_budget,
                    skip_out_of_window_downloads=skip_old,
                    validate_content_magic=validate_magic,
                )
            except SourceStop as exc:
                report = state.hold_source(source, scope, str(exc))
            if download_blocked:
                report["issues"] = list(dict.fromkeys([*report["issues"], "download_authorization_required"]))
                report["coverage"] = "incomplete"
            if transport.diagnostic_issues:
                report["issues"] = list(dict.fromkeys([*report["issues"], *transport.diagnostic_issues]))
            if transport.blocked_url_reasons:
                report["blocked_url_reasons"] = dict(transport.blocked_url_reasons)
            if getattr(inventory, "tools_executed", None):
                tools_by_source[source] = list(inventory.tools_executed)
        reports[source] = report
    manifest = publish_checkpoint_manifest(
        job_dir,
        job,
        reports,
        mode="public_checkpoint",
        tools_by_source=tools_by_source,
        collection_profile=profile if is_historical else None,
        global_budget_usage=global_budget.snapshot() if global_budget is not None else None,
    )
    if (run_preprocessing or configuration.get("run_preprocessing") is True) and any(
        asset.get("extraction_status") == "downloaded" and asset.get("local_path")
        for asset in manifest.get("assets", [])
    ):
        from app.preprocessing import run_media_preprocessing

        run_media_preprocessing(
            job_dir,
            job["job_id"],
            cutoff_date=(job.get("window_start_utc") or "")[:10] or None,
        )
        manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    return manifest


def publish_checkpoint_manifest(
    job_dir,
    job,
    reports,
    *,
    mode,
    tools_by_source=None,
    collection_profile=None,
    global_budget_usage=None,
):
    manifest = {"job_id": job["job_id"], "schema_version": SCHEMA_VERSION,
                "website_url": job.get("website_url"),
                **{field: job[field] for field in WINDOW_FIELDS},
                "collection_mode": mode, "collection_gate": COLLECTION_HOLD,
                "sources": {}, "assets": []}
    if collection_profile:
        manifest["collection_profile"] = collection_profile
    if global_budget_usage is not None:
        manifest["global_budget_usage"] = global_budget_usage
    by_digest = {}
    for source, report in reports.items():
        manifest["sources"][source] = {
            key: report[key] for key in ("status", "items", "coverage", "coverage_percentage", "cursor",
                                        "stop_reason", "termination_reason", "issues", "listing_exhausted")
            if key in report
        }
        if tools_by_source and source in tools_by_source:
            manifest["sources"][source]["tools_executed"] = tools_by_source[source]
        if mode == "recorded_extractor":
            manifest["sources"][source].update(discovery_status=report["coverage"], coverage="unknown")
        items = {item["id"]: item for item in report["items"]}
        blocked_reasons = report.get("blocked_url_reasons") or {}
        for asset in report["assets"]:
            normalized = {
                **asset, "platform": source, "job_id": job["job_id"],
                "source_url": asset.get("url"), "source_page": items[asset["item"]]["url"],
                "type": asset.get("kind"), "sha256": asset["digest"], "bytes": asset["size"],
                "rights_status": "unverified",
            }
            if asset.get("url") in blocked_reasons and asset.get("status") != "verified":
                normalized["failure_reason"] = blocked_reasons[asset["url"]]
            if items[asset["item"]].get("shortcode") is not None:
                normalized["source_shortcode"] = items[asset["item"]]["shortcode"]
            if items[asset["item"]].get("caption"):
                normalized["source_caption"] = items[asset["item"]]["caption"]
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
