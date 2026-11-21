"""Offline acceptance through the real worker API and media pipeline."""

import json
import sqlite3
import time
from functools import partial

import pytest

from app import collector, social_adapter
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
    ("success", "completed"), ("failed", "failed"), ("partial", "partial_success"), ("mock", "blocked"),
])
def test_worker_acceptance(worker, tmp_path, monkeypatch, scenario, stage):
    monkeypatch.setenv("AI_PROVIDER", "mock")
    monkeypatch.setenv("AI_API_KEY", "")
    monkeypatch.setattr(collector, "run_website_extraction", partial(collector.run_website_extraction, allow_private=True))
    runner = social._gallery_dl_rate_limited if scenario == "partial" else social._gallery_dl_ok
    calls = []
    monkeypatch.setattr(social_adapter, "run_social_extraction", partial(
        social_adapter.run_social_extraction, runner=social._mk_runner(runner, calls), enable_fallbacks=False,
    ))
    with _LocalServer(_build_site(tmp_path), deny=scenario == "failed") as website:
        payload = {"website_url": website.url(), "idempotency_key": "test-phase10-" + scenario}
        if scenario in ("partial", "mock"):
            payload.update(instagram_url=social.IG_URL, social_permission_confirmed=True)
        if scenario == "mock":
            payload["run_ai_curation"] = True
        path, status = submit_and_wait(worker, payload)
        assert status["stage"] == stage
        duplicate = request(worker.app, "/jobs", "POST", "test-admin", (json.dumps(payload).encode(),))
        assert json.loads(duplicate[2]) == {
            "job_id": path.split("/")[-1], "status": status["job_status"], "idempotent_reuse": True,
        }
        if scenario == "failed":
            return
        manifest = json.loads(request(worker.app, path + "/manifest", key="test-dashboard")[2])
        assert manifest["assets"]
        assert all(a["rights_status"] == "unverified" for a in manifest["assets"])
        assert any(a["platform"] == "website" and a["date_status"] == "unknown" for a in manifest["assets"])
        if scenario == "partial":
            assert manifest["sources"]["instagram"]["status"] == "rate_limited"
        if scenario == "mock":
            assert calls
            assert any(a.get("representative") is False for a in manifest["assets"])
            curation = json.loads(request(worker.app, path + "/curation", key="test-dashboard")[2])
            assert curation["ai_envelope"]["mock"] is True
            assert curation["mode_a_items"]
            assert curation["curation"]["analysis_coverage"]["mock"] is True
            plan = json.loads(request(worker.app, path + "/delivery-plan", key="test-admin")[2])
            assert plan["status"] == "blocked_non_real_curation"
            result = request(worker.app, path + "/delivery-result", "POST", "test-admin",
                             (b'{"authorization_confirmed":true,"uploads":[]}',))
            assert result[0] == 409


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
