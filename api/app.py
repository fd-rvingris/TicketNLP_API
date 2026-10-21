"""The HTTP API (FastAPI): /v1 endpoints, probes and the OpenAPI document.

    uvicorn --factory api.app:create_app      or      python -m api [--local]

create_app() reads the settings from the environment unless given them. Models load in a background
thread at startup, so /health answers at once and /ready turns 200 when they are loaded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Annotated

import anyio
from fastapi import APIRouter, Body, Depends, FastAPI, Request

from . import API_VERSION, errors, logs
from .auth import Authenticator, require_auth
from .middleware import RequestContext
from .schemas import (REQUEST_EXAMPLES, AnalyzeResponse, CatalogResponse, ClassifyResponse, DetectResponse, Problem,
                      Status, TicketRequest)
from .service import Service, TicketInput
from .settings import Settings, load_settings

log = logging.getLogger("api.app")

DESCRIPTION = """\
Finds the SQL Server hosts and instances a Fortified Data support ticket names, resolved against the
client's known names, and classifies the ticket's taxonomy type and intent. One ticket per request.
Callers: Keystone and other applications.

**Versioning.** The URL version (/v1) changes only for breaking changes. Within v1, changes are
additive: new optional request fields, new response fields and new enum values (new types, intents,
statuses, sources or rule names). Clients must ignore response fields they do not know and accept
enum values they do not know; `GET /v1/catalog` lists the current values. Every response carries
`apiVersion` and a `models` object with the model and directory versions that produced it; models
are retrained and the directory refreshed without an API change.

**Auth.** Send an API key in `X-API-Key` (ApiKeyAuth) or an Entra ID bearer token (EntraBearer),
whichever the deployment is configured for. `/health` and `/ready` need neither.

**Errors** are RFC 9457 problem details (`application/problem+json`) with a stable `code`. They
never repeat the request's text. Bodies are limited to 1 MB.

**Data.** Ticket text is analyzed in memory and never stored or logged.
"""

TAGS = [{"name": "analysis", "description": "Server detection and taxonomy classification of one ticket."},
        {"name": "catalog", "description": "The keys and enumerated values responses can contain."},
        {"name": "probes", "description": "Liveness and readiness for the container platform; no auth."}]

PROBLEMS = {status: {"model": Problem, "description": text, "content": {"application/problem+json": {}}}
            for status, text in {400: "The body is not valid JSON.", 401: "No or invalid credentials.",
                                 403: "Valid credentials without the required app role.",
                                 413: "The body is larger than 1 MB.", 415: "The body is not application/json.",
                                 422: "The body does not match the schema.", 500: "An internal error.",
                                 503: "The models are not loaded yet."}.items()}
TicketBody = Annotated[TicketRequest, Body(openapi_examples=REQUEST_EXAMPLES)]


def ticket_input(body: TicketRequest) -> TicketInput:
    return TicketInput(body.text, body.subject or "", body.org_id(), body.ticket_id())


def _note(request: Request, body: TicketRequest) -> None:
    """What the request log line may say about the request: the text length and whether an org id was given."""
    request.state.textLength = len(body.text)
    request.state.orgIdGiven = body.zendeskOrgId is not None


def _rid(request: Request) -> str:
    return errors.request_id_of(request)


def _service(request: Request) -> Service:
    return request.app.state.service


v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)], responses=PROBLEMS)


@v1.post("/detect", response_model=DetectResponse, tags=["analysis"], summary="Detect servers and instances",
         description="The hosts, bare instance names and HOST\\INSTANCE names in one ticket, as a compact "
                     "`entities` view and one `detections` entry per occurrence. With `zendeskOrgId`, names "
                     "are resolved against that client's known servers and instances.")
async def detect(request: Request, body: TicketBody) -> dict:
    _note(request, body)
    svc = _service(request)
    return await svc.run(svc.detect, ticket_input(body), _rid(request))


@v1.post("/classify", response_model=ClassifyResponse, tags=["analysis"], summary="Classify type and intent",
         description="The taxonomy type and intent of one ticket: the alert-name table for alerts, then the "
                     "keyword rules, then the model, with where each came from and an uncertain flag. "
                     "`zendeskOrgId` is accepted and ignored.")
async def classify(request: Request, body: TicketBody) -> dict:
    _note(request, body)
    svc = _service(request)
    return await svc.run(svc.classify, ticket_input(body), _rid(request))


@v1.post("/analyze", response_model=AnalyzeResponse, tags=["analysis"], summary="Detect and classify",
         description="Both /v1/detect and /v1/classify for one ticket, from one request: `detection` and "
                     "`taxonomy` hold their bodies without the envelope fields.")
async def analyze(request: Request, body: TicketBody) -> dict:
    _note(request, body)
    svc = _service(request)
    return await svc.run(svc.analyze, ticket_input(body), _rid(request))


@v1.get("/catalog", response_model=CatalogResponse, tags=["catalog"], summary="Keys and values",
        description="Every detection key and field with its enumerated values, the taxonomy's types and "
                    "intents, and the type and intent sources (including each keyword rule), generated from "
                    "the code and taxonomy.json. Lists no client's names.")
async def catalog(request: Request) -> dict:
    return _service(request).catalog(_rid(request))


probes = APIRouter(tags=["probes"])


@probes.get("/health", response_model=Status, summary="Liveness",
            description="200 while the process is up. No auth, no detail.")
async def health() -> dict:
    return {"status": "ok"}


@probes.get("/ready", response_model=Status, summary="Readiness",
            responses={503: {"model": Problem, "description": "The models are not loaded.",
                             "content": {"application/problem+json": {}}}},
            description="200 when both models and the directory are loaded, else 503. No auth, no model detail.")
async def ready(request: Request):
    svc = _service(request)
    detail = svc.readiness()
    # The detail goes to the log only, once per state or every READY_LOG_SECONDS, not on every probe
    key = (detail["ready"], detail["problem"], detail["directoryStoreError"])
    now = time.monotonic()
    if key != _ready_log["key"] or now - _ready_log["at"] >= READY_LOG_SECONDS:
        _ready_log.update(key=key, at=now)
        if detail["problem"] or detail["directoryStoreError"]:
            log.warning("Readiness: %s", detail)
        elif not svc.ready:
            log.info("Readiness: the models are still loading")
    if svc.ready:
        return {"status": "ready"}
    return errors.problem(503, "not_ready", None, request_id=_rid(request), headers={"Retry-After": "10"})


READY_LOG_SECONDS = 300
_ready_log = {"key": None, "at": 0.0}


def create_app(settings: Settings | None = None, *, service: Service | None = None, load: bool = True,
               background: bool = True, authenticator: Authenticator | None = None,
               configure_logging: bool = True) -> FastAPI:
    """The app. load=False leaves loading to the caller (tests, the OpenAPI export); background=False
    loads before serving."""
    settings = settings or load_settings()
    settings.check_serving()
    if configure_logging:
        logs.configure(settings.log_level, settings.log_format)
    service = service or Service(settings)
    authenticator = authenticator or Authenticator(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if load and not service.ready:
            if background:
                service.start_loading()
            else:
                await anyio.to_thread.run_sync(service.load)
        refresher = asyncio.create_task(_refresh_loop(service, settings.directory_refresh_minutes * 60))
        try:
            yield
        finally:
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
            service.close()

    app = FastAPI(title="Ticket Analysis API", version=API_VERSION, description=DESCRIPTION, openapi_tags=TAGS,
                  docs_url="/docs" if settings.enable_docs else None, redoc_url=None, openapi_url="/openapi.json",
                  lifespan=lifespan, separate_input_output_schemas=False)
    app.state.service, app.state.authenticator, app.state.settings = service, authenticator, settings
    errors.install(app)
    app.include_router(v1)
    app.include_router(probes)
    app.add_middleware(RequestContext, precheck=authenticator.missing_credentials,
                       loopback_only=settings.auth_mode == "none")
    return app


async def _refresh_loop(service: Service, seconds: float) -> None:
    """Check the directory store for a newer snapshot every DIRECTORY_REFRESH_MINUTES."""
    while True:
        await asyncio.sleep(seconds)
        try:
            await anyio.to_thread.run_sync(service.refresh_directory)
        except Exception as e:  # never let the loop die; the cache keeps the current directory
            log.warning("Directory refresh failed (%s)", type(e).__name__)
