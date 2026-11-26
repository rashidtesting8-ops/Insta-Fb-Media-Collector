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
_WP_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"})
_WP_SIZE_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?P<w>\d{2,5})x(?P<h>\d{2,5})$")
_WP_SCALED_SUFFIX_RE = re.compile(r"^(?P<base>.+)-scaled$", re.IGNORECASE)


def _wordpress_variant_info(url: str) -> tuple[tuple[str, str, str, str, str, str], str, tuple[int, int] | None] | None:
    """Parse a WordPress-style image URL into (family_key, role, dims).

    role is one of:
    - "original": no trailing -WxH and no trailing -scaled
    - "scaled": trailing -scaled (WordPress big-image threshold master)
    - "sized": trailing -WxH responsive thumbnail variant
    """
    parts = urlsplit(url)
    posix = PurePosixPath(parts.path or "")
    ext = posix.suffix.lower()
    if ext not in _WP_IMAGE_EXTS:
        return None
    stem = posix.stem
    if not stem:
        return None
    parent = str(posix.parent)
    sized = _WP_SIZE_SUFFIX_RE.match(stem)
    if sized:
        w, h = int(sized.group("w")), int(sized.group("h"))
        if w > 0 and h > 0:
            base = sized.group("base")
            scaled_inner = _WP_SCALED_SUFFIX_RE.match(base)
            if scaled_inner:
                base = scaled_inner.group("base")
            key = (parts.scheme.lower(), parts.netloc.lower(), parent, parts.query, base, ext)
            return key, "sized", (w, h)
    scaled = _WP_SCALED_SUFFIX_RE.match(stem)
    if scaled:
        key = (parts.scheme.lower(), parts.netloc.lower(), parent, parts.query, scaled.group("base"), ext)
        return key, "scaled", None
    key = (parts.scheme.lower(), parts.netloc.lower(), parent, parts.query, stem, ext)
    return key, "original", None


def _evidence_key(evidence: dict | None) -> str | None:
    if not isinstance(evidence, dict):
        return None
    return json.dumps(evidence, sort_keys=True)


def group_wordpress_responsive_media(
    candidates: list[tuple[str, str, dict | None]],
    page_url: str,
) -> list[tuple[Attachment, tuple[str, ...] | None]]:
    """Collapse WordPress -WxH responsive thumbnail variants on a page while preserving provenance.

    Rules:
    - Non-image or ungrouped URLs remain untouched.
    - When BOTH full-size (`foo.jpg`) and `-scaled` (`foo-scaled.jpg`) exist in the same family,
      BOTH are preserved as distinct records (content identity is not assumed), and `-WxH`
      thumbnails attach to `foo.jpg` (or `foo-scaled.jpg` if explicitly `-scaled-WxH`).
    - When ONLY `foo-scaled.jpg` + `-WxH` exist (or ONLY `foo.jpg` + `-WxH` exist), the master
      URL is selected and `-WxH` URLs are preserved in `alternate_urls`.
    - When ONLY multiple `-WxH` variants exist, the highest pixel-area variant is selected and
      smaller sizes are preserved in `alternate_urls`.
    - Variants with conflicting/distinct verified `publication_evidence` are never merged.
    - Publication dates are NEVER derived from `/wp-content/uploads/YYYY/MM/` directory names.
    """
    dedup_raw: dict[str, tuple[str, dict | None]] = {}
    order: list[str] = []
    for url, kind, evidence in candidates:
        if url not in dedup_raw:
            dedup_raw[url] = (kind, evidence)
            order.append(url)
        elif dedup_raw[url][1] is None and evidence is not None:
            dedup_raw[url] = (dedup_raw[url][0], evidence)

    family_members: dict[tuple[str, str, str, str, str, str], list[tuple[str, str, tuple[int, int] | None, str, dict | None]]] = {}
    ungrouped: dict[str, tuple[str, dict | None]] = {}
    for url in order:
        kind, evidence = dedup_raw[url]
        info = _wordpress_variant_info(url) if kind == "image" else None
        if info is None:
            ungrouped[url] = (kind, evidence)
        else:
            fkey, role, dims = info
            family_members.setdefault(fkey, []).append((url, role, dims, kind, evidence))

    chosen_by_first_url: dict[str, list[tuple[Attachment, tuple[str, ...] | None]]] = {}
    for fkey, members in family_members.items():
        first_url = members[0][0]
        # Partition by verified publication_evidence so distinct verified dates never collapse.
        ev_buckets: dict[str | None, list[tuple[str, str, tuple[int, int] | None, str, dict | None]]] = {}
        for m in members:
            ev_buckets.setdefault(_evidence_key(m[4]), []).append(m)

        # If one bucket has verified evidence and another has None, keep separate unless primary matches.
        emitted: list[tuple[Attachment, tuple[str, ...] | None]] = []
        for _ev_k, bucket in ev_buckets.items():
            originals = [m for m in bucket if m[1] == "original"]
            scaleds = [m for m in bucket if m[1] == "scaled"]
            sizeds = [m for m in bucket if m[1] == "sized"]

            if originals and scaleds:
                # Preserve BOTH full-size and -scaled as distinct records (Task 1 #6).
                orig = originals[0]
                sc = scaleds[0]
                orig_alts: list[str] = []
                sc_alts: list[str] = []
                for s_url, _r, _d, _k, _e in sizeds:
                    if "-scaled-" in PurePosixPath(urlsplit(s_url).path).stem.lower():
                        sc_alts.append(s_url)
                    else:
                        orig_alts.append(s_url)
                emitted.append((
                    Attachment(
                        orig[0], orig[0], orig[3], orig[4],
                        alternate_urls=tuple(sorted(set(orig_alts))),
                        source_pages=(page_url,),
                    ),
                    fkey + ("original",),
                ))
                emitted.append((
                    Attachment(
                        sc[0], sc[0], sc[3], sc[4],
                        alternate_urls=tuple(sorted(set(sc_alts))),
                        source_pages=(page_url,),
                    ),
                    fkey + ("scaled",),
                ))
                continue

            if originals:
                primary = originals[0]
                alts = [m[0] for m in sizeds if m[0] != primary[0]]
                tag = fkey + ("original",)
            elif scaleds:
                primary = scaleds[0]
                alts = [m[0] for m in sizeds if m[0] != primary[0]]
                tag = fkey + ("scaled",)
            else:
                # Only -WxH sized variants present: choose largest pixel area deterministically.
                ranked = sorted(
                    sizeds,
                    key=lambda m: (-(m[2][0] * m[2][1] if m[2] else 0), -(m[2][0] if m[2] else 0), m[0]),
                )
                primary = ranked[0]
                alts = [m[0] for m in ranked[1:]]
                tag = fkey + ("sized",)

            emitted.append((
                Attachment(
                    primary[0], primary[0], primary[3], primary[4],
                    alternate_urls=tuple(sorted(set(alts))),
                    source_pages=(page_url,),
                ),
                tag,
            ))
        chosen_by_first_url[first_url] = emitted

    result: list[tuple[Attachment, tuple[str, ...] | None]] = []
    emitted_families: set[str] = set()
    for url in order:
        if url in ungrouped:
            kind, evidence = ungrouped[url]
            result.append((Attachment(url, url, kind, evidence, source_pages=(page_url,)), None))
        elif url in chosen_by_first_url and url not in emitted_families:
            emitted_families.add(url)
            result.extend(chosen_by_first_url[url])
    return result


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
        videos = {
            candidate_url(str(tag.get("src") or tag.get("data-src") or ""), base)
            for tag in soup.select("video[src], video[data-src], video source[src], video source[data-src]")
        }
        if not self.historical_mode:
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
            return links, tuple(attachments.values())[:self.entry_limit], ()

        raw_candidates: list[tuple[str, str, dict | None]] = []
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
            raw_candidates.append((url, kind, media.get("publication_evidence")))
        return links, tuple(group_wordpress_responsive_media(raw_candidates, base)), ()

    def page(self, cursor):
        seen_media: dict[str, str] = {}
        seen_families: dict[str, str] = {}
        if cursor:
            loaded = json.loads(cursor)
            if isinstance(loaded, dict) and loaded.get("v") == 2:
                pending = list(loaded.get("pending") or ())
                visited_urls = list(loaded.get("visited") or ())
                seen_media = dict(loaded.get("seen_media") or {})
                seen_families = dict(loaded.get("seen_families") or {})
            else:
                pending, visited_urls = loaded
        else:
            pending, visited_urls = self.seed.copy(), []
        visited = set(visited_urls)
        kind, url = pending.pop(0)
        normalized = normalize_page_url(url)
        issues = []
        items = ()
        provenance_updates: list[tuple[str, str, tuple[str, ...]]] = []
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
            if kind == "xml":
                links, attachments = self._sitemap(body, final_url)
            elif not self.historical_mode:
                links, attachments, _ = self._html(body, final_url, issues)
            else:
                links, grouped, _ = self._html(body, final_url, issues)
                fresh: list[Attachment] = []
                for att, fam_tag in grouped:
                    # Check exact canonical URL or same WordPress responsive family (when undated/matching).
                    fam_key = json.dumps(list(fam_tag)) if fam_tag is not None and att.publication_evidence is None else None
                    base_fam_key = (
                        json.dumps(list(fam_tag[:-1]))
                        if fam_tag is not None and fam_tag[-1] == "sized" and att.publication_evidence is None
                        else None
                    )
                    existing_id = seen_media.get(att.id)
                    if existing_id is None and fam_key is not None:
                        existing_id = seen_families.get(fam_key)
                    if existing_id is None and base_fam_key is not None:
                        existing_id = seen_families.get(base_fam_key)
                    if existing_id is not None:
                        alts = tuple(sorted(set([*(att.alternate_urls), *([att.url] if att.url != existing_id else [])])))
                        provenance_updates.append((existing_id, final_url, alts))
                        continue
                    fresh.append(att)
                if len(fresh) > self.entry_limit:
                    issues.append("attachment_cap")
                kept = fresh[:self.entry_limit]
                for att in kept:
                    seen_media[att.id] = att.id
                    for alt in att.alternate_urls:
                        seen_media.setdefault(alt, att.id)
                for att, fam_tag in grouped:
                    if att not in kept or fam_tag is None or att.publication_evidence is not None:
                        continue
                    seen_families[json.dumps(list(fam_tag))] = att.id
                    if fam_tag[-1] in ("original", "scaled"):
                        seen_families.setdefault(json.dumps(list(fam_tag[:-1])), att.id)
                attachments = tuple(kept)
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
        if pending:
            next_cursor = (
                json.dumps(
                    {"v": 2, "pending": pending, "visited": sorted(visited), "seen_media": seen_media, "seen_families": seen_families},
                    sort_keys=True,
                )
                if self.historical_mode
                else json.dumps([pending, sorted(visited)])
            )
        else:
            next_cursor = None
        return InventoryPage(items, next_cursor, tuple(issues), provenance_updates=tuple(provenance_updates))
