"""Website discovery adapter for an injected, offline or guarded HTTP document transport."""

import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from app.collection_checkpoint import Attachment, InventoryPage, SourceItem, SourceStop
from app.legacy_collector import MEDIA_EXTENSIONS, candidate_url, grab_media, normalize_page_url

_HISTORICAL_PATH_HINTS = (
    "archive", "archives", "blog", "news", "post", "posts", "article", "articles",
    "category", "categories", "gallery", "galleries", "portfolio", "photo", "photos",
    "video", "videos", "media", "transformation", "transformations", "before-after",
    "work", "our-work", "grooming", "updates", "diary", "journal", "showcase",
)
_EXCLUDED_PATH_HINTS = (
    "/logout", "/login", "/signin", "/sign-in", "/signup", "/sign-up", "/register",
    "/account", "/cart", "/checkout", "/basket", "/admin", "/wp-admin", "/wp-login",
    "/feed", "/privacy", "/terms", "/cookie", "/search", "/wp-json",
)
_NON_HTML_EXTS = frozenset(MEDIA_EXTENSIONS | {".pdf", ".zip", ".gz", ".tar", ".xml", ".json", ".css", ".js", ".ico", ".woff", ".woff2"})
_PAGINATION_PATH_RE = re.compile(r"(?:/page/\d+/?$|(?:^|&)(?:page|paged|p)=\d+(?:&|$))", re.IGNORECASE)


class WebsiteInventory:
    def __init__(
        self,
        website_url,
        *,
        sitemaps,
        fetch,
        permitted,
        document_limit,
        entry_limit,
        media_permitted=None,
        historical_mode: bool = False,
        robots_sitemaps=(),
    ):
        if min(document_limit, entry_limit) < 1:
            raise ValueError("Fixture discovery limits must be positive")
        self.origin = self._origin(website_url)
        self.historical_mode = bool(historical_mode)
        seed_sitemaps = list(dict.fromkeys([*sitemaps, *(robots_sitemaps if self.historical_mode else ())]))
        self.seed = [("html", website_url), *(("xml", url) for url in seed_sitemaps)]
        self.fetch = fetch
        self.permitted = permitted
        self.media_permitted = media_permitted or self._allowed
        self.document_limit = document_limit
        self.entry_limit = entry_limit

    @staticmethod
    def _origin(url):
        parts = urlsplit(url)
        return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)

    def _allowed(self, url):
        parts = urlsplit(url)
        return (parts.scheme in ("https", "http") and parts.username is None and parts.password is None
                and self._origin(url) == self.origin and self.permitted(url))

    def _same_origin_candidate(self, url):
        if not url:
            return False
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or parts.username is not None or parts.password is not None:
            return False
        return self._origin(url) == self.origin

    @staticmethod
    def _is_excluded_page(url: str) -> bool:
        parts = urlsplit(url)
        low_path = (parts.path or "/").lower()
        if any(bad in low_path for bad in _EXCLUDED_PATH_HINTS):
            return True
        suffix = PurePosixPath(low_path).suffix
        return suffix in _NON_HTML_EXTS

    @staticmethod
    def _is_pagination_url(url: str) -> bool:
        parts = urlsplit(url)
        return bool(_PAGINATION_PATH_RE.search(parts.path or "") or _PAGINATION_PATH_RE.search(parts.query or ""))

    @staticmethod
    def _is_historical_content_url(url: str) -> bool:
        parts = urlsplit(url)
        segments = [seg for seg in (parts.path or "/").lower().split("/") if seg]
        if any(any(hint == seg or hint in seg for hint in _HISTORICAL_PATH_HINTS) for seg in segments):
            return True
        # Match WordPress/static date archives such as /2025/11/ or /2026/02/
        if len(segments) >= 2 and re.fullmatch(r"20\d{2}", segments[0]) and re.fullmatch(r"(?:0[1-9]|1[0-2])", segments[1]):
            return True
        return False

    def _sitemap(self, body, base):
        # Reject declarations (including UTF-16/NUL forms) before using the XML parser.
        if b"\x00" in body or b"<!" in body:
            raise ValueError("Unsupported sitemap markup")
        root = ElementTree.fromstring(body)
        kind = root.tag.rsplit("}", 1)[-1]
        if kind not in ("sitemapindex", "urlset"):
            raise ValueError("Unexpected sitemap document")
        links = []
        for location in root.findall("./*/{*}loc"):
            target = candidate_url((location.text or "").strip(), base)
            if target:
                links.append(("xml" if kind == "sitemapindex" else "html", target))
        return links, ()

    def _html(self, body, base, issues):
        soup = BeautifulSoup(body, "html.parser")
        next_urls = {candidate_url(str(tag["href"]), base) for tag in soup.select('[rel~="next"][href]')}
        if not self.historical_mode:
            archives = {candidate_url(str(tag["href"]), base) for tag in soup.select("a[href]")
                        if "/archive" in str(tag["href"]).lower()}
            links = [("next", target) for target in sorted(next_urls - {None})]
            links.extend(("html", target) for target in sorted(archives - next_urls - {None}))
        else:
            pag_selectors = (
                'nav[aria-label*="pag" i] a[href]',
                '.pagination a[href]',
                '.pager a[href]',
                'a.next[href]',
                'a.page-numbers[href]',
            )
            pag_urls = set(next_urls)
            for selector in pag_selectors:
                for tag in soup.select(selector):
                    pag_urls.add(candidate_url(str(tag["href"]), base))
            content_urls: set[str] = set()
            for tag in soup.select("a[href]"):
                target = candidate_url(str(tag["href"]), base)
                if not target or not self._same_origin_candidate(target) or self._is_excluded_page(target):
                    continue
                if self._is_pagination_url(target):
                    pag_urls.add(target)
                elif self._is_historical_content_url(target):
                    content_urls.add(target)
            clean_next = sorted(
                t for t in pag_urls if t and self._same_origin_candidate(t) and not self._is_excluded_page(t)
            )
            clean_next_set = set(clean_next)
            clean_content = sorted(t for t in content_urls if t not in clean_next_set)
            links = [("next", target) for target in clean_next]
            links.extend(("html", target) for target in clean_content)
        posters = {candidate_url(str(tag["poster"]), base) for tag in soup.select("video[poster]")}
        videos = {candidate_url(str(tag["src"]), base) for tag in soup.select("video[src], video source[src]")}
        attachments = {}
        for media in grab_media(soup, base):
            url = media["url"]
            if not self.media_permitted(url):
                issues.append("access_blocked")
                continue
            suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
            if url in posters:
                kind = "thumbnail"
            elif url in videos or suffix in (".mp4", ".webm", ".mov") or media["discovery"] == "video":
                kind = "video"
            else:
                kind = "image"
            attachments.setdefault(url, Attachment(url, url, kind, media.get("publication_evidence")))
        if len(attachments) > self.entry_limit:
            issues.append("attachment_cap")
        return links, tuple(attachments.values())[:self.entry_limit]

    def page(self, cursor):
        pending, visited_urls = json.loads(cursor) if cursor else (self.seed.copy(), [])
        visited = set(visited_urls)
        kind, url = pending.pop(0)
        normalized = normalize_page_url(url)
        issues = []
        items = ()
        visited.add(normalized)
        try:
            if not self._allowed(url):
                raise SourceStop("access_blocked")
            # Fixture or guarded HTTP transport supplies final URL and bytes.
            final_url, body = self.fetch(url)
            if not self._allowed(final_url):
                raise SourceStop("access_blocked")
            if len(body) > self.document_limit:
                raise SourceStop("document_cap")
            page_id = normalize_page_url(final_url)
            visited.add(page_id)
            links, attachments = (self._sitemap(body, final_url) if kind == "xml" else
                                  self._html(body, final_url, issues))
            items = (SourceItem(page_id, final_url, attachments),)
            if len(links) > self.entry_limit:
                issues.append("discovery_cap")
            queued = {normalize_page_url(target) for _, target in pending}
            for relation, target in links[:self.entry_limit]:
                identity = normalize_page_url(target)
                if relation == "next" and identity in visited:
                    issues.append("pagination_loop")
                if identity not in visited and identity not in queued:
                    pending.append((relation, target))
                    queued.add(identity)
        except SourceStop as exc:
            if str(exc) in ("restricted", "rate_limited", "request_budget", "time_budget"):
                raise
            issues.append(str(exc))
            items = (SourceItem(normalized, url, status=str(exc)),)
        except (ElementTree.ParseError, ValueError):
            issues.append("document_failure")
            items = (SourceItem(normalized, url, status="document_failure"),)
        return InventoryPage(items, json.dumps([pending, sorted(visited)]) if pending else None, tuple(issues))
