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
JOB_READ = re.compile(r"/jobs(?:/[A-Za-z0-9_-]{1,128}(?:/(?:file|pipeline-status|delivery|curation|manifest))?)?")
log = logging.getLogger("uvicorn.error")


def matches(provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(provided.encode(), expected.encode())


class WorkerSecurity:
    def __init__(self, app):
        self.app = app
        self.submissions = deque()

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
            or (method, path) == ("POST", "/intake/validate")
        )
        dashboard = matches(token, os.getenv("MEDIA_WORKER_DASHBOARD_TOKEN", ""))
        if not admin and not (dashboard and dashboard_scope):
            status = 403 if dashboard else 401
            await JSONResponse({"detail": "Forbidden" if dashboard else "Unauthorized"}, status)(scope, receive, respond)
            return

        if method == "POST":
            if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                await JSONResponse({"detail": "Expected application/json"}, 415)(scope, receive, respond)
                return
            length = headers.get("content-length")
            if length is not None and (not length.isascii() or not length.isdecimal() or len(length) > 10):
                await JSONResponse({"detail": "Invalid Content-Length"}, 400)(scope, receive, respond)
                return
            if length is not None and int(length) > MAX_BODY:
                await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, respond)
                return
            if path == "/jobs":
                while self.submissions and self.submissions[0] <= start - 60:
                    self.submissions.popleft()
                if len(self.submissions) >= 20:
                    await JSONResponse({"detail": "Submission limit reached"}, 429, headers={"Retry-After": "60"})(scope, receive, respond)
                    return
                self.submissions.append(start)
            chunks = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(chunks) + len(chunk) > MAX_BODY:
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
