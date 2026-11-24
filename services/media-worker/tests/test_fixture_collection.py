import base64
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from app.fixture_collection import run_fixture_collection
from app.manifest import validate_against_disk
from app.publication import frozen_window, publication_evidence


WEBSITE = "https://fixture.test"
INSTA = "https://www.instagram.com/fixture/"
FACEBOOK = "https://www.facebook.com/fixture/"
JOB_ID = "fixture-checkpoint-integration"
WINDOW = frozen_window(datetime(2024, 3, 10, 7, 30, tzinfo=UTC))
LIMITS = {"page_budget": 4, "transfer_budget": 8, "byte_limit": 128}


def binary(data, revision="recording-1", **extra):
    return {"base64": base64.b64encode(data).decode(), "revision": revision,
            "sha256": hashlib.sha256(data).hexdigest(), **extra}


def attachment(name, url, *, kind="image", date=None):
    return {"attachment_id": name, "source_url": url, "kind": kind,
            "publication_evidence": publication_evidence(
                date, source_url=url, field="datePublished", attribution="recorded fixture publication",
            )}


def post(name, url, attachments, *missing):
    return {"post_id": name, "source_url": url, "attachments": attachments,
            "expected_attachment_ids": [item["attachment_id"] for item in attachments] + list(missing)}


def recording():
    photo = WEBSITE + "/photo.jpg"
    social_photo = INSTA + "media/gallery.jpg"
    fb_photo = FACEBOOK + "media/album.jpg"
    evidence = ('<script type="application/ld+json">'
                '{"@type":"ImageObject","contentUrl":"/photo.jpg","datePublished":"2024-03-10T07:30:00Z"}'
                '</script>')
    return {"fixture_only": True, "job": {"job_id": JOB_ID, **WINDOW}, "sources": {
        "website": {
            **LIMITS, "page_budget": 1, "url": WEBSITE + "/", "sitemaps": [WEBSITE + "/sitemap.xml"],
            "robots": "User-agent: *\nDisallow: /archive/private\nDisallow: /blocked.jpg\n",
            "document_limit": 4096, "entry_limit": 8,
            "documents": {
                WEBSITE + "/": {"body": evidence + '<img src="/photo.jpg"><a href="/archive">Archive</a>'},
                WEBSITE + "/sitemap.xml": {"body": '<sitemapindex><sitemap><loc>/archive-map.xml</loc></sitemap></sitemapindex>'},
                WEBSITE + "/archive-map.xml": {"body": '<urlset><url><loc>/archive/private</loc></url></urlset>'},
                WEBSITE + "/archive": {"body": '<a rel="next" href="/archive?page=2">Next</a>'},
                WEBSITE + "/archive?page=2": {"body": '<img src="/blocked.jpg"><img src="/undated.jpg">'},
            },
            "files": {
                photo: binary(b"old recording asset", interrupt_after=4),
                WEBSITE + "/undated.jpg": binary(b"undated"),
                WEBSITE + "/blocked.jpg": binary(b"must not open"),
            },
        },
        "instagram": {
            **LIMITS, "page_budget": 1, "url": INSTA,
            "pages": [
                {"cursor": None, "next_cursor": "next-ig", "posts": [
                    post("pinned", INSTA + "p/old/", [
                        attachment("old", INSTA + "media/old.jpg", date="2021-01-01T00:00:00Z"),
                    ]),
                ]},
                {"cursor": "next-ig", "next_cursor": None, "posts": [
                    post("carousel", INSTA + "p/carousel/", [
                        attachment("gallery", social_photo, date=WINDOW["window_start_utc"]),
                    ]),
                ]},
            ],
            "files": {social_photo: binary(b"same source bytes"), INSTA + "media/old.jpg": binary(b"old pinned")},
        },
        "facebook": {
            **LIMITS, "url": FACEBOOK,
            "pages": [{"cursor": None, "next_cursor": "album-page", "posts": [
                post("album", FACEBOOK + "posts/album/", [attachment("album-photo", fb_photo)], "missing-child"),
            ]}],
            "files": {fb_photo: binary(b"same source bytes")},
        },
    }}


def test_explicit_worker_cli_recovers_three_sources_across_processes(tmp_path):
    source = tmp_path / "recording.json"
    output = tmp_path / "out"
    doc = recording()

    def run():
        source.write_text(json.dumps(doc), encoding="utf-8")
        subprocess.run([sys.executable, "-m", "app.fixture_collection", str(source), str(output)], check=True)
        directory = output / JOB_ID
        return directory, json.loads((directory / "collection/fixture-manifest.json").read_text())

    directory, first = run()
    assert first["fixture_only"] is True
    assert first["sources"]["website"]["stop_reason"] == "page_budget"
    assert first["sources"]["instagram"]["cursor"] == "next-ig"
    assert first["sources"]["facebook"]["stop_reason"] == "cursor_failure"
    original = next(asset for asset in first["assets"] if asset["source_url"] == WEBSITE + "/photo.jpg")
    assert original["status"] == "download_interrupted"
    assert (directory / "collection" / (original["key"] + ".part")).stat().st_size == 4

    website = doc["sources"]["website"]
    website["page_budget"] = 8
    website["files"][WEBSITE + "/photo.jpg"] = binary(b"new complete asset", revision="recording-2")
    # A continued listing must not request either already-read first page again.
    website["documents"].pop(WEBSITE + "/")
    doc["sources"]["instagram"]["pages"].pop(0)
    facebook = doc["sources"]["facebook"]
    facebook["pages"] = [{"cursor": "album-page", "next_cursor": None, "posts": [
        post("reel", FACEBOOK + "reel/example/", [
            attachment("video", FACEBOOK + "media/video.mp4", kind="video", date=WINDOW["window_end_utc"]),
            attachment("cover", FACEBOOK + "media/cover.jpg", kind="thumbnail"),
        ]),
    ]}]
    facebook["files"][FACEBOOK + "media/video.mp4"] = binary(b"recorded video bytes")

    _, second = run()
    assert {key: second[key] for key in WINDOW} == WINDOW
    assert not (directory / "manifest.json").exists()
    assert validate_against_disk(directory, second) == []
    by_url = {asset["source_url"]: asset for asset in second["assets"] if asset["source_url"]}
    photo = by_url[WEBSITE + "/photo.jpg"]
    assert photo["asset_id"] == original["asset_id"] and photo["revision"] == "recording-2"
    assert (directory / photo["local_path"]).read_bytes() == b"new complete asset"
    assert photo["date_eligibility"] == "in_window"
    assert any(item["status"] == "access_blocked" for item in second["sources"]["website"]["items"])
    assert WEBSITE + "/blocked.jpg" not in by_url
    ig = by_url[INSTA + "media/gallery.jpg"]
    fb = by_url[FACEBOOK + "media/album.jpg"]
    assert ig["sha256"] == fb["sha256"] and ig["asset_id"] != fb["asset_id"]
    assert (ig["item"], ig["native_id"], fb["item"], fb["native_id"]) == ("carousel", "gallery", "album", "album-photo")
    assert ig["source_page"] == INSTA + "p/carousel/"
    assert ig["publication_evidence"]["timestamp_utc"] == WINDOW["window_start_utc"]
    assert fb["asset_id"] in second["unknown_date_review"]
    assert by_url[INSTA + "media/old.jpg"]["date_eligibility"] == "out_of_window"
    assert by_url[FACEBOOK + "media/video.mp4"]["extraction_status"] == "downloaded"
    assert by_url[FACEBOOK + "media/cover.jpg"]["status"] == "thumbnail"
    assert any(asset["status"] == "missing_attachment" for asset in second["assets"])
    assert second["sources"]["instagram"]["coverage"] == "unknown"
    assert second["sources"]["facebook"]["coverage"] == "incomplete"
    assert all(source["coverage_percentage"] is None for source in second["sources"].values())


def test_fixture_budgets_and_existing_job_protection(tmp_path):
    doc = recording()
    existing = tmp_path / JOB_ID
    existing.mkdir()
    original = existing / "manifest.json"
    original.write_text("original job")
    with pytest.raises(ValueError, match="does not match"):
        run_fixture_collection(doc, tmp_path)
    assert original.read_text() == "original job"

    website = doc["sources"]["website"]
    website["page_budget"] = 8
    website["transfer_budget"] = 1
    target = run_fixture_collection(doc, tmp_path / "fresh")
    report = json.loads(target.read_text())["sources"]["website"]
    assert "transfer_budget" in report["issues"]
    assert report["coverage"] == "incomplete"
    with pytest.raises(ValueError, match="does not match"):
        run_fixture_collection({**doc, "job": {"job_id": JOB_ID, **frozen_window(datetime(2024, 4, 1, tzinfo=UTC))}},
                               tmp_path / "fresh")
