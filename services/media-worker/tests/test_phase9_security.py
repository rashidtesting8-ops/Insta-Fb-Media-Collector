import asyncio
import json
from pathlib import Path

import pytest


@pytest.fixture
def worker(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    from app import service

    monkeypatch.setenv("MEDIA_WORKER_TOKEN", "test-admin")
    monkeypatch.setenv("MEDIA_WORKER_DASHBOARD_TOKEN", "test-dashboard")
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "DB", tmp_path / "jobs.sqlite")
    monkeypatch.setattr(service.app, "middleware_stack", None)
    with service.connect() as db:
        db.execute("INSERT INTO jobs VALUES (?,?)", ("a" * 32, json.dumps({"job_id": "a" * 32})))
    return service


def request(app, path, method="GET", key=None, chunks=(), headers=(), query=b""):
    messages = []
    body = iter(chunks or (b"",))
    incoming = [{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
                for index, chunk in enumerate(body)]
    request_headers = list(headers)
    if method == "POST":
        request_headers.append((b"content-type", b"application/json"))
    if key:
        request_headers.append((b"x-api-key", key.encode()))

    async def send(message):
        messages.append(message)

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    asyncio.run(app({"type": "http", "http_version": "1.1", "asgi": {"version": "3.0"},
                     "method": method, "scheme": "http", "path": path, "root_path": "",
                     "query_string": query, "headers": request_headers}, receive, send))
    return messages[0]["status"], dict(messages[0]["headers"]), b"".join(m.get("body", b"") for m in messages)


def test_worker_authorization(worker, monkeypatch):
    app = worker.app
    job = "/jobs/" + "a" * 32
    assert request(app, "/ready")[0] == 200
    assert request(app, "/jobs")[0] == 401
    assert request(app, job, key="test-dashboard")[0] == 200
    assert request(app, job + "/delivery-plan", key="test-dashboard")[0] == 403
    assert request(app, job + "/orchestration", "POST", "test-dashboard")[0] == 403
    assert request(app, job + "/delivery-result", "POST", "test-dashboard")[0] == 403
    assert request(app, "/jobs", "POST", "test-dashboard")[0] == 403
    assert request(app, "/status", key="test-admin")[0] == 200
    assert request(app, "/jobs", key="incorrect")[0] == 401
    monkeypatch.delenv("MEDIA_WORKER_TOKEN")
    assert request(app, "/status", key="test-admin")[0] == 401


def test_input_limits_and_cors(worker):
    app = worker.app
    assert request(app, "/intake/validate", "POST", "test-dashboard", (b"{}",),
                   ((b"content-length", b"100000"),))[0] == 413
    assert request(app, "/intake/validate", "POST", "test-dashboard",
                   (b"x" * 32768, b"x" * 32769))[0] == 413
    assert request(app, "/jobs", "POST", "test-admin",
                   (b'{"website_url":"file:///etc/hosts"}',))[0] == 422
    _, headers, _ = request(app, "/jobs", key="test-dashboard",
                            headers=((b"origin", b"https://untrusted.example"),))
    assert b"access-control-allow-origin" not in headers


def test_preview_keeps_path_boundary(worker):
    job_id = "a" * 32
    (worker.DATA_DIR / job_id).mkdir()
    (worker.DATA_DIR / "outside.png").write_bytes(b"test")
    assert request(worker.app, f"/jobs/{job_id}/file", key="test-dashboard",
                   query=b"path=../outside.png")[0] == 400


def test_submission_rate_limit(worker):
    for _ in range(20):
        assert request(worker.app, "/jobs", "POST", "test-admin")[0] == 422
    assert request(worker.app, "/jobs", "POST", "test-admin")[0] == 429


def test_job_retry_reuses_existing_id_while_slots_are_full(worker, monkeypatch):
    from threading import BoundedSemaphore

    submitted = []
    monkeypatch.setattr(worker, "slots", BoundedSemaphore(1))
    monkeypatch.setattr(worker.executor, "submit", lambda *args: submitted.append(args))
    request_body = (b'{"website_url":"https://business.test","idempotency_key":"test-retry"}',)
    first = request(worker.app, "/jobs", "POST", "test-admin", request_body)
    second = request(worker.app, "/jobs", "POST", "test-admin", request_body)
    assert first[0] == second[0] == 202
    assert json.loads(first[2])["job_id"] == json.loads(second[2])["job_id"]
    assert json.loads(second[2])["idempotent_reuse"] is True
    assert len(submitted) == 1


def test_workflow_auth_and_acknowledgement():
    workflows = Path(__file__).resolve().parents[3] / "n8n-media"
    if not workflows.is_dir():
        pytest.skip("workflow files require the repository layout")
    for name in ("05-ai-curation-probe", "06-google-drive-delivery", "07-pipeline-orchestration"):
        workflow = json.loads((workflows / f"{name}.json").read_text())
        nodes = {node["name"]: node for node in workflow["nodes"]}
        for node in nodes.values():
            params = node["parameters"]
            if node["type"].endswith(".webhook"):
                assert params["authentication"] == "headerAuth"
            if node["type"].endswith(".httpRequest") and "collector:" in params.get("url", ""):
                assert params["genericAuthType"] == "httpHeaderAuth"
                assert node["credentials"]["httpHeaderAuth"]["name"] == "Media Worker API Token"
                assert "collector:8787" in params["url"]
        if name.startswith("06"):
            condition = nodes["Plan Ready Gate"]["parameters"]["conditions"]["conditions"][0]
            assert "authorization_confirmed === true" in condition["leftValue"]
        if name.startswith("07"):
            assert nodes["Respond Accepted"]["parameters"]["options"]["responseCode"] == 202
            assert workflow["connections"]["Session"]["main"][0][0]["node"] == "Respond Accepted"
            assert workflow["connections"]["Respond Accepted"]["main"][0][0]["node"] == "Poll Job Status"
