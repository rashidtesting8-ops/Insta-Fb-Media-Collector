"""Website discovery adapter for an injected, offline document transport."""

import json
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from app.collection_checkpoint import Attachment, InventoryPage, SourceItem, SourceStop
from app.legacy_collector import candidate_url, grab_media, normalize_page_url


class WebsiteInventory:
    def __init__(self, website_url, *, sitemaps, fetch, permitted, document_limit, entry_limit, media_permitted=None):
        if min(document_limit, entry_limit) < 1:
            raise ValueError("Fixture discovery limits must be positive")
        self.origin = self._origin(website_url)
        self.seed = [("html", website_url), *(("xml", url) for url in sitemaps)]
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
        archives = {candidate_url(str(tag["href"]), base) for tag in soup.select("a[href]")
                    if "/archive" in str(tag["href"]).lower()}
        links = [("next", target) for target in sorted(next_urls - {None})]
        links.extend(("html", target) for target in sorted(archives - next_urls - {None}))
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
            # Fixture transport supplies a final URL and bytes. This adapter opens no connections.
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
