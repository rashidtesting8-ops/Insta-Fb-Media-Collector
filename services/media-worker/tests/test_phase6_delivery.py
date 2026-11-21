"""Phase 6 delivery tests. All curation here is an explicitly labeled
FIXTURE (allow_fixture=True) and must never be treated as real AI curation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.delivery import build_delivery_plan, finalize_delivery, sanitize_folder_name

FIXTURE_TAG = "FIXTURE-ONLY-PHASE6-INTEGRATION"


def _job(tmp_path: Path, *, assets=2, rights="unverified") -> tuple[Path, list[dict]]:
    job = tmp_path / "job_p6"
    job.mkdir()
    rows = []
    for i in range(assets):
        blob = f"phase6-fixture-image-{i}".encode()
        rel = f"website/img{i}.jpg"
        path = job / rel
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(blob)
        rows.append({
            "asset_id": f"web_{i:04d}_f{i}",
            "local_path": rel, "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob), "mime": "image/jpeg", "rights_status": rights,
            "extraction_status": "downloaded", "source_url": "https://example.test/fixture",
        })
    manifest = {"schema_version": "3.0-unified", "job_id": "job_p6", "assets": rows}
    (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    curation = {
        "status": "completed", "job_id": "job_p6", "fixture": FIXTURE_TAG,
        "ai_envelope": {"mock": False, "prompt_version": FIXTURE_TAG},
        "curation": {
            "selected_asset_ids": [r["asset_id"] for r in rows],
            "rights_review_required_ids": [r["asset_id"] for r in rows],
            "analysis_coverage": {"mock": False},
        },
    }
    (job / "curation.json").write_text(json.dumps(curation), encoding="utf-8")
    return job, rows


def test_folder_name_sanitization():
    assert sanitize_folder_name("https://Pup-Palace.example.com/path", "abcdef1234567890") == "pup-palace-example-com-abcdef12"
    assert sanitize_folder_name("!!!", "x") == "pet-grooming-business-x"


def test_fixture_curation_blocked_by_default(tmp_path):
    job, _ = _job(tmp_path)
    plan = build_delivery_plan(job, "job_p6", "Pup Palace")
    assert plan["status"] == "blocked_non_real_curation"
    assert plan["assets"] == []


@pytest.mark.parametrize("field", ["envelope", "coverage", "missing"])
def test_mock_boolean_and_unknown_provenance_are_blocked(tmp_path, field):
    job, _ = _job(tmp_path)
    path = job / "curation.json"
    envelope = json.loads(path.read_text())
    del envelope["fixture"]
    envelope["ai_envelope"]["prompt_version"] = "v2.0"
    if field == "envelope":
        envelope["ai_envelope"]["mock"] = True
    elif field == "coverage":
        envelope["curation"]["analysis_coverage"]["mock"] = True
    else:
        del envelope["ai_envelope"]["mock"]
    path.write_text(json.dumps(envelope))
    assert build_delivery_plan(job, "job_p6")["status"] == "blocked_non_real_curation"


def test_plan_maps_validated_files_and_rights(tmp_path):
    job, rows = _job(tmp_path)
    plan = build_delivery_plan(job, "job_p6", "Pup Palace", allow_fixture=True)
    assert plan["status"] == "ready"
    assert len(plan["assets"]) == 2
    assert plan["folder_name"].startswith("pup-palace-job-p6")
    assert all(a["rights_review_required"] for a in plan["assets"])
    assert all(a["n8n_path"].startswith("/data/media/job_p6/") for a in plan["assets"])
    assert all(a["sha256"] == r["sha256"] for a, r in zip(plan["assets"], rows))


def test_plan_rejects_path_traversal_and_missing_files(tmp_path):
    job, rows = _job(tmp_path)
    rows[0]["local_path"] = "../../outside.jpg"
    (job / "manifest.json").write_text(json.dumps({"assets": rows}), encoding="utf-8")
    plan = build_delivery_plan(job, "job_p6", "Pup Palace", allow_fixture=True)
    assert all(a["asset_id"] != rows[0]["asset_id"] for a in plan["assets"])
    assert any("unsafe path" in e for e in plan["errors"])


def test_plan_detects_sha_mismatch(tmp_path):
    job, rows = _job(tmp_path)
    rows[0]["sha256"] = "0" * 64
    (job / "manifest.json").write_text(json.dumps({"assets": rows}), encoding="utf-8")
    plan = build_delivery_plan(job, "job_p6", "Pup Palace", allow_fixture=True)
    assert any("sha256 mismatch" in e for e in plan["errors"])


def test_finalize_rights_gate_and_upload_ledger(tmp_path):
    job, _ = _job(tmp_path)
    plan = build_delivery_plan(job, "job_p6", "Pup Palace", allow_fixture=True)
    uploads = [
        {"upload_key": a["upload_key"], "drive_file_id": f"drive-{i}",
         "drive_link": f"https://drive.google.com/file/d/drive-{i}", "status": "uploaded"}
        for i, a in enumerate(plan["assets"])
    ]
    blocked = finalize_delivery(job, plan, uploads, authorization_confirmed=False)
    assert blocked["status"] == "blocked_rights_review"
    done = finalize_delivery(job, plan, uploads, authorization_confirmed=True)
    assert done["status"] == "completed"
    assert len(done["uploaded"]) == 2
    assert json.loads((job / "delivery.json").read_text())["status"] == "completed"


def test_idempotency_dedupes_retries(tmp_path):
    job, _ = _job(tmp_path)
    plan = build_delivery_plan(job, "job_p6", "Pup Palace", allow_fixture=True)
    a = plan["assets"][0]
    retries = [
        {"upload_key": a["upload_key"], "drive_file_id": "first", "status": "uploaded"},
        {"upload_key": a["upload_key"], "drive_file_id": "second", "status": "uploaded"},
        {"upload_key": "unknown:key", "drive_file_id": "bad", "status": "uploaded"},
    ]
    result = finalize_delivery(job, plan, retries, authorization_confirmed=True)
    assert result["status"] == "partial_success"
    assert len(result["uploaded"]) == 1
    assert result["uploaded"][0]["drive_file_id"] == "first"


def test_partial_upload_failure(tmp_path):
    job, _ = _job(tmp_path)
    plan = build_delivery_plan(job, "job_p6", "Pup Palace", allow_fixture=True)
    uploads = [
        {"upload_key": plan["assets"][0]["upload_key"], "drive_file_id": "d1", "status": "uploaded"},
        {"upload_key": plan["assets"][1]["upload_key"], "status": "failed", "error": "network"},
    ]
    result = finalize_delivery(job, plan, uploads, authorization_confirmed=True, drive_folder_id="fixture-folder")
    assert result["status"] == "partial_success"
    assert result["uploaded"][1]["error"] == "network"
    retry = build_delivery_plan(job, "job_p6", allow_fixture=True)
    assert retry["drive_folder_id"] == "fixture-folder"
    assert retry["pending_uploads"] == [plan["assets"][1]]
    finished = finalize_delivery(job, retry, [
        {"upload_key": plan["assets"][1]["upload_key"], "drive_file_id": "d2", "status": "uploaded"}
    ], authorization_confirmed=True)
    assert finished["status"] == "completed"
    assert [item["drive_file_id"] for item in finished["uploaded"]] == ["d1", "d2"]


def test_delivery_disk_path_rejects_symlink_escape(tmp_path):
    job, rows = _job(tmp_path, assets=1)
    target = tmp_path / "outside.jpg"
    link = job / rows[0]["local_path"]
    link.rename(target)
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    plan = build_delivery_plan(job, "job_p6", allow_fixture=True)
    assert plan["assets"] == []
    assert any("unsafe path" in error for error in plan["errors"])


def test_n8n_workflow_json_imports():
    # CI runs tests from the repo; the worker image only has /srv/tests.
    here = Path(__file__).resolve()
    candidates = [
        here.parents[d] / "n8n-media" / "06-google-drive-delivery.json"
        for d in range(min(4, len(here.parents)))
    ]
    candidates.append(Path("/srv/n8n-media/06-google-drive-delivery.json"))
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        pytest.skip("n8n workflow assets are not present in this runtime layout")
    data = json.loads(path.read_text(encoding="utf-8"))
    names = {n["name"] for n in data["nodes"]}
    assert {"Delivery Webhook", "Create Business Drive Folder", "Upload Original to Drive", "Persist Delivery Ledger"} <= names
    assert "GOOGLE_DRIVE_CREDENTIAL_ID" in path.read_text()  # credential BY NAME only
