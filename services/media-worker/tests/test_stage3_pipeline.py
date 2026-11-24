"""Offline Stage 3 integration tests: website discovery, controlled downloads, preprocessing, and M4 holds.

All tests execute with live sockets hard-blocked (--network none) using real locally
generated Pillow images and ffmpeg MP4 videos. Zero live website, Instagram, Facebook,
Groq, or Google Drive calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
import socket
import subprocess

from PIL import Image, ImageDraw
import pytest
import requests

from app.ai.pipeline import run_ai_curation
from app.collection_checkpoint import Attachment, CollectionCheckpoint, Download, InventoryPage, SourceItem
from app.delivery import build_delivery_plan
from app.manifest import validate_against_disk
from app.public_collection import collect_public_job
from app.publication import frozen_window
from tests.test_phase9_security import request, worker  # noqa: F401

WEB = "https://groomer-fixture.test"
IG = "https://www.instagram.com/lilysclubuk/"
FB = "https://www.facebook.com/people/Lilys-club-Dog-Grooming/61564648418385/"
APPROVED_CDN = "https://approved-cdn.fixture.test"
UNAPPROVED_CDN = "https://scontent.unapproved.fbcdn.net"
WINDOW = frozen_window(datetime(2026, 11, 24, 12, 18, 41, tzinfo=UTC))


@pytest.fixture(autouse=True)
def block_all_sockets(monkeypatch):
    def deny_socket(*_args, **_kwargs):
        pytest.fail("Offline Stage 3 test attempted a live socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny_socket)


def _make_jpeg_bytes(width: int, height: int, *, accent: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (width, height), (240, 235, 225))
    draw = ImageDraw.Draw(img)
    for x in range(0, width, 32):
        draw.rectangle([x, 0, x + 16, height // 2], fill=accent)
    draw.ellipse([width // 4, height // 4, 3 * width // 4, 3 * height // 4], fill=(40, 60, 90))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _make_mp4_bytes(tmp_path: Path) -> bytes:
    target = tmp_path / "fixture-clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1.5:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-t", "1.5", str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return target.read_bytes()


def _mock_http_get(responses: dict[str, tuple[int, dict, bytes | str]], seen: list[str]):
    def fake_get(session, url, **kwargs):
        assert not session.cookies and not session.trust_env and kwargs.get("allow_redirects") is False
        seen.append(url)
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


def test_stage3_end_to_end_website_social_downloads_preprocessing_and_m4_holds(worker, tmp_path, monkeypatch):
    hero_jpg = _make_jpeg_bytes(640, 480, accent=(180, 90, 40))
    near_dup_jpg = _make_jpeg_bytes(480, 360, accent=(180, 90, 40))  # visually near-identical perceptual duplicate
    distinct_jpg = _make_jpeg_bytes(520, 520, accent=(30, 150, 120))
    clip_mp4 = _make_mp4_bytes(tmp_path)

    # Website HTML with JSON-LD dated image, undated image, dated video + poster thumbnail, sitemap & robots.txt.
    ld_json = json.dumps([
        {"@type": "ImageObject", "contentUrl": f"{APPROVED_CDN}/web-hero.jpg", "datePublished": "2026-10-15T10:00:00Z"},
        {"@type": "VideoObject", "contentUrl": f"{APPROVED_CDN}/web-clip.mp4", "uploadDate": "2026-10-20T12:00:00Z"},
    ])
    home_html = (
        f'<html><head><script type="application/ld+json">{ld_json}</script></head><body>'
        f'<img src="{APPROVED_CDN}/web-hero.jpg">'
        f'<img src="{APPROVED_CDN}/web-undated.jpg">'
        f'<img src="{UNAPPROVED_CDN}/should-never-fetch.jpg">'
        f'<video poster="{APPROVED_CDN}/web-poster.jpg"><source src="{APPROVED_CDN}/web-clip.mp4"></video>'
        f'<a href="/private/secret">Private</a></body></html>'
    )
    sitemap_xml = f"<urlset><url><loc>{WEB}/private/secret</loc></url><url><loc>{WEB}/gallery</loc></url></urlset>"
    gallery_html = f'<html><body><img src="{APPROVED_CDN}/web-near-dup.jpg"></body></html>'

    seen_http: list[str] = []
    responses: dict[str, tuple[int, dict, bytes | str]] = {
        f"{WEB}/robots.txt": (200, {}, "User-agent: *\nDisallow: /private/\n"),
        f"{WEB}/": (200, {"Content-Type": "text/html"}, home_html),
        f"{WEB}/sitemap.xml": (200, {"Content-Type": "application/xml"}, sitemap_xml),
        f"{WEB}/gallery": (200, {"Content-Type": "text/html"}, gallery_html),
        f"{APPROVED_CDN}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{APPROVED_CDN}/web-hero.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(hero_jpg)), "ETag": '"h1"'}, hero_jpg),
        f"{APPROVED_CDN}/web-undated.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(distinct_jpg))}, distinct_jpg),
        f"{APPROVED_CDN}/web-near-dup.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(near_dup_jpg))}, near_dup_jpg),
        f"{APPROVED_CDN}/web-clip.mp4": (200, {"Content-Type": "video/mp4", "Content-Length": str(len(clip_mp4))}, clip_mp4),
        # Social approved media copies (identical hero_jpg bytes on IG & FB to prove cross-source exact dedup + provenance).
        f"{APPROVED_CDN}/ig-repost.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(hero_jpg))}, hero_jpg),
        f"{APPROVED_CDN}/fb-photo-1.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(hero_jpg))}, hero_jpg),
        f"{APPROVED_CDN}/fb-photo-2.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(distinct_jpg))}, distinct_jpg),
        f"{APPROVED_CDN}/fb-photo-3.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(distinct_jpg))}, distinct_jpg),
        f"{APPROVED_CDN}/fb-photo-4.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(distinct_jpg))}, distinct_jpg),
        f"{APPROVED_CDN}/fb-photo-old.jpg": (200, {"Content-Type": "image/jpeg", "Content-Length": str(len(distinct_jpg))}, distinct_jpg),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses, seen_http))

    # Stage 3B.4 persisted 5-photo Facebook shape + Instagram open_source_cli discovery.
    def fake_social_cli(cmd: list[str], _stage: Path) -> tuple[int, str]:
        assert cmd[0] == "gallery-dl"
        if "instagram" in cmd:
            return 0, json.dumps([
                [3, f"{APPROVED_CDN}/ig-repost.jpg", {
                    "category": "instagram", "post_id": "ig-100", "post_shortcode": "IgPost100",
                    "post_url": "https://www.instagram.com/p/IgPost100/", "media_id": "ig-m-100",
                    "caption": "Fresh puppy groom at Lily's Club!",
                    "extension": "jpg", "post_date": "2026-10-16T09:00:00+00:00",
                }],
            ])
        assert "facebook" in cmd
        return 0, json.dumps([
            [3, f"{APPROVED_CDN}/fb-photo-1.jpg", {
                "category": "facebook", "subcategory": "photos",
                "id": "122230723652488280", "set_id": "pb.61564648418385.-2207520000",
                "caption": "Complimentary Halloween photos at Harrow & Wood Green!",
                "date": "2026-10-21 08:35:34", "extension": "jpg",
            }],
            [3, f"{APPROVED_CDN}/fb-photo-2.jpg", {
                "category": "facebook", "subcategory": "photos",
                "id": "122230232810488280", "set_id": "pb.61564648418385.-2207520000",
                "date": "2026-10-18 11:20:00", "extension": "jpg",
            }],
            [3, f"{APPROVED_CDN}/fb-photo-3.jpg", {
                "category": "facebook", "subcategory": "photos",
                "id": "122230000000000003", "set_id": "pb.61564648418385.-2207520000",
                "date": "2026-10-17 11:00:00", "extension": "jpg",
            }],
            [3, f"{APPROVED_CDN}/fb-photo-4.jpg", {
                "category": "facebook", "subcategory": "photos",
                "id": "122230000000000004", "set_id": "pb.61564648418385.-2207520000",
                "date": "2026-10-16 11:00:00", "extension": "jpg",
            }],
            [3, f"{APPROVED_CDN}/fb-photo-old.jpg", {
                "category": "facebook", "subcategory": "photos",
                "id": "122100000000000005", "set_id": "pb.61564648418385.-2207520000",
                "date": "2024-01-10 08:00:00", "extension": "jpg",
            }],
        ])

    cfg = {
        "run_preprocessing": True,
        "sources": {
            "website": {
                "url": f"{WEB}/", "sitemaps": [f"{WEB}/sitemap.xml"], "media_origins": [APPROVED_CDN],
                "request_budget": 20, "page_budget": 4, "document_limit": 16384, "entry_limit": 10,
                "transfer_budget": 6, "byte_limit": 5_000_000, "time_budget_seconds": 30,
            },
            "instagram": {
                "url": IG, "discovery_mode": "open_source_cli", "download_authorized": True,
                "media_origins": [APPROVED_CDN],
                "cli_request_budget": 2, "request_budget": 10, "page_budget": 1, "entry_limit": 5,
                "transfer_budget": 4, "byte_limit": 5_000_000, "time_budget_seconds": 30,
            },
            "facebook": {
                "url": FB, "discovery_mode": "open_source_cli", "download_authorized": True,
                "media_origins": [APPROVED_CDN],
                "cli_request_budget": 2, "request_budget": 10, "page_budget": 1, "entry_limit": 5,
                "transfer_budget": 5, "byte_limit": 5_000_000, "time_budget_seconds": 30,
            },
        },
    }

    job_id = "e" * 32
    job_dir = worker.DATA_DIR / job_id
    job_dir.mkdir()
    job = {
        "job_id": job_id,
        "website_url": f"{WEB}/",
        "instagram_url": IG,
        "facebook_url": FB,
        "social_permission_confirmed": True,
        "run_ai_curation": True,
        "collection_mode": "public_checkpoint",
        **WINDOW,
    }
    worker.store(job)
    manifest = collect_public_job(job_dir, job, cfg, social_runner=fake_social_cli)

    # 1. Zero requests to robots-disallowed /private/ or unapproved CDN origin.
    assert not any("/private/" in u for u in seen_http)
    assert not any(UNAPPROVED_CDN in u for u in seen_http)

    # 2. Disk & SHA-256 integrity verified across all downloaded .media files + extracted video frames.
    assert validate_against_disk(job_dir, manifest) == []

    # 3. Preprocessing analyzed all 9 .media images AND probed + extracted frames from .media MP4 video!
    pre = manifest["preprocessing"]
    assert pre["status"] == "completed"
    assert pre["images"]["analyzed"] == 9  # 3 website + 1 IG + 5 Stage 3B.4 FB photos
    assert pre["images"]["unsupported_format"] == 0
    assert pre["images"]["problem"] == 0
    assert pre["videos"]["probed"] == 1 and pre["videos"]["frames_extracted"] >= 1

    # 4. Cross-source exact SHA-256 duplicates + perceptual near-duplicate grouped while keeping distinct provenance!
    hero_sha = hashlib.sha256(hero_jpg).hexdigest()
    hero_assets = [a for a in manifest["assets"] if a.get("sha256") == hero_sha and a.get("type") == "image"]
    assert len(hero_assets) == 3
    assert {a["platform"] for a in hero_assets} == {"website", "instagram", "facebook"}
    assert len({a["asset_id"] for a in hero_assets}) == 3
    exact_groups = [g for g in pre["duplicate_groups"] if g["method"] == "exact_sha256"]
    assert any(set(g["member_asset_ids"]) == {a["asset_id"] for a in hero_assets} for g in exact_groups)
    perceptual_groups = [g for g in pre["duplicate_groups"] if g["method"] == "perceptual_dhash"]
    assert len(perceptual_groups) >= 1

    # 5. Facebook native ID, canonical URL, caption, MIME, and verified UTC date preserved.
    by_native = {a.get("native_id"): a for a in manifest["assets"] if a.get("native_id")}
    fb1 = by_native["122230723652488280"]
    assert fb1["identity_status"] == "source_supplied"
    assert fb1["source_page"] == "https://www.facebook.com/photo/?fbid=122230723652488280&set=pb.61564648418385.-2207520000"
    assert fb1["date_eligibility"] == "in_window" and fb1["post_date"] == "2026-10-21T08:35:34+00:00"
    assert fb1["mime"] == "image/jpeg" and fb1["quality"]["integrity"] == "ok"
    assert "Halloween photos" in fb1["source_caption"]
    assert by_native["122230000000000003"]["date_eligibility"] == "in_window"
    assert by_native["122230000000000004"]["date_eligibility"] == "in_window"
    assert by_native["122100000000000005"]["date_eligibility"] == "out_of_window"

    # 6. Video asset probed, frames extracted on disk, and poster stayed thumbnail.
    web_clip = next(a for a in manifest["assets"] if a.get("source_url") == f"{APPROVED_CDN}/web-clip.mp4")
    assert web_clip["mime"] == "video/mp4" and web_clip["preprocessing"]["outcome"] == "video_processed"
    assert len(web_clip["frames"]) >= 1
    assert (job_dir / web_clip["frames"][0]["local_path"]).is_file()
    web_poster = next(a for a in manifest["assets"] if a.get("source_url") == f"{APPROVED_CDN}/web-poster.jpg")
    assert web_poster["status"] == "thumbnail"

    # 7. Unknown-date & unverified-rights holds + M4 approval / Start AI / Delivery gates remain strictly enforced.
    web_undated = next(a for a in manifest["assets"] if a.get("source_url") == f"{APPROVED_CDN}/web-undated.jpg")
    assert web_undated["date_eligibility"] == "unknown_date"
    assert web_undated["asset_id"] in manifest["unknown_date_review"]
    assert manifest["collection_gate"] == "blocked_unverified_collection"
    assert all(a["rights_status"] == "unverified" for a in manifest["assets"])
    assert all(s["coverage_percentage"] is None for s in manifest["sources"].values())

    class NoProvider:
        @property
        def name(self):
            pytest.fail("Held Stage 3 job must never auto-invoke Groq/AI")

    assert run_ai_curation(job_dir, job_id, provider=NoProvider()).status == "failed"
    assert build_delivery_plan(job_dir, job_id)["status"] in ("no_curation", "blocked_unverified_collection")
    assert request(worker.app, f"/jobs/{job_id}/delivery-result", "POST", "test-admin",
                   (b'{"authorization_confirmed":true}',))[0] == 409
    assert request(worker.app, "/named/collection-panel", "POST", "test-dashboard",
                   (json.dumps({"job_id": job_id}).encode(),))[0] == 404


def test_social_download_authorization_is_separate_from_discovery(tmp_path, monkeypatch):
    # Even when social_permission_confirmed=True and transfer_budget>0, missing download_authorized blocks downloads!
    seen_http: list[str] = []
    responses = {
        f"{WEB}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{WEB}/": (200, {"Content-Type": "text/html"}, "<html><body>No media</body></html>"),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses, seen_http))

    def fake_cli(cmd: list[str], _stage: Path) -> tuple[int, str]:
        if "facebook" in cmd:
            return 0, json.dumps([[3, f"{APPROVED_CDN}/fb1.jpg", {
                "category": "facebook", "subcategory": "photos",
                "id": "122230723652488280", "set_id": "pb.61564648418385.-2207520000",
                "date": "2026-10-21 08:35:34", "extension": "jpg",
            }]])
        return 0, json.dumps([[3, f"{APPROVED_CDN}/ig1.jpg", {
            "category": "instagram", "post_id": "ig1", "post_url": "https://www.instagram.com/p/Ig1/",
            "media_id": "igm1", "extension": "jpg", "post_date": "2026-10-21T08:35:34+00:00",
        }]])

    cfg = {
        "sources": {
            "website": {
                "url": f"{WEB}/", "sitemaps": [], "media_origins": [APPROVED_CDN],
                "request_budget": 4, "page_budget": 1, "document_limit": 4096, "entry_limit": 5,
                "transfer_budget": 0, "byte_limit": 1024, "time_budget_seconds": 20,
            },
            "instagram": {
                "url": IG, "discovery_mode": "open_source_cli", "media_origins": [APPROVED_CDN],
                "request_budget": 2, "page_budget": 1, "entry_limit": 5,
                "transfer_budget": 5, "byte_limit": 1024, "time_budget_seconds": 20,
                # download_authorized intentionally omitted!
            },
            "facebook": {
                "url": FB, "discovery_mode": "open_source_cli", "media_origins": [APPROVED_CDN],
                "request_budget": 2, "page_budget": 1, "entry_limit": 5,
                "transfer_budget": 5, "byte_limit": 1024, "time_budget_seconds": 20,
                "download_authorized": False,
            },
        },
    }
    job_id = "f" * 32
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    job = {
        "job_id": job_id, "website_url": f"{WEB}/", "instagram_url": IG, "facebook_url": FB,
        "social_permission_confirmed": True, "run_ai_curation": False, **WINDOW,
    }
    manifest = collect_public_job(job_dir, job, cfg, social_runner=fake_cli)
    assert not any(APPROVED_CDN in u for u in seen_http)
    assert not list((job_dir / "collection").glob("*.media"))
    assert "download_authorization_required" in manifest["sources"]["instagram"]["issues"]
    assert "download_authorization_required" in manifest["sources"]["facebook"]["issues"]
    assert all(a["status"] == "pending" for a in manifest["assets"])


def test_interrupted_downloads_resume_with_etag_and_clean_up_non_resumable_part_files(tmp_path):
    job_id = "9" * 32
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    cp = CollectionCheckpoint(job_dir, {"job_id": job_id, **WINDOW})
    cp.collect(
        "facebook",
        FB,
        type("Inv", (), {"page": lambda _self, _c: InventoryPage((
            SourceItem("p1", "https://www.facebook.com/photo/?fbid=122230000000000001", (
                Attachment("122230000000000001", f"{APPROVED_CDN}/no-etag.jpg", "image", identity_status="source_supplied"),
                Attachment("122230000000000002", f"{APPROVED_CDN}/with-etag.jpg", "image", identity_status="source_supplied"),
            )),
        ))})(),
        page_budget=1,
    )
    by_native = {a["native_id"]: a["asset_id"] for a in cp.report("facebook")["assets"]}
    k_no_etag = by_native["122230000000000001"]
    k_etag = by_native["122230000000000002"]
    blob = _make_jpeg_bytes(200, 200, accent=(10, 100, 200))
    digest = hashlib.sha256(blob).hexdigest()

    def crash_midway():
        yield blob[:50]
        raise OSError("simulated network drop")

    # 1. Without a strong revision (ETag), interrupted download MUST clean up orphaned .part file.
    assert cp.download(
        k_no_etag, lambda *_: Download(len(blob), crash_midway(), revision=None, mime="image/jpeg"), byte_limit=100_000,
    ) == "download_interrupted"
    assert not (cp.root / f"{k_no_etag}.part").exists()

    # 2. With a strong revision (ETag), interrupted download keeps .part and resumes safely to verified SHA-256.
    assert cp.download(
        k_etag, lambda *_: Download(len(blob), crash_midway(), revision='"etag-v1"', sha256=digest, mime="image/jpeg"),
        byte_limit=100_000,
    ) == "download_interrupted"
    assert (cp.root / f"{k_etag}.part").is_file()

    restarted = CollectionCheckpoint(job_dir, {"job_id": job_id, **WINDOW})
    seen_offsets: list[int] = []

    def resume_fetch(_url, offset, revision):
        seen_offsets.append(offset)
        assert offset == 50 and revision == '"etag-v1"'
        return Download(len(blob), [blob[offset:]], offset=offset, revision=revision, sha256=digest, mime="image/jpeg")

    assert restarted.download(k_etag, resume_fetch, byte_limit=100_000) == "verified"
    assert seen_offsets == [50]
    assert not (restarted.root / f"{k_etag}.part").exists()
    assert (restarted.root / f"{k_etag}.media").is_file()
    asset = next(a for a in restarted.report("facebook")["assets"] if a["asset_id"] == k_etag)
    assert asset["digest"] == digest and asset["mime"] == "image/jpeg"


def test_unapproved_cdn_origin_and_unsupported_mime_fail_closed(tmp_path, monkeypatch):
    seen_http: list[str] = []
    responses = {
        f"{WEB}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{WEB}/": (200, {"Content-Type": "text/html"}, "<html><body>No media</body></html>"),
        f"{APPROVED_CDN}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{APPROVED_CDN}/bad.svg": (200, {"Content-Type": "image/svg+xml", "Content-Length": "11"}, b"<svg></svg>"),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_http_get(responses, seen_http))

    def fake_cli(cmd: list[str], _stage: Path) -> tuple[int, str]:
        if "facebook" in cmd:
            return 0, json.dumps([[3, f"{UNAPPROVED_CDN}/blocked.jpg", {
                "category": "facebook", "subcategory": "photos",
                "id": "122230000000000099", "set_id": "pb.61564648418385.-2207520000",
                "date": "2026-10-21 08:35:34", "extension": "jpg",
            }]])
        return 0, json.dumps([[3, f"{APPROVED_CDN}/bad.svg", {
            "category": "instagram", "post_id": "ig-svg", "post_url": "https://www.instagram.com/p/IgSvg/",
            "media_id": "ig-svg-1", "extension": "jpg", "post_date": "2026-10-21T08:35:34+00:00",
        }]])

    cfg = {
        "sources": {
            "website": {
                "url": f"{WEB}/", "sitemaps": [], "media_origins": [APPROVED_CDN],
                "request_budget": 4, "page_budget": 1, "document_limit": 4096, "entry_limit": 5,
                "transfer_budget": 0, "byte_limit": 1024, "time_budget_seconds": 20,
            },
            "instagram": {
                "url": IG, "discovery_mode": "open_source_cli", "download_authorized": True,
                "media_origins": [APPROVED_CDN],
                "request_budget": 2, "page_budget": 1, "entry_limit": 5,
                "transfer_budget": 2, "byte_limit": 1024, "time_budget_seconds": 20,
            },
            "facebook": {
                "url": FB, "discovery_mode": "open_source_cli", "download_authorized": True,
                "media_origins": [APPROVED_CDN],
                "request_budget": 2, "page_budget": 1, "entry_limit": 5,
                "transfer_budget": 2, "byte_limit": 1024, "time_budget_seconds": 20,
            },
        },
    }
    job_id = "8" * 32
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    job = {
        "job_id": job_id, "website_url": f"{WEB}/", "instagram_url": IG, "facebook_url": FB,
        "social_permission_confirmed": True, "run_ai_curation": False, **WINDOW,
    }
    manifest = collect_public_job(job_dir, job, cfg, social_runner=fake_cli)
    assert not any(UNAPPROVED_CDN in u for u in seen_http)
    assert not list((job_dir / "collection").glob("*.media"))
    by_native = {a.get("native_id"): a for a in manifest["assets"]}
    assert by_native["122230000000000099"]["status"] == "access_blocked"
    assert by_native["ig-svg-1"]["status"] == "unsupported_media"
