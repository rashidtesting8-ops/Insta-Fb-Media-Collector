"""Offline fixture tests for the M5 Stage 3B.1 open-source social discovery adapter.

All tests execute with sockets hard-blocked (--network none) and injected fake
subprocess runners. Zero real requests to Instagram, Facebook, websites, Groq, or Drive.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys

import pytest
import requests

from app.ai.payload import select_ai_candidates
from app.ai.pipeline import run_ai_curation
from app.collection_checkpoint import CollectionCheckpoint, SourceStop
from app.delivery import build_delivery_plan
from app.public_collection import collect_public_job
from app.publication import GALLERY_DL_FACEBOOK_UTC_FIELD, frozen_window, publication_evidence, verified_publication
from app.social_discovery import (
    OpenSourceSocialInventory,
    build_gallery_dl_discovery_cmd,
    build_instaloader_discovery_cmd,
    build_ytdlp_discovery_cmd,
    classify_social_url,
)
from tests.test_phase9_security import request, worker  # noqa: F401

WEB = "https://www.lilys-club.co.uk/"
IG = "https://www.instagram.com/lilysclubuk/"
IG_REEL = "https://www.instagram.com/reel/CxReel123/"
FB_PEOPLE = "https://www.facebook.com/people/Lilys-club-Dog-Grooming/61564648418385/"
FB_VIDEO = "https://www.facebook.com/watch/?v=987654321"
CDN = "https://recorded-media.test"
WINDOW = frozen_window(datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def allow_local_subprocess():
    """Opt in to local offline subprocess.Popen execution (sockets remain blocked)."""


@pytest.fixture(autouse=True)
def block_all_external_io(monkeypatch, request):
    def deny_socket(*_args, **_kwargs):
        pytest.fail("Offline social discovery test attempted a live socket connection")

    def deny_popen(*_args, **_kwargs):
        pytest.fail("Offline social discovery test attempted a real subprocess call")

    monkeypatch.setattr(socket.socket, "connect", deny_socket)
    if "allow_local_subprocess" not in request.fixturenames:
        monkeypatch.setattr(subprocess, "Popen", deny_popen)


def _assert_no_bypass_flags(cmd: list[str]) -> None:
    joined = " ".join(cmd).lower()
    for forbidden in ("--cookies", "--login", "--password", "--proxy", "--username"):
        assert forbidden not in joined, f"Forbidden flag {forbidden} in {cmd}"


def _mock_website_get(responses: dict[str, tuple[int, dict, bytes | str]], seen: list[str]):
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


def test_url_classification_supports_expected_patterns_and_blocks_login_pages():
    assert classify_social_url("instagram", IG) == ("profile", "lilysclubuk")
    assert classify_social_url("instagram", "https://www.instagram.com/lilysclubuk/reels/") == ("profile", "lilysclubuk")
    assert classify_social_url("instagram", IG_REEL) == ("single_post", "CxReel123")
    assert classify_social_url("facebook", FB_PEOPLE) == ("people_profile", "61564648418385")
    assert classify_social_url("facebook", "https://www.facebook.com/profile.php?id=61564648418385") == (
        "profile_id", "61564648418385",
    )
    assert classify_social_url("facebook", "https://www.facebook.com/lilysclubuk/") == ("page", "lilysclubuk")
    assert classify_social_url("facebook", FB_VIDEO) == ("single_post", None)

    for platform, bad_url in (
        ("instagram", "https://www.instagram.com/accounts/login/"),
        ("instagram", "https://www.instagram.com/explore/tags/dogs/"),
        ("facebook", "https://www.facebook.com/login.php"),
        ("facebook", "https://www.facebook.com/checkpoint/"),
    ):
        with pytest.raises(SourceStop, match="access_blocked"):
            classify_social_url(platform, bad_url)


def test_instagram_profile_carousel_windows_and_zero_downloads_with_transfer_budget_zero(tmp_path):
    commands: list[list[str]] = []
    # Carousel with image + video + thumbnail, an out-of-window post, an undated post, and a boundary post.
    payload = [
        [2, {"username": "lilysclubuk"}],
        [3, CDN + "/slide1.jpg", {
            "category": "instagram", "post_id": "car1", "post_shortcode": "CarCode1",
            "post_url": "https://www.instagram.com/p/CarCode1/", "media_id": "slide-img-1",
            "extension": "jpg", "post_date": WINDOW["window_start_utc"],
        }],
        [3, CDN + "/slide2.mp4", {
            "category": "instagram", "post_id": "car1", "post_shortcode": "CarCode1",
            "post_url": "https://www.instagram.com/p/CarCode1/", "media_id": "slide-vid-2",
            "extension": "mp4", "post_date": WINDOW["window_start_utc"],
        }],
        [3, CDN + "/slide2-thumb.jpg", {
            "category": "instagram", "post_id": "car1", "post_shortcode": "CarCode1",
            "post_url": "https://www.instagram.com/p/CarCode1/", "media_type": "thumbnail",
            "extension": "jpg", "post_date": WINDOW["window_start_utc"],
        }],
        [3, CDN + "/old-pinned.jpg", {
            "category": "instagram", "post_id": "old1", "post_shortcode": "OldCode",
            "post_url": "https://www.instagram.com/p/OldCode/", "media_id": "old-img",
            "extension": "jpg", "post_date": "2020-05-01T00:00:00+00:00",
        }],
        [3, CDN + "/undated.jpg", {
            "category": "instagram", "post_id": "und1", "post_shortcode": "UndCode",
            "post_url": "https://www.instagram.com/p/UndCode/", "media_id": "undated-img",
            "extension": "jpg", "date": "2026-06-01T00:00:00+00:00",  # generic date is NOT trusted
        }],
        [3, CDN + "/edge-end.jpg", {
            "category": "instagram", "post_id": "end1", "post_shortcode": "EndCode",
            "post_url": "https://www.instagram.com/p/EndCode/", "media_id": "end-img",
            "extension": "jpg", "post_date": WINDOW["window_end_utc"],
        }],
    ]

    def fake_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        _assert_no_bypass_flags(cmd)
        commands.append(cmd)
        assert cmd[0] == "gallery-dl" and "--resolve-json" in cmd and "--no-postprocessors" in cmd
        return 0, json.dumps(payload)

    job_dir = tmp_path / ("a" * 32)
    job_dir.mkdir()
    checkpoint = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
    inv = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=10, request_budget=2, time_budget_seconds=30, runner=fake_runner,
    )
    report = checkpoint.run_source(
        "instagram", IG, inv, lambda *_: pytest.fail("transfer_budget=0 must never download binaries"),
        page_budget=2, transfer_budget=0, byte_limit=1,
    )

    assert len(commands) == 1
    assert report["coverage"] == "incomplete" and report["coverage_percentage"] is None
    assert "transfer_budget" in report["issues"]
    assert not list((job_dir / "collection").glob("*.media"))

    by_native = {a["native_id"]: a for a in report["assets"]}
    assert by_native["slide-img-1"]["kind"] == "image" and by_native["slide-img-1"]["date_eligibility"] == "in_window"
    assert by_native["slide-vid-2"]["kind"] == "video" and by_native["slide-vid-2"]["date_eligibility"] == "in_window"
    assert by_native["slide-img-1"]["item"] == "car1" and by_native["slide-vid-2"]["item"] == "car1"
    assert by_native["thumbnail:" + CDN + "/slide2-thumb.jpg"]["status"] == "thumbnail"
    assert by_native["old-img"]["date_eligibility"] == "out_of_window"
    assert by_native["undated-img"]["date_eligibility"] == "unknown_date"
    assert by_native["undated-img"]["asset_id"] in report["unknown_date_review"]
    assert by_native["end-img"]["date_eligibility"] == "in_window"
    assert by_native["slide-img-1"]["status"] == "pending"
    # Unknown-date and out-of-window items are excluded from automatic AI payload selection.
    selection = select_ai_candidates(report, job_dir)
    assert selection.candidates == []


def test_gallery_dl_error_6_triggers_single_instaloader_fallback_with_carousel_metadata(tmp_path):
    calls: list[str] = []

    def fake_runner(cmd: list[str], stage: Path) -> tuple[int, str]:
        _assert_no_bypass_flags(cmd)
        calls.append(cmd[0])
        if cmd[0] == "gallery-dl":
            return 0, json.dumps([[6, IG, {"message": "Instagram posts endpoint gated"}]])
        assert cmd[0] == "instaloader"
        assert "--quiet" in cmd and "--no-resume" in cmd and "--max-connection-attempts" in cmd
        assert "--no-pictures" in cmd and "--no-videos" in cmd and "--no-video-thumbnails" in cmd
        assert "--abort-on=429,403,401" in cmd and "--" in cmd
        # A Profile metadata sidecar alongside Post sidecars must be ignored cleanly.
        (stage / "lilysclubuk_12345.json").write_text(
            json.dumps({"instaloader": {"node_type": "Profile"}, "node": {"id": "12345", "username": "lilysclubuk"}}),
            encoding="utf-8",
        )
        sidecar = {
            "instaloader": {"version": "4.16", "node_type": "Post"},
            "node": {
                "id": "ig-car-99",
                "shortcode": "IgFallback99",
                "taken_at_timestamp": int(datetime.fromisoformat(WINDOW["window_end_utc"]).timestamp()),
                "edge_sidecar_to_children": {
                    "edges": [
                        {"node": {"id": "child-img", "is_video": False, "display_url": CDN + "/fb1.jpg"}},
                        {"node": {"id": "child-vid", "is_video": True, "video_url": CDN + "/fb2.mp4",
                                  "display_url": CDN + "/fb2-thumb.jpg"}},
                    ]
                },
            }
        }
        (stage / "2026-11-20_12-00-00_UTC.json").write_text(json.dumps(sidecar), encoding="utf-8")
        return 0, "Saved metadata only"

    job_dir = tmp_path / ("b" * 32)
    job_dir.mkdir()
    checkpoint = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
    inv = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=2, time_budget_seconds=30, runner=fake_runner,
    )
    report = checkpoint.run_source(
        "instagram", IG, inv, lambda *_: pytest.fail("transfer_budget=0 must never download"),
        page_budget=2, transfer_budget=0, byte_limit=1,
    )

    assert calls == ["gallery-dl", "instaloader"]
    assert "gallery_dl_error_6" in report["issues"]
    assert [t["tool"] for t in inv.tools_executed] == ["gallery-dl", "instaloader"]
    assert inv.tools_executed[0]["outcome"] == "gallery_dl_error_6"
    by_native = {a["native_id"]: a for a in report["assets"]}
    assert by_native["child-img"]["kind"] == "image" and by_native["child-img"]["date_eligibility"] == "in_window"
    assert by_native["child-vid"]["kind"] == "video" and by_native["child-vid"]["date_eligibility"] == "in_window"
    assert by_native["thumbnail:" + CDN + "/fb2-thumb.jpg"]["status"] == "thumbnail"


def test_429_rate_limit_stops_immediately_with_zero_fallback(tmp_path):
    calls: list[str] = []

    def fake_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        calls.append(cmd[0])
        return 1, "HttpError: 429 Too Many Requests - Please wait a few minutes"

    job_dir = tmp_path / ("c" * 32)
    job_dir.mkdir()
    checkpoint = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
    inv = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=3, time_budget_seconds=30, runner=fake_runner,
    )
    report = checkpoint.run_source("instagram", IG, inv, lambda *_: None, page_budget=2, transfer_budget=0, byte_limit=1)

    assert calls == ["gallery-dl"]
    assert report["stop_reason"] == "rate_limited"
    assert report["coverage"] == "incomplete" and report["coverage_percentage"] is None
    assert report["assets"] == []


@pytest.mark.parametrize(("output", "expected_stop"), [
    ("HTTP Error 401 Unauthorized: login required", "restricted"),
    ("403 Forbidden: checkpoint_required", "restricted"),
    ("cf-mitigated challenge detected", "restricted"),
])
def test_401_403_login_and_challenge_stop_immediately_with_no_fallback(tmp_path, output, expected_stop):
    calls: list[str] = []

    def fake_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        calls.append(cmd[0])
        return 1, output

    job_dir = tmp_path / ("d" * 32)
    job_dir.mkdir()
    checkpoint = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
    inv = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=3, time_budget_seconds=30, runner=fake_runner,
    )
    report = checkpoint.run_source("instagram", IG, inv, lambda *_: None, page_budget=2, transfer_budget=0, byte_limit=1)

    assert calls == ["gallery-dl"]
    assert report["stop_reason"] == expected_stop
    assert report["coverage_percentage"] is None


def test_facebook_people_profile_and_individual_video_ytdlp_fallback(tmp_path):
    # 1. Facebook /people/name/id/ URL: gallery-dl returns partial photo + trailing code 6; yt-dlp must NOT fire.
    people_calls: list[str] = []

    def people_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        _assert_no_bypass_flags(cmd)
        people_calls.append(cmd[0])
        return 0, json.dumps([
            [3, CDN + "/fb-photo.jpg", {
                "post_id": "fbp1", "post_url": "https://www.facebook.com/61564648418385/posts/101/",
                "media_id": "fb-img-1", "extension": "jpg", "post_date": WINDOW["window_end_utc"],
            }],
            [6, FB_PEOPLE, {"message": "album access gated"}],
        ])

    job_dir = tmp_path / ("e" * 32)
    job_dir.mkdir()
    cp = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
    inv = OpenSourceSocialInventory(
        "facebook", FB_PEOPLE, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=3, time_budget_seconds=30, runner=people_runner,
    )
    rep = cp.run_source("facebook", FB_PEOPLE, inv, lambda *_: None, page_budget=2, transfer_budget=0, byte_limit=1)
    assert people_calls == ["gallery-dl"]
    assert "gallery_dl_error_6" in rep["issues"]
    assert rep["stop_reason"] == "restricted"
    assert len(rep["assets"]) == 1 and rep["assets"][0]["date_eligibility"] == "in_window"

    # 2. Facebook /people/... with zero items + gallery_dl_error_6 must STILL never call yt-dlp.
    empty_people_calls: list[str] = []

    def empty_people_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        empty_people_calls.append(cmd[0])
        return 0, json.dumps([[6, FB_PEOPLE, {"message": "gated"}]])

    job_dir2 = tmp_path / ("f" * 32)
    job_dir2.mkdir()
    cp2 = CollectionCheckpoint(job_dir2, {"job_id": job_dir2.name, **WINDOW})
    inv2 = OpenSourceSocialInventory(
        "facebook", FB_PEOPLE, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=3, time_budget_seconds=30, runner=empty_people_runner,
    )
    rep2 = cp2.run_source("facebook", FB_PEOPLE, inv2, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert empty_people_calls == ["gallery-dl"]
    assert rep2["stop_reason"] == "restricted"

    # 3. Facebook individual public video URL: gallery-dl extractor error -> yt-dlp --dump-single-json --skip-download.
    video_calls: list[list[str]] = []

    def video_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        _assert_no_bypass_flags(cmd)
        video_calls.append(cmd)
        if cmd[0] == "gallery-dl":
            return 1, "ExtractorError: unable to extract video"
        assert cmd[0] == "yt-dlp" and "--dump-single-json" in cmd and "--skip-download" in cmd
        assert "--ignore-config" in cmd and "--no-cache-dir" in cmd
        return 0, json.dumps({
            "id": "987654321",
            "webpage_url": FB_VIDEO,
            "release_timestamp": int(datetime.fromisoformat(WINDOW["window_end_utc"]).timestamp()),
            "url": CDN + "/fb-watch.mp4",
            "thumbnail": CDN + "/fb-watch-thumb.jpg",
        })

    job_dir3 = tmp_path / ("1" * 32)
    job_dir3.mkdir()
    cp3 = CollectionCheckpoint(job_dir3, {"job_id": job_dir3.name, **WINDOW})
    inv3 = OpenSourceSocialInventory(
        "facebook", FB_VIDEO, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=2, time_budget_seconds=30, runner=video_runner,
    )
    rep3 = cp3.run_source("facebook", FB_VIDEO, inv3, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert [c[0] for c in video_calls] == ["gallery-dl", "yt-dlp"]
    by_native = {a["native_id"]: a for a in rep3["assets"]}
    assert by_native["987654321"]["kind"] == "video" and by_native["987654321"]["date_eligibility"] == "in_window"
    assert by_native["thumbnail:" + CDN + "/fb-watch-thumb.jpg"]["status"] == "thumbnail"


def test_shared_request_item_and_time_budgets_prevent_excess_calls(tmp_path):
    # Shared request_budget=1 blocks fallback after gallery-dl consumes the single attempt.
    calls: list[str] = []

    def gdl_only(cmd: list[str], _stage: Path) -> tuple[int, str]:
        calls.append(cmd[0])
        return 0, json.dumps([[6, IG, {"message": "gated"}]])

    job_dir = tmp_path / ("2" * 32)
    job_dir.mkdir()
    cp = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
    inv = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=1, time_budget_seconds=30, runner=gdl_only,
    )
    rep = cp.run_source("instagram", IG, inv, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert calls == ["gallery-dl"]
    assert rep["stop_reason"] == "request_budget"

    # Shared time_budget prevents fallback once wall clock exceeds time_budget_seconds.
    now = [100.0]

    def fake_clock() -> float:
        return now[0]

    def slow_gdl(cmd: list[str], _stage: Path) -> tuple[int, str]:
        now[0] += 15.0  # exhausts 10s budget during primary call
        return 0, json.dumps([[6, IG, {"message": "gated"}]])

    job_dir2 = tmp_path / ("3" * 32)
    job_dir2.mkdir()
    cp2 = CollectionCheckpoint(job_dir2, {"job_id": job_dir2.name, **WINDOW})
    inv2 = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=3, time_budget_seconds=10, runner=slow_gdl, clock=fake_clock,
    )
    rep2 = cp2.run_source("instagram", IG, inv2, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert rep2["stop_reason"] == "time_budget"

    # Shared item_limit caps discovered attachments and flags listing_cap.
    many_records = [
        [3, f"{CDN}/img-{i}.jpg", {
            "post_id": f"p{i}", "post_url": f"https://www.instagram.com/p/Post{i}/",
            "media_id": f"m{i}", "extension": "jpg", "post_date": WINDOW["window_end_utc"],
        }]
        for i in range(5)
    ]
    job_dir3 = tmp_path / ("4" * 32)
    job_dir3.mkdir()
    cp3 = CollectionCheckpoint(job_dir3, {"job_id": job_dir3.name, **WINDOW})
    inv3 = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=2, request_budget=2, time_budget_seconds=30,
        runner=lambda _c, _s: (0, json.dumps(many_records)),
    )
    rep3 = cp3.run_source("instagram", IG, inv3, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert len(rep3["assets"]) == 2
    assert "listing_cap" in rep3["issues"]


def test_public_collection_integration_preserves_provenance_duplicates_snapshot_fallback_and_m4_holds(
    worker, tmp_path, monkeypatch,
):
    shared_bytes = b"identical-grooming-photo-bytes"
    hdr = {"Content-Type": "image/jpeg", "Content-Length": str(len(shared_bytes))}
    seen_http: list[str] = []
    responses = {
        WEB + "robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        WEB: (200, {"Content-Type": "text/html"}, f'<html><body><img src="{CDN}/site.jpg"></body></html>'),
        CDN + "/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        CDN + "/site.jpg": (200, hdr, shared_bytes),
        CDN + "/ig-same.jpg": (200, hdr, shared_bytes),
        CDN + "/fb-snap.jpg": (200, hdr, shared_bytes),
    }
    monkeypatch.setattr(requests.Session, "get", _mock_website_get(responses, seen_http))

    cli_calls: list[list[str]] = []

    def fake_cli(cmd: list[str], _stage: Path) -> tuple[int, str]:
        cli_calls.append(cmd)
        # Duplicate attachment IDs within same post are deduplicated; cross-source identical SHA keeps separate IDs.
        return 0, json.dumps([
            [3, CDN + "/ig-same.jpg", {
                "post_id": "ig-dup-post", "post_url": "https://www.instagram.com/p/IgDup1/",
                "media_id": "dup-id", "extension": "jpg", "post_date": WINDOW["window_end_utc"],
            }],
            [3, CDN + "/ig-same.jpg", {
                "post_id": "ig-dup-post", "post_url": "https://www.instagram.com/p/IgDup1/",
                "media_id": "dup-id", "extension": "jpg", "post_date": WINDOW["window_end_utc"],
            }],
        ])

    # Instagram uses open_source_cli; Facebook exercises optional owner-provided snapshot fallback.
    cfg = {
        "sources": {
            "website": {
                "url": WEB, "sitemaps": [], "media_origins": [CDN],
                "request_budget": 10, "page_budget": 2, "document_limit": 4096, "entry_limit": 10,
                "transfer_budget": 4, "byte_limit": 1024, "time_budget_seconds": 30,
            },
            "instagram": {
                "url": IG, "discovery_mode": "open_source_cli", "download_authorized": True, "media_origins": [CDN],
                "request_budget": 2, "page_budget": 1, "entry_limit": 5,
                "transfer_budget": 4, "byte_limit": 1024, "time_budget_seconds": 30,
            },
            "facebook": {
                "url": FB_PEOPLE, "media_origins": [CDN],
                "request_budget": 5, "page_budget": 1,
                "transfer_budget": 4, "byte_limit": 1024, "time_budget_seconds": 30,
                "snapshot": {
                    "profile_url": FB_PEOPLE,
                    "posts": [{
                        "post_id": "fb-snap-1",
                        "source_url": "https://www.facebook.com/61564648418385/posts/555/",
                        "attachments": [{
                            "attachment_id": "fb-att-1", "kind": "image",
                            "source_url": CDN + "/fb-snap.jpg", "published_at": WINDOW["window_end_utc"],
                        }],
                    }],
                },
            },
        },
    }

    job_id = "5" * 32
    job_dir = worker.DATA_DIR / job_id
    job_dir.mkdir()
    job = {
        "job_id": job_id,
        "website_url": WEB,
        "instagram_url": IG,
        "facebook_url": FB_PEOPLE,
        "social_permission_confirmed": True,
        "run_ai_curation": True,
        "collection_mode": "public_checkpoint",
        **WINDOW,
    }
    worker.store(job)
    manifest = collect_public_job(job_dir, job, cfg, social_runner=fake_cli)

    assert len(cli_calls) == 1 and cli_calls[0][0] == "gallery-dl"
    assert manifest["collection_gate"] == "blocked_unverified_collection"
    assert manifest["sources"]["instagram"]["tools_executed"][0]["tool"] == "gallery-dl"
    assert all(s["coverage_percentage"] is None for s in manifest["sources"].values())

    # Identical bytes downloaded across website, IG, and FB retain 3 distinct asset_ids & provenance entries.
    assert len(manifest["assets"]) == 3
    assert len({a["asset_id"] for a in manifest["assets"]}) == 3
    assert {a["platform"] for a in manifest["assets"]} == {"website", "instagram", "facebook"}
    assert len(manifest["duplicate_provenance_groups"]) == 1
    assert len(manifest["duplicate_provenance_groups"][0]["asset_ids"]) == 3
    assert all(a["rights_status"] == "unverified" for a in manifest["assets"])

    # M4 approval, AI, and Drive delivery remain strictly blocked.
    class NoProvider:
        @property
        def name(self):
            pytest.fail("Blocked public_checkpoint collection must never reach AI")

    assert run_ai_curation(job_dir, job_id, provider=NoProvider()).status == "failed"
    assert build_delivery_plan(job_dir, job_id)["status"] in ("no_curation", "blocked_unverified_collection")
    assert request(worker.app, f"/jobs/{job_id}/delivery-result", "POST", "test-admin",
                   (b'{"authorization_confirmed":true}',))[0] == 409
    assert request(worker.app, "/named/collection-panel", "POST", "test-dashboard",
                   (json.dumps({"job_id": job_id}).encode(),))[0] == 404


def test_untrusted_captions_and_numeric_ids_do_not_spoof_errors_while_error_envelopes_stop(tmp_path):
    # Valid JSON payload with "429" inside numeric ID and "cookies"/"challenge" inside caption must NOT false-positive.
    benign_payload = [
        [3, CDN + "/dog-treat.jpg", {
            "category": "instagram",
            "post_id": "3429123456",
            "post_shortcode": "Treat429",
            "post_url": "https://www.instagram.com/p/Treat429/",
            "media_id": "94290001",
            "description": "Free dog cookies after every grooming challenge! 401/403 happy pups.",
            "extension": "jpg",
            "post_date": WINDOW["window_end_utc"],
        }],
    ]
    job_dir = tmp_path / ("6" * 32)
    job_dir.mkdir()
    cp = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
    inv = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=2, time_budget_seconds=30,
        runner=lambda _c, _s: (0, json.dumps(benign_payload)),
    )
    rep = cp.run_source("instagram", IG, inv, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert rep["stop_reason"] == "listing_exhausted"
    assert "transfer_budget" in rep["issues"]
    assert inv.tools_executed == [{"tool": "gallery-dl", "outcome": "discovered_1", "items": 1}]
    assert len(rep["assets"]) == 1 and rep["assets"][0]["native_id"] == "94290001"

    # Conversely, a gallery-dl code-6 envelope carrying a 429 rate-limit message MUST stop with zero fallback.
    calls: list[str] = []

    def rate_envelope_runner(cmd: list[str], _stage: Path) -> tuple[int, str]:
        calls.append(cmd[0])
        return 0, json.dumps([[6, IG, {"message": "429 Too Many Requests"}]])

    job_dir2 = tmp_path / ("7" * 32)
    job_dir2.mkdir()
    cp2 = CollectionCheckpoint(job_dir2, {"job_id": job_dir2.name, **WINDOW})
    inv2 = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=3, time_budget_seconds=30, runner=rate_envelope_runner,
    )
    rep2 = cp2.run_source("instagram", IG, inv2, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert calls == ["gallery-dl"]
    assert rep2["stop_reason"] == "rate_limited"


def _write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_real_subprocess_replay_and_timeout_cancellation(tmp_path, monkeypatch, allow_local_subprocess):
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    # 1. Real subprocess gallery-dl returns -1 UnboundLocalError -> real subprocess instaloader writes Post + Profile JSONs.
    _write_stub(
        stub_dir / "gallery-dl",
        'import json; print(json.dumps([[-1, {"error": "UnboundLocalError", "message": "cannot access local variable"}]]))',
    )
    post_ts = int(datetime.fromisoformat(WINDOW["window_end_utc"]).timestamp())
    _write_stub(
        stub_dir / "instaloader",
        "\n".join([
            "import json, sys",
            "from pathlib import Path",
            "stage = Path(sys.argv[sys.argv.index('--dirname-pattern') + 1])",
            "stage.mkdir(parents=True, exist_ok=True)",
            "profile = {'instaloader': {'node_type': 'Profile'}, 'node': {'id': '999', 'username': 'lilysclubuk'}}",
            "(stage / 'lilysclubuk_999.json').write_text(json.dumps(profile), encoding='utf-8')",
            "post = {'instaloader': {'node_type': 'Post'}, 'node': {'id': 'sub-post-1', 'shortcode': 'SubCode1', "
            f"'taken_at_timestamp': {post_ts}, 'edge_sidecar_to_children': {{'edges': ["
            f"{{'node': {{'id': 'sub-img-1', 'is_video': False, 'display_url': '{CDN}/sub1.jpg'}}}}, "
            f"{{'node': {{'id': 'sub-vid-2', 'is_video': True, 'video_url': '{CDN}/sub2.mp4', 'display_url': '{CDN}/sub2-t.jpg'}}}}"
            "]}}}",
            "(stage / '2026-11-20_12-00-00_UTC.json').write_text(json.dumps(post), encoding='utf-8')",
        ]),
    )
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    job_dir = tmp_path / ("8" * 32)
    job_dir.mkdir()
    cp = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
    inv = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=2, time_budget_seconds=15, runner=None,
    )
    rep = cp.run_source(
        "instagram", IG, inv, lambda *_: pytest.fail("transfer_budget=0 must never download"),
        page_budget=1, transfer_budget=0, byte_limit=1,
    )
    assert [t["tool"] for t in inv.tools_executed] == ["gallery-dl", "instaloader"]
    assert inv.tools_executed[0]["outcome"] == "extractor_broken"
    assert "gallery_dl_error_envelope" in rep["issues"]
    assert rep["coverage_percentage"] is None
    assert len(rep["items"]) == 1 and rep["items"][0]["id"] == "sub-post-1"
    by_native = {a["native_id"]: a for a in rep["assets"]}
    assert by_native["sub-img-1"]["kind"] == "image" and by_native["sub-img-1"]["date_eligibility"] == "in_window"
    assert by_native["sub-vid-2"]["kind"] == "video" and by_native["sub-vid-2"]["date_eligibility"] == "in_window"
    assert by_native["thumbnail:" + CDN + "/sub2-t.jpg"]["status"] == "thumbnail"

    # 2. Real subprocess timeout cancellation kills hung child cleanly and stops with time_budget (no fallback).
    slow_dir = tmp_path / "slow-stubs"
    slow_dir.mkdir()
    _write_stub(slow_dir / "gallery-dl", "import time; time.sleep(10)")
    _write_stub(slow_dir / "instaloader", "raise SystemExit(99)")
    monkeypatch.setenv("PATH", f"{slow_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    job_dir2 = tmp_path / ("9" * 32)
    job_dir2.mkdir()
    cp2 = CollectionCheckpoint(job_dir2, {"job_id": job_dir2.name, **WINDOW})
    inv2 = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=3, time_budget_seconds=1, runner=None,
    )
    rep2 = cp2.run_source("instagram", IG, inv2, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert [t["tool"] for t in inv2.tools_executed] == ["gallery-dl"]
    assert rep2["stop_reason"] == "time_budget"


def test_real_installed_cli_binaries_accept_generated_flags_offline(tmp_path, monkeypatch, allow_local_subprocess):
    # Dead loopback proxy ensures zero external network access even if pytest is invoked outside --network none.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    cutoff = date(2025, 11, 20)
    assert shutil.which("gallery-dl") and shutil.which("instaloader") and shutil.which("yt-dlp")

    g_cmd = build_gallery_dl_discovery_cmd(platform="facebook", url=FB_PEOPLE, cutoff=cutoff, item_limit=5, http_timeout=5)
    i_prof = build_instaloader_discovery_cmd(
        url_kind="profile", target_hint="lilysclubuk", stage=tmp_path / "il1", cutoff=cutoff, item_limit=5, http_timeout=5,
    )
    i_post = build_instaloader_discovery_cmd(
        url_kind="single_post", target_hint="CxReel123", stage=tmp_path / "il2", cutoff=cutoff, item_limit=5, http_timeout=5,
    )
    y_cmd = build_ytdlp_discovery_cmd(url=FB_VIDEO, http_timeout=5)
    for cmd in (g_cmd, i_prof, i_post, y_cmd):
        _assert_no_bypass_flags(cmd)

    # Real gallery-dl binary executed through OpenSourceSocialInventory offline emits [-1, ...] and stops cleanly.
    job_dir = tmp_path / ("0" * 32)
    job_dir.mkdir()
    cp = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})
    inv = OpenSourceSocialInventory(
        "facebook", FB_PEOPLE, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=2, time_budget_seconds=25, http_timeout=10, runner=None,
    )
    rep = cp.run_source("facebook", FB_PEOPLE, inv, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert inv.tools_executed == [{"tool": "gallery-dl", "outcome": "source_stopped", "items": 0}]
    assert rep["stop_reason"] == "source_stopped"
    assert rep["coverage_percentage"] is None

    # Real instaloader binary accepts both profile and single_post commands without argparse errors; filter compiles cleanly.
    for cmd in (i_prof, i_post):
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        combined = (res.stdout + "\n" + res.stderr).lower()
        assert "unrecognized arguments" not in combined and "usage:" not in combined
    import instaloader.__main__ as imain
    import instaloader.instaloader as il

    pf_expr = i_prof[i_prof.index("--post-filter") + 1]
    pf_fn = imain.filterstr_to_filterfunc(pf_expr, il.Post)
    assert pf_fn(type("P", (), {"date_utc": datetime(2026, 1, 1)})()) is True
    assert pf_fn(type("P", (), {"date_utc": datetime(2024, 1, 1)})()) is False

    # Real yt-dlp binary + option parser accept all generated flags with download disabled.
    import yt_dlp

    parsed = yt_dlp.parse_options(y_cmd[1:])
    assert parsed.ydl_opts.get("simulate") is True and parsed.ydl_opts.get("skip_download") is True
    assert parsed.ydl_opts.get("noplaylist") is True and parsed.ydl_opts.get("retries") == 0
    assert parsed.ydl_opts.get("cachedir") is False
    y_res = subprocess.run(y_cmd, capture_output=True, text=True, timeout=15)
    assert "no such option" not in (y_res.stdout + "\n" + y_res.stderr).lower()


def test_stage_3b4_facebook_photo_normalization_ids_canonical_urls_and_utc_dates(tmp_path):
    # Exact sanitized Stage 3B.4 Facebook gallery-dl record schema (job 2316b085c43142e892c02c366b69cdd5).
    fb_window = frozen_window(datetime(2026, 11, 24, 12, 18, 41, tzinfo=UTC))
    payload = [
        [2, {"category": "facebook", "subcategory": "photos", "set_id": "pb.61564648418385.-2207520000"}],
        [3, "https://scontent.fkhi22-1.fna.fbcdn.net/v/t51.82787-15/photo1.jpg?oh=TOKEN_A&oe=EXP_A", {
            "category": "facebook",
            "subcategory": "photos",
            "id": "122230723652488280",
            "set_id": "pb.61564648418385.-2207520000",
            "caption": "We're already taking the spookiest complimentary\nHalloween photos! Lily's Club Harrow & Wood Green.",
            "date": "2026-10-21 08:35:34",
            "extension": "jpg",
            "filename": "859082278_18125464087928802_3121580397243551685_n",
        }],
        [3, "https://scontent.fkhi22-1.fna.fbcdn.net/v/t51.82787-15/old_photo.jpg?oh=TOKEN_B&oe=EXP_B", {
            "category": "facebook",
            "subcategory": "photos",
            "id": "122100000000000001",
            "set_id": "pb.61564648418385.-2207520000",
            "date": "2024-05-01 10:00:00",
            "extension": "jpg",
        }],
        [3, "https://scontent.fkhi22-1.fna.fbcdn.net/v/t51.82787-15/no_set_photo.jpg", {
            "category": "facebook",
            "subcategory": "photo",
            "id": "122230999999999999",
            "date": "2026-10-22 14:00:00",
            "extension": "jpg",
        }],
        [3, "https://video.fkhi22-1.fna.fbcdn.net/v/t42/clip.mp4", {
            "category": "facebook",
            "subcategory": "video",
            "id": "99887766554433",
            "date": "2026-11-01 09:15:00",
            "extension": "mp4",
            "type": "video",
        }],
    ]

    job_dir = tmp_path / ("a" * 32)
    job_dir.mkdir()
    cp = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **fb_window})
    inv = OpenSourceSocialInventory(
        "facebook", FB_PEOPLE, permission_confirmed=True, window_start_utc=fb_window["window_start_utc"],
        item_limit=5, request_budget=2, time_budget_seconds=25,
        runner=lambda _c, _s: (0, json.dumps(payload)),
    )
    rep = cp.run_source(
        "facebook", FB_PEOPLE, inv, lambda *_: pytest.fail("transfer_budget=0 must never download"),
        page_budget=1, transfer_budget=0, byte_limit=1,
    )

    assert rep["coverage"] == "incomplete" and rep["coverage_percentage"] is None
    items_by_id = {item["id"]: item for item in rep["items"]}
    assets_by_native = {asset["native_id"]: asset for asset in rep["assets"]}

    # Native source-supplied IDs and canonical Facebook URLs preserved (not expiring CDN URLs).
    recent = assets_by_native["122230723652488280"]
    assert recent["identity_status"] == "source_supplied"
    assert items_by_id["122230723652488280"]["identity_status"] == "source_supplied"
    assert items_by_id["122230723652488280"]["url"] == (
        "https://www.facebook.com/photo/?fbid=122230723652488280&set=pb.61564648418385.-2207520000"
    )
    assert "Halloween photos" in (items_by_id["122230723652488280"].get("caption") or "")
    assert recent["post_date"] == "2026-10-21T08:35:34+00:00"
    assert recent["date_status"] == "verified" and recent["date_eligibility"] == "in_window"
    assert recent["publication_evidence"]["field"] == GALLERY_DL_FACEBOOK_UTC_FIELD
    assert recent["publication_evidence"]["original_timestamp"] == "2026-10-21 08:35:34"
    assert recent["publication_evidence"]["original_timezone"] == "UTC (gallery-dl time.gmtime)"
    assert verified_publication(recent) == datetime(2026, 10, 21, 8, 35, 34, tzinfo=UTC)
    assert "raw_date_evidence" not in recent

    # Out-of-window photo, no-set photo, and video canonical URLs + eligibility.
    assert assets_by_native["122100000000000001"]["date_eligibility"] == "out_of_window"
    assert items_by_id["122230999999999999"]["url"] == "https://www.facebook.com/photo/?fbid=122230999999999999"
    assert assets_by_native["122230999999999999"]["date_eligibility"] == "in_window"
    assert items_by_id["99887766554433"]["url"] == "https://www.facebook.com/watch/?v=99887766554433"
    assert assets_by_native["99887766554433"]["kind"] == "video"
    assert assets_by_native["99887766554433"]["date_eligibility"] == "in_window"
    assert rep["unknown_date_review"] == []

    # Re-discovering the same Facebook photo with rotated CDN query tokens yields the exact same asset_id.
    rotated_payload = [[3, "https://scontent.fkhi17-3.fna.fbcdn.net/v/t51.82787-15/photo1.jpg?oh=ROTATED&oe=NEW", {
        "category": "facebook",
        "subcategory": "photos",
        "id": "122230723652488280",
        "set_id": "pb.61564648418385.-2207520000",
        "date": "2026-10-21 08:35:34",
        "extension": "jpg",
    }]]
    job_dir2 = tmp_path / ("b" * 32)
    job_dir2.mkdir()
    cp2 = CollectionCheckpoint(job_dir2, {"job_id": job_dir2.name, **fb_window})
    inv2 = OpenSourceSocialInventory(
        "facebook", FB_PEOPLE, permission_confirmed=True, window_start_utc=fb_window["window_start_utc"],
        item_limit=5, request_budget=2, time_budget_seconds=25,
        runner=lambda _c, _s: (0, json.dumps(rotated_payload)),
    )
    rep2 = cp2.run_source("facebook", FB_PEOPLE, inv2, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert rep2["assets"][0]["asset_id"] == recent["asset_id"]


def test_facebook_uncertain_dates_avatar_malformed_ids_and_instagram_strict_dates(tmp_path):
    fb_window = frozen_window(datetime(2026, 11, 24, 12, 18, 41, tzinfo=UTC))
    payload = [
        # 1. Date-only string (uncertain precision/timezone) -> preserved in raw_date_evidence, NOT in_window.
        [3, f"{CDN}/date-only.jpg", {
            "category": "facebook", "subcategory": "photos", "id": "122230000000000010",
            "set_id": "pb.61564648418385.-2207520000", "date": "2026-10-21", "extension": "jpg",
        }],
        # 2. Profile avatar subcategory -> never treated as attributable historical post or verified UTC.
        [3, f"{CDN}/avatar.jpg", {
            "category": "facebook", "subcategory": "avatar", "id": "122230000000000011",
            "date": "2026-10-21 08:35:34", "extension": "jpg",
        }],
        # 3. Missing/unknown subcategory -> uncertain provenance; preserve raw date, stay unknown_date.
        [3, f"{CDN}/unknown-sub.jpg", {
            "category": "facebook", "id": "122230000000000012",
            "date": "2026-10-21 08:35:34", "extension": "jpg",
        }],
        # 4. Malformed non-numeric Facebook ID -> do not fabricate canonical URL or verify date.
        [3, f"{CDN}/bad-id.jpg", {
            "category": "facebook", "subcategory": "photos", "id": "not-a-numeric-id",
            "date": "2026-10-21 08:35:34", "extension": "jpg",
        }],
    ]

    job_dir = tmp_path / ("c" * 32)
    job_dir.mkdir()
    cp = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **fb_window})
    inv = OpenSourceSocialInventory(
        "facebook", FB_PEOPLE, permission_confirmed=True, window_start_utc=fb_window["window_start_utc"],
        item_limit=5, request_budget=2, time_budget_seconds=25,
        runner=lambda _c, _s: (0, json.dumps(payload)),
    )
    rep = cp.run_source("facebook", FB_PEOPLE, inv, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)

    assert len(rep["assets"]) == 4
    assert len(rep["unknown_date_review"]) == 4
    for asset in rep["assets"]:
        assert asset["post_date"] is None
        assert asset["date_status"] == "unknown_date"
        assert asset["date_eligibility"] == "unknown_date"
        assert asset["publication_evidence"] is None
        assert asset["raw_date_evidence"]["field"] == "date"

    items_by_id = {item["id"]: item for item in rep["items"]}
    # Valid numeric photo with uncertain date keeps native ID & canonical URL, while avatar/malformed do NOT.
    assert items_by_id["122230000000000010"]["identity_status"] == "source_supplied"
    assert items_by_id[f"{CDN}/avatar.jpg"]["url"] == FB_PEOPLE
    assert items_by_id[f"{CDN}/bad-id.jpg"]["url"] == FB_PEOPLE

    # Instagram and non-Facebook URLs can NEVER use gallery_dl_facebook_utc_date naive UTC parsing.
    assert publication_evidence(
        "2026-10-21 08:35:34",
        source_url="https://www.instagram.com/p/TestCode/",
        field=GALLERY_DL_FACEBOOK_UTC_FIELD,
        attribution="attempt",
        gallery_dl_facebook_gmtime_utc=True,
    ) is None
    assert publication_evidence(
        "2026-10-21 08:35:34",
        source_url="https://www.facebook.com/photo/?fbid=122230723652488280",
        field="date",
        attribution="generic date rejected",
        gallery_dl_facebook_gmtime_utc=False,
    ) is None


def test_instagram_not_found_error_classification_and_bounded_instaloader_crosscheck(tmp_path):
    gdl_not_found = json.dumps([[-1, {"error": "NotFoundError", "message": "Requested user could not be found"}]])
    post_ts = int(datetime.fromisoformat(WINDOW["window_end_utc"]).timestamp())

    # 1. Exact Stage 3B.4 NotFoundError -> ONE bounded Instaloader cross-check on unchanged handle succeeds.
    calls_ok: list[list[str]] = []

    def crosscheck_success(cmd: list[str], stage: Path) -> tuple[int, str]:
        _assert_no_bypass_flags(cmd)
        calls_ok.append(cmd)
        if cmd[0] == "gallery-dl":
            return 0, gdl_not_found
        assert cmd[0] == "instaloader" and cmd[-2:] == ["--", "lilysclubuk"]
        sidecar = {
            "instaloader": {"version": "4.16", "node_type": "Post"},
            "node": {
                "id": "ig-cc-1", "shortcode": "IgCc1", "taken_at_timestamp": post_ts,
                "is_video": False, "display_url": f"{CDN}/ig-cc-1.jpg",
            },
        }
        (stage / "2026-11-20_12-00-00_UTC.json").write_text(json.dumps(sidecar), encoding="utf-8")
        return 0, ""

    d1 = tmp_path / ("d" * 32)
    d1.mkdir()
    cp1 = CollectionCheckpoint(d1, {"job_id": d1.name, **WINDOW})
    inv1 = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=2, time_budget_seconds=25, runner=crosscheck_success,
    )
    rep1 = cp1.run_source(
        "instagram", IG, inv1, lambda *_: pytest.fail("transfer_budget=0 must never download"),
        page_budget=1, transfer_budget=0, byte_limit=1,
    )
    assert [c[0] for c in calls_ok] == ["gallery-dl", "instaloader"]
    assert inv1.tools_executed[0]["outcome"] == "user_lookup_not_found"
    assert "gallery_dl_user_lookup_unverified" in rep1["issues"]
    assert rep1["coverage"] == "incomplete" and rep1["coverage_percentage"] is None
    assert len(rep1["assets"]) == 1 and rep1["assets"][0]["date_eligibility"] == "in_window"

    # 2. Instaloader cross-check reports profile does not exist -> distinct profile_not_found -> access_blocked.
    calls_missing: list[str] = []

    def crosscheck_nonexistent(cmd: list[str], _stage: Path) -> tuple[int, str]:
        calls_missing.append(cmd[0])
        if cmd[0] == "gallery-dl":
            return 0, gdl_not_found
        return 1, "Profile lilysclubuk does not exist."

    d2 = tmp_path / ("e" * 32)
    d2.mkdir()
    cp2 = CollectionCheckpoint(d2, {"job_id": d2.name, **WINDOW})
    inv2 = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=2, time_budget_seconds=25, runner=crosscheck_nonexistent,
    )
    rep2 = cp2.run_source("instagram", IG, inv2, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert calls_missing == ["gallery-dl", "instaloader"]
    assert [t["outcome"] for t in inv2.tools_executed] == ["user_lookup_not_found", "profile_not_found"]
    assert rep2["stop_reason"] == "access_blocked"
    assert rep2["coverage"] == "incomplete" and rep2["coverage_percentage"] is None

    # 3. Instaloader cross-check encounters explicit 401/429 restriction -> stops cleanly as restricted / rate_limited.
    for il_msg, expected in (
        ("HTTP Error 401 Unauthorized: Login required", "restricted"),
        ("429 Too Many Requests - Please wait a few minutes", "rate_limited"),
    ):
        d3 = tmp_path / ("f" * 31 + ("1" if expected == "restricted" else "2"))
        d3.mkdir()
        cp3 = CollectionCheckpoint(d3, {"job_id": d3.name, **WINDOW})
        inv3 = OpenSourceSocialInventory(
            "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
            item_limit=5, request_budget=2, time_budget_seconds=25,
            runner=lambda cmd, _s, msg=il_msg: (0, gdl_not_found) if cmd[0] == "gallery-dl" else (1, msg),
        )
        rep3 = cp3.run_source("instagram", IG, inv3, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
        assert rep3["stop_reason"] == expected and rep3["coverage_percentage"] is None

    # 4. gallery-dl NotFoundError WITH explicit 403/login/429 signal stops IMMEDIATELY with ZERO fallback.
    calls_gated: list[str] = []

    def gated_not_found(cmd: list[str], _stage: Path) -> tuple[int, str]:
        calls_gated.append(cmd[0])
        return 0, json.dumps([[-1, {"error": "NotFoundError", "message": "Requested user could not be found; 403 Forbidden login required"}]])

    d4 = tmp_path / ("1" * 31 + "a")
    d4.mkdir()
    cp4 = CollectionCheckpoint(d4, {"job_id": d4.name, **WINDOW})
    inv4 = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=3, time_budget_seconds=25, runner=gated_not_found,
    )
    rep4 = cp4.run_source("instagram", IG, inv4, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert calls_gated == ["gallery-dl"]
    assert rep4["stop_reason"] == "restricted"

    # 5. allow_fallbacks=False stops as source_stopped (does NOT claim handle is nonexistent).
    d5 = tmp_path / ("1" * 31 + "b")
    d5.mkdir()
    cp5 = CollectionCheckpoint(d5, {"job_id": d5.name, **WINDOW})
    inv5 = OpenSourceSocialInventory(
        "instagram", IG, permission_confirmed=True, window_start_utc=WINDOW["window_start_utc"],
        item_limit=5, request_budget=2, time_budget_seconds=25, allow_fallbacks=False,
        runner=lambda _c, _s: (0, gdl_not_found),
    )
    rep5 = cp5.run_source("instagram", IG, inv5, lambda *_: None, page_budget=1, transfer_budget=0, byte_limit=1)
    assert [t["outcome"] for t in inv5.tools_executed] == ["user_lookup_not_found"]
    assert rep5["stop_reason"] == "source_stopped" and rep5["coverage_percentage"] is None
