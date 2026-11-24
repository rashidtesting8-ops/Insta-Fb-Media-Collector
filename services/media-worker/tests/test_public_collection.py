import json
from datetime import UTC, datetime

import pytest
import requests

from app.ai.pipeline import run_ai_curation
from app.delivery import build_delivery_plan
from app.collection_checkpoint import Attachment, CollectionCheckpoint, InventoryPage, SourceItem, SourceStop
from app.public_collection import PublicHTTP, PublicSocialSnapshot, check_response
from app.publication import frozen_window
from tests.test_phase9_security import request, worker  # noqa: F401


WEB = "https://www.lilys-club.co.uk"
IG = "https://www.instagram.com/lilysclubuk/"
FB = "https://www.facebook.com/people/Lilys-club-Dog-Grooming/61564648418385/"
CDN = "https://recorded-media.test"
URLS = {"website_url": WEB + "/", "instagram_url": IG, "facebook_url": FB}
WINDOW = frozen_window(datetime(2026, 11, 20, tzinfo=UTC))
LIMITS = {"page_budget": 8, "transfer_budget": 8, "byte_limit": 1024,
          "time_budget_seconds": 30, "request_budget": 32, "media_origins": [CDN]}


def recorded_get(responses, seen):
    def get(session, url, **kwargs):
        assert not session.cookies
        assert not session.trust_env and kwargs["allow_redirects"] is False
        seen.append((url, kwargs.get("headers", {})))
        assert url in responses, "Off-scope or unexpected request: " + url
        status, headers, body = responses[url]
        response = requests.Response()
        response.status_code = status
        response.url = url
        response.headers.update(headers)
        response._content = body.encode() if isinstance(body, str) else body
        response._content_consumed = True
        return response
    return get


def test_opt_in_worker_collects_from_guarded_public_records_and_holds_ai(worker, tmp_path, monkeypatch):
    seen = []
    image = b"recorded image bytes"
    common = {"Content-Type": "image/jpeg", "Content-Length": str(len(image))}
    responses = {
        WEB + "/robots.txt": (200, {}, "User-agent: *\nDisallow: /archive/private\n"),
        WEB + "/": (200, {}, '<a href="/archive">Archive</a>'),
        WEB + "/sitemap.xml": (200, {}, "<sitemapindex><sitemap><loc>/nested.xml</loc></sitemap></sitemapindex>"),
        WEB + "/nested.xml": (200, {}, "<urlset><url><loc>/archive/private</loc></url></urlset>"),
        WEB + "/archive": (200, {}, '<a rel="next" href="/archive?p=2">Next</a>'),
        WEB + "/archive?p=2": (200, {}, '<img src="' + CDN + '/website.jpg"><img src="' + CDN + '/blocked.jpg">'),
        CDN + "/robots.txt": (200, {}, "User-agent: *\nDisallow: /blocked.jpg\n"),
        CDN + "/website.jpg": (200, common, image),
        CDN + "/social.jpg": (200, common, image),
        CDN + "/old.jpg": (200, common, image),
        CDN + "/redirect.jpg": (302, {"Location": "https://unapproved.test/file.jpg"}, b""),
    }
    monkeypatch.setattr(requests.Session, "get", recorded_get(responses, seen))
    config = {
        "sources": {
            "website": {**LIMITS, "url": URLS["website_url"], "sitemaps": [WEB + "/sitemap.xml"],
                        "document_limit": 4096, "entry_limit": 12},
            "instagram": {**LIMITS, "url": IG, "snapshot": {"profile_url": IG, "truncated": True, "records": [
                [3, CDN + "/old.jpg", {"post_url": "https://www.instagram.com/p/old/", "post_shortcode": "old",
                                      "media_id": "old-photo", "extension": "jpg", "date": "2021-01-01T00:00:00Z"}],
                [3, CDN + "/social.jpg", {"post_url": "https://www.instagram.com/p/new/", "post_shortcode": "new",
                                         "extension": "jpg", "date": WINDOW["window_start_utc"]}],
                [3, CDN + "/cover.jpg", {"post_url": "https://www.instagram.com/p/new/", "post_shortcode": "new",
                                         "extension": "jpg", "media_type": "thumbnail"}],
            ]}},
            "facebook": {**LIMITS, "url": FB, "snapshot": {"profile_url": FB, "posts": [
                {"post_id": "album", "source_url": "https://www.facebook.com/example/posts/1/",
                 "expected_attachment_ids": ["video", "missing"],
                 "attachments": [{"attachment_id": "video", "kind": "video", "source_url": CDN + "/redirect.jpg"}]},
            ]}},
        },
    }
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("PUBLIC_COLLECTION_CHECKPOINTS", "1")
    monkeypatch.setenv("PUBLIC_COLLECTION_CONFIG", str(path))
    monkeypatch.setattr(worker, "frozen_window", lambda _: WINDOW)
    scheduled = []
    monkeypatch.setattr(worker.executor, "submit", lambda *args: scheduled.append(args))
    code, _, data = request(worker.app, "/jobs", "POST", "test-admin", (json.dumps({
        **URLS, "run_ai_curation": True, "social_permission_confirmed": True,
    }).encode(),))
    assert code == 202
    job_id = json.loads(data)["job_id"]
    function, *args = scheduled.pop()
    function(*args)
    record = worker.fetch(job_id)
    assert record["status"] == "partial_success", record
    assert record["ai_curation"]["status"] == "blocked_unverified_collection"
    directory = worker.DATA_DIR / job_id
    manifest = json.loads((directory / "manifest.json").read_text())
    assert {key: manifest[key] for key in WINDOW} == WINDOW
    assert all(s["coverage_percentage"] is None for s in manifest["sources"].values())
    assert "truncated_listing" in manifest["sources"]["instagram"]["issues"]
    assert any(item["status"] == "access_blocked" for item in manifest["sources"]["website"]["items"])
    by_url = {asset.get("source_url"): asset for asset in manifest["assets"]}
    first, second = (by_url[CDN + "/" + name + ".jpg"] for name in ("website", "social"))
    assert first["sha256"] == second["sha256"] and first["asset_id"] != second["asset_id"]
    assert second["identity_status"] == "derived" and second["date_eligibility"] == "in_window"
    assert by_url[CDN + "/old.jpg"]["date_eligibility"] == "out_of_window"
    assert first["asset_id"] in manifest["unknown_date_review"]
    assert by_url[CDN + "/cover.jpg"]["status"] == "thumbnail"
    assert by_url[CDN + "/redirect.jpg"]["status"] == "access_blocked"
    assert any(a["status"] == "missing_attachment" for a in manifest["assets"])
    assert all(a["rights_status"] == "unverified" for a in manifest["assets"])
    assert len([url for url, _ in seen if url == CDN + "/redirect.jpg"]) == 1

    class NoProvider:
        @property
        def name(self):
            pytest.fail("Held job reached the AI provider")

    assert run_ai_curation(directory, job_id, provider=NoProvider()).status == "failed"
    (directory / "curation.json").write_text(json.dumps({
        "ai_envelope": {"mock": False},
        "curation": {"analysis_coverage": {"mock": False}, "selected_asset_ids": [first["asset_id"]]},
    }))
    assert build_delivery_plan(directory, job_id)["status"] == "blocked_unverified_collection"
    assert request(worker.app, f"/jobs/{job_id}/delivery-result", "POST", "test-admin",
                   (b'{"authorization_confirmed":true}',))[0] == 409


@pytest.mark.parametrize(("status", "headers", "reason"), [
    (429, {}, "rate_limited"),
    (403, {}, "restricted"),
    (503, {"cf-mitigated": "challenge"}, "restricted"),
])
def test_http_denials_are_classified(status, headers, reason):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers)
    with pytest.raises(SourceStop, match=reason):
        check_response(response)


@pytest.mark.parametrize(("snapshot", "consent", "reason"), [
    (None, True, "public_inventory_unavailable"),
    ({"profile_url": FB, "posts": []}, True, "access_blocked"),
    ({"profile_url": IG, "posts": []}, False, "access_blocked"),
    ({"profile_url": IG, "stop": "login_required"}, True, "restricted"),
    ({"profile_url": IG, "stop": "rate_limited"}, True, "rate_limited"),
])
def test_snapshot_access_gates_have_no_continuation(snapshot, consent, reason):
    with pytest.raises(SourceStop, match=reason):
        PublicSocialSnapshot("instagram", IG, snapshot, consent).page(None)


def test_guarded_range_requires_strong_revision_and_correct_total(monkeypatch):
    seen = []
    monkeypatch.setattr(requests.Session, "get", recorded_get({
        CDN + "/robots.txt": (200, {}, "User-agent: *\nAllow: /\n"),
        CDN + "/range.mp4": (206, {"Content-Type": "video/mp4", "Content-Length": "3",
                                 "Content-Range": "bytes 2-4/5", "ETag": '"recorded"'}, b"end"),
    }, seen))
    from app.legacy_collector import build_collector_session

    with build_collector_session() as session:
        transport = PublicHTTP(WEB, LIMITS, session)
        response = transport.download(CDN + "/range.mp4", 2, '"recorded"')
        try:
            assert (response.size, response.offset, response.revision) == (5, 2, '"recorded"')
            assert b"".join(response.chunks) == b"end"
        finally:
            response.close()
    assert seen[-1][1]["If-Range"] == '"recorded"'


def test_zero_transfer_budget_records_discovery_and_never_fetches_bytes(tmp_path):
    job_dir = tmp_path / "discovery"
    job_dir.mkdir()
    checkpoint = CollectionCheckpoint(job_dir, {"job_id": job_dir.name, **WINDOW})

    class Inventory:
        def page(self, cursor):
            return InventoryPage((SourceItem("page", WEB, (Attachment("photo", CDN + "/image.jpg", "image"),)),))

    report = checkpoint.run_source(
        "website", WEB, Inventory(), lambda *_: pytest.fail("discovery-only job fetched an attachment"),
        page_budget=1, transfer_budget=0, byte_limit=1,
    )
    assert report["assets"][0]["status"] == "pending" and report["coverage"] == "incomplete"
    assert report["issues"] == ["transfer_budget"]
