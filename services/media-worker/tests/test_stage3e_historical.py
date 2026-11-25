"""Offline Stage 3E validation tests for 365-day historical media collection & hardening.

All tests execute inside network-disabled containers (--network none) with live
sockets hard-blocked. Zero live website, Instagram, Facebook, Groq, or Drive calls.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
import socket

from PIL import Image
import pytest
import requests

from app.ai.payload import select_ai_candidates
from app.ai.pipeline import run_ai_curation
from app.collection_checkpoint import (
    Attachment,
    CollectionCheckpoint,
    Download,
    GlobalBudget,
    InventoryPage,
    SourceItem,
    SourceStop,
)
from app.delivery import build_delivery_plan
from app.public_collection import collect_public_job
from app.publication import frozen_window
from app.social_discovery import (
    OpenSourceSocialInventory,
    build_gallery_dl_historical_cmd,
    build_instaloader_historical_cmd,
)
from app.website_inventory import WebsiteInventory
from tests.test_phase9_security import request, worker  # noqa: F401

WEB = "https://groomer-hist.fixture.test"
IG = "https://www.instagram.com/lilysclubuk/"
FB = "https://www.facebook.com/people/Lilys-club-Dog-Grooming/61564648418385/"
APPROVED_CDN = "https://cdn-hist.fixture.test"
UNAPPROVED_CDN = "https://cdn-unapproved.fixture.test"
WINDOW = frozen_window(datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC))


@pytest.fixture(autouse=True)
def block_all_sockets(monkeypatch):
    def deny(*_args, **_kwargs):
        pytest.fail("Offline Stage 3E test attempted a live socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny)


def _jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (64, 64), color)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _mock_http_get(responses: dict[str, tuple[int, dict, bytes | str]], seen: list[tuple[str, dict]]):
    def fake_get(session, url, **kwargs):
        assert not session.cookies and not session.trust_env and kwargs.get("allow_redirects") is False
        seen.append((url, dict(kwargs.get("headers") or {})))
        assert url in responses, f"Unexpected HTTP fetch: {url}"
        entry = responses[url]
        status, headers, body = entry(kwargs.get("headers") or {}) if callable(entry) else entry
        resp = requests.Response()
        resp.status_code = status
        resp.url = url
        resp.headers.update(headers)
        resp._content = body.encode("utf-8") if isinstance(body, str) else body
        resp._content_consumed = True
        return resp

    return fake_get


def _assert_no_bypass_flags(cmd: list[str]) -> None:
    joined = " ".join(cmd).lower()
    for forbidden in ("--cookies", "--login", "--password", "--proxy", "--username", "--xff"):
        assert forbidden not in joined, f"Forbidden bypass flag {forbidden} in {cmd}"


def test_website_historical_traversal_sitemaps_archives_galleries_pagination_nested_jsonld_and_pilot_preservation(
    tmp_path, monkeypatch,
):
    """Task 3: Historical mode traverses robots Sitemaps, sitemap indexes, galleries, blogs, pagination,
    nested JSON-LD dates, while excluding auth/cart/robots/off-origin paths; Pilot mode stays unchanged."""
    img_a = _jpeg_bytes((200, 100, 60))
    img_b = _jpeg_bytes((50, 140, 190))
    img_c = _jpeg_bytes((80, 180, 90))
    seen: list[tuple[str, dict]] = []

    nested_ld = json.dumps({
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": "Autumn Grooming Tips",
        "datePublished": "2020-01-01T00:00:00Z",  # parent date MUST NOT override exact ImageObject date
        "image": {
            "@type": "ImageObject",
            "contentUrl": f"{APPROVED_CDN}/blog-nested.jpg",
            "datePublished": "2026-10-05T09:00:00Z",
        },
        "video": {
            "@type": "VideoObject",
            "contentUrl": f"{APPROVED_CDN}/blog-video.mp4",
            "uploadDate": "2026-10-06T09:00:00Z",  # uploadDate is NOT trusted
        },
    })
    responses = {
        f"{WEB}/robots.txt": (
            200,
            {"Content-Type": "text/plain"},
            f"User-agent: *\nDisallow: /private-gallery/\nSitemap: {WEB}/sitemap_index.xml\nSitemap: https://evil.test/sm.xml\n",
        ),
        f"{WEB}/": (
            200,
            {"Content-Type": "text/html"},
            (
                f'<html><body>'
                f'<a href="/gallery">Gallery</a>'
                f'<a href="/private-gallery/secret">Secret</a>'
                f'<a href="/login">Login</a><a href="/cart">Cart</a><a href="/wp-admin/edit">Admin</a>'
                f'<a href="https://offsite.test/gallery">Offsite</a>'
                f'<img src="{APPROVED_CDN}/home.jpg">'
                f'<img src="{UNAPPROVED_CDN}/blocked-cdn.jpg">'
                f'</body></html>'
            ),
        ),
        f"{WEB}/sitemap_index.xml": (
            200,
            {"Content-Type": "application/xml"},
            f"<sitemapindex><sitemap><loc>{WEB}/posts-sitemap.xml</loc></sitemap></sitemapindex>",
        ),
        f"{WEB}/posts-sitemap.xml": (
            200,
            {"Content-Type": "application/xml"},
            f"<urlset><url><loc>{WEB}/2026/10/autumn-groom</loc></url></urlset>",
        ),
        f"{WEB}/gallery": (
            200,
            {"Content-Type": "text/html"},
            f'<html><body><nav aria-label="Pagination"><a href="/gallery/page/2/">2</a></nav>'
            f'<img src="{APPROVED_CDN}/gal1.jpg"></body></html>',
        ),
        f"{WEB}/gallery/page/2/": (
            200,
            {"Content-Type": "text/html"},
            f'<html><body><a rel="next" href="/gallery">Loop</a><img src="{APPROVED_CDN}/gal2.jpg"></body></html>',
        ),
        f"{WEB}/2026/10/autumn-groom": (
            200,
            {"Content-Type": "text/html"},
            f'<html><head><script type="application/ld+json">{nested_ld}</script></head>'
            f'<body><img src="{APPROVED_CDN}/blog-nested.jpg">'
            f'<video><source src="{APPROVED_CDN}/blog-video.mp4"></video></body></html>',
        ),
        f"{APPROVED_CDN}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{APPROVED_CDN}/home.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(img_a))}, img_a),
        f"{APPROVED_CDN}/gal1.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(img_b))}, img_b),
        f"{APPROVED_CDN}/gal2.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(img_c))}, img_c),
        f"{APPROVED_CDN}/blog-nested.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(img_a))}, img_a),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses, seen))

    # 1. Verify Pilot Mode does NOT crawl /gallery or robots Sitemap automatically.
    pilot_dir = tmp_path / ("1" * 32)
    pilot_dir.mkdir()
    pilot_job = {
        "job_id": pilot_dir.name, "website_url": f"{WEB}/", "instagram_url": IG, "facebook_url": FB,
        "social_permission_confirmed": False, **WINDOW,
    }
    pilot_cfg = {
        "sources": {
            "website": {
                "url": f"{WEB}/", "sitemaps": [], "media_origins": [APPROVED_CDN],
                "request_budget": 10, "page_budget": 5, "document_limit": 16384, "entry_limit": 10,
                "transfer_budget": 0, "byte_limit": 100_000, "time_budget_seconds": 30,
            },
            "instagram": {"url": IG, "media_origins": [APPROVED_CDN], "request_budget": 1, "page_budget": 1, "transfer_budget": 0, "byte_limit": 1024, "time_budget_seconds": 10},
            "facebook": {"url": FB, "media_origins": [APPROVED_CDN], "request_budget": 1, "page_budget": 1, "transfer_budget": 0, "byte_limit": 1024, "time_budget_seconds": 10},
        },
    }
    pilot_manifest = collect_public_job(pilot_dir, pilot_job, pilot_cfg)
    assert "collection_profile" not in pilot_manifest
    pilot_urls = {i["url"] for i in pilot_manifest["sources"]["website"]["items"]}
    assert pilot_urls == {f"{WEB}/"}  # /gallery and robots.txt Sitemap NOT followed in Pilot Mode

    # 2. Verify Historical Mode traverses robots Sitemap, nested sitemap, /gallery, /gallery/page/2/, and nested JSON-LD!
    seen.clear()
    hist_dir = tmp_path / ("2" * 32)
    hist_dir.mkdir()
    hist_job = {**pilot_job, "job_id": hist_dir.name}
    hist_cfg = {
        "collection_profile": "historical",
        "sources": {
            "website": {
                "url": f"{WEB}/", "sitemaps": [], "media_origins": [APPROVED_CDN],
                "request_budget": 25, "page_budget": 10, "document_limit": 16384, "entry_limit": 10,
                "transfer_budget": 0, "byte_limit": 100_000, "time_budget_seconds": 30,
            },
            "instagram": {"url": IG, "media_origins": [APPROVED_CDN], "request_budget": 1, "page_budget": 1, "transfer_budget": 0, "byte_limit": 1024, "time_budget_seconds": 10},
            "facebook": {"url": FB, "media_origins": [APPROVED_CDN], "request_budget": 1, "page_budget": 1, "transfer_budget": 0, "byte_limit": 1024, "time_budget_seconds": 10},
        },
    }
    hist_manifest = collect_public_job(hist_dir, hist_job, hist_cfg)
    assert hist_manifest["collection_profile"] == "historical"
    fetched_urls = [u for u, _ in seen]
    assert not any(bad in u for u in fetched_urls for bad in ("/private-gallery/", "/login", "/cart", "/wp-admin", "offsite.test", "evil.test", UNAPPROVED_CDN))
    web_items = {i["url"] for i in hist_manifest["sources"]["website"]["items"]}
    assert {f"{WEB}/", f"{WEB}/gallery", f"{WEB}/gallery/page/2/", f"{WEB}/2026/10/autumn-groom"} <= web_items
    by_src = {a["source_url"]: a for a in hist_manifest["assets"] if a["platform"] == "website"}
    assert by_src[f"{APPROVED_CDN}/blog-nested.jpg"]["date_eligibility"] == "in_window"
    assert by_src[f"{APPROVED_CDN}/blog-nested.jpg"]["post_date"] == "2026-10-05T09:00:00+00:00"
    assert by_src[f"{APPROVED_CDN}/blog-video.mp4"]["date_eligibility"] == "unknown_date"  # uploadDate rejected
    assert "pagination_loop" in hist_manifest["sources"]["website"]["issues"]
    assert hist_manifest["sources"]["website"]["coverage_percentage"] is None


def test_facebook_historical_multi_batch_pinned_out_of_order_cutoff_albums_and_resume(tmp_path):
    """Tasks 2 & 4: Facebook multi-batch continuation via real next_photo_id (&setextract), pinned/out-of-order
    handling, frozen UTC cutoff with evidence, bounded album traversal, and checkpoint resume across runs."""
    set_main = "pb.61564648418385.-2207520000"
    set_album = "a.122199999999999"
    cmds: list[list[str]] = []

    def fake_fb_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        _assert_no_bypass_flags(cmd)
        assert "--date-after" not in cmd, "Historical mode must NEVER pass --date-after (breaks pinned posts)"
        assert "-o" in cmd and "loop=false" in cmd
        cmds.append(cmd)
        target = cmd[-1]

        # Batch 1: Pinned 2023 photo + 2 recent photos -> real next_photo_id=10000004
        if target == FB:
            return 0, json.dumps([
                [3, f"{APPROVED_CDN}/fb-pinned.jpg", {
                    "category": "facebook", "subcategory": "photos", "id": "10000001", "set_id": set_main,
                    "pinned": True, "date": "2023-05-01 10:00:00", "extension": "jpg", "next_photo_id": "10000002",
                }],
                [3, f"{APPROVED_CDN}/fb-recent-1.jpg", {
                    "category": "facebook", "subcategory": "photos", "id": "10000002", "set_id": set_main,
                    "date": "2026-11-10 09:00:00", "extension": "jpg", "next_photo_id": "10000003",
                }],
                [3, f"{APPROVED_CDN}/fb-recent-2.jpg", {
                    "category": "facebook", "subcategory": "photos", "id": "10000003", "set_id": set_main,
                    "date": "2026-08-15 12:00:00", "extension": "jpg", "next_photo_id": "10000004",
                }],
            ])

        # Batch 2: 1 out-of-window photo + 1 in-window photo (resets consecutive_older!) + 1 undated photo
        if "fbid=10000004" in target and "&setextract" in target:
            return 0, json.dumps([
                [3, f"{APPROVED_CDN}/fb-out-of-order-old.jpg", {
                    "category": "facebook", "subcategory": "photos", "id": "10000004", "set_id": set_main,
                    "date": "2025-01-10 10:00:00", "extension": "jpg", "next_photo_id": "10000005",
                }],
                [3, f"{APPROVED_CDN}/fb-recent-3.jpg", {
                    "category": "facebook", "subcategory": "photos", "id": "10000005", "set_id": set_main,
                    "date": "2026-02-01 10:00:00", "extension": "jpg", "next_photo_id": "10000006",
                }],
                [3, f"{APPROVED_CDN}/fb-undated.jpg", {
                    "category": "facebook", "subcategory": "photos", "id": "10000006", "set_id": set_main,
                    "extension": "jpg", "next_photo_id": "10000007",
                }],
            ])

        # Batch 3: 3 consecutive non-pinned out-of-window photos -> triggers cutoff_reached with evidence!
        if "fbid=10000007" in target and "&setextract" in target:
            return 0, json.dumps([
                [3, f"{APPROVED_CDN}/fb-old-1.jpg", {
                    "category": "facebook", "subcategory": "photos", "id": "10000007", "set_id": set_main,
                    "date": "2025-11-20 10:00:00", "extension": "jpg", "next_photo_id": "10000008",
                }],
                [3, f"{APPROVED_CDN}/fb-old-2.jpg", {
                    "category": "facebook", "subcategory": "photos", "id": "10000008", "set_id": set_main,
                    "date": "2025-10-15 10:00:00", "extension": "jpg", "next_photo_id": "10000009",
                }],
                [3, f"{APPROVED_CDN}/fb-old-3.jpg", {
                    "category": "facebook", "subcategory": "photos", "id": "10000009", "set_id": set_main,
                    "date": "2025-09-01 10:00:00", "extension": "jpg", "next_photo_id": "10000010",
                }],
            ])

        # Phase 2: /photos_albums returns already-visited main set (skipped) + 1 fresh public album.
        if target.endswith("/photos_albums"):
            assert "--dump-json" in cmd
            return 0, json.dumps([
                [6, f"https://www.facebook.com/media/set/?set={set_main}", {"set_id": set_main, "title": "Timeline"}],
                [6, f"https://www.facebook.com/media/set/?set={set_album}", {"set_id": set_album, "title": "Summer Grooms"}],
            ])

        # Album set: 1 duplicate photo (10000002 already visited -> skipped) + 1 fresh album photo, then exhausted.
        if f"set={set_album}" in target:
            return 0, json.dumps([
                [3, f"{APPROVED_CDN}/fb-recent-1-newtoken.jpg", {
                    "category": "facebook", "subcategory": "set", "id": "10000002", "set_id": set_album,
                    "date": "2026-11-10 09:00:00", "extension": "jpg", "next_photo_id": "20000001",
                }],
                [3, f"{APPROVED_CDN}/fb-album-photo.jpg", {
                    "category": "facebook", "subcategory": "set", "id": "20000001", "set_id": set_album,
                    "date": "2026-07-04 14:00:00", "extension": "jpg", "next_photo_id": "",
                }],
            ])
        raise AssertionError(f"Unexpected Facebook target: {target}")

    job_dir = tmp_path / ("3" * 32)
    job_dir.mkdir()
    job = {"job_id": job_dir.name, **WINDOW}

    # Run 1: Bounded page_budget=2 (pauses mid-history with persisted real &setextract cursor).
    cp1 = CollectionCheckpoint(job_dir, job)
    inv1 = OpenSourceSocialInventory(
        "facebook", FB, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=25, request_budget=10, time_budget_seconds=60, runner=fake_fb_runner,
        historical_mode=True, batch_size=3, cutoff_consecutive_older=3, include_albums=True, album_limit=3,
    )
    rep1 = cp1.run_source("facebook", FB, inv1, lambda *_: None, page_budget=2, transfer_budget=0, byte_limit=1024)
    assert rep1["stop_reason"] == "page_budget"
    assert rep1["termination_reason"] == "budget_reached"
    assert rep1["cursor"] is not None and "fbid=10000007" in rep1["cursor"]
    assert len(rep1["assets"]) == 6  # batches 1 & 2 persisted

    # Run 2: Resume from persisted checkpoint (fresh inventory instance -> picks up at fbid=10000007, then albums!).
    cp2 = CollectionCheckpoint(job_dir, job)
    inv2 = OpenSourceSocialInventory(
        "facebook", FB, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=25, request_budget=10, time_budget_seconds=60, runner=fake_fb_runner,
        historical_mode=True, batch_size=3, cutoff_consecutive_older=3, include_albums=True, album_limit=3,
    )
    rep2 = cp2.run_source("facebook", FB, inv2, lambda *_: None, page_budget=5, transfer_budget=0, byte_limit=1024)
    assert rep2["stop_reason"] == "cutoff_reached"
    assert rep2["termination_reason"] == "cutoff_reached_with_evidence"
    assert rep2["listing_exhausted"] is False  # cutoff reached != full account exhaustion
    assert rep2["coverage_percentage"] is None
    by_native = {a["native_id"]: a for a in rep2["assets"]}
    assert set(by_native) == {
        "10000001", "10000002", "10000003", "10000004", "10000005", "10000006",
        "10000007", "10000008", "10000009", "20000001",
    }
    assert by_native["10000001"]["date_eligibility"] == "out_of_window"
    assert by_native["10000005"]["date_eligibility"] == "in_window"
    assert by_native["10000006"]["date_eligibility"] == "unknown_date"
    assert by_native["20000001"]["date_eligibility"] == "in_window"
    # Never fetched photo 10000010 past the 3-consecutive-older cutoff!
    assert not any("10000010" in c[-1] for c in cmds)

    # Run 3: Resuming an already cutoff_reached source does NOT re-fetch older pages.
    calls_before = len(cmds)
    cp3 = CollectionCheckpoint(job_dir, job)
    cp3.collect("facebook", FB, inv2, page_budget=3)
    assert len(cmds) == calls_before


def test_instagram_historical_posts_reels_real_cursor_instaloader_fallback_and_unsupported_pagination(tmp_path):
    """Tasks 2 & 4: Instagram real -o cursor continuation, pinned post preservation, Reels fallback via
    instaloader --no-posts --reels --resume-prefix, and honest unsupported_pagination when no cursor exists."""
    cmds: list[list[str]] = []

    def fake_ig_runner(cmd: list[str], stage: Path) -> tuple[int, str]:
        _assert_no_bypass_flags(cmd)
        cmds.append(cmd)
        if cmd[0] == "gallery-dl":
            assert "--date-after" not in cmd
            url = cmd[-1]
            if url.endswith("/posts/") and "cursor=REAL_IG_CUR_2" not in cmd:
                # Batch 1: Pinned 2024 post + recent carousel + real gallery-dl cursor log line.
                body = json.dumps([
                    [3, f"{APPROVED_CDN}/ig-pin.jpg", {
                        "category": "instagram", "post_id": "ig-pin", "post_shortcode": "IgPin1",
                        "post_url": "https://www.instagram.com/p/IgPin1/", "media_id": "ig-pin-m",
                        "pinned": ["999"], "extension": "jpg", "post_date": "2024-01-05T10:00:00+00:00",
                    }],
                    [3, f"{APPROVED_CDN}/ig-c1.jpg", {
                        "category": "instagram", "post_id": "ig-car", "post_shortcode": "IgCar1",
                        "post_url": "https://www.instagram.com/p/IgCar1/", "media_id": "ig-c1",
                        "extension": "jpg", "post_date": "2026-10-20T10:00:00+00:00",
                    }],
                ])
                return 0, body + "\nUse '-o cursor=REAL_IG_CUR_2' to continue downloading from the current position\n"
            if url.endswith("/posts/") and "cursor=REAL_IG_CUR_2" in cmd:
                # Batch 2: 2 consecutive non-pinned older posts (threshold=2) -> posts section reaches cutoff!
                return 0, json.dumps([
                    [3, f"{APPROVED_CDN}/ig-old1.jpg", {
                        "category": "instagram", "post_id": "ig-old1", "post_shortcode": "IgOld1",
                        "post_url": "https://www.instagram.com/p/IgOld1/", "media_id": "ig-old1-m",
                        "extension": "jpg", "post_date": "2025-11-01T10:00:00+00:00",
                    }],
                    [3, f"{APPROVED_CDN}/ig-old2.jpg", {
                        "category": "instagram", "post_id": "ig-old2", "post_shortcode": "IgOld2",
                        "post_url": "https://www.instagram.com/p/IgOld2/", "media_id": "ig-old2-m",
                        "extension": "jpg", "post_date": "2025-10-01T10:00:00+00:00",
                    }],
                ])
            if url.endswith("/reels/"):
                # gallery-dl returns NotFoundError -> eligible single Instaloader reels fallback!
                return 1, "[instagram][error] NotFoundError: The requested user could not be found"
        assert cmd[0] == "instaloader"
        assert "--no-posts" in cmd and "--reels" in cmd and "--resume-prefix" in cmd and "pgm_resume" in cmd
        (stage / "pgm_resume_iterator.json.xz").write_bytes(b"resume-marker")
        reel_sidecar = {
            "instaloader": {"version": "4.16", "node_type": "Post"},
            "node": {
                "id": "ig-reel-77",
                "shortcode": "ReelCode77",
                "product_type": "clips",
                "is_video": True,
                "video_url": f"{APPROVED_CDN}/reel-77.mp4",
                "display_url": f"{APPROVED_CDN}/reel-77-thumb.jpg",
                "taken_at_timestamp": int(datetime(2026, 11, 1, 12, 0, tzinfo=UTC).timestamp()),
            },
        }
        (stage / "2026-11-01_12-00-00_UTC.json").write_text(json.dumps(reel_sidecar), encoding="utf-8")
        return 0, ""

    job_dir = tmp_path / ("4" * 32)
    job_dir.mkdir()
    resume_dir = job_dir / "collection" / "instaloader-resume"
    cp = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
    inv = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=20, request_budget=6, time_budget_seconds=60, runner=fake_ig_runner,
        historical_mode=True, batch_size=2, cutoff_consecutive_older=2, include_reels=True, resume_dir=resume_dir,
    )
    rep = cp.run_source("instagram", IG, inv, lambda *_: None, page_budget=5, transfer_budget=0, byte_limit=1024)
    assert rep["stop_reason"] == "cutoff_reached"
    assert rep["termination_reason"] == "cutoff_reached_with_evidence"
    assert "instaloader_resume_state_saved" in rep["issues"]
    by_native = {a["native_id"]: a for a in rep["assets"]}
    assert by_native["ig-pin-m"]["date_eligibility"] == "out_of_window"
    assert by_native["ig-c1"]["date_eligibility"] == "in_window"
    assert by_native["ig-reel-77"]["kind"] == "video" and by_native["ig-reel-77"]["date_eligibility"] == "in_window"
    assert any(item["url"] == "https://www.instagram.com/reel/ReelCode77/" for item in rep["items"])

    # Sub-test: Capped batch WITHOUT a real cursor must NEVER fabricate a cursor -> unsupported_pagination!
    def no_cursor_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        return 0, json.dumps([
            [3, f"{APPROVED_CDN}/ig-nc-1.jpg", {
                "category": "instagram", "post_id": "nc1", "post_shortcode": "Nc1",
                "post_url": "https://www.instagram.com/p/Nc1/", "media_id": "nc1-m",
                "extension": "jpg", "post_date": "2026-11-15T10:00:00+00:00",
            }],
            [3, f"{APPROVED_CDN}/ig-nc-2.jpg", {
                "category": "instagram", "post_id": "nc2", "post_shortcode": "Nc2",
                "post_url": "https://www.instagram.com/p/Nc2/", "media_id": "nc2-m",
                "extension": "jpg", "post_date": "2026-11-14T10:00:00+00:00",
            }],
        ])

    job_dir2 = tmp_path / ("5" * 32)
    job_dir2.mkdir()
    cp2 = CollectionCheckpoint(job_dir2, {"job_id": job_dir2.name, **WINDOW})
    inv2 = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=10, request_budget=3, time_budget_seconds=30, runner=no_cursor_runner,
        historical_mode=True, batch_size=2, include_reels=False,
    )
    rep2 = cp2.run_source("instagram", IG, inv2, lambda *_: None, page_budget=3, transfer_budget=0, byte_limit=1024)
    assert rep2["stop_reason"] == "unsupported_pagination"
    assert rep2["termination_reason"] == "unsupported_pagination"
    assert rep2["cursor"] is None and rep2["coverage"] == "incomplete" and rep2["coverage_percentage"] is None


@pytest.mark.parametrize(("batch2_code", "batch2_out", "expected_stop", "expected_term"), [
    (1, "HttpError: 429 Too Many Requests", "rate_limited", "rate_limited"),
    (1, "AuthenticationError: login required to view older posts", "restricted", "access_blocked"),
    (0, json.dumps([[6, FB, {"message": "Login required for older photos"}]]), "restricted", "access_blocked"),
])
def test_mid_history_restriction_and_rate_limit_stop_honestly_and_preserve_prior_batches(
    tmp_path, batch2_code, batch2_out, expected_stop, expected_term,
):
    """Task 4 & 6: Mid-history 429 or login wall stops immediately without poking albums/fallbacks
    and preserves all previously checkpointed assets and honest coverage."""
    calls: list[str] = []
    set_id = "pb.61564648418385.-2207520000"

    def runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        calls.append(cmd[-1])
        if len(calls) == 1:
            return 0, json.dumps([
                [3, f"{APPROVED_CDN}/fb-ok.jpg", {
                    "category": "facebook", "subcategory": "photos", "id": "30000001", "set_id": set_id,
                    "date": "2026-11-10 10:00:00", "extension": "jpg", "next_photo_id": "30000002",
                }],
            ])
        return batch2_code, batch2_out

    job_dir = tmp_path / ("6" * 32)
    job_dir.mkdir()
    cp = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
    inv = OpenSourceSocialInventory(
        "facebook", FB, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=10, request_budget=5, time_budget_seconds=30, runner=runner,
        historical_mode=True, batch_size=1, include_albums=True,
    )
    rep = cp.run_source("facebook", FB, inv, lambda *_: None, page_budget=4, transfer_budget=0, byte_limit=1024)
    assert len(calls) == 2 and not any("/photos_albums" in c for c in calls)
    assert rep["stop_reason"] == expected_stop
    assert rep["termination_reason"] == expected_term
    assert rep["coverage"] == "incomplete" and rep["coverage_percentage"] is None
    assert len(rep["assets"]) == 1 and rep["assets"][0]["native_id"] == "30000001"
    assert rep["cursor"] is not None  # checkpoint preserved for later authorized retry


def test_download_hardening_expired_url_token_refresh_magic_bytes_streaming_limit_resume_and_out_of_window_skip(
    tmp_path, monkeypatch,
):
    """Task 5 & 6: Expiring CDN 410 handling without unauthorized retries, token refresh on same native ID,
    skipping already-verified files, magic-byte HTML rejection, chunked size cap, skip_out_of_window, and ETag resume."""
    valid_jpg = _jpeg_bytes((220, 120, 40))
    valid_jpg_2 = _jpeg_bytes((40, 180, 160))
    old_jpg = _jpeg_bytes((100, 100, 100))
    fake_html_as_jpg = b"<html><body>Soft error masquerading as JPEG</body></html>"
    seen: list[tuple[str, dict]] = []

    def range_jpg_2(headers: dict):
        if headers.get("Range") == "bytes=200-" and headers.get("If-Range") == '"etag-jpg2"':
            tail = valid_jpg_2[200:]
            return 206, {
                "Content-Type": "image/jpeg",
                "Content-Length": str(len(tail)),
                "Content-Range": f"bytes 200-{len(valid_jpg_2) - 1}/{len(valid_jpg_2)}",
                "ETag": '"etag-jpg2"',
            }, tail
        return 200, {"Content-Type": "image/jpeg", "Content-Length": str(len(valid_jpg_2)), "ETag": '"etag-jpg2"'}, valid_jpg_2

    responses = {
        f"{WEB}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{WEB}/": (200, {"Content-Type": "text/html"}, "<html><body>No media</body></html>"),
        f"{APPROVED_CDN}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{APPROVED_CDN}/expired.jpg?oe=OLD": (410, {}, b"Gone"),
        f"{APPROVED_CDN}/expired.jpg?oe=FRESH": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(valid_jpg))}, valid_jpg),
        f"{APPROVED_CDN}/good-verified.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(valid_jpg_2))}, valid_jpg_2),
        f"{APPROVED_CDN}/bad-magic.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(fake_html_as_jpg))}, fake_html_as_jpg),
        f"{APPROVED_CDN}/oversized-chunked.jpg": (200, {"Content-Type": "image/jpeg"}, b"\xff\xd8\xff" + b"X" * 5000),  # no Content-Length!
        f"{APPROVED_CDN}/old-2023.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(old_jpg))}, old_jpg),
        f"{APPROVED_CDN}/resumable.jpg": range_jpg_2,
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses, seen))

    run_no = [1]

    def fb_runner(_cmd: list[str], _stage: Path) -> tuple[int, str]:
        token = "OLD" if run_no[0] == 1 else "FRESH"
        return 0, json.dumps([
            [3, f"{APPROVED_CDN}/expired.jpg?oe={token}", {
                "category": "facebook", "subcategory": "photos", "id": "50000001", "set_id": "pb.1",
                "date": "2026-11-10 10:00:00", "extension": "jpg",
            }],
            [3, f"{APPROVED_CDN}/good-verified.jpg", {
                "category": "facebook", "subcategory": "photos", "id": "50000002", "set_id": "pb.1",
                "date": "2026-11-09 10:00:00", "extension": "jpg",
            }],
            [3, f"{APPROVED_CDN}/bad-magic.jpg", {
                "category": "facebook", "subcategory": "photos", "id": "50000003", "set_id": "pb.1",
                "date": "2026-11-08 10:00:00", "extension": "jpg",
            }],
            [3, f"{APPROVED_CDN}/oversized-chunked.jpg", {
                "category": "facebook", "subcategory": "photos", "id": "50000004", "set_id": "pb.1",
                "date": "2026-11-07 10:00:00", "extension": "jpg",
            }],
            [3, f"{APPROVED_CDN}/old-2023.jpg", {
                "category": "facebook", "subcategory": "photos", "id": "50000005", "set_id": "pb.1",
                "date": "2023-01-01 10:00:00", "extension": "jpg",
            }],
        ])

    job_dir = tmp_path / ("7" * 32)
    job_dir.mkdir()
    job = {
        "job_id": job_dir.name, "website_url": f"{WEB}/", "instagram_url": IG, "facebook_url": FB,
        "social_permission_confirmed": True, **WINDOW,
    }
    cfg = {
        "collection_profile": "historical",
        "sources": {
            "website": {
                "url": f"{WEB}/", "sitemaps": [], "media_origins": [APPROVED_CDN], "discover_robots_sitemaps": False,
                "request_budget": 10, "page_budget": 1, "document_limit": 8192, "entry_limit": 5,
                "transfer_budget": 0, "byte_limit": 3000, "time_budget_seconds": 30,
            },
            "instagram": {
                "url": IG, "media_origins": [APPROVED_CDN], "request_budget": 1, "page_budget": 1,
                "transfer_budget": 0, "byte_limit": 3000, "time_budget_seconds": 10,
            },
            "facebook": {
                "url": FB, "discovery_mode": "open_source_cli", "download_authorized": True,
                "media_origins": [APPROVED_CDN], "include_albums": False,
                "cli_request_budget": 4, "request_budget": 20, "page_budget": 2, "entry_limit": 10, "batch_size": 10,
                "transfer_budget": 10, "byte_limit": 3000, "time_budget_seconds": 30,
            },
        },
    }
    manifest1 = collect_public_job(job_dir, job, cfg, social_runner=fb_runner)
    by_native1 = {a["native_id"]: a for a in manifest1["assets"] if a["platform"] == "facebook"}
    assert by_native1["50000001"]["status"] == "media_url_expired"  # HTTP 410 recorded without retrying!
    assert by_native1["50000002"]["status"] == "verified" and by_native1["50000002"]["sha256"] == hashlib.sha256(valid_jpg_2).hexdigest()
    assert by_native1["50000003"]["status"] == "invalid_media_content"  # HTML blocked by magic-byte check!
    assert by_native1["50000004"]["status"] == "byte_limit_or_invalid_range"  # chunked > 3000 bytes aborted!
    assert by_native1["50000005"]["status"] == "pending"  # out_of_window skipped in Historical Mode!
    assert not any("old-2023.jpg" in u for u, _ in seen)
    assert not list((job_dir / "collection").glob("*.part"))

    # Simulate explicit re-discovery of the SAME Facebook native ID 50000001 with refreshed CDN token ?oe=FRESH:
    # must refresh URL without duplicating the asset and without re-downloading verified 50000002!
    cp = CollectionCheckpoint(job_dir, job)
    with cp._transaction() as db:
        db.execute("UPDATE source SET exhausted=0, cursor=NULL, stop=NULL WHERE name='facebook'")
        db.execute("DELETE FROM page WHERE source='facebook'")
    seen.clear()
    run_no[0] = 2
    manifest2 = collect_public_job(job_dir, job, cfg, social_runner=fb_runner)
    fb_assets2 = [a for a in manifest2["assets"] if a["platform"] == "facebook"]
    assert len(fb_assets2) == 5  # ZERO duplicate rows!
    by_native2 = {a["native_id"]: a for a in fb_assets2}
    assert by_native2["50000001"]["status"] == "verified"
    assert by_native2["50000001"]["sha256"] == hashlib.sha256(valid_jpg).hexdigest()
    assert not any("good-verified.jpg" in u for u, _ in seen)  # verified file NEVER re-downloaded!


def test_global_budgets_source_byte_budget_download_authorization_m4_holds_and_cli_flags(worker, tmp_path, monkeypatch):
    """Tasks 2, 5 & 6: GlobalBudget, source_byte_budget, download_authorized separation, M4 holds intact,
    and real offline CLI option parser validation for gallery-dl and instaloader."""
    img1 = _jpeg_bytes((150, 50, 50))
    img2 = _jpeg_bytes((50, 150, 50))
    seen: list[tuple[str, dict]] = []
    responses = {
        f"{WEB}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{WEB}/": (200, {"Content-Type": "text/html"}, f'<img src="{APPROVED_CDN}/w1.jpg"><img src="{APPROVED_CDN}/w2.jpg">'),
        f"{APPROVED_CDN}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{APPROVED_CDN}/w1.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(img1))}, img1),
        f"{APPROVED_CDN}/w2.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(img2))}, img2),
        f"{APPROVED_CDN}/fb1.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(img1))}, img1),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses, seen))

    def fb_runner(_cmd: list[str], _stage: Path) -> tuple[int, str]:
        return 0, json.dumps([[3, f"{APPROVED_CDN}/fb1.jpg", {
            "category": "facebook", "subcategory": "photos", "id": "60000001", "set_id": "pb.6",
            "date": "2026-11-10 10:00:00", "extension": "jpg",
        }]])

    job_id = "8" * 32
    job_dir = worker.DATA_DIR / job_id
    job_dir.mkdir()
    job = {
        "job_id": job_id, "website_url": f"{WEB}/", "instagram_url": IG, "facebook_url": FB,
        "social_permission_confirmed": True, "collection_mode": "public_checkpoint", **WINDOW,
    }
    worker.store(job)
    cfg = {
        "collection_profile": "historical",
        "global_budgets": {
            "max_pages": 5,
            "max_items": 10,
            "max_transfers": 1,  # allows only 1 total download across all 3 sources!
            "max_bytes": len(img1) + 50,
            "max_time_seconds": 30,
        },
        "sources": {
            "website": {
                "url": f"{WEB}/", "sitemaps": [], "media_origins": [APPROVED_CDN], "discover_robots_sitemaps": False,
                "request_budget": 10, "page_budget": 2, "document_limit": 8192, "entry_limit": 5,
                "transfer_budget": 5, "byte_limit": 10_000, "source_byte_budget": 10_000, "time_budget_seconds": 30,
            },
            "instagram": {
                "url": IG, "media_origins": [APPROVED_CDN], "request_budget": 1, "page_budget": 1,
                "transfer_budget": 0, "byte_limit": 10_000, "time_budget_seconds": 10,
            },
            "facebook": {
                "url": FB, "discovery_mode": "open_source_cli", "download_authorized": False,  # blocked separately!
                "media_origins": [APPROVED_CDN], "include_albums": False,
                "cli_request_budget": 2, "request_budget": 5, "page_budget": 1, "entry_limit": 5,
                "transfer_budget": 3, "byte_limit": 10_000, "time_budget_seconds": 30,
            },
        },
    }
    manifest = collect_public_job(job_dir, job, cfg, social_runner=fb_runner)
    assert manifest["global_budget_usage"]["transfers_used"] == 1
    assert "global_budget" in manifest["sources"]["website"]["issues"]
    assert "download_authorization_required" in manifest["sources"]["facebook"]["issues"]
    assert not any("fb1.jpg" in u for u, _ in seen)

    # Existing M4 security holds remain strictly intact on Historical Mode manifests.
    assert manifest["collection_gate"] == "blocked_unverified_collection"
    assert all(a["rights_status"] == "unverified" for a in manifest["assets"])
    assert all(s["coverage_percentage"] is None for s in manifest["sources"].values())
    assert run_ai_curation(job_dir, job_id).status == "failed"
    assert build_delivery_plan(job_dir, job_id)["status"] in ("no_curation", "blocked_unverified_collection")
    assert request(worker.app, f"/jobs/{job_id}/delivery-result", "POST", "test-admin",
                   (b'{"authorization_confirmed":true}',))[0] == 409

    # Verify generated historical CLI flags parse cleanly against the real installed gallery-dl & instaloader parsers!
    import gallery_dl.option as gdl_opt

    gdl_fb = build_gallery_dl_historical_cmd(
        platform="facebook", url=FB, batch_limit=10, disable_loop=True, resolve=True,
    )
    gdl_alb = build_gallery_dl_historical_cmd(
        platform="facebook", url=FB + "photos_albums", batch_limit=5, disable_loop=True, resolve=False,
    )
    gdl_ig = build_gallery_dl_historical_cmd(
        platform="instagram", url=IG, batch_limit=10, cursor="REAL_TOKEN_123", resolve=True,
    )
    for cmd in (gdl_fb, gdl_alb, gdl_ig):
        _assert_no_bypass_flags(cmd)
        assert "--date-after" not in cmd
        parsed = gdl_opt.build_parser().parse_args(cmd[1:])
        assert parsed.urls == [cmd[-1]]

    il_cmd = build_instaloader_historical_cmd(
        url_kind="profile", target_hint="lilysclubuk", stage=tmp_path, cutoff=date(2025, 11, 24),
        reels_only=True, use_resume=True,
    )
    _assert_no_bypass_flags(il_cmd)
    assert "--no-posts" in il_cmd and "--reels" in il_cmd and "--resume-prefix" in il_cmd


def test_empty_unknown_date_and_source_byte_and_time_budgets(tmp_path):
    """Task 6: Empty history, unknown-date-only history (no premature cutoff), source_byte_budget, and GlobalBudget time cap."""
    # 1. Empty history -> public_inventory_unavailable / access_blocked, coverage_percentage=None.
    job_empty = tmp_path / ("9" * 32)
    job_empty.mkdir()
    cp_empty = CollectionCheckpoint(job_empty, {"job_id": job_empty.name, **WINDOW})
    inv_empty = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=10, request_budget=3, time_budget_seconds=30,
        runner=lambda _c, _s: (0, "[]"), historical_mode=True, batch_size=3, include_reels=False,
    )
    rep_empty = cp_empty.run_source("instagram", IG, inv_empty, lambda *_: None, page_budget=2, transfer_budget=0, byte_limit=1024)
    assert rep_empty["stop_reason"] == "public_inventory_unavailable"
    assert rep_empty["termination_reason"] == "access_blocked"
    assert rep_empty["coverage"] == "incomplete" and rep_empty["coverage_percentage"] is None

    # 2. Unknown-date-only multi-batch Facebook history never triggers premature cutoff & stays out of AI payload selection.
    def undated_fb(cmd: list[str], _stage: Path) -> tuple[int, str]:
        if "fbid=70000003" in cmd[-1]:
            return 0, json.dumps([
                [3, f"{APPROVED_CDN}/u3.jpg", {
                    "category": "facebook", "subcategory": "photos", "id": "70000003", "set_id": "pb.u",
                    "extension": "jpg", "next_photo_id": "",
                }],
            ])
        return 0, json.dumps([
            [3, f"{APPROVED_CDN}/u1.jpg", {
                "category": "facebook", "subcategory": "photos", "id": "70000001", "set_id": "pb.u",
                "extension": "jpg", "next_photo_id": "70000002",
            }],
            [3, f"{APPROVED_CDN}/u2.jpg", {
                "category": "facebook", "subcategory": "photos", "id": "70000002", "set_id": "pb.u",
                "extension": "jpg", "next_photo_id": "70000003",
            }],
        ])

    job_und = tmp_path / ("a" * 32)
    job_und.mkdir()
    cp_und = CollectionCheckpoint(job_und, {"job_id": job_und.name, **WINDOW})
    inv_und = OpenSourceSocialInventory(
        "facebook", FB, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=10, request_budget=5, time_budget_seconds=30,
        runner=undated_fb, historical_mode=True, batch_size=2, cutoff_consecutive_older=1, include_albums=False,
    )
    rep_und = cp_und.run_source("facebook", FB, inv_und, lambda *_: None, page_budget=3, transfer_budget=0, byte_limit=1024)
    assert rep_und["stop_reason"] == "listing_exhausted"
    assert rep_und["termination_reason"] == "exhausted"
    assert len(rep_und["assets"]) == 3
    assert len(rep_und["unknown_date_review"]) == 3
    assert select_ai_candidates(rep_und, job_und).candidates == []

    # 3. source_byte_budget and GlobalBudget(max_time_seconds) enforce hard limits cleanly.
    img = _jpeg_bytes((120, 80, 40))
    job_b = tmp_path / ("b" * 32)
    job_b.mkdir()
    cp_b = CollectionCheckpoint(job_b, {"job_id": job_b.name, **WINDOW})

    class TwoItems:
        def page(self, cursor):
            assert cursor is None
            return InventoryPage((
                SourceItem("p1", WEB, (Attachment("a1", f"{APPROVED_CDN}/1.jpg", "image"), Attachment("a2", f"{APPROVED_CDN}/2.jpg", "image"))),
            ))

    now = [100.0]
    gb = GlobalBudget(max_time_seconds=10, clock=lambda: now[0])
    rep_b = cp_b.run_source(
        "website", WEB, TwoItems(),
        lambda _u, _o, _r: Download(len(img), [img], mime="image/jpeg"),
        page_budget=1, transfer_budget=5, byte_limit=len(img) + 10,
        source_byte_budget=len(img) + 10, global_budget=gb, validate_content_magic=True,
    )
    assert "byte_budget" in rep_b["issues"]
    assert sum(1 for a in rep_b["assets"] if a["status"] == "verified") == 1
    assert rep_b["coverage"] == "incomplete"

    now[0] += 20.0
    assert gb.can_fetch_page() == "time_budget"
    assert gb.can_transfer() == "time_budget"


def test_cutoff_resets_on_later_in_window_post_ig_error6_fallback_and_album_cutoff_does_not_mask_timeline(tmp_path):
    """Stage 3F Task 2:
    1. Within a single batch, [old1, old2, old3, RECENT_IN_WINDOW] must reset cutoff evaluation and continue!
    2. On Instagram Historical Mode, gallery_dl_error_6 must still trigger single bounded Instaloader fallback.
    3. An old Facebook album reaching cutoff must NOT mask unsupported_pagination on the main photos timeline.
    """
    # 1. Single-batch out-of-order [old1, old2, old3, RECENT] resets cutoff and fetches Batch 2!
    set_id = "pb.outoforder"
    fb_calls: list[str] = []

    def out_of_order_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        fb_calls.append(cmd[-1])
        if len(fb_calls) == 1:
            return 0, json.dumps([
                [3, f"{APPROVED_CDN}/o1.jpg", {"category": "facebook", "subcategory": "photos", "id": "80000001", "set_id": set_id, "date": "2025-01-01 10:00:00", "extension": "jpg", "next_photo_id": "80000002"}],
                [3, f"{APPROVED_CDN}/o2.jpg", {"category": "facebook", "subcategory": "photos", "id": "80000002", "set_id": set_id, "date": "2025-01-02 10:00:00", "extension": "jpg", "next_photo_id": "80000003"}],
                [3, f"{APPROVED_CDN}/o3.jpg", {"category": "facebook", "subcategory": "photos", "id": "80000003", "set_id": set_id, "date": "2025-01-03 10:00:00", "extension": "jpg", "next_photo_id": "80000004"}],
                [3, f"{APPROVED_CDN}/recent-reset.jpg", {"category": "facebook", "subcategory": "photos", "id": "80000004", "set_id": set_id, "date": "2026-10-15 10:00:00", "extension": "jpg", "next_photo_id": "80000005"}],
            ])
        return 0, json.dumps([
            [3, f"{APPROVED_CDN}/batch2-eligible.jpg", {"category": "facebook", "subcategory": "photos", "id": "80000005", "set_id": set_id, "date": "2026-09-01 10:00:00", "extension": "jpg", "next_photo_id": ""}],
        ])

    d1 = tmp_path / ("c" * 32)
    d1.mkdir()
    cp1 = CollectionCheckpoint(d1, {"job_id": d1.name, **WINDOW})
    inv1 = OpenSourceSocialInventory(
        "facebook", FB, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=10, request_budget=4, time_budget_seconds=30, runner=out_of_order_runner,
        historical_mode=True, batch_size=4, cutoff_consecutive_older=3, include_albums=False,
    )
    rep1 = cp1.run_source("facebook", FB, inv1, lambda *_: None, page_budget=3, transfer_budget=0, byte_limit=1024)
    assert len(fb_calls) == 2 and "fbid=80000005" in fb_calls[1]
    assert rep1["stop_reason"] == "listing_exhausted"
    by_nat = {a["native_id"]: a for a in rep1["assets"]}
    assert by_nat["80000004"]["date_eligibility"] == "in_window"
    assert by_nat["80000005"]["date_eligibility"] == "in_window"

    # 2. Instagram Historical Mode: gallery_dl_error_6 triggers single bounded Instaloader fallback.
    ig_tools: list[str] = []

    def ig_err6_runner(cmd: list[str], stage: Path) -> tuple[int, str]:
        ig_tools.append(cmd[0])
        if cmd[0] == "gallery-dl":
            return 0, json.dumps([[6, cmd[-1], {"message": "Instagram feed endpoint gated"}]])
        sidecar = {
            "instaloader": {"version": "4.16", "node_type": "Post"},
            "node": {
                "id": "ig-err6-ok", "shortcode": "IgErr6Ok", "is_video": False,
                "display_url": f"{APPROVED_CDN}/ig-err6.jpg",
                "taken_at_timestamp": int(datetime(2026, 11, 5, 12, 0, tzinfo=UTC).timestamp()),
            },
        }
        (stage / "2026-11-05_12-00-00_UTC.json").write_text(json.dumps(sidecar), encoding="utf-8")
        return 0, ""

    d2 = tmp_path / ("d" * 32)
    d2.mkdir()
    cp2 = CollectionCheckpoint(d2, {"job_id": d2.name, **WINDOW})
    inv2 = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=10, request_budget=4, time_budget_seconds=30, runner=ig_err6_runner,
        historical_mode=True, batch_size=5, include_reels=False,
    )
    rep2 = cp2.run_source("instagram", IG, inv2, lambda *_: None, page_budget=2, transfer_budget=0, byte_limit=1024)
    assert ig_tools == ["gallery-dl", "instaloader"]
    assert len(rep2["assets"]) == 1 and rep2["assets"][0]["native_id"] == "ig-err6-ok"

    # 3. Facebook timeline hitting unsupported_pagination MUST NOT be masked by an old album hitting cutoff.
    def masked_album_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        target = cmd[-1]
        if target == FB:
            # Hits batch_size=1 with NO next_photo_id -> timeline unsupported_continuation!
            return 0, json.dumps([
                [3, f"{APPROVED_CDN}/tl1.jpg", {"category": "facebook", "subcategory": "photos", "id": "81000001", "set_id": "pb.tl", "date": "2026-11-10 10:00:00", "extension": "jpg"}],
            ])
        if target.endswith("/photos_albums"):
            return 0, json.dumps([[6, "https://www.facebook.com/media/set/?set=a.oldalb", {"set_id": "a.oldalb", "title": "Old"}]])
        return 0, json.dumps([
            [3, f"{APPROVED_CDN}/oa1.jpg", {"category": "facebook", "subcategory": "set", "id": "81000002", "set_id": "a.oldalb", "date": "2023-01-01 10:00:00", "extension": "jpg"}],
        ])

    d3 = tmp_path / ("e" * 32)
    d3.mkdir()
    cp3 = CollectionCheckpoint(d3, {"job_id": d3.name, **WINDOW})
    inv3 = OpenSourceSocialInventory(
        "facebook", FB, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=10, request_budget=5, time_budget_seconds=30, runner=masked_album_runner,
        historical_mode=True, batch_size=1, cutoff_consecutive_older=1, include_albums=True,
    )
    rep3 = cp3.run_source("facebook", FB, inv3, lambda *_: None, page_budget=4, transfer_budget=0, byte_limit=1024)
    assert rep3["stop_reason"] == "unsupported_pagination"
    assert rep3["termination_reason"] == "unsupported_pagination"


def test_consolidated_historical_mode_through_m4_approval_start_ai_rights_ledger_and_private_drive(
    worker, tmp_path, monkeypatch,
):
    """Stage 3F Task 1 & 3: End-to-end consolidation test linking Stage 3E Historical Mode
    (multi-page website + multi-batch Facebook cutoff + preprocessing) into Workstream B M4
    (production activation, Admin panel outcomes/frame counts, independent approval, Start AI,
    fake non-mock Groq, rights ledger rejecting expired/out-of-window assets, and private Drive)."""
    from contextlib import closing
    import sqlite3
    import uuid
    from app import collection_approval as ledger, named_auth
    from app.ai.provider import GROQ_ACCEPTANCE_ENDPOINT, OpenAICompatibleProvider
    from tests.test_m4_real_pipeline import FAKE_GROQ_KEY, _build_fake_groq_transport, _jpeg_bytes as _m4_jpeg, _mp4_bytes
    from tests.test_named_auth import KEY as ASSERTION_KEY, envelope

    monkeypatch.setenv("NAMED_ASSERTION_SECRET", ASSERTION_KEY)
    monkeypatch.setenv("M4_COLLECTION_APPROVAL_ENABLED", "1")
    monkeypatch.setattr(worker.app.state, "offline_approval_qa", False, raising=False)
    system_db = worker.DATA_DIR / "_system" / "jobs.sqlite"
    system_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(system_db)) as copied, worker.connect() as original:
        original.backup(copied)
        ledger.migrate(copied)
    monkeypatch.setattr(worker, "DB", system_db)

    named_auth.bootstrap(system_db, "hist-admin-a", "pass-a", verified_operator="stage3f-test")

    def invoke(action: str, body: dict, session: str = ""):
        raw, headers = envelope(action, body, session)
        st, _, data = request(worker.app, "/named/" + action, "POST", "test-dashboard", (raw,), headers)
        return st, json.loads(data)

    sess_a = invoke("login", {"name": "hist-admin-a", "password": "pass-a"})[1]["session"]
    invoke("create", {"name": "hist-admin-b", "password": "pass-b"}, sess_a)
    sess_b = invoke("login", {"name": "hist-admin-b", "password": "pass-b"})[1]["session"]

    web_jpg = _m4_jpeg(640, 480, "bars")
    fb_jpg = _m4_jpeg(540, 540, "checker")
    clip_mp4 = _mp4_bytes(tmp_path)
    seen: list[tuple[str, dict]] = []
    ld = json.dumps({
        "@type": "BlogPosting",
        "image": {"@type": "ImageObject", "contentUrl": f"{APPROVED_CDN}/hist-web.jpg", "datePublished": "2026-10-12T09:00:00Z"},
        "video": {"@type": "VideoObject", "contentUrl": f"{APPROVED_CDN}/hist-clip.mp4", "datePublished": "2026-10-14T09:00:00Z"},
    })
    responses = {
        f"{WEB}/robots.txt": (200, {}, f"User-agent: *\nAllow: /\nSitemap: {WEB}/sm.xml\n"),
        f"{WEB}/": (200, {"Content-Type": "text/html"}, '<html><body><a href="/gallery">Gallery</a></body></html>'),
        f"{WEB}/sm.xml": (200, {"Content-Type": "application/xml"}, f"<urlset><url><loc>{WEB}/gallery</loc></url></urlset>"),
        f"{WEB}/gallery": (200, {"Content-Type": "text/html"}, f'<script type="application/ld+json">{ld}</script><img src="{APPROVED_CDN}/hist-web.jpg"><video><source src="{APPROVED_CDN}/hist-clip.mp4"></video>'),
        f"{APPROVED_CDN}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{APPROVED_CDN}/hist-web.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(web_jpg))}, web_jpg),
        f"{APPROVED_CDN}/hist-clip.mp4": (200, {"Content-Type": "video/mp4", "Content-Length": str(len(clip_mp4))}, clip_mp4),
        f"{APPROVED_CDN}/hist-fb.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(fb_jpg))}, fb_jpg),
        f"{APPROVED_CDN}/hist-fb-expired.jpg": (410, {}, b"Gone"),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses, seen))

    def social_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        if "facebook" in cmd:
            if "fbid=90000003" in cmd[-1]:
                return 0, json.dumps([
                    [3, f"{APPROVED_CDN}/old-skip.jpg", {"category": "facebook", "subcategory": "photos", "id": "90000003", "set_id": "pb.h", "date": "2024-01-01 10:00:00", "extension": "jpg", "next_photo_id": "90000004"}],
                ])
            return 0, json.dumps([
                [3, f"{APPROVED_CDN}/hist-fb.jpg", {"category": "facebook", "subcategory": "photos", "id": "90000001", "set_id": "pb.h", "date": "2026-10-20 10:00:00", "extension": "jpg", "next_photo_id": "90000002"}],
                [3, f"{APPROVED_CDN}/hist-fb-expired.jpg", {"category": "facebook", "subcategory": "photos", "id": "90000002", "set_id": "pb.h", "date": "2026-10-19 10:00:00", "extension": "jpg", "next_photo_id": "90000003"}],
            ])
        return 0, "[]"

    cfg = {
        "collection_profile": "historical",
        "run_preprocessing": True,
        "global_budgets": {"max_pages": 10, "max_items": 20, "max_transfers": 10, "max_bytes": 5_000_000, "max_time_seconds": 60},
        "sources": {
            "website": {"url": f"{WEB}/", "sitemaps": [], "media_origins": [APPROVED_CDN], "request_budget": 15, "page_budget": 4, "document_limit": 16384, "entry_limit": 10, "transfer_budget": 5, "byte_limit": 2_000_000, "time_budget_seconds": 30},
            "instagram": {"url": IG, "discovery_mode": "open_source_cli", "download_authorized": False, "media_origins": [APPROVED_CDN], "include_reels": False, "cli_request_budget": 2, "request_budget": 2, "page_budget": 1, "entry_limit": 5, "transfer_budget": 0, "byte_limit": 1_000_000, "time_budget_seconds": 20},
            "facebook": {"url": FB, "discovery_mode": "open_source_cli", "download_authorized": True, "media_origins": [APPROVED_CDN], "include_albums": False, "cutoff_consecutive_older": 1, "cli_request_budget": 4, "request_budget": 15, "page_budget": 3, "entry_limit": 10, "batch_size": 2, "transfer_budget": 5, "byte_limit": 2_000_000, "time_budget_seconds": 30},
        },
    }
    cfg_path = tmp_path / "stage3f-cfg.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("PUBLIC_COLLECTION_CHECKPOINTS", "1")
    monkeypatch.setenv("PUBLIC_COLLECTION_CONFIG", str(cfg_path))
    monkeypatch.setattr(worker, "frozen_window", lambda _: WINDOW)
    from app import public_collection
    real_collect = public_collection.collect_public_job
    monkeypatch.setattr(
        public_collection, "collect_public_job",
        lambda job_dir, job, config, **kw: real_collect(job_dir, job, config, social_runner=social_runner),
    )

    ticket = invoke("intake", {
        "website_url": f"{WEB}/", "instagram_url": IG, "facebook_url": FB,
        "social_permission_confirmed": True, "run_ai_curation": False, "idempotency_key": f"s3f-{uuid.uuid4().hex[:8]}",
    }, sess_a)[1]["idempotency_key"]
    scheduled: list[tuple] = []
    monkeypatch.setattr(worker.executor, "submit", lambda *args: scheduled.append(args))
    st, _, raw = request(worker.app, "/jobs", "POST", "test-admin", (json.dumps({
        "website_url": f"{WEB}/", "instagram_url": IG, "facebook_url": FB,
        "social_permission_confirmed": True, "run_ai_curation": False, "idempotency_key": ticket,
    }).encode(),))
    assert st == 202
    job_id = json.loads(raw)["job_id"]
    fn, *args = scheduled.pop()
    fn(*args)

    # Admin panel reflects Historical Mode termination reasons cleanly and excludes derived video frames.
    st, panel = invoke("collection-panel", {"job_id": job_id}, sess_b)
    assert st == 200, panel
    fb_panel = next(r for r in panel["sources"] if r["source"] == "facebook")
    web_panel = next(r for r in panel["sources"] if r["source"] == "website")
    assert "cutoff_reached" in fb_panel["outcomes"] and "cutoff_reached_with_evidence" in fb_panel["outcomes"]
    assert web_panel["discovered"] == 2 and web_panel["downloaded"] == 2  # derived video frames excluded!
    assert fb_panel["discovered"] == 3 and fb_panel["downloaded"] == 1  # 1 verified, 1 expired (410), 1 out_of_window skipped!

    manifest = json.loads((worker.DATA_DIR / job_id / "manifest.json").read_text(encoding="utf-8"))
    by_url = {a.get("source_url"): a for a in manifest["assets"] if a.get("type") != "frame"}
    eligible = [by_url[f"{APPROVED_CDN}/hist-web.jpg"], by_url[f"{APPROVED_CDN}/hist-clip.mp4"], by_url[f"{APPROVED_CDN}/hist-fb.jpg"]]
    expired_fb = by_url[f"{APPROVED_CDN}/hist-fb-expired.jpg"]
    assert set(panel["eligible_assets"]) == {a["asset_id"] for a in eligible}

    # Independent approve -> Start AI -> fake non-mock Groq execution -> rights ledger -> private delivery!
    approved = invoke("collection-approve", {"job_id": job_id, "if_match": panel["etag"], "request_key": "s3f-app", "reason": "Reviewed historical manifest"}, sess_b)[1]
    started = invoke("collection-start", {"job_id": job_id, "if_match": approved["etag"], "request_key": "s3f-start"}, sess_a)[1]

    groq_calls: list[dict] = []
    monkeypatch.setenv("AI_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AI_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    monkeypatch.setenv("AI_ENDPOINT", GROQ_ACCEPTANCE_ENDPOINT)
    monkeypatch.setenv("AI_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("AI_MIN_REQUEST_SPACING_S", "0")
    monkeypatch.setattr(
        worker.app.state, "ai_provider_factory",
        lambda c: OpenAICompatibleProvider(c, sleep=lambda _: None, transport=_build_fake_groq_transport(groq_calls)),
        raising=False,
    )
    consent = {a["asset_id"]: {"source_url": a["source_url"], "sha256": a["sha256"]} for a in eligible}
    st, exec_res = invoke("collection-execute", {"job_id": job_id, "if_match": started["etag"], "processing_consent": consent}, sess_b)
    assert st == 200 and exec_res["status"] == "completed" and exec_res["mock"] is False

    # Rights ledger rejects expired (410) Facebook asset (409) and clears verified Facebook photo -> delivery ready!
    assert invoke("collection-rights-decide", {
        "job_id": job_id, "asset_id": expired_fb["asset_id"], "decision": "verified",
        "reason": "Should fail", "evidence_ref": "ev-bad", "if_match": started["etag"], "request_key": "s3f-r-bad",
    }, sess_b)[0] == 409
    fb_ok = by_url[f"{APPROVED_CDN}/hist-fb.jpg"]
    assert invoke("collection-rights-decide", {
        "job_id": job_id, "asset_id": fb_ok["asset_id"], "decision": "verified",
        "reason": "Client confirmed", "evidence_ref": "ev-fb-ok", "if_match": started["etag"], "request_key": "s3f-r-ok",
    }, sess_b)[0] == 200
    st, _, plan_raw = request(worker.app, f"/jobs/{job_id}/delivery-plan", "GET", "test-admin")
    plan = json.loads(plan_raw)
    assert st == 200 and plan["status"] == "ready" and plan["visibility"] == "private" and plan["public_sharing"] is False
    assert [a["asset_id"] for a in plan["assets"]] == [fb_ok["asset_id"]]
    assert plan["assets"][0]["filename"].endswith(".jpg")


@pytest.mark.parametrize(
    ("label", "code", "output", "expected_class", "expected_stop", "expected_term"),
    [
        (
            "ig_login_redirect_with_code6",
            0,
            json.dumps([
                [6, IG, {}],
                [-1, {"error": "AbortExtraction", "message": "HTTP redirect to login page (https://www.instagram.com/accounts/login/)"}],
            ]),
            "restricted",
            "restricted",
            "access_blocked",
        ),
        (
            "ig_home_redirect_auth_with_code6",
            0,
            json.dumps([
                [6, IG, {}],
                [-1, {"error": "AbortExtraction", "message": "HTTP redirect to home page (https://www.instagram.com/)"}],
            ]),
            "restricted",
            "restricted",
            "access_blocked",
        ),
        (
            "ig_home_redirect_stderr_context",
            1,
            "[instagram][error] AbortExtraction: HTTP redirect to home page",
            "restricted",
            "restricted",
            "access_blocked",
        ),
        (
            "ig_challenge_redirect_with_code6",
            0,
            json.dumps([
                [6, IG, {}],
                [-1, {"error": "AbortExtraction", "message": "HTTP redirect to challenge page (https://www.instagram.com/challenge/)"}],
            ]),
            "restricted",
            "restricted",
            "access_blocked",
        ),
        (
            "ig_challenge_error_envelope",
            0,
            json.dumps([
                [6, IG, {}],
                [-1, {"error": "ChallengeError", "message": "Cloudflare challenge (403 Forbidden) for 'https://www.instagram.com/'"}],
            ]),
            "restricted",
            "restricted",
            "access_blocked",
        ),
        (
            "fb_auth_required_must_be_logged_in_with_code6",
            0,
            json.dumps([
                [6, FB, {}],
                [-1, {"error": "AuthRequired", "message": "You must be logged in to continue viewing images.\nYou can use this URL..."}],
            ]),
            "restricted",
            "restricted",
            "access_blocked",
        ),
        (
            "fb_auth_required_profile_cookies",
            0,
            json.dumps([
                [-1, {"error": "AuthRequired", "message": "authenticated cookies needed to access this profile ('This content isn't available right now')"}],
            ]),
            "restricted",
            "restricted",
            "access_blocked",
        ),
        (
            "plain_you_must_be_logged_in",
            1,
            "You must be logged in to continue viewing images.",
            "restricted",
            "restricted",
            "access_blocked",
        ),
        (
            "fb_temporarily_blocked_comet_with_code6",
            0,
            json.dumps([
                [6, FB, {}],
                [-1, {"error": "AbortExtraction", "message": "You've been temporarily blocked from viewing images.\nPlease try using a different account, using a VPN or waiting before you retry."}],
            ]),
            "rate_limited",
            "rate_limited",
            "rate_limited",
        ),
        (
            "http_401_empty_reason_with_code6",
            0,
            json.dumps([
                [6, IG, {}],
                [-1, {"error": "HttpError", "message": "'401 ' for 'https://www.instagram.com/api/v1/users/web_profile_info/'"}],
            ]),
            "restricted",
            "restricted",
            "access_blocked",
        ),
        (
            "http_403_empty_reason_with_code6",
            0,
            json.dumps([
                [6, IG, {}],
                [-1, {"error": "HttpError", "message": "'403 ' for 'https://www.instagram.com/api/v1/users/web_profile_info/'"}],
            ]),
            "restricted",
            "restricted",
            "access_blocked",
        ),
        (
            "http_429_empty_reason_with_code6",
            0,
            json.dumps([
                [6, IG, {}],
                [-1, {"error": "HttpError", "message": "'429 ' for 'https://www.instagram.com/api/v1/users/web_profile_info/'"}],
            ]),
            "rate_limited",
            "rate_limited",
            "rate_limited",
        ),
        (
            "ig_posts_are_private_warning",
            0,
            "[instagram][warning] lilysclubuk's posts are private\n[]",
            "restricted",
            "restricted",
            "access_blocked",
        ),
        (
            "ig_checkpoint_required_with_code6",
            0,
            json.dumps([[6, IG, {"message": "checkpoint_required"}]]),
            "restricted",
            "restricted",
            "access_blocked",
        ),
    ],
)
def test_stage3g1_gallery_dl_13220_restrictions_never_trigger_instaloader_fallback(
    tmp_path, label, code, output, expected_class, expected_stop, expected_term,
):
    """Stage 3G.1 Task 1: All real gallery-dl 1.32.20 access restrictions and temporary blocks
    must hard-stop without ever triggering Instaloader fallback, even alongside gallery-dl code 6."""
    from app.social_discovery import _extract_diagnostic_text, classify_cli_outcome
    from app.social_normalizers import gallery_error_codes, parse_gallery_json

    records = parse_gallery_json(output)
    diag = _extract_diagnostic_text("gallery-dl", output, records)
    classified = classify_cli_outcome("gallery-dl", code, output, gallery_error_codes(records), diagnostic_text=diag)
    assert classified == expected_class, (label, classified, diag)

    for mode_idx, historical_mode in enumerate((True, False)):
        calls: list[str] = []

        def runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
            calls.append(cmd[0])
            return code, output

        job_dir = tmp_path / f"{abs(hash((label, mode_idx))) % 10**32:032x}"
        job_dir.mkdir(exist_ok=True)
        cp = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
        inv = OpenSourceSocialInventory(
            "instagram",
            IG,
            permission_confirmed=True,
            window_start_utc=WINDOW["window_start_utc"],
            item_limit=10,
            request_budget=4,
            time_budget_seconds=30,
            allow_fallbacks=True,
            runner=runner,
            historical_mode=historical_mode,
            batch_size=5,
            include_reels=historical_mode,
        )
        rep = cp.run_source("instagram", IG, inv, lambda *_: None, page_budget=3, transfer_budget=0, byte_limit=1024)
        assert calls == ["gallery-dl"], (label, historical_mode, calls)
        assert rep["stop_reason"] == expected_stop, (label, historical_mode, rep["stop_reason"])
        assert rep["termination_reason"] == expected_term, (label, historical_mode, rep["termination_reason"])
        assert rep["coverage"] == "incomplete" and rep["coverage_percentage"] is None


def test_stage3g1_non_restriction_fallbacks_preserved_and_non_auth_redirects_or_500_not_misclassified(tmp_path):
    """Stage 3G.1 Task 1 & 2:
    1. Non-auth generic home redirect and HTTP 500 with '/403/' in URL are NOT misclassified as restricted.
    2. Ambiguous non-restriction gallery-dl failures (user_lookup_not_found, plain code 6, extractor_broken)
       still trigger single bounded Instaloader fallback.
    3. Both Facebook and Instagram Historical Mode generated commands omit --date-after while preserving
       frozen 365-day UTC publication-evidence filtering and honest incomplete coverage."""
    from app.social_discovery import _extract_diagnostic_text, classify_cli_outcome
    from app.social_normalizers import gallery_error_codes, parse_gallery_json

    # Non-auth generic home redirect must NOT be misclassified as restricted.
    generic_home = json.dumps([
        [-1, {"error": "AbortExtraction", "message": "HTTP redirect to home page (https://example.org/)"}],
    ])
    recs_gh = parse_gallery_json(generic_home)
    assert classify_cli_outcome(
        "gallery-dl", 0, generic_home, gallery_error_codes(recs_gh),
        diagnostic_text=_extract_diagnostic_text("gallery-dl", generic_home, recs_gh),
    ) == "source_stopped"

    # HTTP 500 whose URL path happens to contain '/403/' must NOT false-positive as 403 restricted.
    http_500 = json.dumps([
        [-1, {"error": "HttpError", "message": "'500 Internal Server Error' for 'https://www.instagram.com/p/403/'"}],
    ])
    recs_500 = parse_gallery_json(http_500)
    assert classify_cli_outcome(
        "gallery-dl", 0, http_500, gallery_error_codes(recs_500),
        diagnostic_text=_extract_diagnostic_text("gallery-dl", http_500, recs_500),
    ) == "source_stopped"

    # Ambiguous non-restriction failures STILL trigger Instaloader fallback in Historical Mode.
    for idx, (gdl_code, gdl_out, expected_gdl_outcome) in enumerate((
        (0, json.dumps([[-1, {"error": "NotFoundError", "message": "Requested user could not be found"}]]), "user_lookup_not_found"),
        (0, json.dumps([[6, IG, {"message": "Instagram feed endpoint gated"}]]), "gallery_dl_error_6"),
        (0, json.dumps([[-1, {"error": "UnboundLocalError", "message": "cannot access local variable"}]]), "extractor_broken"),
    )):
        tools_seen: list[str] = []
        gdl_cmds: list[list[str]] = []

        def ok_fallback_runner(cmd: list[str], stage: Path, c=gdl_code, o=gdl_out, i=idx) -> tuple[int, str]:
            tools_seen.append(cmd[0])
            if cmd[0] == "gallery-dl":
                gdl_cmds.append(cmd)
                return c, o
            sidecar = {
                "instaloader": {"version": "4.16", "node_type": "Post"},
                "node": {
                    "id": f"ig-ambig-{i}",
                    "shortcode": f"IgAmbig{i}",
                    "is_video": False,
                    "display_url": f"{APPROVED_CDN}/ig-ambig-{i}.jpg",
                    "taken_at_timestamp": int(datetime(2026, 11, 10, 12, 0, tzinfo=UTC).timestamp()),
                },
            }
            (stage / f"2026-11-10_12-00-0{i}_UTC.json").write_text(json.dumps(sidecar), encoding="utf-8")
            return 0, ""

        jd = tmp_path / f"{(idx + 10):032x}"
        jd.mkdir()
        cp = CollectionCheckpoint(jd, {"job_id": jd.name, **WINDOW})
        inv = OpenSourceSocialInventory(
            "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
            item_limit=10, request_budget=4, time_budget_seconds=30, allow_fallbacks=True,
            runner=ok_fallback_runner, historical_mode=True, batch_size=5, include_reels=False,
        )
        rep = cp.run_source("instagram", IG, inv, lambda *_: None, page_budget=2, transfer_budget=0, byte_limit=1024)
        assert tools_seen == ["gallery-dl", "instaloader"]
        assert inv.tools_executed[0]["outcome"] == expected_gdl_outcome
        assert len(gdl_cmds) == 1 and "--date-after" not in gdl_cmds[0]
        assert len(rep["assets"]) == 1 and rep["assets"][0]["date_eligibility"] == "in_window"

    # Explicit Task 2 verification: Facebook Historical Mode commands NEVER pass --date-after
    # (even when photos_albums and &setextract continuation execute), and cutoff_reached keeps coverage incomplete.
    fb_cmds: list[list[str]] = []

    def fb_cmd_check(cmd: list[str], _stage: Path) -> tuple[int, str]:
        fb_cmds.append(cmd)
        assert "--date-after" not in cmd
        return 0, json.dumps([
            [3, f"{APPROVED_CDN}/fb-cut-1.jpg", {"category": "facebook", "subcategory": "photos", "id": "95000001", "set_id": "pb.c", "date": "2024-01-01 10:00:00", "extension": "jpg", "next_photo_id": "95000002"}],
            [3, f"{APPROVED_CDN}/fb-cut-2.jpg", {"category": "facebook", "subcategory": "photos", "id": "95000002", "set_id": "pb.c", "date": "2024-01-02 10:00:00", "extension": "jpg", "next_photo_id": "95000003"}],
            [3, f"{APPROVED_CDN}/fb-cut-3.jpg", {"category": "facebook", "subcategory": "photos", "id": "95000003", "set_id": "pb.c", "date": "2024-01-03 10:00:00", "extension": "jpg", "next_photo_id": "95000004"}],
        ])

    jd_fb = tmp_path / ("f" * 30 + "99")
    jd_fb.mkdir()
    cp_fb = CollectionCheckpoint(jd_fb, {"job_id": jd_fb.name, **WINDOW})
    inv_fb = OpenSourceSocialInventory(
        "facebook", FB, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=10, request_budget=4, time_budget_seconds=30, runner=fb_cmd_check,
        historical_mode=True, batch_size=5, cutoff_consecutive_older=3, include_albums=False,
    )
    rep_fb = cp_fb.run_source("facebook", FB, inv_fb, lambda *_: None, page_budget=3, transfer_budget=0, byte_limit=1024)
    assert len(fb_cmds) == 1 and "--date-after" not in fb_cmds[0]
    assert rep_fb["stop_reason"] == "cutoff_reached"
    assert rep_fb["termination_reason"] == "cutoff_reached_with_evidence"
    assert rep_fb["coverage"] == "incomplete" and rep_fb["coverage_percentage"] is None
    assert all(a["date_eligibility"] == "out_of_window" for a in rep_fb["assets"])


def test_stage3h_website_wordpress_thumbnail_grouping_cross_page_dedupe_provenance_and_entry_budget(tmp_path):
    """Stage 3H Task 1: Sanitized real Website pilot fixture replay proving:
    1. WordPress -WxH responsive thumbnails collapse into canonical master with alternate_urls preserved.
    2. Real full-size (`photo.jpg`) vs `-scaled` (`photo-scaled.jpg`) remain distinct when both appear.
    3. Cross-page duplicates do NOT create duplicate asset rows or consume per-page entry_limit,
       while merging all source_pages and alternate_urls without losing attribution.
    4. WordPress /wp-content/uploads/YYYY/MM/ directory paths NEVER fabricate publication dates."""
    site = "https://www.lilys-fixture.test"
    # Mirror real Lily's Club Run A patterns: -WxH pairs, -scaled + -WxH pairs, multiple -WxH only,
    # explicit full-size + -scaled coexistence, shared logo across pages, and /services vs /services2!
    pages = {
        f"{site}/": (
            f"<html><body>"
            f'<a href="/services">Services</a><a href="/services2">Services 2</a><a href="/about-us">About</a>'
            f'<img src="{site}/wp-content/uploads/2025/09/Logo-1024x278.png" '
            f'srcset="{site}/wp-content/uploads/2025/09/Logo-1024x278.png 1024w, {site}/wp-content/uploads/2025/09/Logo.png 2000w">'
            f'<video src="{site}/wp-content/uploads/2025/10/Bath-Video.mp4"></video>'
            f"</body></html>"
        ),
        f"{site}/services": (
            f"<html><body>"
            f'<img src="{site}/wp-content/uploads/2025/09/Logo.png">'
            f'<img src="{site}/wp-content/uploads/2025/09/Blueberry-Facial-768x1024.jpg" '
            f'srcset="{site}/wp-content/uploads/2025/09/Blueberry-Facial-768x1024.jpg 768w, {site}/wp-content/uploads/2025/09/Blueberry-Facial-scaled.jpg 1920w">'
            f'<img src="{site}/wp-content/uploads/2025/09/Teeth-Brushing-1024x576.jpg" '
            f'srcset="{site}/wp-content/uploads/2025/09/Teeth-Brushing-1024x576.jpg 1024w, {site}/wp-content/uploads/2025/09/Teeth-Brushing.jpg 1600w">'
            f'<img src="{site}/wp-content/uploads/2025/09/Both-Exist.jpg">'
            f'<img src="{site}/wp-content/uploads/2025/09/Both-Exist-scaled.jpg">'
            f'<img src="{site}/wp-content/uploads/2025/09/Both-Exist-768x1024.jpg">'
            f"</body></html>"
        ),
        f"{site}/services2": (
            f"<html><body>"
            # Repeats Logo, Blueberry-Facial, Teeth-Brushing + adds 2 genuinely new photos; with entry_limit=4,
            # cross-page duplicates & -WxH thumbs MUST NOT exhaust entry_limit or trigger attachment_cap!
            f'<img src="{site}/wp-content/uploads/2025/09/Logo-1024x278.png">'
            f'<img src="{site}/wp-content/uploads/2025/09/Blueberry-Facial-768x1024.jpg">'
            f'<img src="{site}/wp-content/uploads/2025/09/Teeth-Brushing-1024x576.jpg">'
            f'<img src="{site}/wp-content/uploads/2025/10/Fresh-Groom-1-768x1024.jpeg" '
            f'srcset="{site}/wp-content/uploads/2025/10/Fresh-Groom-1-768x1024.jpeg 768w, {site}/wp-content/uploads/2025/10/Fresh-Groom-1.jpeg 1500w">'
            f'<img src="{site}/wp-content/uploads/2025/10/Fresh-Groom-2-768x1024.jpeg" '
            f'srcset="{site}/wp-content/uploads/2025/10/Fresh-Groom-2-768x1024.jpeg 768w, {site}/wp-content/uploads/2025/10/Fresh-Groom-2.jpeg 1500w">'
            f"</body></html>"
        ),
        f"{site}/about-us": (
            f"<html><body>"
            # Multiple -WxH only (no unscaled original): selects largest area 2048x1536, keeps 1024x768 as alternate!
            f'<img src="{site}/wp-content/uploads/2024/09/Pup-1024x768.jpeg" '
            f'srcset="{site}/wp-content/uploads/2024/09/Pup-1024x768.jpeg 1024w, {site}/wp-content/uploads/2024/09/Pup-2048x1536.jpeg 2048w">'
            f"</body></html>"
        ),
    }
    sitemap = (
        "<urlset>"
        f"<url><loc>{site}/services</loc></url>"
        f"<url><loc>{site}/services2</loc></url>"
        f"<url><loc>{site}/about-us</loc></url>"
        "</urlset>"
    )

    def fake_fetch(url: str) -> tuple[str, bytes]:
        if url.endswith("/sitemap.xml"):
            return url, sitemap.encode("utf-8")
        return url, pages[url].encode("utf-8")

    # 1. Compare raw non-deduped count (Pilot Mode) vs Stage 3H Historical Mode deduped count
    jd_hist = tmp_path / ("a1" * 16)
    jd_hist.mkdir()
    cp = CollectionCheckpoint(jd_hist, {"job_id": jd_hist.name, **WINDOW})
    inv = WebsiteInventory(
        f"{site}/",
        sitemaps=[],
        robots_sitemaps=[f"{site}/sitemap.xml"],
        fetch=fake_fetch,
        permitted=lambda u: u.startswith(site),
        media_permitted=lambda u: u.startswith(site),
        document_limit=65536,
        entry_limit=4,  # Tight limit: would trigger attachment_cap without Stage 3H dedupe!
        historical_mode=True,
    )
    rep = cp.run_source("website", f"{site}/", inv, lambda *_: None, page_budget=10, transfer_budget=0, byte_limit=1024)
    assert "attachment_cap" not in rep["issues"], rep["issues"]
    by_url = {a["url"]: a for a in rep["assets"]}

    # Expected 9 canonical unique assets (down from 19 raw URL occurrences across pages!):
    # Logo.png, Bath-Video.mp4, Blueberry-Facial-scaled.jpg, Teeth-Brushing.jpg,
    # Both-Exist.jpg, Both-Exist-scaled.jpg (both preserved!), Fresh-Groom-1.jpeg, Fresh-Groom-2.jpeg, Pup-2048x1536.jpeg
    expected_urls = {
        f"{site}/wp-content/uploads/2025/09/Logo.png",
        f"{site}/wp-content/uploads/2025/10/Bath-Video.mp4",
        f"{site}/wp-content/uploads/2025/09/Blueberry-Facial-scaled.jpg",
        f"{site}/wp-content/uploads/2025/09/Teeth-Brushing.jpg",
        f"{site}/wp-content/uploads/2025/09/Both-Exist.jpg",
        f"{site}/wp-content/uploads/2025/09/Both-Exist-scaled.jpg",
        f"{site}/wp-content/uploads/2025/10/Fresh-Groom-1.jpeg",
        f"{site}/wp-content/uploads/2025/10/Fresh-Groom-2.jpeg",
        f"{site}/wp-content/uploads/2024/09/Pup-2048x1536.jpeg",
    }
    assert set(by_url) == expected_urls
    assert len(rep["assets"]) == len(expected_urls)

    # Alternate thumbnail URLs and cross-page source_pages provenance preserved!
    logo = by_url[f"{site}/wp-content/uploads/2025/09/Logo.png"]
    assert logo["alternate_urls"] == [f"{site}/wp-content/uploads/2025/09/Logo-1024x278.png"]
    assert set(logo["source_pages"]) == {f"{site}/", f"{site}/services", f"{site}/services2"}

    bb = by_url[f"{site}/wp-content/uploads/2025/09/Blueberry-Facial-scaled.jpg"]
    assert bb["alternate_urls"] == [f"{site}/wp-content/uploads/2025/09/Blueberry-Facial-768x1024.jpg"]
    assert set(bb["source_pages"]) == {f"{site}/services", f"{site}/services2"}

    pup = by_url[f"{site}/wp-content/uploads/2024/09/Pup-2048x1536.jpeg"]
    assert pup["alternate_urls"] == [f"{site}/wp-content/uploads/2024/09/Pup-1024x768.jpeg"]

    # WordPress upload directory names (/2025/09/, /2025/10/, /2024/09/) NEVER fabricate publication dates!
    assert all(a["post_date"] is None and a["date_status"] == "unknown_date" and a["date_eligibility"] == "unknown_date" for a in rep["assets"])
    assert len(rep["unknown_date_review"]) == len(expected_urls)
    assert rep["coverage_percentage"] is None


def test_stage3h_facebook_four_batch_time_guard_cursor_validation_and_checkpoint_resume(tmp_path):
    """Stage 3H Task 2: Reproduces actual Stage 3G Run B (4 completed batches -> pre-batch time guard
    prevents doomed 5th subprocess -> checkpoint resume continues at batch 5 without repeating 1-4,
    preserves frozen UTC window, rejects tampered cursors, and reaches album/cutoff cleanly)."""
    set_main = "pb.61564648418385.-2207520000"
    now = [0.0]
    calls_run1: list[str] = []

    def batch_payload(start_idx: int, count: int, dates: list[str], next_pid: str) -> str:
        rows = []
        for k in range(count):
            pid = f"12221000000{start_idx + k:02d}"
            nxt = f"12221000000{start_idx + k + 1:02d}" if k + 1 < count else next_pid
            rows.append([3, f"{APPROVED_CDN}/fb-{pid}.jpg", {
                "category": "facebook", "subcategory": "set" if start_idx > 1 else "photos",
                "id": pid, "set_id": set_main, "date": dates[k], "extension": "jpg", "next_photo_id": nxt,
            }])
        return json.dumps(rows)

    def runner_run1(cmd: list[str], _stage: Path) -> tuple[int, str]:
        assert "--date-after" not in cmd
        calls_run1.append(cmd[-1])
        now[0] += 25.0  # Simulate actual Run B ~25s per 10-photo batch!
        b = len(calls_run1)
        if b == 1:
            return 0, batch_payload(1, 2, ["2026-10-21 08:35:34", "2026-09-10 12:00:00"], "1222100000003")
        if b == 2:
            return 0, batch_payload(3, 2, ["2026-07-27 16:52:07", "2026-07-20 12:00:00"], "1222100000005")
        if b == 3:
            return 0, batch_payload(5, 2, ["2026-07-14 15:27:02", "2026-07-01 12:00:00"], "1222100000007")
        if b == 4:
            return 0, batch_payload(7, 2, ["2026-06-16 15:37:44", "2026-06-08 17:58:42"], "1222100000009")
        raise AssertionError("Pre-batch remaining-time guard should have stopped BEFORE spawning batch 5!")

    jd = tmp_path / ("b2" * 16)
    jd.mkdir()
    frozen_job = {"job_id": jd.name, **WINDOW}
    cp1 = CollectionCheckpoint(jd, frozen_job)
    inv1 = OpenSourceSocialInventory(
        "facebook", FB, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=20, request_budget=8, time_budget_seconds=120, runner=runner_run1,
        clock=lambda: now[0], sleep_request=1.5, historical_mode=True, batch_size=2,
        cutoff_consecutive_older=2, include_albums=True, album_limit=2,
    )
    rep1 = cp1.run_source("facebook", FB, inv1, lambda *_: None, page_budget=6, transfer_budget=0, byte_limit=1024)
    # 4 batches * 25s = 100s elapsed -> remaining 20s < 25s * 0.85 (21.25s) -> pre-batch guard stops BEFORE batch 5!
    assert len(calls_run1) == 4
    assert rep1["stop_reason"] == "time_budget"
    assert rep1["termination_reason"] == "budget_reached"
    assert rep1["listing_exhausted"] is False
    assert rep1["coverage"] == "incomplete" and rep1["coverage_percentage"] is None
    assert len(rep1["assets"]) == 8
    assert rep1["cursor"] is not None and "fbid=1222100000009" in rep1["cursor"]

    # Tampered cursor URL pointing to an untrusted host or login page MUST fail validation!
    tampered = json.loads(rep1["cursor"])
    tampered["next_url"] = "https://www.facebook.com/login.php"
    with pytest.raises(SourceStop, match="invalid_continuation"):
        OpenSourceSocialInventory.validate_historical_cursor("facebook", json.dumps(tampered))
    tampered["next_url"] = "https://evil.example.test/photo"
    with pytest.raises(SourceStop, match="invalid_continuation"):
        OpenSourceSocialInventory.validate_historical_cursor("facebook", json.dumps(tampered))

    # Mismatched frozen window on resume MUST be rejected by CollectionCheckpoint!
    bad_window = frozen_window(datetime(2026, 12, 1, 0, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="frozen window|original job window"):
        CollectionCheckpoint(jd, {"job_id": jd.name, **bad_window})

    # Run 2: Resume from persisted checkpoint with original frozen window -> picks up at batch 5 (fbid=...09),
    # NEVER repeats batches 1-4, avoids duplicates, and traverses /photos_albums cleanly!
    calls_run2: list[str] = []
    now2 = [0.0]

    def runner_run2(cmd: list[str], _stage: Path) -> tuple[int, str]:
        assert "--date-after" not in cmd
        target = cmd[-1]
        calls_run2.append(target)
        now2[0] += 10.0
        if "fbid=1222100000009" in target:
            # Batch 5: 2 consecutive pre-cutoff photos -> triggers timeline cutoff!
            return 0, batch_payload(9, 2, ["2025-11-10 10:00:00", "2025-10-01 10:00:00"], "1222100000011")
        if target.endswith("/photos_albums"):
            return 0, json.dumps([[6, "https://www.facebook.com/media/set/?set=a.album99", {"set_id": "a.album99"}]])
        if "set=a.album99" in target:
            # Includes duplicate 1222100000001 (from batch 1!) + 1 genuinely new album photo
            return 0, json.dumps([
                [3, f"{APPROVED_CDN}/dup1.jpg", {"category": "facebook", "subcategory": "set", "id": "1222100000001", "set_id": "a.album99", "date": "2026-10-21 08:35:34", "extension": "jpg", "next_photo_id": "1222100000099"}],
                [3, f"{APPROVED_CDN}/alb99.jpg", {"category": "facebook", "subcategory": "set", "id": "1222100000099", "set_id": "a.album99", "date": "2026-04-01 12:00:00", "extension": "jpg", "next_photo_id": ""}],
            ])
        raise AssertionError(f"Unexpected resumed Facebook target: {target}")

    cp2 = CollectionCheckpoint(jd, frozen_job)
    inv2 = OpenSourceSocialInventory(
        "facebook", FB, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=20, request_budget=8, time_budget_seconds=120, runner=runner_run2,
        clock=lambda: now2[0], sleep_request=1.5, historical_mode=True, batch_size=2,
        cutoff_consecutive_older=2, include_albums=True, album_limit=2,
    )
    rep2 = cp2.run_source("facebook", FB, inv2, lambda *_: None, page_budget=5, transfer_budget=0, byte_limit=1024)
    assert calls_run2[0].startswith("https://www.facebook.com/photo/?fbid=1222100000009")
    assert not any(c == FB for c in calls_run2)  # Batch 1 NEVER repeated!
    assert any(c.endswith("/photos_albums") for c in calls_run2)
    assert rep2["stop_reason"] == "cutoff_reached"
    assert rep2["termination_reason"] == "cutoff_reached_with_evidence"
    assert rep2["listing_exhausted"] is False and rep2["coverage_percentage"] is None
    assert len(rep2["assets"]) == 11  # 8 from Run 1 + 2 cutoff photos + 1 new album photo (duplicate skipped!)
    assert len({a["native_id"] for a in rep2["assets"]}) == 11


def test_stage3h_instagram_quiet_removed_explicit_restriction_short_circuit_and_ambiguous_reels_allowed(tmp_path):
    """Stage 3H Task 3: Based on actual Stage 3G Run C Instagram diagnostic evidence:
    1. Historical Instaloader command omits --quiet to surface real HTTP 401/403/429/login errors.
    2. Explicit Instaloader restriction/rate-limit on /posts/ (429, 401, 403, Redirected to login page,
       PrivateProfileNotFollowedException) HARD-STOPS immediately and NEVER pokes /reels/.
    3. Ambiguous posts-only non-restriction failure (Run C Call 1 NotFoundError + opaque Call 2 failure)
       still permits independent /reels/ attempt within shared budget, and stops immediately when /reels/
       hits explicit 429, keeping coverage incomplete/unknown."""
    gdl_not_found = json.dumps([[-1, {"error": "NotFoundError", "message": "Requested user could not be found"}]])

    # 1. Verify Historical Instaloader command omits --quiet while retaining all no-download & abort-on flags!
    hist_cmd = build_instaloader_historical_cmd(
        url_kind="profile", target_hint="lilysclubuk", stage=tmp_path, cutoff=date(2025, 11, 25),
        include_reels=False, reels_only=False, use_resume=True,
    )
    _assert_no_bypass_flags(hist_cmd)
    assert "--quiet" not in hist_cmd
    assert "--no-pictures" in hist_cmd and "--no-videos" in hist_cmd and "--abort-on=429,403,401" in hist_cmd

    # 2. Explicit Instaloader restriction/rate-limit on /posts/ MUST hard-stop BEFORE /reels/!
    for idx, (il_code, il_out, expected_stop, expected_term) in enumerate((
        (4, 'Download aborted: Query to https://www.instagram.com/api/v1/users/web_profile_info/?username=lilysclubuk responded with "429 Too Many Requests".', "rate_limited", "rate_limited"),
        (4, 'Download aborted: Query to https://www.instagram.com/api/v1/users/web_profile_info/ responded with "401 Unauthorized".', "restricted", "access_blocked"),
        (4, 'Download aborted: Query to https://www.instagram.com/api/v1/users/web_profile_info/ responded with "403 Forbidden".', "restricted", "access_blocked"),
        (1, "HTTP redirect from https://www.instagram.com/lilysclubuk/ to https://www.instagram.com/accounts/login/?next=%2Flilysclubuk%2F\nRedirected to login page. Use --login or --load-cookies.", "restricted", "access_blocked"),
        (1, "PrivateProfileNotFollowedException: Profile lilysclubuk: private but not followed.", "restricted", "access_blocked"),
        (1, "TooManyRequestsException: Too many queries in the last time. Need to wait 10 minutes.", "rate_limited", "rate_limited"),
    )):
        calls: list[tuple[str, str]] = []

        def restricted_posts_runner(cmd: list[str], _stage: Path, c=il_code, o=il_out) -> tuple[int, str]:
            calls.append((cmd[0], cmd[-1]))
            if cmd[0] == "gallery-dl":
                return 0, gdl_not_found
            assert "--quiet" not in cmd
            return c, o

        jd = tmp_path / f"c{idx:031x}"
        jd.mkdir()
        cp = CollectionCheckpoint(jd, {"job_id": jd.name, **WINDOW})
        inv = OpenSourceSocialInventory(
            "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
            item_limit=20, request_budget=4, time_budget_seconds=60, allow_fallbacks=True,
            runner=restricted_posts_runner, historical_mode=True, batch_size=12, include_reels=True,
        )
        rep = cp.run_source("instagram", IG, inv, lambda *_: None, page_budget=3, transfer_budget=0, byte_limit=1024)
        # ONLY /posts/ gallery-dl + /posts/ instaloader ran; /reels/ NEVER probed!
        assert [t for t, _ in calls] == ["gallery-dl", "instaloader"], (idx, calls)
        assert not any("/reels/" in target for _, target in calls)
        assert rep["stop_reason"] == expected_stop
        assert rep["termination_reason"] == expected_term
        assert rep["coverage"] == "incomplete" and rep["coverage_percentage"] is None

    # 3. Ambiguous non-restriction /posts/ failure (no 401/403/429/login) allows independent /reels/ attempt,
    #    and succeeds if /reels/ returns a valid Reel (or stops cleanly if /reels/ hits 429)!
    reels_calls: list[tuple[str, str]] = []

    def ambiguous_posts_then_reels_ok(cmd: list[str], stage: Path) -> tuple[int, str]:
        reels_calls.append((cmd[0], cmd[-1]))
        if cmd[0] == "gallery-dl":
            return 0, gdl_not_found
        if "--reels" not in cmd:
            return 1, "lilysclubuk: Fetching Post metadata failed."  # Ambiguous non-restriction error
        sidecar = {
            "instaloader": {"version": "4.16", "node_type": "Post"},
            "node": {
                "id": "ig-reel-3h", "shortcode": "Reel3H", "product_type": "clips", "is_video": True,
                "video_url": f"{APPROVED_CDN}/reel-3h.mp4", "display_url": f"{APPROVED_CDN}/reel-3h.jpg",
                "taken_at_timestamp": int(datetime(2026, 11, 15, 12, 0, tzinfo=UTC).timestamp()),
            },
        }
        (stage / "2026-11-15_12-00-00_UTC.json").write_text(json.dumps(sidecar), encoding="utf-8")
        return 0, ""

    jd_amb = tmp_path / ("d" * 31 + "1")
    jd_amb.mkdir()
    cp_amb = CollectionCheckpoint(jd_amb, {"job_id": jd_amb.name, **WINDOW})
    inv_amb = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=20, request_budget=4, time_budget_seconds=60, allow_fallbacks=True,
        runner=ambiguous_posts_then_reels_ok, historical_mode=True, batch_size=12, include_reels=True,
    )
    rep_amb = cp_amb.run_source("instagram", IG, inv_amb, lambda *_: None, page_budget=3, transfer_budget=0, byte_limit=1024)
    assert any("/reels/" in target for _, target in reels_calls)
    assert len(rep_amb["assets"]) == 2  # Reel video + thumbnail
    assert any(a["native_id"] == "ig-reel-3h" and a["date_eligibility"] == "in_window" for a in rep_amb["assets"])
    assert rep_amb["coverage_percentage"] is None

    # 4. Exact Run C replay: ambiguous /posts/ -> /reels/ hits 429 -> stops as rate_limited, 0 items, unknown/incomplete coverage!
    run_c_calls: list[str] = []

    def exact_run_c_replay(cmd: list[str], _stage: Path) -> tuple[int, str]:
        run_c_calls.append(cmd[0])
        if cmd[0] == "gallery-dl":
            return 0, gdl_not_found
        if "--reels" not in cmd:
            return 1, "lilysclubuk: Fetching Post metadata failed."
        return 4, 'Download aborted: Query to https://www.instagram.com/api/v1/users/web_profile_info/ responded with "429 Too Many Requests".'

    jd_rc = tmp_path / ("d" * 31 + "2")
    jd_rc.mkdir()
    cp_rc = CollectionCheckpoint(jd_rc, {"job_id": jd_rc.name, **WINDOW})
    inv_rc = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=20, request_budget=4, time_budget_seconds=60, allow_fallbacks=True,
        runner=exact_run_c_replay, historical_mode=True, batch_size=12, include_reels=True,
    )
    rep_rc = cp_rc.run_source("instagram", IG, inv_rc, lambda *_: None, page_budget=3, transfer_budget=0, byte_limit=1024)
    assert run_c_calls == ["gallery-dl", "instaloader", "gallery-dl", "instaloader"]
    assert rep_rc["stop_reason"] == "rate_limited" and rep_rc["termination_reason"] == "rate_limited"
    assert rep_rc["assets"] == [] and rep_rc["coverage"] == "incomplete" and rep_rc["coverage_percentage"] is None
