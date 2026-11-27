"""Optional bounded, SSRF-hardened Playwright JS fallback renderer for website discovery.

Security & compliance invariants (Audit H-3 airtight remediation):
- Strictly opt-in (triggered ONLY on permitted public website pages in historical mode when
  static HTML extraction finds fewer than `min_static_media` media candidates and JS/SPA
  signals are present).
- Never bypasses login walls, CAPTCHAs, Cloudflare challenges, paywalls, or robots.txt.
  Checks `robots_check(url)` BEFORE launching Chromium, on EVERY document redirect hop,
  AND on the final `page.url` after rendering.
- Zero `route.continue_()` and zero direct Chromium sockets:
  1. Every browser network request is intercepted via `context.route("**/*", ...)` and
     either aborted, stubbed with a 1x1 transparent PNG (for vetted image URLs so JS
     `onload` handlers succeed with zero media bytes transferred inside Chromium), or
     fulfilled via Python's `SafeHTTPAdapter` (`build_collector_session` + `response_get`),
     which resolves DNS once per hop, validates EVERY IP against private/loopback/link-local/
     metadata ranges, dials the verified IP directly (`dial_verified_socket`), validates
     every manual redirect hop (up to 6 hops), clears cookies, and caps decompressed bytes.
  2. As defense-in-depth, Chromium launches with `--proxy-server=http://127.0.0.1:<port>`
     and `--proxy-bypass-list=<-loopback>` pointing to an in-process fail-closed loopback
     proxy that rejects every connection with `403 Forbidden`, preventing any unintercepted
     background/WebSocket/prefetch socket from ever reaching private or external networks.
- Subresources (`script`, `stylesheet`, `xhr`, `fetch`) are restricted to same-site origins
  plus vetted website-builder / public JS framework CDNs, read-only methods (`GET`/`HEAD`),
  strict per-render request count (`<= 60`), per-response byte cap (`<= 3 MB`), cumulative
  byte cap (`<= 15 MB`), and a process-wide `BoundedSemaphore(1)` concurrency cap.
- Degrades gracefully with `BrowserRendererUnavailable` when Playwright/Chromium is not
  installed in the container image, and `BrowserRenderError` on render timeouts/failures.
"""

from __future__ import annotations

import base64
from html import escape as html_escape
import importlib.util
import re
import socket
import threading
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from app.legacy_collector import (
    USER_AGENT,
    _default_resolver,
    build_collector_session,
    response_get,
    safe_url,
    validate_ip_address,
)

DEFAULT_TIMEOUT_MS = 15_000
DEFAULT_MAX_SCROLLS = 3
DEFAULT_MAX_HTML_BYTES = 5_000_000
DEFAULT_MAX_SUBRESOURCES = 60
DEFAULT_MAX_SINGLE_RESPONSE_BYTES = 3_000_000
DEFAULT_MAX_TOTAL_RESPONSE_BYTES = 15_000_000
DEFAULT_MIN_STATIC_MEDIA = 3

# 1x1 transparent PNG (70 bytes) returned synthetically for allowed image subresources so
# JS `img.onload` handlers succeed without fetching heavy image binaries inside Chromium.
_TRANSPARENT_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABJzQnCgAAAABJRU5ErkJggg=="
)

_FETCHABLE_RESOURCE_TYPES = frozenset({"document", "script", "stylesheet", "xhr", "fetch"})
_STUBBED_RESOURCE_TYPES = frozenset({"image"})
_ALLOWED_HTTP_METHODS = frozenset({"GET", "HEAD"})

_LOGIN_OR_CHALLENGE_PATHS = frozenset({
    "/login",
    "/login.php",
    "/accounts/login",
    "/signin",
    "/sign-in",
    "/wp-login",
    "/wp-login.php",
    "/customer_authentication",
    "/checkpoint",
    "/challenge",
})

_STRIPPED_RESPONSE_HEADERS = frozenset({
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
    "set-cookie",
    "strict-transport-security",
})

# Vetted website-builder & standard public JS/CSS framework CDNs needed to hydrate SPA galleries.
# Trackers/ad networks and arbitrary untrusted domains remain blocked.
BROWSER_SUBRESOURCE_HOST_SUFFIXES: tuple[str, ...] = (
    # Wix
    ".wixstatic.com",
    ".wixmp.com",
    ".parastorage.com",
    ".wix.com",
    ".wixapps.net",
    # Squarespace
    ".squarespace.com",
    ".squarespace-cdn.com",
    ".sqspcdn.com",
    # Shopify
    ".shopify.com",
    ".shopifycdn.net",
    ".shopifysvc.com",
    # Webflow
    ".website-files.com",
    ".webflow.com",
    # GoDaddy Website Builder
    ".wsimg.com",
    ".godaddysites.com",
    ".secureserver.net",
    # Duda
    ".cdn-website.com",
    ".multiscreensite.com",
    ".dudaone.com",
    # GoHighLevel / LeadConnector
    ".leadconnectorhq.com",
    ".msgsndr.com",
    ".filesafe.space",
    # Weebly / Square Online
    ".weebly.com",
    ".editmysite.com",
    ".square.site",
    # Framer
    ".framer.website",
    ".framerusercontent.com",
    ".framerstatic.com",
    # WordPress / Jetpack / Managed media CDNs
    ".wp.com",
    ".wordpress.com",
    ".cloudinary.com",
    ".imgix.net",
    ".cloudfront.net",
    ".b-cdn.net",
    ".fastly.net",
    ".ctfassets.net",
    ".sanity.io",
    ".prismic.io",
    ".strikinglycdn.com",
    ".zyrosite.com",
    ".jimcdn.com",
    ".site123.me",
    ".amazonaws.com",
    ".storage.googleapis.com",
    # Standard public JS/CSS framework CDNs
    ".jsdelivr.net",
    ".unpkg.com",
    ".googleapis.com",
    ".gstatic.com",
    ".jquery.com",
    ".bootstrapcdn.com",
)
_EXACT_SUBRESOURCE_HOSTS = frozenset({"cdnjs.cloudflare.com", "unpkg.com"})

_JS_RENDER_SIGNALS = (
    "wix-thunderbolt",
    "wix-warmup-data",
    "id=\"root\"",
    "id='root'",
    "id=\"__next\"",
    "id='__next'",
    "id=\"__nuxt\"",
    "id='__nuxt'",
    "id=\"app\"",
    "id='app'",
    "data-reactroot",
    "ng-version",
    "data-sveltekit",
    "enable javascript",
    "requires javascript",
    "javascript is required",
    "squarespace-config",
    "static.squarespace.com",
    "webflow.js",
    "shopify-section",
    "godaddysites",
    "wsimg.com",
    "ux-app",
    "dmbody",
    "cdn-website.com",
    "leadconnectorhq",
    "msgsndr.com",
    "editmysite.com",
    "__framer_",
    "elementor-gallery",
    "swiper-wrapper",
)

_CHALLENGE_DOM_MARKERS = (
    "<title>just a moment...</title>",
    "cf-browser-verification",
    "cdn-cgi/challenge-platform",
    "checking your browser before accessing",
    "verify you are human",
)

_SCRIPT_TAG_RE = re.compile(r"<script\b", re.IGNORECASE)

# Process-wide concurrency limit: at most 1 headless Chromium instance at a time so
# container memory stays strictly bounded.
_BROWSER_SEMAPHORE = threading.BoundedSemaphore(1)


class BrowserRendererUnavailable(RuntimeError):
    """Raised when Playwright or Chromium is not installed in the runtime image."""


class BrowserRenderError(RuntimeError):
    """Raised when headless Chromium fails or times out rendering a page."""


def playwright_installed() -> bool:
    """Return True if the `playwright` Python package is importable in this runtime."""
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


def _origin_tuple(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").strip().strip("[]").lower().rstrip(".")
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _canonical_site_origins(url: str) -> set[tuple[str, str, int]]:
    scheme, host, port = _origin_tuple(url)
    out = {(scheme, host, port)}
    if host.startswith("www.") and len(host) > 4:
        out.add((scheme, host[4:], port))
    elif host and "." in host and not host.replace(".", "").isdigit():
        out.add((scheme, f"www.{host}", port))
    return out


def _apex_domain(host: str) -> str:
    low = host.lower().rstrip(".")
    return low[4:] if low.startswith("www.") and len(low) > 4 else low


def _is_login_or_challenge_path(url: str) -> bool:
    path = (urlsplit(url).path or "/").lower().rstrip("/")
    if path in _LOGIN_OR_CHALLENGE_PATHS:
        return True
    return any(
        path.startswith(prefix + "/")
        for prefix in ("/customer_authentication", "/accounts/login", "/checkpoint", "/challenge")
    )


def verify_browser_url(
    url: str,
    *,
    resolver: Any = None,
    allow_private: bool = False,
) -> list[str]:
    """Validate URL syntax, port, and verify every resolved DNS IP is public."""
    safe_url(url, allow_private=allow_private)
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported browser scheme: {parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials in browser URL are forbidden")
    host = (parsed.hostname or "").strip().strip("[]")
    if not host:
        raise ValueError("Browser URL missing hostname")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not allow_private and port not in (80, 443):
        raise ValueError(f"Non-standard browser port blocked: {port}")

    active_resolver = resolver or _default_resolver
    records = list(active_resolver(host, port))
    if not records:
        raise ValueError(f"DNS resolution returned no addresses for {host!r}")
    verified_ips: list[str] = []
    for entry in records:
        sockaddr = entry[4]
        raw_ip = str(sockaddr[0])
        verified_ips.append(validate_ip_address(raw_ip, allow_private=allow_private))
    return verified_ips


def looks_like_js_rendered_page(
    html_body: bytes | str,
    static_media_count: int,
    *,
    min_static_media: int = DEFAULT_MIN_STATIC_MEDIA,
) -> bool:
    """Return True when static HTML yielded fewer than `min_static_media` assets and shows JS/SPA signals."""
    threshold = max(1, int(min_static_media))
    if static_media_count >= threshold:
        return False
    text = (
        html_body.decode("utf-8", errors="replace")
        if isinstance(html_body, (bytes, bytearray))
        else str(html_body)
    )
    low = text.lower()
    if any(sig in low for sig in _JS_RENDER_SIGNALS):
        return True
    return static_media_count == 0 and bool(_SCRIPT_TAG_RE.search(low))


def _subresource_host_allowed(
    url: str,
    *,
    site_origins: set[tuple[str, str, int]],
    allowed_suffixes: tuple[str, ...],
    allow_private: bool = False,
) -> bool:
    orig = _origin_tuple(url)
    if orig in site_origins:
        return True
    scheme, host, port = orig
    if not host:
        return False
    # Allow same-site subdomains (e.g., assets.salon.com for www.salon.com) over HTTPS-443 (or local test port).
    if scheme == "https" and port == 443 or allow_private:
        for _, site_host, _ in site_origins:
            apex = _apex_domain(site_host)
            if apex and "." in apex and (host == apex or host.endswith("." + apex)):
                return True
    if not allow_private and not (scheme == "https" and port == 443):
        return False
    if host in _EXACT_SUBRESOURCE_HOSTS:
        return True
    return any(
        host.endswith(suffix) and len(host) > len(suffix)
        for suffix in allowed_suffixes
    )


def _sanitize_fulfill_headers(raw_headers: Any) -> dict[str, str]:
    clean: dict[str, str] = {}
    if not raw_headers:
        return clean
    for key, val in raw_headers.items():
        k_low = str(key).lower().strip()
        if k_low in _STRIPPED_RESPONSE_HEADERS:
            continue
        if k_low in (
            "content-type",
            "access-control-allow-origin",
            "access-control-allow-methods",
            "access-control-allow-headers",
            "vary",
            "cache-control",
        ):
            clean[k_low] = str(val)
    return clean


class _FailClosedProxy:
    """Minimal loopback TCP server that rejects every connection with HTTP 403.

    Attached as Chromium's `--proxy-server` with `--proxy-bypass-list=<-loopback>` so that
    any unintercepted Chromium network connection (e.g., speculative preconnect, WebSocket,
    background fetch) fails closed without ever reaching internal or external networks.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.25)
        self.port: int = int(self._sock.getsockname()[1])
        self._stop = threading.Event()
        self.blocked_connections: int = 0
        self._thread = threading.Thread(target=self._serve, name="pgm-browser-deny-proxy", daemon=True)

    @property
    def server_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.blocked_connections += 1
            try:
                conn.settimeout(0.5)
                try:
                    conn.recv(1024)
                except OSError:
                    pass
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1.5)


def make_ssrf_route_handler(
    *,
    site_url: str | None = None,
    site_origins: Iterable[tuple[str, str, int]] | None = None,
    session: Any = None,
    resolver: Any = None,
    connector: Any = None,
    robots_check: Callable[[str], bool] | None = None,
    allow_private: bool = False,
    allowed_subresource_suffixes: tuple[str, ...] = BROWSER_SUBRESOURCE_HOST_SUFFIXES,
    max_requests: int = DEFAULT_MAX_SUBRESOURCES,
    max_response_bytes: int = DEFAULT_MAX_SINGLE_RESPONSE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_RESPONSE_BYTES,
    deadline: float | None = None,
    state: dict[str, Any] | None = None,
) -> Callable[[Any], None]:
    """Return a Playwright `context.route` callback that NEVER calls `route.continue_()`.

    Every allowed network request is fetched in Python through `SafeHTTPAdapter` +
    `response_get()` (IP-pinned DNS, hop-by-hop redirect validation, robots checks,
    cookie stripping, and strict byte/request/time caps) and returned via `route.fulfill()`.
    """
    resolved_origins: set[tuple[str, str, int]] = set()
    if site_origins is not None:
        for orig in site_origins:
            resolved_origins.add((str(orig[0]).lower(), str(orig[1]).lower().rstrip("."), int(orig[2])))
    elif site_url:
        resolved_origins = _canonical_site_origins(site_url)

    active_session = session or build_collector_session(
        allow_private=allow_private,
        resolver=resolver,
        connector=connector,
        user_agent=USER_AGENT,
    )
    budget = state if state is not None else {}
    budget.setdefault("requests", 0)
    budget.setdefault("total_bytes", 0)
    budget.setdefault("aborted", 0)
    budget.setdefault("stubbed_images", 0)
    budget.setdefault("document_error", None)
    verified_stub_hosts: set[tuple[str, int]] = set()

    def _abort(route: Any, reason: str | None = None) -> None:
        budget["aborted"] = int(budget.get("aborted", 0)) + 1
        if reason:
            budget.setdefault("abort_reasons", []).append(reason)
        route.abort()

    def _handle_route(route: Any) -> None:
        try:
            if deadline is not None and time.monotonic() >= deadline:
                _abort(route, "deadline_exceeded")
                return

            req = route.request
            method = str(getattr(req, "method", "GET") or "GET").upper()
            if method not in _ALLOWED_HTTP_METHODS:
                _abort(route, "method_blocked")
                return

            res_type = str(getattr(req, "resource_type", "") or "").lower()
            req_url = str(getattr(req, "url", "") or "")
            if not req_url.startswith(("http://", "https://")):
                _abort(route, "scheme_blocked")
                return

            # Validate syntactic URL safety upfront (no credentials, standard ports, valid host).
            safe_url(req_url, allow_private=allow_private)

            # Image subresources: fulfill with 1x1 PNG stub (0 upstream bytes) if host is vetted,
            # or abort if private/untrusted. Real pixels are downloaded later by PublicHTTP.
            if res_type in _STUBBED_RESOURCE_TYPES:
                if resolved_origins and not _subresource_host_allowed(
                    req_url,
                    site_origins=resolved_origins,
                    allowed_suffixes=allowed_subresource_suffixes,
                    allow_private=allow_private,
                ):
                    _abort(route, "image_host_blocked")
                    return
                _, img_host, img_port = _origin_tuple(req_url)
                if (img_host, img_port) not in verified_stub_hosts:
                    verify_browser_url(req_url, resolver=resolver, allow_private=allow_private)
                    verified_stub_hosts.add((img_host, img_port))
                budget["stubbed_images"] = int(budget.get("stubbed_images", 0)) + 1
                route.fulfill(
                    status=200,
                    headers={"content-type": "image/png", "cache-control": "no-store"},
                    body=_TRANSPARENT_PNG_1X1,
                )
                return

            if res_type not in _FETCHABLE_RESOURCE_TYPES:
                _abort(route, "resource_type_blocked")
                return

            is_document = res_type == "document"

            if int(budget.get("requests", 0)) >= max_requests:
                _abort(route, "request_budget_exceeded")
                return

            def _check_hop(hop_url: str) -> None:
                if deadline is not None and time.monotonic() >= deadline:
                    raise PermissionError("deadline_exceeded")
                safe_url(hop_url, allow_private=allow_private)
                if _is_login_or_challenge_path(hop_url):
                    raise PermissionError("login_or_challenge_redirect")
                if is_document:
                    if resolved_origins and _origin_tuple(hop_url) not in resolved_origins:
                        raise PermissionError("offsite_document_redirect")
                    if robots_check is not None and not robots_check(hop_url):
                        raise PermissionError("robots_disallowed")
                else:
                    if resolved_origins and not _subresource_host_allowed(
                        hop_url,
                        site_origins=resolved_origins,
                        allowed_suffixes=allowed_subresource_suffixes,
                        allow_private=allow_private,
                    ):
                        raise PermissionError("subresource_host_blocked")
                active_session.cookies.clear()

            budget["requests"] = int(budget.get("requests", 0)) + 1
            resp = response_get(
                active_session,
                req_url,
                allow_private=allow_private,
                stream=True,
                resolver=resolver,
                connector=connector,
                url_policy=_check_hop,
                headers={"Accept-Encoding": "gzip, deflate", "User-Agent": USER_AGENT},
            )
            try:
                if is_document:
                    if resp.status_code == 429:
                        budget["document_error"] = "rate_limited"
                        _abort(route, "rate_limited")
                        return
                    if resp.status_code in (401, 403, 407, 451) or resp.headers.get("cf-mitigated") == "challenge":
                        budget["document_error"] = "restricted"
                        _abort(route, "restricted")
                        return

                body = bytearray()
                if method != "HEAD":
                    for chunk in resp.iter_content(chunk_size=65536):
                        if deadline is not None and time.monotonic() >= deadline:
                            _abort(route, "deadline_exceeded")
                            return
                        if not chunk:
                            continue
                        if len(body) + len(chunk) > max_response_bytes:
                            _abort(route, "response_size_cap")
                            return
                        if int(budget.get("total_bytes", 0)) + len(body) + len(chunk) > max_total_bytes:
                            _abort(route, "total_size_cap")
                            return
                        body.extend(chunk)
                budget["total_bytes"] = int(budget.get("total_bytes", 0)) + len(body)
                headers = _sanitize_fulfill_headers(resp.headers)
                route.fulfill(
                    status=int(resp.status_code),
                    headers=headers,
                    body=bytes(body),
                )
            finally:
                resp.close()
        except PermissionError as exc:
            if str(getattr(getattr(route, "request", None), "resource_type", "") or "").lower() == "document":
                budget["document_error"] = str(exc)
            _abort(route, str(exc))
        except Exception as exc:
            _abort(route, type(exc).__name__)

    return _handle_route


_COMPUTED_MEDIA_JS = """() => {
  const urls = new Set();
  const add = (u) => {
    if (!u || typeof u !== "string") return;
    const trimmed = u.trim();
    if ((trimmed.startsWith("https://") || trimmed.startsWith("http://")) && urls.size < 120) {
      urls.add(trimmed);
    }
  };
  for (const img of document.querySelectorAll("img")) {
    add(img.currentSrc || img.src);
  }
  for (const v of document.querySelectorAll("video")) {
    add(v.currentSrc || v.src);
    add(v.poster);
  }
  for (const s of document.querySelectorAll("video source, picture source")) {
    add(s.src);
  }
  const urlRe = /url\\(\\s*['"]?(https?:\\/\\/[^'"\\)\\s]+)['"]?\\s*\\)/gi;
  const elements = document.querySelectorAll("div, section, figure, span, a, li, article, header, main");
  const limit = Math.min(elements.length, 600);
  for (let i = 0; i < limit && urls.size < 120; i++) {
    try {
      const bg = window.getComputedStyle(elements[i]).backgroundImage;
      if (!bg || bg === "none") continue;
      let m;
      urlRe.lastIndex = 0;
      while ((m = urlRe.exec(bg)) !== null) {
        add(m[1]);
      }
    } catch (_) {}
  }
  return Array.from(urls);
}"""


def _append_computed_media_hints(html_text: str, media_urls: list[str]) -> str:
    if not media_urls:
        return html_text
    tags: list[str] = []
    for raw_url in media_urls[:120]:
        if not isinstance(raw_url, str):
            continue
        cleaned = raw_url.strip()
        if not cleaned.startswith(("http://", "https://")):
            continue
        escaped = html_escape(cleaned, quote=True)
        low_path = urlsplit(cleaned).path.lower()
        if low_path.endswith((".mp4", ".webm", ".mov")):
            tags.append(f'<video src="{escaped}"></video>')
        else:
            tags.append(f'<img src="{escaped}" alt="" />')
    if not tags:
        return html_text
    block = '<div data-pgm-computed-media="1" style="display:none">' + "".join(tags) + "</div>"
    low = html_text.lower()
    idx = low.rfind("</body>")
    if idx != -1:
        return html_text[:idx] + block + html_text[idx:]
    return html_text + block


def _looks_like_challenge_dom(html_text: str) -> bool:
    low = html_text.lower()
    return any(marker in low for marker in _CHALLENGE_DOM_MARKERS)


def render_page_html(
    url: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    resolver: Any = None,
    connector: Any = None,
    robots_check: Callable[[str], bool] | None = None,
    site_origins: Iterable[tuple[str, str, int]] | None = None,
    session_factory: Callable[[], Any] | None = None,
    allow_private: bool = False,
) -> bytes:
    """Render a permitted public webpage with bounded, IP-pinned headless Chromium."""
    verify_browser_url(url, resolver=resolver, allow_private=allow_private)
    if _is_login_or_challenge_path(url):
        raise PermissionError("login_or_challenge_url")
    if robots_check is not None and not robots_check(url):
        raise PermissionError("robots_disallowed")

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BrowserRendererUnavailable("Playwright is not installed in this worker image") from exc

    safe_timeout = max(3_000, min(int(timeout_ms), 25_000))
    safe_scrolls = max(0, min(int(max_scrolls), 4))
    safe_max_bytes = max(65_536, min(int(max_html_bytes), 10_000_000))
    resolved_origins = (
        {(str(o[0]).lower(), str(o[1]).lower().rstrip("."), int(o[2])) for o in site_origins}
        if site_origins is not None
        else _canonical_site_origins(url)
    )
    deadline = time.monotonic() + (safe_timeout / 1000.0) + 6.0

    route_state: dict[str, Any] = {}
    acquired = _BROWSER_SEMAPHORE.acquire(timeout=max(5.0, safe_timeout / 1000.0))
    if not acquired:
        raise BrowserRenderError("Browser renderer busy; concurrency limit reached")

    deny_proxy = _FailClosedProxy()
    deny_proxy.start()
    created_session = None
    try:
        if session_factory is not None:
            created_session = session_factory()
        else:
            created_session = build_collector_session(
                allow_private=allow_private,
                resolver=resolver,
                connector=connector,
                user_agent=USER_AGENT,
            )
        route_handler = make_ssrf_route_handler(
            site_url=url,
            site_origins=resolved_origins,
            session=created_session,
            resolver=resolver,
            connector=connector,
            robots_check=robots_check,
            allow_private=allow_private,
            deadline=deadline,
            state=route_state,
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-extensions",
                    "--disable-sync",
                    "--disable-translate",
                    "--metrics-recording-only",
                    "--mute-audio",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    f"--proxy-server={deny_proxy.server_url}",
                    "--proxy-bypass-list=<-loopback>",
                ],
            )
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                    java_script_enabled=True,
                    ignore_https_errors=False,
                    service_workers="block",
                )
                try:
                    context.route("**/*", route_handler)
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=safe_timeout)
                    # Wait briefly for JS hydration / XHR bursts without hanging on long-lived timers.
                    try:
                        page.wait_for_load_state("networkidle", timeout=min(3_500, safe_timeout))
                    except Exception:
                        pass
                    page.wait_for_timeout(800)
                    for step in range(safe_scrolls):
                        fraction = (step + 1) / max(1, safe_scrolls)
                        page.evaluate(
                            f"window.scrollTo(0, Math.floor(document.body.scrollHeight * {fraction:.2f}))"
                        )
                        page.wait_for_timeout(450)

                    # Post-navigation compliance verification (catches client-side JS redirects).
                    final_page_url = str(page.url or "")
                    if not final_page_url.startswith(("http://", "https://")):
                        raise PermissionError("invalid_post_navigation_url")
                    if _origin_tuple(final_page_url) not in resolved_origins:
                        raise PermissionError("offsite_post_navigation_redirect")
                    if _is_login_or_challenge_path(final_page_url):
                        raise PermissionError("login_or_challenge_redirect")
                    if robots_check is not None and not robots_check(final_page_url):
                        raise PermissionError("robots_disallowed_post_navigation")

                    rendered = page.content()
                    if _looks_like_challenge_dom(rendered):
                        raise PermissionError("challenge_detected")

                    try:
                        computed_urls = page.evaluate(_COMPUTED_MEDIA_JS)
                        if isinstance(computed_urls, list):
                            rendered = _append_computed_media_hints(rendered, computed_urls)
                    except Exception:
                        # Computed style extraction is best-effort; preserve rendered DOM HTML.
                        pass
                finally:
                    context.close()
            finally:
                browser.close()
    except (BrowserRendererUnavailable, PermissionError):
        raise
    except Exception as exc:
        if route_state.get("document_error"):
            raise PermissionError(str(route_state["document_error"])) from exc
        msg = str(exc).lower()
        if "executable doesn't exist" in msg or "playwright install" in msg:
            raise BrowserRendererUnavailable("Chromium browser binary is not installed") from exc
        raise BrowserRenderError(f"Headless browser render failed: {type(exc).__name__}") from exc
    finally:
        if created_session is not None:
            try:
                created_session.close()
            except Exception:
                pass
        deny_proxy.close()
        _BROWSER_SEMAPHORE.release()

    if route_state.get("document_error"):
        raise PermissionError(str(route_state["document_error"]))

    raw = rendered.encode("utf-8", errors="replace")
    if len(raw) > safe_max_bytes:
        raise ValueError("Rendered DOM exceeded max_html_bytes cap")
    return raw
