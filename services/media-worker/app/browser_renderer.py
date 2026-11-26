"""Optional bounded, SSRF-hardened Playwright JS fallback renderer for website discovery.

Design & compliance invariants:
- Strictly opt-in (triggered ONLY on permitted public website pages in historical mode when
  static HTML extraction finds fewer than 2 media candidates and JS/SPA signals are present).
- Never bypasses login walls, CAPTCHAs, paywalls, or robots.txt. Checks robots_check(url)
  BEFORE launching a browser.
- Every network request made by the headless browser passes through `verify_browser_url`,
  which enforces HTTP(S), ports 80/443, credential-free URLs, and validates ALL DNS-resolved
  IPs against private/loopback/link-local/metadata ranges.
- Blocks heavy binary/media/font downloads inside the browser; Chromium returns rendered DOM
  HTML only (capped at 5 MB). All actual media downloads still go through `PublicHTTP`
  (SafeHTTPAdapter + robots.txt + vetted builder CDN allowlist + magic-byte validation).
- Degrades gracefully with `BrowserRendererUnavailable` when Playwright/Chromium is not
  installed in the container image.
"""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlparse

from app.legacy_collector import (
    USER_AGENT,
    _default_resolver,
    safe_url,
    validate_ip_address,
)

DEFAULT_TIMEOUT_MS = 15_000
DEFAULT_MAX_SCROLLS = 2
DEFAULT_MAX_HTML_BYTES = 5_000_000

_BLOCKED_RESOURCE_TYPES = frozenset({"font", "media", "websocket", "eventsource"})
_JS_RENDER_SIGNALS = (
    "wix-thunderbolt",
    "wix-warmup-data",
    "id=\"root\"",
    "id='root'",
    "id=\"__next\"",
    "id='__next'",
    "id=\"app\"",
    "id='app'",
    "data-reactroot",
    "enable javascript",
    "requires javascript",
    "javascript is required",
    "squarespace-config",
    "static.squarespace.com",
    "webflow.js",
    "shopify-section",
)
_SCRIPT_TAG_RE = re.compile(r"<script\b", re.IGNORECASE)


class BrowserRendererUnavailable(RuntimeError):
    """Raised when Playwright or Chromium is not installed in the runtime image."""


def verify_browser_url(
    url: str,
    *,
    resolver: Any = None,
    allow_private: bool = False,
) -> list[str]:
    """Validate URL syntax, port, and verify every resolved DNS IP is public."""
    safe_url(url, allow_private=allow_private)
    parsed = urlparse(url)
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


def looks_like_js_rendered_page(html_body: bytes | str, static_media_count: int) -> bool:
    """Return True only when static HTML yielded <2 media candidates and shows JS/SPA signals."""
    if static_media_count >= 2:
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


def make_ssrf_route_handler(
    *,
    resolver: Any = None,
    allow_private: bool = False,
) -> Callable[[Any], None]:
    """Return a Playwright `page.route` callback enforcing SSRF and resource-type hygiene."""

    def _handle_route(route: Any) -> None:
        try:
            req = route.request
            res_type = str(getattr(req, "resource_type", "") or "").lower()
            if res_type in _BLOCKED_RESOURCE_TYPES:
                route.abort()
                return
            req_url = str(getattr(req, "url", "") or "")
            if not req_url.startswith(("http://", "https://")):
                route.abort()
                return
            verify_browser_url(req_url, resolver=resolver, allow_private=allow_private)
            route.continue_()
        except Exception:
            route.abort()

    return _handle_route


def render_page_html(
    url: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    resolver: Any = None,
    robots_check: Callable[[str], bool] | None = None,
    allow_private: bool = False,
) -> bytes:
    """Render a permitted public webpage with bounded headless Chromium and return DOM HTML bytes."""
    verify_browser_url(url, resolver=resolver, allow_private=allow_private)
    if robots_check is not None and not robots_check(url):
        raise PermissionError("robots_disallowed")

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BrowserRendererUnavailable("Playwright is not installed in this worker image") from exc

    safe_timeout = max(3_000, min(int(timeout_ms), 30_000))
    safe_scrolls = max(0, min(int(max_scrolls), 4))
    safe_max_bytes = max(65_536, min(int(max_html_bytes), 10_000_000))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-first-run",
                    "--disable-background-networking",
                ],
            )
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                    java_script_enabled=True,
                    ignore_https_errors=False,
                )
                try:
                    page = context.new_page()
                    page.route("**/*", make_ssrf_route_handler(resolver=resolver, allow_private=allow_private))
                    page.goto(url, wait_until="domcontentloaded", timeout=safe_timeout)
                    page.wait_for_timeout(1200)
                    for _ in range(safe_scrolls):
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(400)
                    rendered = page.content()
                finally:
                    context.close()
            finally:
                browser.close()
    except BrowserRendererUnavailable:
        raise
    except Exception as exc:
        msg = str(exc).lower()
        if "executable doesn't exist" in msg or "playwright install" in msg:
            raise BrowserRendererUnavailable("Chromium browser binary is not installed") from exc
        raise

    raw = rendered.encode("utf-8", errors="replace")
    if len(raw) > safe_max_bytes:
        raise ValueError("Rendered DOM exceeded max_html_bytes cap")
    return raw
