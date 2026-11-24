import base64
from dataclasses import replace
from hashlib import sha256
import json

import pytest
from PIL import Image

from app import qa_live
from app.ai.provider import AIProviderConfig, ImagePayload, MockVisionProvider, UsageTracker
from app.manifest import atomic_write_json
from tests.test_phase9_security import request, worker
from tests.test_named_auth import identity
from tests.test_ai_execution import ledger_setup


@pytest.fixture
def scope(tmp_path):
    job_id = "4" * 32
    folder = tmp_path / job_id
    folder.mkdir()
    image = folder / "synthetic.jpg"
    Image.new("RGB", (240, 240), "#cb9767").save(image, format="JPEG")
    digest = sha256(image.read_bytes()).hexdigest()
    website = "https://local-qa.example.test/"
    src = website + image.name
    approval = qa_live.ScopedApproval(job_id, website, "asset_qa", src, digest, True, True)
    asset = {"job_id": job_id, "asset_id": approval.asset_id, "local_path": image.name, "filename": image.name,
             "source_url": src, "mime": "image/jpeg", "type": "image", "platform": "website", "sha256": digest,
             "extraction_status": "downloaded", "rights_status": "unverified", "representative": True,
             "preprocessing": {"outcome": "processed"},
             "quality": {"integrity": "ok", "width": 240, "height": 240, "usable_size": True}}
    atomic_write_json(folder / "manifest.json", {"job_id": job_id, "assets": [asset]})
    job = {"job_id": job_id, "website_url": website, "status": "extraction_complete", "run_ai_curation": False}
    return folder, job, approval, asset


def test_scope_checks_the_single_job_source_and_bytes(scope):
    folder, job, approval, asset = scope
    assert qa_live.scoped_original(folder, job, approval)[0] == asset
    fixture_job = {
        **job,
        "instagram_url": "https://www.instagram.com/pgm_qa_fixture/",
        "facebook_url": "https://www.facebook.com/pgm_qa_fixture/",
        "social_permission_confirmed": False,
    }
    assert qa_live.scoped_original(folder, fixture_job, approval)[0] == asset
    with pytest.raises(ValueError, match="Unauthorized QA job"):
        qa_live.scoped_original(folder, {**fixture_job, "social_permission_confirmed": True}, approval)
    with pytest.raises(ValueError, match="Unauthorized QA job"):
        qa_live.scoped_original(folder, {**job, "instagram_url": "https://example.test/other"}, approval)
    with pytest.raises(ValueError, match="authorized source"):
        qa_live.scoped_original(folder, job, replace(approval, source_url="https://other.test/image.jpg"))
    (folder / asset["local_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash or byte limit"):
        qa_live.scoped_original(folder, job, approval)


def test_mock_mode_b_cannot_be_promoted_and_failed_job_cannot_retry(scope, monkeypatch, worker, identity):
    from datetime import datetime
    from app.publication import frozen_window, publication_evidence

    folder, job, approval, asset = scope
    calls = []
    mock = MockVisionProvider()
    tracker = UsageTracker(AIProviderConfig(provider="mock"))

    def fake_post(url, *, headers, json, timeout, verify, allow_redirects):
        content = json["messages"][0]["content"]
        calls.append((url, json["model"], json["max_completion_tokens"], json["reasoning_effort"], len(content)))
        assert verify is True and allow_redirects is False
        images = []
        if len(content) > 1:
            original = base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1])
            assert original == (folder / asset["local_path"]).read_bytes()
            images = [ImagePayload(asset["asset_id"], "image/jpeg", original)]
        response = mock.complete(content[0]["text"], images, tracker)

        class SimulatedResponse:
            status_code = 200

            def json(self):
                return {"usage": {"prompt_tokens": 400, "completion_tokens": 300}, "choices": [
                    {"finish_reason": "stop", "message": {"content": response.text}},
                ]}

        return SimulatedResponse()

    monkeypatch.setattr(qa_live.requests, "post", fake_post)
    with pytest.raises(ValueError, match="Explicit single-job inference approval required"):
        qa_live.run_guarded_groq(folder, job, replace(approval, groq_approved=False), "fixture-token")
    with pytest.raises(ValueError, match="lacks verified in-window publication"):
        qa_live.run_guarded_groq(folder, job, approval, "fixture-token")
    assert calls == []
    published = "2026-11-19T00:00:00+00:00"
    window = frozen_window(datetime.fromisoformat("2026-11-20T00:00:00+00:00"))
    job.update(window)
    asset.update(post_date=published, date_status="verified", publication_evidence=publication_evidence(
        published, source_url=approval.website_url, field="datePublished", attribution="test source fixture",
    ))
    atomic_write_json(folder / "manifest.json", {"job_id": approval.job_id, "assets": [asset], **window})
    with pytest.raises(ValueError):
        qa_live.run_guarded_groq(folder, job, approval, "fixture-token")
    assert calls == []
    action = ledger_setup(worker, identity, job, monkeypatch)
    action("approve")
    approval = replace(approval, start_etag=action("start"))
    with pytest.raises(ValueError, match="Bounded genuine Groq analysis did not pass"):
        qa_live.run_guarded_groq(folder, job, approval, "fixture-token")
    assert calls == [(qa_live.ENDPOINT, qa_live.MODEL, 1200, "none", 2),
                     (qa_live.ENDPOINT, qa_live.MODEL, 900, "none", 1)]
    assert not (folder / "curation.json").exists()
    assert not (folder / "guarded-groq-verified.json").exists()
    with pytest.raises(ValueError):
        qa_live.run_guarded_groq(folder, job, approval, "fixture-token")
    assert len(calls) == 2


def test_rejected_or_unproven_delivery_never_reaches_adapter(scope, monkeypatch):
    folder, job, approval, _ = scope
    recorded = []
    monkeypatch.setattr(qa_live, "run_guarded_groq", lambda *args, save_analysis:
                        save_analysis(approval.job_id, {"status": "completed", "mock": False}))

    def cannot_deliver(*args):
        pytest.fail("Unauthorized Drive invocation")

    result = qa_live.run_guarded_job(folder, job, replace(approval, drive_approved=False), "unused",
                                     save_analysis=lambda *args: recorded.append(args), deliver_private=cannot_deliver)
    assert result == {"job_id": approval.job_id, "status": "no_eligible_delivery"}
    assert recorded[0][0] == approval.job_id
    atomic_write_json(folder / "curation.json", {"status": "completed", "job_id": approval.job_id,
                                                "ai_envelope": {"mock": True}, "curation": {}})
    with pytest.raises(ValueError, match="Untrusted curation"):
        qa_live.run_guarded_job(folder, job, approval, "unused", save_analysis=lambda *args: None,
                                deliver_private=cannot_deliver)


def test_receipt_must_match_job_folder_key_checksum_and_permissions(scope, monkeypatch):
    folder, job, approval, _ = scope
    key = approval.asset_id + ":" + approval.sha256
    monkeypatch.setattr(qa_live, "eligible_delivery_key", lambda *args: key)
    receipt = {"job_id": approval.job_id, "folder_id": approval.folder_id, "upload_key": key,
               "sha256": approval.sha256, "owner_only": True, "status": "verified", "drive_file_id": "private_file"}
    assert qa_live.worker_delivery_payload(folder, job, approval, receipt)["uploads"] == [
        {"upload_key": key, "drive_file_id": "private_file", "status": "uploaded"}]
    for field, bad in [("job_id", "wrong"), ("folder_id", "wrong"), ("upload_key", "wrong"),
                       ("sha256", "wrong"), ("owner_only", False)]:
        with pytest.raises(ValueError, match="Drive receipt"):
            qa_live.worker_delivery_payload(folder, job, approval, {**receipt, field: bad})


def test_fixed_budget_and_retry_limits():
    config = qa_live.bounded_config("fixture-token")
    assert config.bounded_acceptance and config.mode_a_max_output_tokens == 1200
    assert config.mode_b_max_output_tokens == 900 and config.max_requests_per_job == 2
    assert config.max_cost_usd_per_job == 0.25 and config.max_retries == 0


def test_isolated_bridge_accepts_only_the_operator_granted_job(scope, monkeypatch):
    folder, job, approval, _ = scope
    called = []

    def run(directory, record, grant, key, **callbacks):
        assert directory == folder and record == job and grant is approval
        called.append(record["job_id"])
        return {"job_id": grant.job_id, "status": "no_eligible_delivery"}

    monkeypatch.setattr(qa_live, "run_guarded_job", run)
    token = "fixture-independent-adapter-token"
    app = qa_live.build_guarded_qa_app(jobs_root=folder.parent, approval=approval, adapter_token=token, key="unused-fixture",
                                       load_job=lambda _: job, save_analysis=lambda *args: None,
                                       deliver_private=lambda *args: pytest.fail("Unexpected Drive invocation"))

    def submit(payload, key=token):
        return request(app, "/guarded-job", "POST", chunks=(json.dumps(payload).encode(),),
                       headers=((b"x-qa-adapter-token", key.encode()),))

    assert submit({"job_id": job["job_id"]}, "wrong")[0] == 401
    assert submit({"job_id": "3" * 32})[0] == 403
    assert submit({"job_id": job["job_id"], "drive_approved": True})[0] == 422
    assert called == []
    response = submit({"job_id": job["job_id"]})
    assert response[0] == 200 and json.loads(response[2])["job_id"] == job["job_id"]
    assert called == [job["job_id"]]
