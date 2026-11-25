"""Sprint 4 worker readiness status endpoint security & capability tests."""

from __future__ import annotations

import json

from tests.test_phase9_security import request, worker  # noqa: F401 — pytest fixture


def test_status_exposes_non_secret_capabilities_to_dashboard_and_admin(worker, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "groq")
    monkeypatch.setenv("AI_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    monkeypatch.setenv("AI_API_KEY", "gsk_super_secret_do_not_expose_123456789")
    monkeypatch.setenv("M4_COLLECTION_APPROVAL_ENABLED", "1")
    monkeypatch.setenv("COLLECTION_ENGINE", "historical")

    # Unauthenticated still blocked
    assert request(worker.app, "/status")[0] == 401
    assert request(worker.app, "/status", key="wrong-token")[0] == 401

    # Both dashboard and admin tokens receive non-secret capability flags
    for key in ("test-dashboard", "test-admin"):
        status_code, _, body = request(worker.app, "/status", key=key)
        assert status_code == 200
        raw = body.decode("utf-8")
        assert "gsk_super_secret" not in raw
        assert "test-admin" not in raw
        assert "test-dashboard" not in raw
        data = json.loads(raw)
        assert data["ok"] is True
        assert data["ai_configured"] is True
        assert data["ai_provider"] == "groq"
        assert data["ai_model"] == "meta-llama/llama-4-scout-17b-16e-instruct"
        assert data["m4_approval_enabled"] is True
        assert data["collection_engine"] == "historical"


def test_status_reports_ai_unconfigured_when_api_key_missing(worker, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "groq")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("M4_COLLECTION_APPROVAL_ENABLED", raising=False)
    monkeypatch.delenv("COLLECTION_ENGINE", raising=False)
    monkeypatch.delenv("PUBLIC_COLLECTION_CHECKPOINTS", raising=False)

    status_code, _, body = request(worker.app, "/status", key="test-dashboard")
    assert status_code == 200
    data = json.loads(body.decode("utf-8"))
    assert data["ai_configured"] is False
    assert data["m4_approval_enabled"] is False
    assert data["collection_engine"] == "legacy"
