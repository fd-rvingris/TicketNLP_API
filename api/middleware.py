"""The request wrapper around the whole app (pure ASGI): the X-Request-Id header, the body limit (413),
JSON-only bodies on /v1 (415), a safe 500, and one log line per request without its content."""

from __future__ import annotations

import json
import logging
import re
import time
import traceback
import uuid
from http import HTTPStatus

from starlette.datastructures import Headers, MutableHeaders

from . import API_VERSION

log = logging.getLogger("api.request")

MAX_BODY_BYTES = 1_000_000
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
BODY_METHODS = {"POST", "PUT", "PATCH"}


def _is_json(content_type: str | None) -> bool:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return media == "application/json" or (media.startswith("application/") and media.endswith("+json"))


async def _send_problem(send, status: int, code: str, detail: str, request_id: str, headers: dict | None = None) -> None:
    body = json.dumps({"type": "about:blank", "title": HTTPStatus(status).phrase, "status": status, "detail": detail,
                       "code": code, "requestId": request_id, "apiVersion": API_VERSION}).encode("utf-8")
    extra = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/problem+json"), (b"content-length", str(len(body)).encode()),
                            (b"x-request-id", request_id.encode()), (b"x-content-type-options", b"nosniff"),
                            (b"cache-control", b"no-store"), *extra]})
    await send({"type": "http.response.body", "body": body})


LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


def _host_name(host_header: str) -> str:
    """The name in a Host header, without the port: '[::1]:8000' -> '::1', 'localhost:8000' -> 'localhost'."""
    if host_header.startswith("["):
        return host_header[1:].split("]", 1)[0]
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


def _loopback_request(scope, headers: Headers) -> bool:
    """A connection to a loopback address with a loopback Host header (no DNS-rebinding page)."""
    server = scope.get("server")
    if not server or str(server[0]) not in LOOPBACK_NAMES:
        return False
    return _host_name(headers.get("host") or "").lower() in LOOPBACK_NAMES


def _where(exc: BaseException) -> str:
    """file:line of the innermost frame, for a 500 log line without the exception's message."""
    tb = traceback.extract_tb(exc.__traceback__)
    return f"{tb[-1].filename.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}:{tb[-1].lineno}" if tb else "?"


class RequestContext:
    """precheck(headers) returns the auth error for a request with no credential at all, so such a request
    is refused before its body is read. loopback_only (AUTH_MODE=none) refuses any request that did not
    arrive on a loopback address with a loopback Host header, whatever address the server was bound to."""

    def __init__(self, app, max_body: int = MAX_BODY_BYTES, precheck=None, loopback_only: bool = False):
        self.app, self.max_body, self.precheck, self.loopback_only = app, max_body, precheck, loopback_only

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        given = headers.get("x-request-id") or ""
        request_id = given if REQUEST_ID_RE.match(given) else uuid.uuid4().hex
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        method, path = scope["method"], scope["path"]
        start = time.perf_counter()
        result = {"status": 500, "started": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                result["status"], result["started"] = message["status"], True
                h = MutableHeaders(scope=message)
                h["X-Request-Id"] = request_id
                h.setdefault("X-Content-Type-Options", "nosniff")
                if path.startswith("/v1/"):
                    h.setdefault("Cache-Control", "no-store")
            await send(message)

        try:
            if self.loopback_only and not _loopback_request(scope, headers):
                result["status"] = 403
                return await _send_problem(send, 403, "loopback_only",
                                           "Without auth the API answers loopback requests only.", request_id)
            if method in BODY_METHODS:
                if self.precheck and path.startswith("/v1/"):
                    error = self.precheck(headers)
                    if error is not None:
                        result["status"] = error.status
                        return await _send_problem(send, error.status, error.code, error.detail, request_id,
                                                   error.headers)
                if path.startswith("/v1/") and not _is_json(headers.get("content-type")):
                    result["status"] = 415
                    return await _send_problem(send, 415, "unsupported_media_type",
                                               "Send the body as application/json.", request_id)
                length = headers.get("content-length")
                if length and length.isdigit() and int(length) > self.max_body:
                    result["status"] = 413
                    return await _send_problem(send, 413, "body_too_large",
                                               f"The body is larger than {self.max_body:,} bytes.", request_id)
                body = bytearray()
                more = True
                while more:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body += message.get("body", b"")
                    more = message.get("more_body", False)
                    if len(body) > self.max_body:
                        result["status"] = 413
                        return await _send_problem(send, 413, "body_too_large",
                                                   f"The body is larger than {self.max_body:,} bytes.", request_id)
                replayed = False

                async def replay():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                return await self.app(scope, replay, send_wrapper)
            return await self.app(scope, receive, send_wrapper)
        except Exception as e:
            # The type and place only; the message could quote request data.
            log.error("Unhandled %s at %s", type(e).__name__, _where(e), extra={"requestId": request_id})
            if not result["started"]:
                result["status"] = 500
                await _send_problem(send, 500, "internal_error", "The request could not be completed.", request_id)
        finally:
            extra = {"method": method, "path": path, "status": result["status"],
                     "latencyMs": round((time.perf_counter() - start) * 1000, 1), "requestId": request_id}
            for key in ("textLength", "orgIdGiven", "caller"):
                if key in state:
                    extra[key] = state[key]
            if path not in ("/health", "/ready"):
                log.info("%s %s %s", method, path, result["status"], extra=extra)
