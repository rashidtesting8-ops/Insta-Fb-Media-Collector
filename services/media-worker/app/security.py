"""Internal HTTP boundary for the worker."""

import hmac
import json
import logging
import os
import re
import time
from collections import deque

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

MAX_BODY = 64 * 1024
MAX_LARGE_BODY = 512 * 1024
MAX_UPLOAD_BODY = 100 * 1024 * 1024 + 8192
LOGIN_RATE_LIMIT = 20
LOGIN_WINDOW_S = 60
LARGE_BODY_PATH = re.compile(r"^(?:/named/collection-execute|/jobs/[A-Za-z0-9_-]{1,128}/delivery-result)$")
UPLOAD_BODY_PATH = "/named/collection-media-upload"
JOB_READ = re.compile(r"/jobs(?:/[A-Za-z0-9_-]{1,128}(?:/(?:file|pipeline-status|delivery|curation|manifest))?)?")
log = logging.getLogger("uvicorn.error")

_SUBPROCESS_ENV_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)
_SUBPROCESS_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def sanitized_subprocess_env(*, stage: object | None = None, allow_proxy: bool = False) -> dict[str, str]:
    """Build a minimal allowlisted environment for untrusted CLI subprocesses.

    Never inherits worker tokens, AI_API_KEY, NAMED_ASSERTION_SECRET, or host user config paths.
    """
    keys = _SUBPROCESS_ENV_KEYS + (_SUBPROCESS_PROXY_KEYS if allow_proxy else ())
    env = {k: v for k in keys if (v := os.environ.get(k)) is not None}
    if stage is not None:
        stage_dir = str(stage)
        env["HOME"] = stage_dir
        env["USERPROFILE"] = stage_dir
        env["XDG_CONFIG_HOME"] = stage_dir
        env["XDG_CACHE_HOME"] = stage_dir
    return env


def matches(provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(provided.encode(), expected.encode())


class WorkerSecurity:
    def __init__(self, app):
        self.app = app
        self.submissions = deque()
        self.logins = deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = HTTPConnection(scope)
        path, method = scope["path"], scope["method"]
        headers = request.headers
        start = time.monotonic()

        async def respond(message):
            if message["type"] == "http.response.start":
                route = getattr(scope.get("route"), "path", "unmatched")
                log.info(json.dumps({
                    "event": "worker_http", "method": method, "route": route,
                    "status": message["status"], "ms": round((time.monotonic() - start) * 1000),
                }))
            await send(message)

        if (method, path) in {("GET", "/health"), ("GET", "/ready")}:
            await self.app(scope, receive, respond)
            return

        token = headers.get("x-api-key", "")
        admin = matches(token, os.getenv("MEDIA_WORKER_TOKEN", ""))
        dashboard_scope = (
            method == "GET" and JOB_READ.fullmatch(path)
            or (method, path) == ("GET", "/status")
            or (method, path) == ("POST", "/intake/validate")
            or (method == "POST" and path.startswith("/named/"))
        )
        dashboard = matches(token, os.getenv("MEDIA_WORKER_DASHBOARD_TOKEN", ""))
        if not admin and not (dashboard and dashboard_scope):
            status = 403 if dashboard else 401
            await JSONResponse({"detail": "Forbidden" if dashboard else "Unauthorized"}, status)(scope, receive, respond)
            return

        if method == "POST":
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            is_media_upload = path == UPLOAD_BODY_PATH
            expected_ctype = "application/octet-stream" if is_media_upload else "application/json"
            if ctype != expected_ctype:
                await JSONResponse({"detail": f"Expected {expected_ctype}"}, 415)(scope, receive, respond)
                return
            body_limit = (
                MAX_UPLOAD_BODY
                if is_media_upload
                else (MAX_LARGE_BODY if LARGE_BODY_PATH.fullmatch(path) else MAX_BODY)
            )
            length = headers.get("content-length")
            if length is not None and (not length.isascii() or not length.isdecimal() or len(length) > 10):
                await JSONResponse({"detail": "Invalid Content-Length"}, 400)(scope, receive, respond)
                return
            if length is not None and int(length) > body_limit:
                await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, respond)
                return
            if path == "/jobs":
                while self.submissions and self.submissions[0] <= start - 60:
                    self.submissions.popleft()
                if len(self.submissions) >= 20:
                    await JSONResponse({"detail": "Submission limit reached"}, 429, headers={"Retry-After": "60"})(scope, receive, respond)
                    return
                self.submissions.append(start)
            elif path == "/named/login":
                while self.logins and self.logins[0] <= start - LOGIN_WINDOW_S:
                    self.logins.popleft()
                if len(self.logins) >= LOGIN_RATE_LIMIT:
                    await JSONResponse(
                        {"detail": "Login rate limit reached"},
                        429,
                        headers={"Retry-After": str(LOGIN_WINDOW_S)},
                    )(scope, receive, respond)
                    return
                self.logins.append(start)
            chunks = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(chunks) + len(chunk) > body_limit:
                    await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, respond)
                    return
                chunks.extend(chunk)
                if not message.get("more_body"):
                    break

            replayed = False

            async def replay():
                nonlocal replayed
                if replayed:
                    return await receive()
                replayed = True
                return {"type": "http.request", "body": bytes(chunks), "more_body": False}

            await self.app(scope, replay, respond)
            return
        await self.app(scope, receive, respond)
