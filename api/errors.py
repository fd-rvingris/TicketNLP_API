"""RFC 9457 problem details (application/problem+json) for every error. A problem never repeats the
request's text: validation errors keep the location, message and type of each error and drop the
rejected value."""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import API_VERSION
from .auth import AuthError, AuthUnavailable
from .service import NotReady

log = logging.getLogger("api.errors")
MEDIA_TYPE = "application/problem+json"


def problem(status: int, code: str, detail: str | None = None, *, request_id: str | None = None,
            errors: list[dict] | None = None, headers: dict | None = None) -> JSONResponse:
    body = {"type": "about:blank", "title": HTTPStatus(status).phrase, "status": status, "detail": detail,
            "code": code, "requestId": request_id, "apiVersion": API_VERSION}
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, headers=headers, media_type=MEDIA_TYPE)


def request_id_of(request: Request) -> str | None:
    return request.scope.get("state", {}).get("request_id")


def _field_errors(exc: RequestValidationError) -> list[dict]:
    return [{"loc": [p for p in e.get("loc", ())], "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()]


async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = _field_errors(exc)
    if any(e["type"] == "json_invalid" for e in errors):
        return problem(400, "invalid_json", "The request body is not valid JSON.", request_id=request_id_of(request))
    return problem(422, "validation_error", "The request body does not match the schema.",
                   request_id=request_id_of(request), errors=errors)


async def on_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    codes = {404: "not_found", 405: "method_not_allowed"}
    return problem(exc.status_code, codes.get(exc.status_code, "http_error"),
                   exc.detail if isinstance(exc.detail, str) and exc.status_code not in (404, 405) else None,
                   request_id=request_id_of(request), headers=getattr(exc, "headers", None))


async def on_auth_error(request: Request, exc: AuthError) -> JSONResponse:
    return problem(exc.status, exc.code, exc.detail, request_id=request_id_of(request), headers=exc.headers)


async def on_auth_unavailable(request: Request, exc: AuthUnavailable) -> JSONResponse:
    log.warning("Entra signing keys could not be fetched")
    return problem(503, "auth_unavailable", "Tokens cannot be validated right now; try again shortly.",
                   request_id=request_id_of(request), headers={"Retry-After": "30"})


async def on_not_ready(request: Request, exc: NotReady) -> JSONResponse:
    return problem(503, "not_ready", "The models are not loaded; try again shortly.",
                   request_id=request_id_of(request), headers={"Retry-After": "30"})


def install(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, on_validation_error)
    app.add_exception_handler(StarletteHTTPException, on_http_error)
    app.add_exception_handler(AuthError, on_auth_error)
    app.add_exception_handler(AuthUnavailable, on_auth_unavailable)
    app.add_exception_handler(NotReady, on_not_ready)
