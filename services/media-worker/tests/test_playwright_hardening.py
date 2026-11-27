"""Audit H-3 & Milestone 2: Offline loopback tests for IP-pinned Playwright transport,
DNS-rebinding TOCTOU prevention, fail-closed deny proxy, and real Chromium SPA hydration.

- Never connects to any external host (strictly 127.0.0.1 loopback fixtures).
- Tests marked with Playwright execute automatically inside Playwright-enabled worker images
  and skip cleanly in lean CI images without Playwright installed.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import socket
import threading
from types import SimpleNamespace

import pytest

from app.browser_renderer import (
    _FailClosedProxy,
    make_ssrf_route_handler,
    playwright_installed,
    render_page_html,
)
from app.legacy_collector import build_collector_session


def test_fail_closed_proxy_rejects_every_connection_with_403():
    proxy = _FailClosedProxy()
    proxy.start()
    try:
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=2.0) as sock:
            sock.sendall(b"CONNECT 169.254.169.254:80 HTTP/1.1\r\nHost: 169.254.169.254:80\r\n\r\n")
            resp = sock.recv(256)
        assert resp.startswith(b"HTTP/1.1 403 Forbidden")
        assert proxy.blocked_connections >= 1
    finally:
        proxy.close()


def test_dns_rebinding_toctou_prevented_by_single_ip_pinned_dial():
    """Audit H-3: SafeHTTPAdapter resolves DNS ONCE per connection and dials that verified IP.
    If DNS subsequently rebinds to 127.0.0.1, the next request is blocked BEFORE connecting."""
    calls: list[str] = []
    dialed: list[tuple[str, int]] = []

    def rebinding_resolver(host: str, port: int):
        calls.append(host)
        ip = "93.184.216.34" if len(calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def fake_connector(addr: tuple[str, int], timeout, source_address):
        dialed.append(addr)
        raise OSError("synthetic-stop-after-verified-dial")

    session = build_collector_session(
        allow_private=False,
        resolver=rebinding_resolver,
        connector=fake_connector,
    )
    try:
        handler = make_ssrf_route_handler(
            site_url="http://rebind-salon.example.test/",
            session=session,
            resolver=rebinding_resolver,
            connector=fake_connector,
            allow_private=False,
        )

        def call(url: str) -> str:
            out: list[str] = []
            route = SimpleNamespace(
                request=SimpleNamespace(url=url, resource_type="document", method="GET"),
                continue_=lambda: pytest.fail("route.continue_() must never be called"),
                fulfill=lambda **_kw: out.append("fulfill"),
                abort=lambda: out.append("abort"),
            )
            handler(route)
            return out[0]

        # Call #1 resolves ONCE to 93.184.216.34 and dials that exact verified IP.
        assert call("http://rebind-salon.example.test/") == "abort"
        assert calls == ["rebind-salon.example.test"]
        assert dialed == [("93.184.216.34", 80)]

        # Call #2 rebinds to 127.0.0.1 -> rejected BEFORE fake_connector is ever invoked!
        assert call("http://rebind-salon.example.test/") == "abort"
        assert len(calls) == 2
        assert dialed == [("93.184.216.34", 80)]
    finally:
        session.close()


@pytest.mark.skipif(not playwright_installed(), reason="Playwright not installed in lean worker image")
def test_real_chromium_hydrates_spa_and_enforces_post_navigation_robots_and_method_blocks():
    """End-to-end real headless Chromium test against a local loopback fixture server:
    1. Hydrates a React-like SPA (<img> + CSS computed background-image) via SafeHTTPAdapter.
    2. Blocks POST requests & enforces read-only rendering.
    3. Blocks client-side JS redirect to a robots.txt-disallowed path."""
    post_hits: list[str] = []

    class _FixtureHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            pass

        def do_POST(self):
            post_hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            if self.path == "/":
                body = (
                    b'<!doctype html><html><head><style>'
                    b'.hero-bg { width: 300px; height: 200px; background-image: url("/bg-poodle.jpg"); }'
                    b'</style></head><body>'
                    b'<div id="root"></div>'
                    b'<script src="/bundle.js"></script>'
                    b'</body></html>'
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Set-Cookie", "leak=forbidden")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/bundle.js":
                body = (
                    b'document.getElementById("root").innerHTML = '
                    b'\'<img src="/hydrated-dog.jpg" alt="Hydrated"><div class="hero-bg"></div>\';'
                    b'fetch("/forbidden-mutation", { method: "POST", body: "x" }).catch(() => {});'
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/js-redirect-private":
                body = b'<html><body><script>window.location.replace("/private-room");</script></body></html>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/private-room":
                body = b"<html><body>Private</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), _FixtureHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        rendered = render_page_html(
            f"{base}/",
            timeout_ms=10_000,
            max_scrolls=1,
            robots_check=lambda u: "/private-room" not in u,
            allow_private=True,
        ).decode("utf-8", errors="replace")

        assert "/hydrated-dog.jpg" in rendered
        assert f"{base}/bg-poodle.jpg" in rendered
        assert post_hits == []

        # Client-side JS redirect to robots-disallowed path must be blocked.
        with pytest.raises(PermissionError):
            render_page_html(
                f"{base}/js-redirect-private",
                timeout_ms=8_000,
                max_scrolls=0,
                robots_check=lambda u: "/private-room" not in u,
                allow_private=True,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
