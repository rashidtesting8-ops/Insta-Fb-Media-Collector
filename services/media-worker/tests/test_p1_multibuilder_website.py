"""Milestone P1 offline tests: multi-builder website compatibility (Wix, Shopify,
Squarespace, Webflow, WordPress), default sitemap probing, core salon navigation discovery,
third-party video embed diagnostics, SSRF-hardened Playwright JS fallback, and multi-batch
website checkpoint resume.

100% offline — live sockets are hard-blocked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace
import uuid

from PIL import Image, ImageDraw
import pytest
import requests

from app import public_collection
from app.browser_renderer import (
    BrowserRendererUnavailable,
    looks_like_js_rendered_page,
    make_ssrf_route_handler,
    verify_browser_url,
)
from app.legacy_collector import build_collector_session
from app.public_collection import PublicHTTP, build_multitenant_collection_config, collect_public_job
from app.publication import frozen_window
from tests.test_phase9_security import request, worker  # noqa: F401


@pytest.fixture(autouse=True)
def block_live_sockets(monkeypatch):
    def deny(*_args, **_kwargs):
        pytest.fail("P1 offline test attempted a live socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny)


def _jpeg_bytes(w: int = 360, h: int = 280, color: tuple[int, int, int] = (170, 120, 80)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([16, 16, w - 16, h - 16], outline=(35, 55, 75), width=8)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _mp4_bytes(tmp_path: Path) -> bytes:
    target = tmp_path / f"p1-{uuid.uuid4().hex[:8]}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1.0:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-t", "1.0", str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return target.read_bytes()


def _mock_http_get(responses: dict[str, tuple[int, dict, bytes | str]]):
    def fake_get(session, url, **kwargs):
        assert not session.cookies and not session.trust_env and kwargs.get("allow_redirects") is False
        assert url in responses, f"Unexpected HTTP fetch: {url}"
        status, headers, body = responses[url]
        resp = requests.Response()
        resp.status_code = status
        resp.url = url
        resp.headers.update(headers)
        resp._content = body.encode("utf-8") if isinstance(body, str) else body
        resp._content_consumed = True
        return resp

    return fake_get


def test_p1_builder_cdn_allowlist_covers_squarespace_shopify_wix_webflow_and_godaddy():
    cfg = build_multitenant_collection_config({
        "website_url": "https://luxury-paws.example.test/",
        "facebook_url": "https://www.facebook.com/luxurypaws/",
        "social_permission_confirmed": False,
    })
    with build_collector_session() as session:
        http = PublicHTTP(cfg["sources"]["website"]["url"], cfg["sources"]["website"], session)
        for allowed_url in (
            "https://static.wixstatic.com/media/abc~mv2.jpg",
            "https://video.wixstatic.com/video/abc/1080p/mp4/file.mp4",
            "https://images-wixmp-123.wixmp.com/f/groom.jpg",
            "https://images.squarespace-cdn.com/content/v1/dog.jpg",
            "https://static1.squarespace.com/static/123/t/456/poodle.jpg",
            "https://cdn.shopify.com/s/files/1/0123/groom_1600x.jpg",
            "https://cdn.shopifycdn.net/s/files/1/0123/spa.jpg",
            "https://cdn.prod.website-files.com/65ab/hero.mp4",
            "https://img1.wsimg.com/isteam/ip/salon.jpg",
            "https://media.godaddysites.com/salon/dog.jpg",
        ):
            assert http.media_permitted(allowed_url) is True, allowed_url
        for blocked_url in (
            "http://static1.squarespace.com/static/123/dog.jpg",
            "https://static1.squarespace.com:8443/static/123/dog.jpg",
            "https://evilsquarespace.com/dog.jpg",
            "https://squarespace.com/dog.jpg",
            "https://untrusted-tracker.example.net/pixel.jpg",
        ):
            assert http.media_permitted(blocked_url) is False, blocked_url


def test_p1_multibuilder_wix_shopify_squarespace_webflow_static_and_json_extraction(tmp_path, monkeypatch):
    """Verify Wix JSON warmup, Shopify {width} templates, Squarespace <noscript>+static1,
    Webflow data-video-urls, salon nav pages (/services, /our-team), and YouTube embed diagnostics."""
    web = "https://multi-builder-salon.example.test"
    fb = "https://www.facebook.com/multibuildersalon/"
    window = frozen_window(datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC))

    jpg_wix = _jpeg_bytes(420, 320, (210, 140, 90))
    jpg_shop = _jpeg_bytes(400, 300, (90, 180, 140))
    jpg_sq = _jpeg_bytes(380, 280, (120, 100, 200))
    mp4_wf = _mp4_bytes(tmp_path)

    wix_img = "https://static.wixstatic.com/media/salon_hero~mv2.jpg"
    shop_img_tpl = "//cdn.shopify.com/s/files/1/0099/poodle_{width}x.jpg"
    shop_img_resolved = "https://cdn.shopify.com/s/files/1/0099/poodle_1600x.jpg"
    sq_img = "https://static1.squarespace.com/static/55/t/99/groomed.jpg"
    wf_vid = "https://cdn.prod.website-files.com/65aa/salon-tour.mp4"

    home_html = (
        f'<html><head>'
        f'<script type="application/json" id="wix-warmup-data">{{"hero":"{wix_img}"}}</script>'
        f'</head><body>'
        f'<nav><a href="/services">Services</a><a href="/our-team">Our Team</a></nav>'
        f'<img data-src="{shop_img_tpl}" alt="Shopify poodle">'
        f'<div class="w-background-video" data-video-urls="{wf_vid}"></div>'
        f'<iframe src="https://www.youtube.com/embed/abc123"></iframe>'
        f'</body></html>'
    )
    services_html = (
        f'<html><body>'
        f'<noscript><img src="{sq_img}" alt="Squarespace groom"></noscript>'
        f'</body></html>'
    )
    team_html = "<html><body><h1>Meet our groomers</h1></body></html>"

    responses = {
        f"{web}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        # Speculative /sitemap.xml returns 403 -> must NOT poison/hold the main website origin!
        f"{web}/sitemap.xml": (403, {}, "Forbidden"),
        f"{web}/sitemap_index.xml": (404, {}, "Not Found"),
        f"{web}/": (200, {"Content-Type": "text/html"}, home_html),
        f"{web}/services": (200, {"Content-Type": "text/html"}, services_html),
        f"{web}/our-team": (200, {"Content-Type": "text/html"}, team_html),
        "https://static.wixstatic.com/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        wix_img: (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(jpg_wix))}, jpg_wix),
        "https://cdn.shopify.com/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        shop_img_resolved: (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(jpg_shop))}, jpg_shop),
        "https://static1.squarespace.com/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        sq_img: (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(jpg_sq))}, jpg_sq),
        "https://cdn.prod.website-files.com/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        wf_vid: (200, {"Content-Type": "video/mp4", "Content-Length": str(len(mp4_wf))}, mp4_wf),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses))
    monkeypatch.setenv("WEBSITE_PROBE_DEFAULT_SITEMAPS", "1")

    job_dir = tmp_path / ("a" * 32)
    job_dir.mkdir()
    job = {
        "job_id": job_dir.name,
        "website_url": f"{web}/",
        "facebook_url": fb,
        "social_permission_confirmed": False,
        "max_pages": 6,
        "max_media": 10,
        **window,
    }
    cfg = build_multitenant_collection_config(job)
    assert cfg["sources"]["website"]["probe_default_sitemaps"] is True

    manifest = collect_public_job(job_dir, job, cfg, run_preprocessing=True)
    web_source = manifest["sources"]["website"]
    visited_pages = {item["url"] for item in web_source["items"]}
    assert {f"{web}/", f"{web}/services", f"{web}/our-team"} <= visited_pages
    assert "third_party_video_embed" in web_source["issues"]

    by_url = {a.get("source_url"): a for a in manifest["assets"] if a.get("type") != "frame"}
    for expected in (wix_img, shop_img_resolved, sq_img, wf_vid):
        assert expected in by_url, f"Missing builder asset {expected}"
        assert by_url[expected]["extraction_status"] == "downloaded"
    assert by_url[wf_vid]["type"] == "video"
    assert any(a.get("type") == "frame" for a in manifest["assets"])


def test_p1_default_sitemap_xml_probe_discovers_pages_when_robots_omits_sitemap(tmp_path, monkeypatch):
    web = "https://sq-sitemap-salon.example.test"
    fb = "https://www.facebook.com/sqsitemap/"
    window = frozen_window(datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC))
    jpg = _jpeg_bytes(320, 240, (90, 150, 210))
    photo = "https://images.squarespace-cdn.com/content/v1/hidden-groom.jpg"

    responses = {
        f"{web}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{web}/sitemap.xml": (
            200,
            {"Content-Type": "application/xml"},
            f"<urlset><url><loc>{web}/unlinked-showcase</loc></url></urlset>",
        ),
        f"{web}/": (200, {"Content-Type": "text/html"}, "<html><body><h1>Salon</h1></body></html>"),
        f"{web}/unlinked-showcase": (
            200,
            {"Content-Type": "text/html"},
            f'<html><body><img data-image-src="{photo}"></body></html>',
        ),
        "https://images.squarespace-cdn.com/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        photo: (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(jpg))}, jpg),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses))
    monkeypatch.setenv("WEBSITE_PROBE_DEFAULT_SITEMAPS", "1")

    job_dir = tmp_path / ("b" * 32)
    job_dir.mkdir()
    job = {
        "job_id": job_dir.name,
        "website_url": f"{web}/",
        "facebook_url": fb,
        "social_permission_confirmed": False,
        "max_pages": 5,
        "max_media": 5,
        **window,
    }
    manifest = collect_public_job(job_dir, job, build_multitenant_collection_config(job))
    by_url = {a.get("source_url"): a for a in manifest["assets"]}
    assert photo in by_url and by_url[photo]["extraction_status"] == "downloaded"


def test_p1_browser_renderer_ssrf_guard_blocks_private_ips_schemes_ports_and_heavy_types():
    def fake_resolver(host: str, port: int):
        mapping = {
            "public-salon.example.test": "93.184.216.34",
            "rebind-loopback.example.test": "127.0.0.1",
            "rebind-rfc1918.example.test": "10.0.0.42",
            "rebind-metadata.example.test": "169.254.169.254",
            "rebind-v6-loopback.example.test": "::1",
        }
        ip = mapping[host]
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]

    assert verify_browser_url("https://public-salon.example.test/gallery", resolver=fake_resolver) == ["93.184.216.34"]
    for bad in (
        "https://rebind-loopback.example.test/",
        "https://rebind-rfc1918.example.test/",
        "https://rebind-metadata.example.test/latest/meta-data/",
        "https://rebind-v6-loopback.example.test/",
        "https://public-salon.example.test:8443/gallery",
        "https://user:pass@public-salon.example.test/",
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
    ):
        with pytest.raises(ValueError):
            verify_browser_url(bad, resolver=fake_resolver)

    handler = make_ssrf_route_handler(resolver=fake_resolver)

    def run_route(url: str, resource_type: str = "document") -> str:
        outcome = []
        route = SimpleNamespace(
            request=SimpleNamespace(url=url, resource_type=resource_type),
            continue_=lambda: outcome.append("continue"),
            abort=lambda: outcome.append("abort"),
        )
        handler(route)
        return outcome[0]

    assert run_route("https://public-salon.example.test/app.js", "script") == "continue"
    assert run_route("https://public-salon.example.test/font.woff2", "font") == "abort"
    assert run_route("https://public-salon.example.test/hero.mp4", "media") == "abort"
    assert run_route("https://rebind-metadata.example.test/token", "xhr") == "abort"
    assert run_route("file:///etc/hosts", "document") == "abort"


def test_p1_playwright_js_fallback_recovers_spa_gallery_and_degrades_gracefully(tmp_path, monkeypatch):
    web = "https://spa-groomers.example.test"
    fb = "https://www.facebook.com/spagroomers/"
    window = frozen_window(datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC))
    jpg = _jpeg_bytes(440, 320, (195, 130, 70))
    rendered_img = "https://static.wixstatic.com/media/hydrated_spaniel~mv2.jpg"

    spa_shell = '<html><body><div id="root"></div><script src="/bundle.js"></script></body></html>'
    assert looks_like_js_rendered_page(spa_shell, 0) is True
    assert looks_like_js_rendered_page(spa_shell, 2) is False

    responses = {
        f"{web}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{web}/": (200, {"Content-Type": "text/html"}, spa_shell),
        "https://static.wixstatic.com/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        rendered_img: (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(jpg))}, jpg),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses))

    # 1. With working JS renderer -> discovers & downloads hydrated image through PublicHTTP.
    render_calls: list[str] = []

    def fake_renderer(url: str) -> bytes:
        render_calls.append(url)
        return f'<html><body><div id="root"><img src="{rendered_img}"></div></body></html>'.encode("utf-8")

    job_dir = tmp_path / ("c" * 32)
    job_dir.mkdir()
    job = {
        "job_id": job_dir.name,
        "website_url": f"{web}/",
        "facebook_url": fb,
        "social_permission_confirmed": False,
        "max_pages": 3,
        "max_media": 5,
        **window,
    }
    manifest = collect_public_job(job_dir, job, build_multitenant_collection_config(job), js_renderer=fake_renderer)
    assert render_calls == [f"{web}/"]
    assert "js_rendered_fallback" in manifest["sources"]["website"]["issues"]
    by_url = {a.get("source_url"): a for a in manifest["assets"]}
    assert rendered_img in by_url and by_url[rendered_img]["extraction_status"] == "downloaded"

    # 2. When Playwright is not installed -> degrades gracefully with js_renderer_unavailable.
    job_dir2 = tmp_path / ("d" * 32)
    job_dir2.mkdir()
    job2 = {**job, "job_id": job_dir2.name}

    def missing_renderer(_url: str) -> bytes:
        raise BrowserRendererUnavailable("Playwright not installed")

    manifest2 = collect_public_job(job_dir2, job2, build_multitenant_collection_config(job2), js_renderer=missing_renderer)
    assert "js_renderer_unavailable" in manifest2["sources"]["website"]["issues"]


def test_p1_multibatch_website_resume_continues_checkpoint_across_pages(worker, tmp_path, monkeypatch):
    """Verify website pagination checkpoint resumes cleanly via POST /jobs/{id}/resume-collection."""
    web = "https://resume-web-salon.example.test"
    fb = "https://www.facebook.com/resumewebsalon/"
    window = frozen_window(datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC))
    jpg1 = _jpeg_bytes(320, 240, (200, 100, 80))
    jpg2 = _jpeg_bytes(320, 240, (80, 160, 200))

    responses = {
        f"{web}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{web}/": (200, {"Content-Type": "text/html"}, f'<html><body><a href="/gallery">Gallery</a><img src="{web}/p1.jpg"></body></html>'),
        f"{web}/gallery": (200, {"Content-Type": "text/html"}, f'<html><body><img src="{web}/p2.jpg"></body></html>'),
        f"{web}/p1.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(jpg1))}, jpg1),
        f"{web}/p2.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(jpg2))}, jpg2),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses))
    monkeypatch.setenv("COLLECTION_ENGINE", "historical")
    monkeypatch.delenv("WEBSITE_PROBE_DEFAULT_SITEMAPS", raising=False)
    monkeypatch.delenv("PUBLIC_COLLECTION_CONFIG", raising=False)
    monkeypatch.setattr(worker, "frozen_window", lambda _: window)

    scheduled: list[tuple] = []
    monkeypatch.setattr(worker.executor, "submit", lambda *args: scheduled.append(args))

    # Batch 1: max_pages=1 -> crawls / and downloads p1.jpg, saves /gallery in checkpoint cursor.
    st, _, raw = request(worker.app, "/jobs", "POST", "test-admin", (json.dumps({
        "website_url": f"{web}/",
        "facebook_url": fb,
        "social_permission_confirmed": False,
        "max_pages": 1,
        "max_media": 5,
    }).encode(),))
    assert st == 202, raw
    job_id = json.loads(raw)["job_id"]
    fn, *args = scheduled.pop()
    fn(*args)

    m1 = json.loads((worker.DATA_DIR / job_id / "manifest.json").read_text(encoding="utf-8"))
    urls1 = {a.get("source_url") for a in m1["assets"]}
    assert f"{web}/p1.jpg" in urls1 and f"{web}/p2.jpg" not in urls1
    assert m1["sources"]["website"]["cursor"] is not None

    # Batch 2: POST /jobs/{id}/resume-collection -> resumes from saved cursor and collects /gallery + p2.jpg!
    st2, _, raw2 = request(worker.app, f"/jobs/{job_id}/resume-collection", "POST", "test-admin", (b"{}",))
    assert st2 == 202, raw2
    fn2, *args2 = scheduled.pop()
    fn2(*args2)

    m2 = json.loads((worker.DATA_DIR / job_id / "manifest.json").read_text(encoding="utf-8"))
    urls2 = {a.get("source_url") for a in m2["assets"]}
    assert {f"{web}/p1.jpg", f"{web}/p2.jpg"} <= urls2
    assert m2["window_start_utc"] == m1["window_start_utc"]
    assert m2["window_end_utc"] == m1["window_end_utc"]
