"""Sprint 7 regression tests: Groq Qwen3.8 serialization, provider/status parity, and real interactive bootstrap."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys

import pytest
from PIL import Image

from app import ai_execution
from app.ai.prompts import build_mode_a_prompt, get_prompt_metadata, system_prompt_for_mode
from app.ai.provider import (
    GROQ_ACCEPTANCE_ENDPOINT,
    GROQ_ACCEPTANCE_MODEL,
    AIProviderConfig,
    ImagePayload,
    OpenAICompatibleProvider,
    UsageTracker,
    build_provider,
)
from tests.test_phase9_security import request, worker  # noqa: F401 — pytest fixture


def test_groq_qwen38_production_serialization_sets_reasoning_none_and_v2_prompt(monkeypatch):
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (90, 140, 180)).save(buf, format="JPEG", quality=80)
    jpg = buf.getvalue()

    cfg = AIProviderConfig(
        provider="openai_compatible",
        model=GROQ_ACCEPTANCE_MODEL,
        endpoint=GROQ_ACCEPTANCE_ENDPOINT,
        api_key="gsk_test_fixture_only",
        batch_size=1,
        max_images_per_request=3,
        mode_a_max_output_tokens=1200,
        min_request_spacing_s=0.0,
    )
    captured = []

    def fake_transport(req):
        captured.append(req)
        return {
            "choices": [{"message": {"content": '{"mode":"ANALYZE_BATCH","items":[]}'}}],
            "usage": {"prompt_tokens": 2500, "completion_tokens": 120},
        }

    monkeypatch.setattr(ai_execution, "attempt", lambda *a, **k: contextlib.nullcontext(0.0))
    provider = OpenAICompatibleProvider(cfg, sleep=lambda _: None, transport=fake_transport)
    sys_prompt = system_prompt_for_mode("A")
    user_prompt, hits = build_mode_a_prompt(
        "Lily's Club",
        [{
            "kind": "image",
            "asset": {
                "asset_id": "web_001",
                "filename": "hero.jpg",
                "platform": "website",
                "type": "image",
                "width": 800,
                "height": 600,
                "sharpness": 0.12,
                "usable_size": True,
            },
            "frame_timestamps": [],
        }],
    )
    assert hits == []
    provider.complete(
        user_prompt,
        [ImagePayload("web_001", "image/jpeg", jpg, "primary image")],
        UsageTracker(cfg),
        system_prompt=sys_prompt,
    )
    assert len(captured) == 1
    body = captured[0]["body"]
    assert body["model"] == GROQ_ACCEPTANCE_MODEL
    assert body["reasoning_effort"] == "none"
    assert body["max_completion_tokens"] == 1200
    assert "max_tokens" not in body
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    parts = body["messages"][1]["content"]
    assert [p["type"] for p in parts] == ["text", "text", "image_url"]
    assert "[IMAGE asset_id=web_001 label=primary image]" in parts[1]["text"]
    assert parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert get_prompt_metadata()["prompt_version"] == "V2.0"


def test_groq_provider_alias_and_status_endpoint_parity(worker, monkeypatch):
    # Provider "groq" works and defaults endpoint to GROQ_ACCEPTANCE_ENDPOINT
    cfg = AIProviderConfig(provider="groq", model=GROQ_ACCEPTANCE_MODEL, endpoint="", api_key="gsk_test_only")
    prov, st = build_provider(cfg)
    assert prov is not None and st == "ready"
    assert prov.config.endpoint == GROQ_ACCEPTANCE_ENDPOINT

    # /status agrees with build_provider() — reports false when model or endpoint missing
    monkeypatch.setenv("AI_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AI_API_KEY", "gsk_test_only")
    monkeypatch.delenv("AI_MODEL", raising=False)
    monkeypatch.delenv("AI_ENDPOINT", raising=False)
    code, _, body = request(worker.app, "/status", key="test-dashboard")
    assert code == 200
    assert json.loads(body)["ai_configured"] is False

    # Unknown provider also reports false
    monkeypatch.setenv("AI_PROVIDER", "unsupported_vendor")
    monkeypatch.setenv("AI_MODEL", "x")
    code, _, body = request(worker.app, "/status", key="test-dashboard")
    assert code == 200
    assert json.loads(body)["ai_configured"] is False

    # Valid groq alias reports true
    monkeypatch.setenv("AI_PROVIDER", "groq")
    monkeypatch.setenv("AI_MODEL", GROQ_ACCEPTANCE_MODEL)
    code, _, body = request(worker.app, "/status", key="test-dashboard")
    assert code == 200
    assert json.loads(body)["ai_configured"] is True


def test_named_bootstrap_cli_real_db_interactive_and_single_use(tmp_path, monkeypatch, capsys):
    from scripts import named_bootstrap

    db = tmp_path / "jobs.sqlite"
    sqlite3.connect(db).close()

    # Short password rejected
    monkeypatch.setattr(sys, "argv", ["named_bootstrap", "--db", str(db), "--verified-operator", "owner-qa"])
    monkeypatch.setattr("builtins.input", lambda _: "admin1")
    pwds = iter(["short", "short"])
    monkeypatch.setattr(named_bootstrap.getpass, "getpass", lambda _: next(pwds))
    with pytest.raises(SystemExit) as short_err:
        named_bootstrap.main()
    assert short_err.value.code == 1

    # Valid >=12-char confirmed password succeeds once without printing secret
    secret_pw = "CorrectHorseBatteryStaple2026!"
    pwds = iter([secret_pw, secret_pw])
    monkeypatch.setattr(named_bootstrap.getpass, "getpass", lambda _: next(pwds))
    named_bootstrap.main()
    out = capsys.readouterr().out
    assert "NAMED_BOOTSTRAP_OK principal_id=" in out
    assert secret_pw not in out

    # Second bootstrap on same DB rejected (409 -> exit 1)
    pwds = iter([secret_pw, secret_pw])
    monkeypatch.setattr(named_bootstrap.getpass, "getpass", lambda _: next(pwds))
    with pytest.raises(SystemExit) as dup_err:
        named_bootstrap.main()
    assert dup_err.value.code == 1
