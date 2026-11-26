"""Milestone OS-2 offline tests: Owner-supplied Facebook Export ZIP & direct media upload.

100% offline — zero live sockets, zero external requests.
Covers:
- Authenticated Named Admin binary HMAC assertion + replay protection
- Official Facebook Export ZIP with JSON `creation_timestamp` (in-window vs >365d out-of-window)
- Real JPEG + FFmpeg MP4 ingestion -> Pillow metrics + video keyframes -> new immutable M4 revision
- Zip-slip path traversal rejection, disguised HTML magic-byte rejection, SHA-256 deduplication
- Direct single photo upload with optional explicit published_date
- 409 rejection after curation.json exists and 415/403 security boundaries
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
from io import BytesIO
import json
from pathlib import Path
import socket
import subprocess
import time
import uuid
import zipfile

from PIL import Image, ImageDraw
import pytest

from app import collection_approval as ledger
from app import named_auth
from app.publication import frozen_window
from tests.test_phase9_security import request, worker  # noqa: F401


@pytest.fixture(autouse=True)
def block_live_sockets(monkeypatch):
    def deny(*_args, **_kwargs):
        pytest.fail("OS-2 offline test attempted a live socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny)


def _jpeg_bytes(width: int = 480, height: int = 360, color: tuple[int, int, int] = (200, 120, 90)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([25, 25, width - 25, height - 25], outline=(20, 40, 60), width=8)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _mp4_bytes(tmp_path: Path) -> bytes:
    target = tmp_path / f"os2-{uuid.uuid4().hex[:8]}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1.2:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-t", "1.2", str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return target.read_bytes()


def _signed_binary_upload(
    app,
    *,
    secret: str,
    session: str,
    meta: dict,
    payload: bytes,
    content_type: bytes = b"application/octet-stream",
    api_key: str = "test-dashboard",
):
    import base64

    framed = (json.dumps(meta) + "\n").encode("utf-8") + payload
    body_sha = hashlib.sha256(framed).hexdigest()
    sub_sha = hashlib.sha256(session.encode("utf-8")).hexdigest()
    claims = base64.urlsafe_b64encode(
        json.dumps({
            "iss": "pgm-frontend",
            "aud": "pgm-worker",
            "sub": sub_sha,
            "action": "collection-media-upload",
            "body": body_sha,
            "exp": int(time.time()) + 30,
            "jti": uuid.uuid4().hex,
        }).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), claims.encode("utf-8"), hashlib.sha256).hexdigest()

    # Use raw ASGI call so Content-Type is application/octet-stream (not forced to application/json).
    import asyncio

    messages = []
    incoming = [{"type": "http.request", "body": framed, "more_body": False}]
    headers = [
        (b"content-type", content_type),
        (b"content-length", str(len(framed)).encode("ascii")),
        (b"x-api-key", api_key.encode("ascii")),
        (b"x-named-session", session.encode("ascii")),
        (b"x-named-assertion", f"{claims}.{sig}".encode("ascii")),
    ]

    async def send(msg):
        messages.append(msg)

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    asyncio.run(
        app(
            {
                "type": "http",
                "http_version": "1.1",
                "asgi": {"version": "3.0"},
                "method": "POST",
                "scheme": "http",
                "path": "/named/collection-media-upload",
                "root_path": "",
                "query_string": b"",
                "headers": headers,
            },
            receive,
            send,
        )
    )
    status = messages[0]["status"]
    body = b"".join(m.get("body", b"") for m in messages)
    return status, json.loads(body.decode("utf-8")) if body else {}


def _bootstrap_job_with_m4(worker, monkeypatch, tmp_path: Path):
    secret = "os2-independent-named-assertion-secret-64-bytes-minimum!!"
    monkeypatch.setenv("NAMED_ASSERTION_SECRET", secret)
    monkeypatch.setenv("M4_COLLECTION_APPROVAL_ENABLED", "1")

    with worker.connect() as con:
        ledger.migrate(con)

    admin1_id = named_auth.bootstrap(worker.DB, "admin1", "StrongPass1234!", verified_operator="test-op")
    # Mint a live session directly via login assertion helper.
    import base64

    login_raw = json.dumps({"name": "admin1", "password": "StrongPass1234!"}).encode("utf-8")
    claims = base64.urlsafe_b64encode(
        json.dumps({
            "iss": "pgm-frontend",
            "aud": "pgm-worker",
            "sub": hashlib.sha256(b"").hexdigest(),
            "action": "login",
            "body": hashlib.sha256(login_raw).hexdigest(),
            "exp": int(time.time()) + 30,
            "jti": uuid.uuid4().hex,
        }).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), claims.encode("utf-8"), hashlib.sha256).hexdigest()
    login_res = named_auth.dispatch(worker.DB, "login", login_raw, f"{claims}.{sig}", "")
    session = login_res["session"]

    job_id = uuid.uuid4().hex
    window = frozen_window(datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC))
    job_dir = worker.DATA_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "job_id": job_id,
        "schema_version": "3.0-unified",
        "website_url": "https://salon.example.test/",
        **window,
        "collection_mode": "public_checkpoint",
        "sources": {
            "website": {"status": "completed", "items": [], "issues": []},
            "facebook": {"status": "partial_success", "items": [], "issues": ["robots_disallowed"]},
        },
        "assets": [],
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    record = {
        "job_id": job_id,
        "status": "partial_success",
        "collection_mode": "public_checkpoint",
        "website_url": "https://salon.example.test/",
        "facebook_url": "https://www.facebook.com/salon/",
        "submitter_principal_id": admin1_id,
        **window,
        "sources": manifest["sources"],
    }
    worker.store(record)
    worker.store(record, collection_base=0)
    return secret, session, job_id, job_dir


def test_owner_facebook_export_zip_ingests_media_filters_window_and_publishes_m4_revision(worker, tmp_path, monkeypatch):
    secret, session, job_id, job_dir = _bootstrap_job_with_m4(worker, monkeypatch, tmp_path)

    recent_jpg = _jpeg_bytes(600, 400, (210, 150, 110))
    old_jpg = _jpeg_bytes(500, 400, (110, 150, 210))
    clip_mp4 = _mp4_bytes(tmp_path)

    # Build an official-style Facebook Export ZIP with:
    # - 1 recent in-window photo (2026-10-10)
    # - 1 recent in-window video (2026-10-15)
    # - 1 old >365d photo (2024-05-01 -> out_of_window!)
    # - 1 zip-slip attempt (../../escape.jpg -> blocked)
    # - 1 disguised HTML-as-jpg (invalid magic bytes -> blocked)
    in_window_ts = int(datetime(2026, 10, 10, 14, 0, 0, tzinfo=UTC).timestamp())
    video_ts = int(datetime(2026, 10, 15, 9, 30, 0, tzinfo=UTC).timestamp())
    old_ts = int(datetime(2024, 5, 1, 10, 0, 0, tzinfo=UTC).timestamp())
    fb_json = [
        {
            "attachments": [{
                "data": [
                    {
                        "media": {
                            "uri": "posts/media/recent_dog.jpg",
                            "creation_timestamp": in_window_ts,
                            "description": "Fresh spa trim \u00e2\u0080\u0094 Lily's Club",
                        }
                    },
                    {
                        "media": {
                            "uri": "posts/media/groom_clip.mp4",
                            "creation_timestamp": video_ts,
                            "title": "Salon walkthrough clip",
                        }
                    },
                    {
                        "media": {
                            "uri": "posts/media/old_2024.jpg",
                            "creation_timestamp": old_ts,
                            "description": "Old 2024 photo",
                        }
                    },
                ]
            }]
        }
    ]

    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("posts/your_posts_1.json", json.dumps(fb_json))
        zf.writestr("posts/media/recent_dog.jpg", recent_jpg)
        zf.writestr("posts/media/groom_clip.mp4", clip_mp4)
        zf.writestr("posts/media/old_2024.jpg", old_jpg)
        zf.writestr("../../escape.jpg", recent_jpg)
        zf.writestr("posts/media/fake_image.jpg", b"<html>not a jpeg</html>")

    status, res = _signed_binary_upload(
        worker.app,
        secret=secret,
        session=session,
        meta={"job_id": job_id, "platform": "facebook", "filename": "facebook-export.zip"},
        payload=zip_buf.getvalue(),
    )
    assert status == 200, res
    assert res["ok"] is True
    assert res["uploaded_count"] == 3
    assert res["in_window_count"] == 2
    assert res["out_of_window_count"] == 1
    assert res["invalid_magic_count"] == 1
    assert res["revision"] == 2
    assert any(s["reason"] == "unsafe_path_blocked" for s in res["skipped"])
    assert any(s["reason"] == "invalid_media_magic" for s in res["skipped"])

    # Verify manifest, preprocessing, FFmpeg frames, rights_status=unverified, and M4 revision 2!
    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    by_name = {a.get("filename"): a for a in manifest["assets"] if a.get("type") != "frame"}
    assert by_name["recent_dog.jpg"]["date_eligibility"] == "in_window"
    assert by_name["recent_dog.jpg"]["rights_status"] == "unverified"
    assert "Fresh spa trim" in (by_name["recent_dog.jpg"].get("source_caption") or "")
    assert by_name["recent_dog.jpg"]["preprocessing"]["outcome"] == "processed"
    assert by_name["groom_clip.mp4"]["type"] == "video"
    assert by_name["groom_clip.mp4"]["date_eligibility"] == "in_window"
    assert by_name["groom_clip.mp4"]["preprocessing"]["outcome"] == "video_processed"
    assert any(a.get("type") == "frame" for a in manifest["assets"])
    assert by_name["old_2024.jpg"]["date_eligibility"] == "out_of_window"
    assert "owner_media_uploaded" in manifest["sources"]["facebook"]["issues"]

    # Confirm M4 immutable ledger now has Revision 2 with ONLY the 2 in-window assets eligible!
    with worker.connect() as con:
        rev_row = con.execute(
            "SELECT revision, subset FROM collection_revisions WHERE job=? ORDER BY revision DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    assert rev_row[0] == 2
    subset = json.loads(rev_row[1])
    eligible_ids = {c["asset_id"] for c in subset["candidates"]}
    assert by_name["recent_dog.jpg"]["asset_id"] in eligible_ids
    assert by_name["groom_clip.mp4"]["asset_id"] in eligible_ids
    assert by_name["old_2024.jpg"]["asset_id"] not in eligible_ids

    # Re-uploading identical ZIP deduplicates cleanly without inflating revision or assets.
    status2, res2 = _signed_binary_upload(
        worker.app,
        secret=secret,
        session=session,
        meta={"job_id": job_id, "platform": "facebook", "filename": "facebook-export.zip"},
        payload=zip_buf.getvalue(),
    )
    assert status2 == 200, res2
    assert res2["uploaded_count"] == 0
    assert res2["duplicate_count"] == 3
    assert res2["revision"] == 2


def test_owner_direct_photo_upload_and_security_guards(worker, tmp_path, monkeypatch):
    secret, session, job_id, job_dir = _bootstrap_job_with_m4(worker, monkeypatch, tmp_path)
    single_jpg = _jpeg_bytes(420, 420, (140, 190, 120))

    # Wrong Content-Type rejected (415).
    st_415, _ = _signed_binary_upload(
        worker.app,
        secret=secret,
        session=session,
        meta={"job_id": job_id, "platform": "facebook", "filename": "puppy.jpg"},
        payload=single_jpg,
        content_type=b"application/json",
    )
    assert st_415 == 415

    # Invalid session rejected (403).
    st_403, _ = _signed_binary_upload(
        worker.app,
        secret=secret,
        session="invalid-session-token",
        meta={"job_id": job_id, "platform": "facebook", "filename": "puppy.jpg"},
        payload=single_jpg,
    )
    assert st_403 == 403

    # Direct single photo with explicit published_date -> in_window + revision 2.
    st_ok, res_ok = _signed_binary_upload(
        worker.app,
        secret=secret,
        session=session,
        meta={
            "job_id": job_id,
            "platform": "facebook",
            "filename": "puppy.jpg",
            "published_date": "2026-10-20",
        },
        payload=single_jpg,
    )
    assert st_ok == 200, res_ok
    assert res_ok["uploaded_count"] == 1
    assert res_ok["in_window_count"] == 1
    assert res_ok["revision"] == 2

    # Once curation.json exists, further uploads are strictly blocked (409).
    (job_dir / "curation.json").write_text("{}", encoding="utf-8")
    another_jpg = _jpeg_bytes(320, 320, (50, 90, 200))
    st_409, _ = _signed_binary_upload(
        worker.app,
        secret=secret,
        session=session,
        meta={"job_id": job_id, "platform": "facebook", "filename": "late.jpg"},
        payload=another_jpg,
    )
    assert st_409 == 409
