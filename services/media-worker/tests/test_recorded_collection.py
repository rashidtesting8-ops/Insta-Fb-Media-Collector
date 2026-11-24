import base64
import hashlib
import json
import subprocess
from datetime import UTC, datetime

import pytest
import requests

from app.ai.pipeline import run_ai_curation
from app.delivery import build_delivery_plan
from app.recorded_collection import RecordedSocialInventory
from app.publication import frozen_window
from tests.test_phase9_security import request, worker  # noqa: F401

WEB = "https://www.lilys-club.co.uk/"
IG = "https://www.instagram.com/lilysclubuk/"
FB = "https://www.facebook.com/people/Lilys-club-Dog-Grooming/61564648418385/"
MEDIA = "https://recorded.invalid/"
WINDOW = frozen_window(datetime(2026, 11, 20, tzinfo=UTC))
URLS = {"website_url": WEB, "instagram_url": IG, "facebook_url": FB}
LIMITS = {"page_budget": 1, "transfer_budget": 8, "byte_limit": 512}


def binary(data, **extra):
    return {"base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest(),
            "revision": "v1", **extra}


def ig_record(name, post, kind="jpg", **meta):
    return [3, MEDIA + name, {"post_shortcode": post, "post_url": "https://www.instagram.com/p/" + post + "/",
                              "extension": kind, **meta}]


def config():
    token = {"token": "recorded-next", "extractor": "gallery-dl", "profile_url": IG,
             "provenance": "recorded_extractor"}
    return {"offline_only": True, "sources": {
        "website": {**LIMITS, "url": WEB, "sitemaps": [], "document_limit": 4096, "entry_limit": 10,
                    "robots": "User-agent: *\nDisallow: /blocked.jpg\n",
                    "documents": {WEB: {"body": '<img src="/site.jpg"><img src="/blocked.jpg">'}},
                    "files": {WEB + "site.jpg": binary(b"fixture website image")}},
        "instagram": {**LIMITS, "url": IG, "extractor": "gallery-dl", "files": {
            MEDIA + "old.jpg": binary(b"old pinned"),
            MEDIA + "first.jpg": binary(b"same bytes", interrupt_after=4),
            MEDIA + "reel.mp4": binary(b"recorded media"),
        }, "recording": {"profile_url": IG, "extractor": "gallery-dl", "pages": [
            {"cursor": None, "continuation": token, "records": [
                ig_record("old.jpg", "old", media_id="old-photo", post_date="2020-01-01T00:00:00Z"),
                ig_record("first.jpg", "carousel", media_id="first", post_date=WINDOW["window_start_utc"]),
                ig_record("ambiguous.jpg", "carousel", media_id="ambiguous", post_date=WINDOW["window_end_utc"],
                          taken_at_timestamp=1),
            ], "expected_children": {"carousel": {"ids": ["first", "missing"], "count": 3,
                                                 "provenance": "recorded_extractor", "reference": "carousel record"}}},
            {"cursor": "recorded-next", "truncated": True, "records": [
                ig_record("reel.mp4", "reel", "mp4", media_id="video", post_date=WINDOW["window_end_utc"]),
                ig_record("cover.jpg", "reel", media_type="thumbnail"),
            ]},
        ]}},
        "facebook": {**LIMITS, "url": FB, "extractor": "gallery-dl",
                     "files": {MEDIA + "album.jpg": binary(b"same bytes")},
                     "recording": {"profile_url": FB, "extractor": "gallery-dl", "pages": [
            {"cursor": None, "records": [[3, MEDIA + "album.jpg", {
                "post_id": "album", "post_url": "https://www.facebook.com/example/posts/123/",
                "extension": "jpg", "date": "2026-01-01T12:00:00+00:00",
            }]], "expected_children": {"album": {"count": 2, "provenance": "recorded_extractor",
                                                  "reference": "album record"}}},
        ]}},
    }}


def test_real_worker_recorded_path_reopens_checkpoint_and_holds_delivery(worker, tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(WINDOW["created_at"]).astimezone(tz)

    def forbidden(*_, **__):
        pytest.fail("Recorded worker attempted HTTP or an external program")

    monkeypatch.setattr(worker._dt, "datetime", FrozenDateTime)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("RECORDED_EXTRACTOR_HANDOFF", "1")
    monkeypatch.setenv("PUBLIC_COLLECTION_CHECKPOINTS", "1")
    path = tmp_path / "recording.json"
    recording = config()
    path.write_text(json.dumps(recording))
    monkeypatch.setenv("RECORDED_EXTRACTOR_CONFIG", str(path))
    scheduled = []
    monkeypatch.setattr(worker.executor, "submit", lambda *args: scheduled.append(args))
    body = (json.dumps({**URLS, "idempotency_key": "recorded-retry", "run_ai_curation": True,
                       "lookback_days": 1, "collection_mode": "public_checkpoint"}).encode(),)
    status, _, result = request(worker.app, "/jobs", "POST", "test-admin", body)
    assert status == 202
    job_id = json.loads(result)["job_id"]
    function, *args = scheduled.pop()
    function(*args)
    directory = worker.DATA_DIR / job_id
    first = json.loads((directory / "manifest.json").read_text())
    assert first["collection_mode"] == "recorded_extractor"
    assert first["sources"]["instagram"]["cursor"] == "recorded-next"
    assert any(asset["status"] == "download_interrupted" for asset in first["assets"])

    # Simulate the persisted interrupted state set by worker startup recovery.
    worker.store({**worker.fetch(job_id), "status": "interrupted"})
    wrong = (json.dumps({**URLS, "website_url": "https://different.test/",
                        "idempotency_key": "recorded-retry"}).encode(),)
    assert request(worker.app, "/jobs", "POST", "test-admin", wrong)[0] == 409
    recording["sources"]["instagram"]["recording"]["pages"].pop(0)
    recording["sources"]["instagram"]["files"][MEDIA + "first.jpg"] = binary(b"same bytes")
    path.write_text(json.dumps(recording))
    status, _, result = request(worker.app, "/jobs", "POST", "test-admin", body)
    assert status == 202
    assert json.loads(result) == {"job_id": job_id, "status": "queued", "idempotent_reuse": True}
    function, *args = scheduled.pop()
    function(*args)
    final = json.loads((directory / "manifest.json").read_text())
    assert {key: final[key] for key in WINDOW} == WINDOW
    assert all(s["coverage"] == "unknown" and s["coverage_percentage"] is None for s in final["sources"].values())
    assert final["sources"]["instagram"]["stop_reason"] == "truncated_listing"
    by_id = {asset["native_id"]: asset for asset in final["assets"]}
    assert by_id["first"]["source_shortcode"] == "carousel"
    assert by_id["first"]["date_eligibility"] == "in_window"
    assert by_id["old-photo"]["date_eligibility"] == "out_of_window"
    assert by_id["ambiguous"]["asset_id"] in final["unknown_date_review"]
    assert by_id["missing"]["status"] == "missing_attachment"
    assert by_id["thumbnail:" + MEDIA + "cover.jpg"]["collection_outcome"] == "skipped"
    assert "missing_child_count" in final["sources"]["facebook"]["issues"]
    assert not any(a["status"] == "missing_attachment" and a["platform"] == "facebook" for a in final["assets"])
    assert final["duplicate_provenance_groups"] and all(a["rights_status"] == "unverified" for a in final["assets"])

    class NoProvider:
        @property
        def name(self):
            pytest.fail("Held recording reached the AI provider")

    assert run_ai_curation(directory, job_id, provider=NoProvider()).status == "failed"
    (directory / "curation.json").write_text(json.dumps({
        "ai_envelope": {"mock": False},
        "curation": {"analysis_coverage": {"mock": False}, "selected_asset_ids": [by_id["first"]["asset_id"]]},
    }))
    assert build_delivery_plan(directory, job_id)["status"] == "blocked_unverified_collection"
    assert request(worker.app, f"/jobs/{job_id}/delivery-result", "POST", "test-admin",
                   (b'{"authorization_confirmed":true}',))[0] == 409
    plan = request(worker.app, f"/jobs/{job_id}/delivery-plan", key="test-admin")
    assert json.loads(plan[2])["status"] == "blocked_unverified_collection"


@pytest.mark.parametrize(("stop", "expected"), [
    ("login_required", "restricted"), ("challenge", "restricted"), ("access_denied", "access_blocked"),
    ("rate_limited", "rate_limited"), ("interrupted", "listing_interrupted"), ("cap", "listing_cap"),
])
def test_recorded_stops_keep_discovered_records(stop, expected):
    recording = config()["sources"]["instagram"]["recording"]
    recording["pages"][0]["stop"] = stop
    result = RecordedSocialInventory("instagram", IG, recording).page(None)
    assert result.items and result.next_cursor is None and result.stop_reason == expected


def test_invalid_continuation_does_not_erase_discovered_posts():
    recording = config()["sources"]["instagram"]["recording"]
    recording["pages"][0]["continuation"]["profile_url"] = FB
    result = RecordedSocialInventory("instagram", IG, recording).page(None)
    assert result.items and result.next_cursor is None and result.stop_reason == "invalid_continuation"


def test_unattributed_child_count_remains_unknown():
    recording = config()["sources"]["instagram"]["recording"]
    recording["pages"][0]["expected_children"]["carousel"] = {"ids": ["invented"], "count": 500}
    result = RecordedSocialInventory("instagram", IG, recording).page(None)
    carousel = next(item for item in result.items if item.id == "carousel")
    assert carousel.expected_count is None and not carousel.expected_ids
    assert "unverified_child_expectation" in result.issues


@pytest.mark.parametrize(("platform", "profile", "extractor", "raw"), [
    ("instagram", IG, "instaloader", {"node": {"id": "ig-post", "shortcode": "album", "taken_at_timestamp": 1760000000,
     "edge_sidecar_to_children": {"edges": [{"node": {"id": "child", "is_video": True, "video_url": MEDIA + "ig.mp4",
                                                   "display_url": MEDIA + "ig.jpg"}}]}}}),
    ("facebook", FB, "yt-dlp", {"id": "fb-post", "webpage_url": "https://www.facebook.com/watch/?v=123",
                               "release_timestamp": 1760000000, "url": MEDIA + "fb.mp4",
                               "thumbnail": MEDIA + "fb.jpg"}),
])
def test_local_sidecar_preserves_video_separately_from_thumbnail(platform, profile, extractor, raw):
    page = RecordedSocialInventory(platform, profile, {"profile_url": profile, "extractor": extractor,
        "pages": [{"cursor": None, "records": [raw]}]}).page(None)
    video, thumbnail = page.items[0].attachments
    assert [video.kind, thumbnail.kind] == ["video", "thumbnail"]
    assert video.identity_status == "source_supplied" and thumbnail.identity_status == "derived"
    assert video.publication_evidence["original_timestamp"] == 1760000000
