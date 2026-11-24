import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

import pytest
from bs4 import BeautifulSoup
from PIL import Image

from app.ai.payload import select_ai_candidates
from app.ai.pipeline import run_ai_curation
from app.ai.provider import AIProviderConfig, OpenAICompatibleProvider
from app.legacy_collector import grab_media
from app.legacy_social_collect import _match_metadata_for_file, derive_post_metadata, parse_dump_json_metadata
from app.preprocessing import run_media_preprocessing
from app.publication import date_eligibility, frozen_window, publication_evidence, record_publication_review, window_bounds
from tests.test_phase7_orchestration import SOURCE_URLS
from tests.test_phase9_security import request, worker  # noqa: F401


WINDOW = frozen_window(datetime.fromisoformat("2024-03-10T03:30:00-04:00"))


def published(value):
    evidence = publication_evidence(value, source_url="https://source.test/post", field="datePublished",
                                    attribution="exact media object")
    return {"post_date": evidence["timestamp_utc"] if evidence else None,
            "date_status": "verified" if evidence else "unknown_date", "publication_evidence": evidence}


def test_frozen_window_crosses_leap_day_and_daylight_saving():
    assert WINDOW == {
        "created_at": "2024-03-10T07:30:00+00:00",
        "window_start_utc": "2023-03-11T07:30:00+00:00",
        "window_end_utc": "2024-03-10T07:30:00+00:00",
    }
    assert published("2024-03-10T01:30:00-05:00")["post_date"] == "2024-03-10T06:30:00+00:00"
    assert published("2024-03-10T01:30:00-05:00")["publication_evidence"]["original_timezone"] == "-0500"


@pytest.mark.parametrize(("timestamp", "state"), [
    ("2023-03-11T07:29:59.999999+00:00", "out_of_window"),
    ("2023-03-11T12:30:00+05:00", "in_window"),
    ("2024-03-10T03:30:00-04:00", "in_window"),
    ("2024-03-10T07:30:00.000001Z", "out_of_window"),
    ("2024-02-29T12:00:00", "unknown_date"),
    ("2024-02-30T12:00:00Z", "unknown_date"),
    ("2024-02-29", "unknown_date"),
    (None, "unknown_date"),
])
def test_date_eligibility_boundaries(timestamp, state):
    assert date_eligibility(published(timestamp), window_bounds(WINDOW)) == state


def test_website_requires_exact_media_object_and_publication_time():
    data = [
        {"@type": "Article", "datePublished": "2024-02-29T12:00:00Z"},
        {"@type": "ImageObject", "contentUrl": "/dated.jpg", "datePublished": "2024-02-29T12:00:00+05:00"},
        {"@type": "VideoObject", "contentUrl": "/video.mp4", "uploadDate": "2024-02-29T12:00:00Z"},
    ]
    html = '<img src="/dated.jpg"><img src="/undated.jpg"><video src="/video.mp4"></video>'
    page = BeautifulSoup(html + f'<script type="application/ld+json">{json.dumps(data)}</script>', "html.parser")
    media = grab_media(page, "https://source.test/page")
    assert media[0]["publication_evidence"]["timestamp_utc"] == "2024-02-29T07:00:00+00:00"
    assert all("publication_evidence" not in item for item in media[1:])


@pytest.mark.parametrize(("profile", "post"), [
    ("https://www.instagram.com/business/", "https://www.instagram.com/p/example/"),
    ("https://www.facebook.com/business/", "https://www.facebook.com/business/posts/123"),
])
def test_social_timestamp_needs_unambiguous_file_match(tmp_path, profile, post):
    record = {"filename": "actual", "extension": "jpg", "post_url": post, "date": "2024-02-29T12:00:00-05:00"}
    assert not _match_metadata_for_file(tmp_path / "unrelated.jpg", [record])
    assert not _match_metadata_for_file(tmp_path / "actual.jpg", [record, {**record, "date": "2024-03-01T12:00:00Z"}])
    matched = _match_metadata_for_file(tmp_path / "actual.jpg", [record])
    assert derive_post_metadata(matched, fallback_url=profile)["post_date"] == "2024-02-29T17:00:00+00:00"
    assert derive_post_metadata({**matched, "date": None, "upload_date": "20240229"},
                                fallback_url=profile)["publication_evidence"] is None


def test_review_and_direct_pipeline_fail_closed(tmp_path):
    (tmp_path / "sample.jpg").write_bytes(b"never read by provider")
    common = {"sha256": "same", "extraction_status": "downloaded", "type": "image", "local_path": "sample.jpg",
              "preprocessing": {"outcome": "processed"}, "rights_status": "unverified"}
    assets = [
        {**common, "asset_id": "old", **published("2023-03-01T12:00:00Z")},
        {**common, "asset_id": "inferred", "date_status": "verified", "post_date": WINDOW["created_at"],
         "captured_at": WINDOW["created_at"], "date_eligibility": "in_window"},
    ]
    manifest = {"assets": assets}
    record_publication_review(manifest, WINDOW)
    assert manifest["unknown_date_review"] == ["inferred"]
    assert assets[0]["date_eligibility"] == "out_of_window"
    assert assets[0]["post_date"] == "2023-03-01T12:00:00+00:00"
    assert assets[1]["post_date"] is None and assets[1]["rights_status"] == "unverified"
    config = AIProviderConfig(provider="openai_compatible", model="fixture", api_key="fixture-key")
    provider = OpenAICompatibleProvider(config, transport=lambda _: pytest.fail("blocked asset reached transport"))
    for document in (manifest, {"assets": [{**common, "asset_id": "historic", **published(WINDOW["created_at"])}]}):
        (tmp_path / "manifest.json").write_text(json.dumps(document))
        result = run_ai_curation(tmp_path, "fixture", config=config, provider=provider)
        assert result.status == "failed"
        assert not (tmp_path / "curation.json").exists()


def test_gallery_directory_metadata_does_not_supply_a_publication_date(tmp_path):
    profile = "https://www.instagram.com/business/"
    records = parse_dump_json_metadata(json.dumps([
        [2, {"date": "2024-02-29T12:00:00Z", "post_url": "https://www.instagram.com/p/other/"}],
        [3, "https://media.test/item.jpg", {"filename": "item", "extension": "jpg"}],
    ]))
    matched = _match_metadata_for_file(tmp_path / "item.jpg", records)
    assert matched["filename"] == "item"
    assert derive_post_metadata(matched, fallback_url=profile)["publication_evidence"] is None


def test_duplicate_prefers_in_window_provenance(tmp_path):
    from hashlib import sha256

    path = tmp_path / "same.png"
    Image.new("RGB", (200, 200)).save(path)
    blob = path.read_bytes()
    common = {"sha256": sha256(blob).hexdigest(), "local_path": path.name, "type": "image",
              "mime": "image/png", "bytes": len(blob), "rights_status": "unverified"}
    old = {**common, "asset_id": "old", "extraction_status": "downloaded",
           "source_url": "https://source.test/old", **published("2023-03-01T12:00:00Z")}
    current = {**common, "asset_id": "recent", "extraction_status": "duplicate",
               "source_url": "https://source.test/recent", **published(WINDOW["created_at"])}
    doc = {"assets": [old, current]}
    record_publication_review(doc, WINDOW)
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps(doc))
    run_media_preprocessing(tmp_path, "duplicate-fixture")
    enriched = json.loads(target.read_text())
    assets = {item["asset_id"]: item for item in enriched["assets"]}
    for source in (old, current):
        assert assets[source["asset_id"]]["source_url"] == source["source_url"]
        assert assets[source["asset_id"]]["publication_evidence"] == source["publication_evidence"]
    result = select_ai_candidates(enriched, tmp_path)
    assert [candidate.asset["asset_id"] for candidate in result.candidates] == ["recent"]


def test_restart_and_idempotent_retry_preserve_original_window(worker, monkeypatch):
    def never_run(*_):
        pass

    monkeypatch.setattr(worker.executor, "submit", never_run)
    restart_root = worker.DATA_DIR / "restart"
    restart_db = restart_root / "_system" / "jobs.sqlite"
    restart_db.parent.mkdir(parents=True)
    with worker.connect() as source:
        with sqlite3.connect(restart_db) as destination:
            source.backup(destination)
    monkeypatch.setattr(worker, "DATA_DIR", restart_root)
    monkeypatch.setattr(worker, "DB", restart_db)
    body = (json.dumps({**SOURCE_URLS, "idempotency_key": "window-restart", "lookback_days": 1}).encode(),)
    first = request(worker.app, "/jobs", "POST", "test-admin", body)
    job_id = json.loads(first[2])["job_id"]
    saved = worker.fetch(job_id)
    assert saved["lookback_days"] == 365 and window_bounds(saved) is not None
    subprocess.run([sys.executable, "-c", "from app import service"],
                   env={**os.environ, "PROJECTS_DIR": str(worker.DATA_DIR)}, check=True)
    retry = request(worker.app, "/jobs", "POST", "test-admin", body)
    assert json.loads(retry[2]) == {"job_id": job_id, "status": "interrupted", "idempotent_reuse": True}
    assert {key: worker.fetch(job_id)[key] for key in WINDOW} == {key: saved[key] for key in WINDOW}
    resume = request(worker.app, "/intake/validate", "POST", "test-admin",
                     (json.dumps({"job_id": job_id}).encode(),))
    assert json.loads(resume[2])["resume"]
    assert worker.fetch("a" * 32) == {"job_id": "a" * 32}
