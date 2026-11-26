"""Sprint 2 tests: multi-tenant Website+Facebook historical collector, optional Instagram,
checkpoint resume endpoint, unified M4 rights ledger, and real SafeHTTPAdapter/dial_verified_socket SSRF tests.

100% offline — zero live sockets, zero external network calls.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import uuid

from PIL import Image, ImageDraw
import pytest
import requests

from app import collection_approval as ledger
from app import delivery, named_auth, public_collection
from app.ai.provider import GROQ_ACCEPTANCE_ENDPOINT, OpenAICompatibleProvider
from app.legacy_collector import (
    SafeHTTPAdapter,
    build_collector_session,
    dial_verified_socket,
    safe_url,
    validate_ip_address,
)
from app.public_collection import PublicHTTP, build_multitenant_collection_config
from app.publication import frozen_window, publication_evidence
from tests.test_m4_real_pipeline import (
    FAKE_GROQ_KEY,
    _build_fake_groq_transport,
    m4_env,  # noqa: F401
)
from tests.test_phase9_security import request, worker  # noqa: F401


@pytest.fixture(autouse=True)
def block_live_sockets(monkeypatch):
    def deny(*_args, **_kwargs):
        pytest.fail("Sprint 2 offline test attempted a live socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny)


def _jpeg_bytes(width: int = 480, height: int = 360, color: tuple[int, int, int] = (180, 110, 60)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, width - 20, height - 20], outline=(30, 50, 80), width=10)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _mp4_bytes(tmp_path: Path) -> bytes:
    target = tmp_path / f"s2-{uuid.uuid4().hex[:8]}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1.2:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-t", "1.2", str(target),
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


def test_multitenant_website_and_facebook_without_instagram_or_static_config(worker, tmp_path, monkeypatch):
    """Any arbitrary business submits Website + Facebook (no Instagram, no PUBLIC_COLLECTION_CONFIG)."""
    web = "https://paws-and-suds.example.test"
    fb = "https://www.facebook.com/pawsandsuds/"
    fb_cdn = "https://scontent-lhr8-1.xx.fbcdn.net"
    window = frozen_window(datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC))

    web_jpg = _jpeg_bytes(640, 480, (200, 120, 70))
    fb_jpg = _jpeg_bytes(540, 540, (60, 140, 120))
    web_mp4 = _mp4_bytes(tmp_path)

    ld_json = json.dumps([
        {"@type": "ImageObject", "contentUrl": f"{web}/wp-content/uploads/hero.jpg", "datePublished": "2026-10-15T10:00:00Z"},
        {"@type": "VideoObject", "contentUrl": f"{web}/wp-content/uploads/clip.mp4", "datePublished": "2026-10-18T12:00:00Z"},
    ])
    home_html = (
        f'<html><head><script type="application/ld+json">{ld_json}</script></head><body>'
        f'<img src="{web}/wp-content/uploads/hero.jpg">'
        f'<img src="https://evil-cdn.example.net/offscope.jpg">'
        f'<video><source src="{web}/wp-content/uploads/clip.mp4"></video>'
        f"</body></html>"
    )
    responses = {
        f"{web}/robots.txt": (200, {}, f"User-agent: *\nAllow: /\nSitemap: {web}/sitemap.xml\n"),
        f"{web}/sitemap.xml": (200, {}, f"<urlset><url><loc>{web}/</loc></url></urlset>"),
        f"{web}/": (200, {"Content-Type": "text/html"}, home_html),
        f"{web}/wp-content/uploads/hero.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(web_jpg))}, web_jpg),
        f"{web}/wp-content/uploads/clip.mp4": (200, {"Content-Type": "video/mp4", "Content-Length": str(len(web_mp4))}, web_mp4),
        f"{fb_cdn}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{fb_cdn}/v/t39.30808-6/fb-photo-1.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(fb_jpg))}, fb_jpg),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses))

    cli_calls: list[list[str]] = []

    def fake_social_cli(cmd: list[str], _stage: Path) -> tuple[int, str]:
        cli_calls.append(list(cmd))
        assert "instagram" not in cmd, "Parked/omitted Instagram must never invoke CLI"
        if "photos_albums" in cmd[-1]:
            return 0, json.dumps([])
        return 0, json.dumps([[3, f"{fb_cdn}/v/t39.30808-6/fb-photo-1.jpg", {
            "category": "facebook",
            "subcategory": "photos",
            "id": "122230001",
            "set_id": "pb.99999.-2207520000",
            "caption": "Fresh spaniel trim",
            "date": "2026-10-21 09:15:00",
            "extension": "jpg",
        }]])

    monkeypatch.setenv("COLLECTION_ENGINE", "historical")
    monkeypatch.delenv("PUBLIC_COLLECTION_CHECKPOINTS", raising=False)
    monkeypatch.delenv("PUBLIC_COLLECTION_CONFIG", raising=False)
    monkeypatch.setattr(worker, "frozen_window", lambda _: window)
    real_collect = public_collection.collect_public_job
    monkeypatch.setattr(
        public_collection,
        "collect_public_job",
        lambda job_dir, job, config, **kw: real_collect(job_dir, job, config, social_runner=fake_social_cli),
    )

    scheduled: list[tuple] = []
    monkeypatch.setattr(worker.executor, "submit", lambda *args: scheduled.append(args))

    # Submit Website + Facebook ONLY (no instagram_url at all!).
    st, _, raw = request(worker.app, "/jobs", "POST", "test-admin", (json.dumps({
        "website_url": f"{web}/",
        "facebook_url": fb,
        "social_permission_confirmed": True,
        "max_pages": 5,
        "max_media": 10,
        "max_social_files": 10,
    }).encode(),))
    assert st == 202, raw
    job_id = json.loads(raw)["job_id"]
    fn, *args = scheduled.pop()
    fn(*args)

    rec = worker.fetch(job_id)
    assert rec["collection_mode"] == "public_checkpoint"
    assert rec["instagram_url"] is None
    assert rec["status"] == "partial_success"
    manifest = json.loads((worker.DATA_DIR / job_id / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["collection_profile"] == "historical"
    assert manifest["website_url"] == f"{web}/"
    assert manifest["sources"]["instagram"]["status"] == "not_requested"
    assert manifest["sources"]["instagram"]["coverage"] == "not_requested"

    by_url = {a.get("source_url"): a for a in manifest["assets"] if a.get("type") != "frame"}
    assert f"{web}/wp-content/uploads/hero.jpg" in by_url
    assert f"{web}/wp-content/uploads/clip.mp4" in by_url
    assert f"{fb_cdn}/v/t39.30808-6/fb-photo-1.jpg" in by_url
    assert "https://evil-cdn.example.net/offscope.jpg" not in by_url
    assert by_url[f"{fb_cdn}/v/t39.30808-6/fb-photo-1.jpg"]["extraction_status"] == "downloaded"
    assert by_url[f"{web}/wp-content/uploads/clip.mp4"]["preprocessing"]["outcome"] == "video_processed"
    # Derived FFmpeg frames exist from automatic preprocessing.
    assert any(a.get("type") == "frame" for a in manifest["assets"])
    assert cli_calls, "Expected Facebook CLI invocation"


def test_public_http_media_host_suffix_blocks_unapproved_and_insecure_origins():
    cfg = build_multitenant_collection_config({
        "website_url": "https://salon.example.test/",
        "facebook_url": "https://www.facebook.com/salon/",
        "social_permission_confirmed": True,
    })
    assert "instagram" not in cfg["sources"]
    assert cfg["sources"]["facebook"]["media_host_suffixes"] == [".fbcdn.net", ".fbsbx.com"]

    with build_collector_session() as session:
        http = PublicHTTP(cfg["sources"]["facebook"]["url"], cfg["sources"]["facebook"], session)
        assert http.media_permitted("https://scontent-lhr8-1.xx.fbcdn.net/photo.jpg") is True
        assert http.media_permitted("https://lookaside.fbsbx.com/photo.jpg") is True
        # Must reject HTTP (non-TLS), non-443 ports, spoofed suffixes, and bare suffix domains.
        assert http.media_permitted("http://scontent-lhr8-1.xx.fbcdn.net/photo.jpg") is False
        assert http.media_permitted("https://scontent-lhr8-1.xx.fbcdn.net:8443/photo.jpg") is False
        assert http.media_permitted("https://evilfbcdn.net/photo.jpg") is False
        assert http.media_permitted("https://fbcdn.net/photo.jpg") is False
        assert http.media_permitted("https://unapproved.example.net/photo.jpg") is False


def test_resume_collection_endpoint_continues_saved_facebook_checkpoint(worker, tmp_path, monkeypatch):
    web = "https://resume-salon.example.test"
    fb = "https://www.facebook.com/resumesalon/"
    fb_cdn = "https://scontent-man2-1.xx.fbcdn.net"
    window = frozen_window(datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC))

    jpg1 = _jpeg_bytes(400, 300, (120, 80, 200))
    jpg2 = _jpeg_bytes(400, 300, (80, 200, 120))
    responses = {
        f"{web}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{web}/": (200, {"Content-Type": "text/html"}, "<html><body>No media</body></html>"),
        f"{fb_cdn}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{fb_cdn}/p1.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(jpg1))}, jpg1),
        f"{fb_cdn}/p2.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(jpg2))}, jpg2),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses))

    seen_fb_urls: list[str] = []

    def fake_social_cli(cmd: list[str], _stage: Path) -> tuple[int, str]:
        target = cmd[-1]
        seen_fb_urls.append(target)
        if "photos_albums" in target:
            return 0, json.dumps([])
        if "fbid=1002" in target:
            return 0, json.dumps([[3, f"{fb_cdn}/p2.jpg", {
                "category": "facebook", "subcategory": "photos",
                "id": "1002", "set_id": "pb.77.-2207520000",
                "date": "2026-09-10 10:00:00", "extension": "jpg",
            }]])
        # Batch 1 emits a genuine photo + explicit next continuation URL via set_id + next photo cursor.
        return 0, json.dumps([
            [3, f"{fb_cdn}/p1.jpg", {
                "category": "facebook", "subcategory": "photos",
                "id": "1001", "set_id": "pb.77.-2207520000",
                "date": "2026-10-10 10:00:00", "extension": "jpg",
            }],
            [2, {"_cursor": "1002", "next_url": "https://www.facebook.com/photo/?fbid=1002&set=pb.77.-2207520000"}],
        ])

    # Use page_budget=1 for Facebook so Run 1 stops after Batch 1 and Run 2 resumes Batch 2 cleanly.
    real_builder = public_collection.build_multitenant_collection_config

    def one_page_builder(job: dict) -> dict:
        cfg = real_builder(job)
        cfg["sources"]["facebook"]["page_budget"] = 1
        cfg["sources"]["facebook"]["include_albums"] = False
        return cfg

    monkeypatch.setenv("COLLECTION_ENGINE", "historical")
    monkeypatch.setattr(worker, "frozen_window", lambda _: window)
    monkeypatch.setattr(public_collection, "build_multitenant_collection_config", one_page_builder)
    real_collect = public_collection.collect_public_job
    monkeypatch.setattr(
        public_collection,
        "collect_public_job",
        lambda job_dir, job, config, **kw: real_collect(job_dir, job, config, social_runner=fake_social_cli),
    )

    scheduled: list[tuple] = []
    monkeypatch.setattr(worker.executor, "submit", lambda *args: scheduled.append(args))

    # Non-checkpoint job cannot be resumed via resume-collection.
    assert request(worker.app, f"/jobs/{'a' * 32}/resume-collection", "POST", "test-admin", (b"{}",))[0] == 409

    # Run 1
    st, _, raw = request(worker.app, "/jobs", "POST", "test-admin", (json.dumps({
        "website_url": f"{web}/", "facebook_url": fb, "social_permission_confirmed": True,
    }).encode(),))
    assert st == 202
    job_id = json.loads(raw)["job_id"]
    fn, *args = scheduled.pop()
    fn(*args)

    m1 = json.loads((worker.DATA_DIR / job_id / "manifest.json").read_text(encoding="utf-8"))
    fb_urls_1 = {a["source_url"] for a in m1["assets"] if a.get("platform") == "facebook"}
    assert fb_urls_1 == {f"{fb_cdn}/p1.jpg"}

    # Run 2 via POST /jobs/{job_id}/resume-collection
    st_res, _, res_raw = request(worker.app, f"/jobs/{job_id}/resume-collection", "POST", "test-admin", (b"{}",))
    assert st_res == 202 and json.loads(res_raw)["resumed"] is True
    fn2, *args2 = scheduled.pop()
    fn2(*args2)

    m2 = json.loads((worker.DATA_DIR / job_id / "manifest.json").read_text(encoding="utf-8"))
    assert {k: m2[k] for k in window} == window  # Frozen window preserved across resume!
    fb_urls_2 = {a["source_url"] for a in m2["assets"] if a.get("platform") == "facebook"}
    assert f"{fb_cdn}/p1.jpg" in fb_urls_2


def test_unified_m4_rights_ledger_unblocks_standard_non_checkpoint_delivery(m4_env, monkeypatch):
    """Non-checkpoint M4 job with immutable manifest.rights_status='unverified' releases via SQLite rights ledger."""
    worker = m4_env["worker"]
    db = m4_env["db"]
    invoke = m4_env["invoke"]
    session_a = m4_env["session_a"]
    session_b = m4_env["session_b"]

    with closing(sqlite3.connect(db)) as con:
        ledger.migrate(con)
    monkeypatch.setenv("M4_COLLECTION_APPROVAL_ENABLED", "1")

    job_id = "d" * 32
    job_dir = worker.DATA_DIR / job_id
    (job_dir / "website").mkdir(parents=True)
    jpg_bytes = _jpeg_bytes(480, 360)
    img_path = job_dir / "website" / "salon.jpg"
    img_path.write_bytes(jpg_bytes)
    sha = hashlib.sha256(jpg_bytes).hexdigest()

    window = frozen_window(datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC))
    url = "https://std-salon.example.test/salon.jpg"
    record = {
        "job_id": job_id,
        "status": "extraction_complete",
        "website_url": "https://std-salon.example.test/",
        "facebook_url": "https://www.facebook.com/stdsalon/",
        "instagram_url": None,
        "submitter_principal_id": m4_env["admin_a_id"],
        "sources": {"website": {"status": "completed"}},
        **window,
    }
    manifest = {
        "schema_version": "3.0-unified",
        "job_id": job_id,
        "website_url": record["website_url"],
        **window,
        "assets": [{
            "asset_id": "web_0001",
            "job_id": job_id,
            "platform": "website",
            "type": "image",
            "source_url": url,
            "local_path": "website/salon.jpg",
            "mime": "image/jpeg",
            "bytes": len(jpg_bytes),
            "sha256": sha,
            "post_date": window["created_at"],
            "date_status": "verified",
            "publication_evidence": publication_evidence(
                window["created_at"], source_url=url, field="datePublished", attribution="media"
            ),
            "rights_status": "unverified",  # stays immutable on disk!
            "extraction_status": "downloaded",
            "preprocessing": {"outcome": "processed"},
        }],
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    worker.store(record)
    worker.store(record, collection_base=0)

    # Approve, start, execute curation, and verify rights ONLY in SQLite ledger.
    panel = invoke("collection-panel", {"job_id": job_id}, session_b)[1]
    approved = invoke("collection-approve", {
        "job_id": job_id, "if_match": panel["etag"], "request_key": "std-app", "reason": "Reviewed",
    }, session_b)[1]
    started = invoke("collection-start", {
        "job_id": job_id, "if_match": approved["etag"], "request_key": "std-start",
    }, session_a)[1]

    groq_calls: list[dict] = []
    monkeypatch.setenv("AI_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AI_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    monkeypatch.setenv("AI_ENDPOINT", GROQ_ACCEPTANCE_ENDPOINT)
    monkeypatch.setenv("AI_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("AI_MIN_REQUEST_SPACING_S", "0")
    monkeypatch.setattr(
        worker.app.state,
        "ai_provider_factory",
        lambda cfg: OpenAICompatibleProvider(cfg, sleep=lambda _: None, transport=_build_fake_groq_transport(groq_calls)),
        raising=False,
    )
    st_exec, _ = invoke("collection-execute", {
        "job_id": job_id,
        "if_match": started["etag"],
        "processing_consent": {"web_0001": {"source_url": url, "sha256": sha}},
    }, session_b)
    assert st_exec == 200

    # Before rights decision -> blocked_rights_review.
    st_p1, _, raw_p1 = request(worker.app, f"/jobs/{job_id}/delivery-plan", "GET", "test-admin")
    assert st_p1 == 200 and json.loads(raw_p1)["status"] == "blocked_rights_review"

    # Record verified rights in SQLite ledger -> delivery-plan immediately becomes ready!
    st_r, _ = invoke("collection-rights-decide", {
        "job_id": job_id,
        "asset_id": "web_0001",
        "decision": "verified",
        "reason": "Client confirmed ownership",
        "evidence_ref": "lic-std-001",
        "if_match": started["etag"],
        "request_key": "std-rights-1",
    }, session_b)
    assert st_r == 200

    st_p2, _, raw_p2 = request(worker.app, f"/jobs/{job_id}/delivery-plan", "GET", "test-admin")
    plan = json.loads(raw_p2)
    assert st_p2 == 200 and plan["status"] == "ready"
    assert [a["asset_id"] for a in plan["assets"]] == ["web_0001"]
    assert plan["assets"][0]["rights_evidence_ref"] == "lic-std-001"


@pytest.mark.parametrize("bad_ip", [
    "127.0.0.1",
    "127.255.255.254",
    "::1",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "100.64.0.1",
    "0.0.0.0",
    "fe80::1",
    "fc00::1",
    "fd12:3456:789a::1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:10.0.0.1",
    "[::ffff:192.168.0.1]",
])
def test_ssrf_validate_ip_address_blocks_non_public_and_mapped_ips(bad_ip):
    with pytest.raises(ValueError, match="Non-public address blocked"):
        validate_ip_address(bad_ip)


@pytest.mark.parametrize("good_ip", [
    "93.184.216.34",
    "1.1.1.1",
    "2606:2800:220:1:248:1893:25c8:1946",
])
def test_ssrf_validate_ip_address_allows_global_public_ips(good_ip):
    assert validate_ip_address(good_ip) == good_ip


@pytest.mark.parametrize("bad_url", [
    "http://0177.0.0.1/",
    "http://0x7f000001/",
    "http://2130706433/",
    "http://127.1/",
    "http://[::ffff:127.0.0.1]/",
    "http://169.254.169.254/latest/meta-data/",
    "https://example.com:8443/",
    "https://:@example.com/",
    "https://user:@example.com/",
    "file:///etc/passwd",
])
def test_ssrf_safe_url_blocks_encoded_ips_ports_credentials_and_schemes(bad_url):
    with pytest.raises(ValueError):
        safe_url(bad_url)


def test_ssrf_dial_verified_socket_pins_single_resolution_and_blocks_mixed_rebinding():
    dialed: list[tuple[str, int]] = []

    class FakeSocket:
        family = socket.AF_INET

        def setsockopt(self, *_args):
            pass

        def close(self):
            pass

    def fake_connector(addr, _timeout, _src):
        dialed.append(addr)
        return FakeSocket()

    # 1. Mixed DNS response (1 public + 1 cloud metadata IP) MUST fail before connecting to EITHER IP!
    def mixed_resolver(_host, port):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
        ]

    with pytest.raises(ValueError, match="Non-public address blocked"):
        dial_verified_socket("rebind.example.test", 443, resolver=mixed_resolver, connector=fake_connector)
    assert dialed == [], "Connector must never be called when ANY resolved IP is non-public"

    # 2. Non-standard port fails BEFORE resolver runs.
    resolver_called = []
    with pytest.raises(ValueError, match="Non-standard port blocked"):
        dial_verified_socket(
            "public.example.test",
            8080,
            resolver=lambda h, p: resolver_called.append((h, p)) or [],
            connector=fake_connector,
        )
    assert resolver_called == []

    # 3. Valid public resolution dials the exact validated IP tuple directly (no second DNS lookup).
    resolve_count = 0

    def public_resolver(_host, port):
        nonlocal resolve_count
        resolve_count += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    sock = dial_verified_socket("public.example.test", 443, resolver=public_resolver, connector=fake_connector)
    assert isinstance(sock, FakeSocket)
    assert resolve_count == 1
    assert dialed == [("93.184.216.34", 443)]


def test_ssrf_safe_http_adapter_blocks_private_dns_before_connect():
    dialed: list[tuple[str, int]] = []

    def loopback_resolver(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    def record_connector(addr, _timeout, _src):
        dialed.append(addr)
        raise AssertionError("SafeHTTPAdapter attempted to connect to loopback!")

    session = requests.Session()
    session.trust_env = False
    adapter = SafeHTTPAdapter(resolver=loopback_resolver, connector=record_connector)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    try:
        with pytest.raises((ValueError, requests.RequestException)):
            session.get("http://internal-rebind.example.test/", timeout=2)
    finally:
        session.close()
    assert dialed == []


def test_website_builder_cdn_suffixes_allow_vetted_and_block_spoofed_origins():
    cfg = build_multitenant_collection_config({
        "website_url": "https://paws-salon.example.test/",
        "facebook_url": "https://www.facebook.com/pawssalon/",
        "social_permission_confirmed": True,
    })
    web_cfg = cfg["sources"]["website"]
    assert ".wixstatic.com" in web_cfg["media_host_suffixes"]
    assert ".squarespace-cdn.com" in web_cfg["media_host_suffixes"]
    assert ".shopify.com" in web_cfg["media_host_suffixes"]
    assert ".website-files.com" in web_cfg["media_host_suffixes"]

    with build_collector_session() as session:
        http = PublicHTTP(web_cfg["url"], web_cfg, session)
        for allowed_url in (
            "https://paws-salon.example.test/media/hero.jpg",
            "https://static.wixstatic.com/media/grooming.jpg",
            "https://images.squarespace-cdn.com/content/v1/dog.jpg",
            "https://cdn.shopify.com/s/files/1/salon.webp",
            "https://cdn.prod.website-files.com/abc/banner.png",
            "https://i0.wp.com/paws-salon.example.test/photo.jpg",
            "https://res.cloudinary.com/demo/image/upload/puppy.jpg",
            "https://img1.wsimg.com/isteam/ip/dog.jpg",
            "https://salon.b-cdn.net/uploads/cut.jpg",
        ):
            assert http.media_permitted(allowed_url) is True, allowed_url

        for blocked_url in (
            "http://static.wixstatic.com/media/insecure.jpg",
            "https://static.wixstatic.com:8443/media/port.jpg",
            "https://evilwixstatic.com/media/spoof.jpg",
            "https://wixstatic.com/media/bare.jpg",
            "https://evil-cdn.example.net/offscope.jpg",
        ):
            assert http.media_permitted(blocked_url) is False, blocked_url


def test_wix_squarespace_lazy_gallery_and_fb_robots_disallowed_diagnostics(worker, tmp_path, monkeypatch):
    """Multi-builder salon website downloads Wix/Squarespace/lazy video cleanly while
    Facebook strictly obeys fbcdn robots.txt Disallow and records truthful diagnostics."""
    web = "https://boutique-grooming.example.test"
    fb = "https://www.facebook.com/boutiquegrooming/"
    wix_cdn = "https://static.wixstatic.com"
    sq_cdn = "https://images.squarespace-cdn.com"
    fb_cdn = "https://scontent.fkhi22-1.fna.fbcdn.net"
    window = frozen_window(datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC))

    wix_jpg = _jpeg_bytes(600, 400, (190, 140, 100))
    sq_jpg = _jpeg_bytes(640, 480, (90, 160, 150))
    lazy_mp4 = _mp4_bytes(tmp_path)

    home_html = (
        "<html><body>"
        f'<img data-src="{wix_cdn}/media/poodle-cut.jpg">'
        f'<div data-bg="{sq_cdn}/content/v1/salon-banner.jpg"></div>'
        f'<img src="https://evil-cdn.example.net/blocked.jpg">'
        f'<video><source data-src="{web}/media/wash-clip.mp4"></video>'
        "</body></html>"
    )
    responses = {
        f"{web}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{web}/": (200, {"Content-Type": "text/html"}, home_html),
        f"{web}/media/wash-clip.mp4": (200, {"Content-Type": "video/mp4", "Content-Length": str(len(lazy_mp4))}, lazy_mp4),
        f"{wix_cdn}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{wix_cdn}/media/poodle-cut.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(wix_jpg))}, wix_jpg),
        f"{sq_cdn}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{sq_cdn}/content/v1/salon-banner.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(sq_jpg))}, sq_jpg),
        # Real-world Meta fbcdn robots.txt policy: must be strictly respected (never bypassed).
        f"{fb_cdn}/robots.txt": (200, {}, "User-agent: *\nDisallow: /\n"),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses))

    def fake_social_cli(cmd: list[str], _stage: Path) -> tuple[int, str]:
        if "photos_albums" in cmd[-1]:
            return 0, json.dumps([])
        return 0, json.dumps([[3, f"{fb_cdn}/v/t51.82787-15/fb_dog.jpg", {
            "category": "facebook",
            "subcategory": "photos",
            "id": "122299001",
            "set_id": "pb.555.-2207520000",
            "caption": "Fresh puppy groom",
            "date": "2026-10-15 11:00:00",
            "extension": "jpg",
        }]])

    monkeypatch.setenv("COLLECTION_ENGINE", "historical")
    monkeypatch.delenv("PUBLIC_COLLECTION_CHECKPOINTS", raising=False)
    monkeypatch.delenv("PUBLIC_COLLECTION_CONFIG", raising=False)
    monkeypatch.setattr(worker, "frozen_window", lambda _: window)
    real_collect = public_collection.collect_public_job
    monkeypatch.setattr(
        public_collection,
        "collect_public_job",
        lambda job_dir, job, config, **kw: real_collect(job_dir, job, config, social_runner=fake_social_cli),
    )

    scheduled: list[tuple] = []
    monkeypatch.setattr(worker.executor, "submit", lambda *args: scheduled.append(args))

    st, _, raw = request(worker.app, "/jobs", "POST", "test-admin", (json.dumps({
        "website_url": f"{web}/",
        "facebook_url": fb,
        "social_permission_confirmed": True,
        "max_pages": 3,
        "max_media": 10,
        "max_social_files": 5,
    }).encode(),))
    assert st == 202, raw
    job_id = json.loads(raw)["job_id"]
    fn, *args = scheduled.pop()
    fn(*args)

    manifest = json.loads((worker.DATA_DIR / job_id / "manifest.json").read_text(encoding="utf-8"))
    by_url = {a.get("source_url"): a for a in manifest["assets"] if a.get("type") != "frame"}

    # Wix + Squarespace data-bg + lazy video all downloaded cleanly; evil-cdn dropped.
    assert by_url[f"{wix_cdn}/media/poodle-cut.jpg"]["extraction_status"] == "downloaded"
    assert by_url[f"{sq_cdn}/content/v1/salon-banner.jpg"]["extraction_status"] == "downloaded"
    assert by_url[f"{web}/media/wash-clip.mp4"]["extraction_status"] == "downloaded"
    assert by_url[f"{web}/media/wash-clip.mp4"]["type"] == "video"
    assert by_url[f"{web}/media/wash-clip.mp4"]["preprocessing"]["outcome"] == "video_processed"
    assert "https://evil-cdn.example.net/blocked.jpg" not in by_url

    # Facebook strictly obeyed robots.txt Disallow and recorded truthful diagnostics.
    fb_asset = by_url[f"{fb_cdn}/v/t51.82787-15/fb_dog.jpg"]
    assert fb_asset["extraction_status"] == "access_blocked"
    assert fb_asset["failure_reason"] == "robots_disallowed"
    assert "robots_disallowed" in manifest["sources"]["facebook"]["issues"]
