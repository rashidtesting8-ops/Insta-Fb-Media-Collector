"""Milestone P2 offline tests: truthful robots_disallowed propagation across blocked
Facebook CDN items and real unmodified Meta 'Download Page Information' ZIP ingestion
(wrapped top-level directory, UTF-8 mojibake captions, photos + MP4 videos).

100% offline — live sockets are hard-blocked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
import json
from pathlib import Path
import socket
import subprocess
import time
import uuid
import zipfile

from PIL import Image, ImageDraw
import pytest
import requests

from app import named_auth
from app.public_collection import build_multitenant_collection_config, collect_public_job
from app.publication import frozen_window
from app.social_upload import ingest_owner_media_upload


@pytest.fixture(autouse=True)
def block_live_sockets(monkeypatch):
    def deny(*_args, **_kwargs):
        pytest.fail("P2 offline test attempted a live socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny)


def _jpeg_bytes(w: int = 420, h: int = 320, color: tuple[int, int, int] = (190, 125, 85)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([18, 18, w - 18, h - 18], outline=(35, 50, 75), width=9)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _mp4_bytes(tmp_path: Path) -> bytes:
    target = tmp_path / f"p2-{uuid.uuid4().hex[:8]}.mp4"
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


def test_p2_fbcdn_robots_disallowed_propagates_truthfully_to_all_remaining_fb_items(tmp_path, monkeypatch):
    """When scontent-1.fbcdn.net/robots.txt returns Disallow: /, the transfer loop stops politely
    after item #1 AND all remaining discovered *.fbcdn.net items carry failure_reason='robots_disallowed'
    (never leaving items #2..N as unexplained pending cards)."""
    web = "https://p2-groomer.example.test"
    fb = "https://www.facebook.com/p2groomer/"
    cdn1 = "https://scontent.fkhi22-1.fna.fbcdn.net"
    cdn2 = "https://scontent.fkhi22-2.fna.fbcdn.net"
    window = frozen_window(datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC))

    fetched: list[str] = []
    responses = {
        f"{web}/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        f"{web}/": (200, {"Content-Type": "text/html"}, "<html><body>No media</body></html>"),
        f"{cdn1}/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /\n"),
        f"{cdn2}/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /\n"),
    }

    def fake_get(session, url, **kwargs):
        assert not session.cookies and not session.trust_env and kwargs.get("allow_redirects") is False
        fetched.append(url)
        assert url in responses, f"Unexpected HTTP fetch (must not bypass robots.txt): {url}"
        status, headers, body = responses[url]
        resp = requests.Response()
        resp.status_code = status
        resp.url = url
        resp.headers.update(headers)
        resp._content = body.encode("utf-8") if isinstance(body, str) else body
        resp._content_consumed = True
        return resp

    monkeypatch.setattr(requests.Session, "get", fake_get)

    def fake_social_cli(cmd: list[str], _stage: Path) -> tuple[int, str]:
        if "photos_albums" in cmd[-1]:
            return 0, json.dumps([])
        return 0, json.dumps([
            [3, f"{cdn1}/v/t51/photo1.jpg", {"category": "facebook", "subcategory": "photos", "id": "500001", "date": "2026-11-10 10:00:00", "extension": "jpg"}],
            [3, f"{cdn1}/v/t51/photo2.jpg", {"category": "facebook", "subcategory": "photos", "id": "500002", "date": "2026-11-11 10:00:00", "extension": "jpg"}],
            [3, f"{cdn2}/v/t51/photo3.jpg", {"category": "facebook", "subcategory": "photos", "id": "500003", "date": "2026-11-12 10:00:00", "extension": "jpg"}],
        ])

    job_dir = tmp_path / ("e" * 32)
    job_dir.mkdir()
    job = {
        "job_id": job_dir.name,
        "website_url": f"{web}/",
        "facebook_url": fb,
        "social_permission_confirmed": True,
        "max_pages": 2,
        "max_media": 3,
        "max_social_files": 5,
        **window,
    }
    manifest = collect_public_job(job_dir, job, build_multitenant_collection_config(job), social_runner=fake_social_cli)
    fb_assets = [a for a in manifest["assets"] if a["platform"] == "facebook"]
    assert len(fb_assets) == 3
    assert all(a.get("failure_reason") == "robots_disallowed" for a in fb_assets), fb_assets
    assert "robots_disallowed" in manifest["sources"]["facebook"]["issues"]
    # Polite behavior: never hit the actual JPEG URLs once robots.txt returned Disallow: /
    assert not any(u.endswith(".jpg") for u in fetched)


def test_p2_unmodified_meta_page_export_zip_with_wrapper_dir_mojibake_photo_and_video(tmp_path):
    """Exercise a real Meta 'Download Page Information' ZIP wrapped in a top-level archive folder
    containing both photos and MP4 video plus UTF-8 mojibake captions."""
    job_id = "f" * 32
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    now_dt = datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC)
    window = frozen_window(now_dt)
    job = {
        "job_id": job_id,
        "website_url": "https://www.lilys-club.co.uk/",
        "facebook_url": "https://www.facebook.com/people/Lilys-club-Dog-Grooming/61564648418385/",
        **window,
    }
    base_manifest = {
        "job_id": job_id,
        "schema_version": "2.0",
        "website_url": job["website_url"],
        **window,
        "collection_mode": "public_checkpoint",
        "collection_gate": "blocked_unverified_collection",
        "sources": {"website": {"issues": []}, "facebook": {"issues": ["robots_disallowed"]}},
        "assets": [],
        "unknown_date_review": [],
    }
    (job_dir / "manifest.json").write_text(json.dumps(base_manifest), encoding="utf-8")

    now_ts = int(now_dt.timestamp())
    photo_uri = "your_facebook_activity/posts/media/MobileUploads/poodle_trim.jpg"
    video_uri = "your_facebook_activity/posts/media/Videos/salon_walkthrough.mp4"
    # Official Meta JSON encodes UTF-8 curly apostrophe (U+2019) as latin-1 bytes \u00e2\u0080\u0099
    mojibake_caption = "Lily’s Club — fresh poodle trim!".encode("utf-8").decode("latin-1")

    posts_json = [
        {
            "timestamp": now_ts - 15 * 86400,
            "attachments": [{"data": [{"media": {"uri": photo_uri, "creation_timestamp": now_ts - 15 * 86400, "description": mojibake_caption}}]}],
        }
    ]
    videos_json = {
        "videos_v2": [
            {"uri": video_uri, "creation_timestamp": now_ts - 25 * 86400, "title": "Salon walkthrough clip"}
        ]
    }
    jpg = _jpeg_bytes(480, 360)
    mp4 = _mp4_bytes(tmp_path)

    wrapper = "facebook-lilysclub-2026-11-26"
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{wrapper}/your_facebook_activity/posts/your_posts_1.json", json.dumps(posts_json))
        zf.writestr(f"{wrapper}/your_facebook_activity/posts/videos.json", json.dumps(videos_json))
        zf.writestr(f"{wrapper}/{photo_uri}", jpg)
        zf.writestr(f"{wrapper}/{video_uri}", mp4)

    manifest, summary = ingest_owner_media_upload(
        job_dir,
        job,
        platform="facebook",
        filename="facebook-lilysclub-2026-11-26.zip",
        payload=buf.getvalue(),
        actor="admin-p2",
    )
    assert summary["uploaded_count"] == 2
    assert summary["in_window_count"] == 2
    assert summary.get("skipped") == []

    fb_assets = [a for a in manifest["assets"] if a.get("platform") == "facebook" and a.get("type") != "frame"]
    types = {a["type"] for a in fb_assets}
    assert types == {"image", "video"}
    img_asset = next(a for a in fb_assets if a["type"] == "image")
    assert "Lily’s Club" in (img_asset.get("source_caption") or "")
    assert img_asset["date_status"] == "verified"
    assert img_asset["rights_status"] == "unverified"
    # Video must have extracted FFmpeg preview frames automatically.
    assert any(a.get("type") == "frame" for a in manifest["assets"])
