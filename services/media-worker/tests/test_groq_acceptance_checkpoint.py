"""Exercise the standalone acceptance runner against fake HTTP responses."""

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("isolated_provider_unit")
import requests
from PIL import Image

from app.ai import pipeline, provider


DIAGNOSTICS = {
    "truncated": ("$.choices[0].finish_reason", "truncated at output limit"),
    "invalid_json": ("$", "invalid completion JSON"),
    "missing_field": ("$.rights_review_required_ids", "missing required output field"),
    "wrong_missing_category": ("$.missing_assets[0].category", "literal_error"),
    "wrong_enum": ("$.hero_selection[0].text_overlay_suitability", "literal_error"),
    "wrong_rights_ref": ("$.rights_review_required_ids[0]", "unauthorized asset reference"),
    "unknown_key": ("$.<unrecognized-field>", "extra_forbidden"),
}


@pytest.mark.parametrize("failure", ["mode_b", "final_report", "echoed_secret", "success", *DIAGNOSTICS])
def test_acceptance_checkpoint_survives_failure(tmp_path, monkeypatch, failure):
    runner = Path(__file__).resolve().parents[1] / "scripts/groq_bounded_acceptance.py"
    image = tmp_path / "approved.jpg"
    Image.new("RGB", (256, 256), "teal").save(image, format="JPEG")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    out = tmp_path / "result"
    out.mkdir()
    secret = "fake-private-key-for-checkpoint-test"
    monkeypatch.setattr(sys, "argv", [str(runner), "--live"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret))
    monkeypatch.setattr(pipeline, "build_image_payloads", pipeline.build_image_payloads)
    # Only this fake-transport test supplies publication evidence for the synthetic JPEG.
    from datetime import datetime
    from app.publication import frozen_window, publication_evidence

    real_pipeline = pipeline.run_ai_curation

    def with_fixture_publication(job_dir, *args, **kwargs):
        target = job_dir / "manifest.json"
        manifest = json.loads(target.read_text())
        created = "2026-11-20T00:00:00+00:00"
        manifest.update(frozen_window(datetime.fromisoformat(created)))
        manifest["assets"][0].update(
            post_date=created, date_status="verified",
            publication_evidence=publication_evidence(created, source_url="https://fixture.test/post",
                                                      field="datePublished", attribution="offline checkpoint fixture"),
        )
        target.write_text(json.dumps(manifest))
        return real_pipeline(job_dir, *args, **kwargs)

    monkeypatch.setattr(pipeline, "run_ai_curation", with_fixture_publication)
    source = runner.read_text(encoding="utf8")
    source = (source.replace('Path("/approved.jpg")', f"Path({str(image)!r})")
              .replace('Path("/result")', f"Path({str(out)!r})")
              .replace('"/workspace/app"', repr(str(Path(provider.__file__).resolve().parents[1])))
              .replace("2e01cb8ce50473e7901d70777ef7f9578feb358d7642d2e6e78df5442c5cf986", digest))
    mock = provider.MockVisionProvider()
    tracker = provider.UsageTracker(provider.AIProviderConfig(provider="mock"))
    calls = []
    echo_recovery_secret = False

    def fake_post(url, *, headers, json, **kwargs):
        import base64
        import json as codec
        calls.append(json["max_completion_tokens"])
        content = json["messages"][0]["content"]
        response = requests.Response()
        if json["max_completion_tokens"] == 900:
            assert (out / "mode-a-validated.json").is_file()
            assert len(content) == 1
            assert json["reasoning_effort"] == "none"
            assert "Return minified JSON" in content[0]["text"]
            if failure == "mode_b":
                response.status_code = 429
                response._content = b'{"error":{"code":"rate_limit_exceeded","message":"Request too large"}}'
                return response
        images = [provider.ImagePayload("synthetic_grooming_dog_2e01cb8ce504", "image/jpeg",
                   base64.b64decode(part["image_url"]["url"].split(",", 1)[1])) for part in content[1:]]
        result = mock.complete(content[0]["text"], images, tracker)
        payload = codec.loads(result.text)
        if failure == "echoed_secret":
            payload["items"][0]["detected_content"] = secret
        if json["max_completion_tokens"] == 900:
            if echo_recovery_secret:
                payload["business_visual_identity"]["style_summary"] = secret
            compact = codec.dumps(payload, separators=(",", ":"))
            assert len(compact.encode("utf8")) <= 2200
        if json["max_completion_tokens"] == 900:
            if failure == "missing_field":
                del payload["rights_review_required_ids"]
            elif failure == "wrong_missing_category":
                payload["missing_assets"][0]["category"] = secret
            elif failure == "wrong_enum":
                payload["hero_selection"][0]["text_overlay_suitability"] = secret
            elif failure == "wrong_rights_ref":
                payload["rights_review_required_ids"] = [secret]
            elif failure == "unknown_key":
                payload[secret] = "sensitive field name"
        text = "{" if failure == "invalid_json" and json["max_completion_tokens"] == 900 else codec.dumps(payload)
        response.status_code = 200
        response._content = codec.dumps({
            "choices": [{"finish_reason": "length" if failure == "truncated" and json["max_completion_tokens"] == 900 else "stop", "message": {"content": text}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
        }).encode()
        return response

    monkeypatch.setattr(requests, "post", fake_post)
    ns = {"__name__": "__main__"}
    if failure == "final_report":
        original = Path.write_text

        def fail_report(path, *args, **kwargs):
            if path.name == "acceptance-result.json":
                raise OSError("fixture reporting failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail_report)
        with pytest.raises(OSError, match="fixture reporting failure"):
            exec(compile(source, str(runner), "exec"), ns)
    else:
        exec(compile(source, str(runner), "exec"), ns)
    checkpoint = out / "mode-a-validated.json"
    assert all(secret not in p.read_text(encoding="utf8") for p in out.glob("*.json"))
    if failure == "echoed_secret":
        assert calls == [1200]
        assert not checkpoint.exists()
        return
    assert calls == [1200, 900]
    if failure in ("mode_b", "success", *DIAGNOSTICS):
        report = json.loads((out / "acceptance-result.json").read_text())
        assert report["request_count"] == 2
        assert (out / "curation.json").is_file() == (failure == "success")
        if failure == "mode_b":
            assert report["provider_error"]["code"] == "rate_limit_exceeded"
        if failure == "truncated":
            assert report["attempts"][1]["schema"] == "FAIL"
    record = ns["read_mode_a_checkpoint"](checkpoint)
    assert record["image_sha256"] == digest
    assert record["asset_id"] == "synthetic_grooming_dog_2e01cb8ce504"
    assert record["schema_validation"] == "PASS"
    assert record["request"]["usage"]["total_tokens"] == 200
    assert not list(out.glob("*.tmp"))
    if failure in ("success", "mode_b", *DIAGNOSTICS):
        recovery_out = tmp_path / "recovery"
        recovery_out.mkdir()
        prior = checkpoint.read_bytes()
        recovery_source = source.replace(repr(str(out)), repr(str(recovery_out)))
        monkeypatch.setattr(sys, "argv", [str(runner), "--recover-mode-b", str(checkpoint),
                                          "--authorize-live-mode-b"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(secret))
        calls.clear()
        exec(compile(recovery_source, str(runner), "exec"), {"__name__": "__main__"})
        assert calls == [900]
        assert checkpoint.read_bytes() == prior
        assert not (recovery_out / "mode-a-validated.json").exists()
        report = json.loads((recovery_out / "acceptance-result.json").read_text())
        assert report["request_count"] == 1
        assert (recovery_out / "curation.json").is_file() == (failure == "success")
        if failure == "mode_b":
            assert report["provider_error"]["http_status"] == 429
            assert report["provider_error"]["code"] == "rate_limit_exceeded"
        if failure in DIAGNOSTICS:
            issue = report["attempts"][0]["validation_errors"][0]
            expected_path, expected_reason = DIAGNOSTICS[failure]
            assert report["attempts"][0]["schema"] == "FAIL"
            assert issue["path"] == expected_path and issue["reason"].startswith(expected_reason)
        assert all(secret not in p.read_text(encoding="utf8") for p in recovery_out.glob("*.json"))
        if failure == "success":
            echo_recovery_secret = True
            secret_out = tmp_path / "recovery-secret"
            secret_out.mkdir()
            secret_source = source.replace(repr(str(out)), repr(str(secret_out)))
            monkeypatch.setattr(sys, "stdin", io.StringIO(secret))
            calls.clear()
            exec(compile(secret_source, str(runner), "exec"), {"__name__": "__main__"})
            assert calls == [900]
            assert not (secret_out / "curation.json").exists()
            assert all(secret not in p.read_text(encoding="utf8") for p in secret_out.glob("*.json"))
            bad = tmp_path / "invalid-asset.json"
            invalid = json.loads(prior)
            invalid.pop("checkpoint_sha256")
            invalid["analysis"]["items"][0]["asset_id"] = "foreign"
            invalid["checkpoint_sha256"] = ns["checkpoint_digest"](invalid)
            bad.write_text(json.dumps(invalid))
            for checkpoint_input, message in ((bad, "asset mismatch"), (checkpoint, "integrity mismatch")):
                if checkpoint_input == checkpoint:
                    broken = tmp_path / "corrupted.json"
                    corrupted = json.loads(prior)
                    corrupted["request"]["attempt"] = 4
                    broken.write_text(json.dumps(corrupted))
                    checkpoint_input = broken
                blocked_out = tmp_path / message.replace(" ", "-")
                blocked_out.mkdir()
                blocked_source = source.replace(repr(str(out)), repr(str(blocked_out)))
                monkeypatch.setattr(sys, "argv", [str(runner), "--recover-mode-b", str(checkpoint_input),
                                                  "--authorize-live-mode-b"])
                monkeypatch.setattr(sys, "stdin", io.StringIO(secret))
                calls.clear()
                with pytest.raises(RuntimeError, match=message):
                    exec(compile(blocked_source, str(runner), "exec"), {"__name__": "__main__"})
                assert calls == []
    tampered = json.loads(checkpoint.read_text())
    tampered["request"]["usage"]["total_tokens"] = 999
    checkpoint.write_text(json.dumps(tampered))
    with pytest.raises(RuntimeError, match="integrity mismatch"):
        ns["read_mode_a_checkpoint"](checkpoint)

def test_mode_b_prompt_lists_exact_missing_asset_enum_and_valid_example():
    import re
    from typing import get_args

    from app.ai.prompts import build_mode_b_prompt
    from app.ai.schemas import MissingAsset

    prompt = build_mode_b_prompt("fixture", [], {}, max_images=1, max_videos=0, compact_output=True)
    allowed = get_args(MissingAsset.model_fields["category"].annotation)
    listed = re.search(r"missing_assets\[\]\.category must be one of: (.+?)\.", prompt)
    assert listed and tuple(listed.group(1).split(", ")) == allowed
    example = re.search(r'Example: (\{"missing_assets":\[.+?\]\})', prompt)
    assert example
    assert MissingAsset.model_validate(json.loads(example.group(1))["missing_assets"][0], strict=True).category == "salon_facilities"
    assert "Never invent a category or force a match" in prompt
    assert "final_quality_assessment.weaknesses" in prompt
