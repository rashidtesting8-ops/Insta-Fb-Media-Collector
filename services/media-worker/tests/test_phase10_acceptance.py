"""Offline acceptance through the real worker API and media pipeline."""

import json
import sqlite3
import time
from datetime import datetime
from functools import partial

import pytest

from app import collector, social_adapter
from app.publication import frozen_window
from tests import test_social_collector as social
from tests.test_phase9_security import request, worker  # noqa: F401
from tests.test_website_collector import _LocalServer, _build_site


def submit_and_wait(worker, payload):
    result = request(worker.app, "/jobs", "POST", "test-admin", (json.dumps(payload).encode(),))
    assert result[0] == 202, result
    job_id = json.loads(result[2])["job_id"]
    path = f"/jobs/{job_id}"
    for _ in range(200):
        status = json.loads(request(worker.app, path + "/pipeline-status", key="test-dashboard")[2])
        if status["terminal"]:
            return path, status
        time.sleep(0.025)
    pytest.fail("Worker did not reach a terminal stage")


@pytest.mark.parametrize(("scenario", "stage"), [
    ("success", "completed"), ("failed", "failed"), ("partial", "partial_success"),
    ("mock", "blocked"), ("undated", "blocked"),
])
def test_worker_acceptance(worker, tmp_path, monkeypatch, scenario, stage):
    window = frozen_window(datetime.fromisoformat("2026-11-20T00:00:00+00:00"))
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(window["window_end_utc"]).astimezone(tz)

    monkeypatch.setattr(worker._dt, "datetime", FrozenDateTime)
    monkeypatch.setenv("AI_PROVIDER", "mock")
    monkeypatch.setenv("AI_API_KEY", "")
    monkeypatch.setattr(collector, "run_website_extraction", partial(collector.run_website_extraction, allow_private=True))
    facebook_url = "https://www.facebook.com/example/"

    def runner(cmd, stage):
        if scenario == "partial":
            return social._gallery_dl_rate_limited(cmd, stage)
        if facebook_url not in cmd:
            return social._gallery_dl_ok(cmd, stage)
        if "--dump-json" in cmd:
            return 0, json.dumps({
                "filename": "facebook-grooming", "extension": "jpg",
                "post_url": facebook_url + "posts/123",
            })
        (stage / "facebook-grooming.jpg").write_bytes(social._png_bytes_alt())
        return 0, ""

    calls = []
    monkeypatch.setattr(social_adapter, "run_social_extraction", partial(
        social_adapter.run_social_extraction, runner=social._mk_runner(runner, calls), enable_fallbacks=False,
    ))
    with _LocalServer(_build_site(tmp_path), deny=scenario == "failed") as website:
        payload = {
            "website_url": website.url(), "instagram_url": social.IG_URL,
            "facebook_url": facebook_url,
            "idempotency_key": "test-phase10-" + scenario,
            "window_end_utc": "1900-01-01T00:00:00+00:00",
        }
        if scenario in ("partial", "mock"):
            payload.update(social_permission_confirmed=True)
        if scenario == "mock":
            payload["run_ai_curation"] = True
        if scenario == "undated":
            from app.ai.provider import MockVisionProvider

            def forbidden_inference(*args, **kwargs):
                pytest.fail("Undated direct worker submission must not reach the provider")

            monkeypatch.setattr(MockVisionProvider, "complete", forbidden_inference)
            payload.update(run_ai_curation=True, post_date=window["created_at"], date_status="verified")
        path, status = submit_and_wait(worker, payload)
        assert status["stage"] == stage, worker.fetch(path.split("/")[-1])
        duplicate = request(worker.app, "/jobs", "POST", "test-admin", (json.dumps(payload).encode(),))
        assert json.loads(duplicate[2]) == {
            "job_id": path.split("/")[-1], "status": status["job_status"], "idempotent_reuse": True,
        }
        if scenario == "failed":
            return
        manifest = json.loads(request(worker.app, path + "/manifest", key="test-dashboard")[2])
        assert manifest["assets"]
        assert all(a["rights_status"] == "unverified" for a in manifest["assets"])
        assert {key: manifest[key] for key in window} == window
        assert any(a["platform"] == "website" and a["date_status"] == "unknown_date" for a in manifest["assets"])
        assert manifest["unknown_date_review"]
        if scenario == "undated":
            job = worker.fetch(path.split("/")[-1])
            assert job["ai_curation"]["status"] == "awaiting_collection_approval"
            assert not (worker.DATA_DIR / job["job_id"] / "curation.json").exists()
        if scenario == "partial":
            assert manifest["sources"]["instagram"]["status"] == "rate_limited"
        if scenario == "mock":
            assert calls
            assert any(a.get("representative") is False for a in manifest["assets"])
            assert not (worker.DATA_DIR / path.split("/")[-1] / "curation.json").exists()
            assert worker.fetch(path.split("/")[-1])["ai_curation"]["status"] == "awaiting_collection_approval"


def test_restore_job_database_into_fresh_path(worker, tmp_path, monkeypatch):
    restored = tmp_path / "restore" / "jobs.sqlite"
    restored.parent.mkdir()
    source = sqlite3.connect(worker.DB)
    target = sqlite3.connect(restored)
    try:
        source.backup(target)
        assert target.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        source.close()
        target.close()
    monkeypatch.setattr(worker, "DB", restored)
    assert request(worker.app, "/ready")[0] == 200
    assert request(worker.app, "/jobs/" + "a" * 32, key="test-dashboard")[0] == 200
