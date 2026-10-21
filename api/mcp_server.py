"""The MCP server: the API's operations as tools for AI assistants, with the official MCP Python SDK.

Tools detect_servers, classify_ticket, analyze_ticket and get_catalog take the same inputs and return
the same JSON as POST /v1/detect, /v1/classify, /v1/analyze and GET /v1/catalog. They call the service
layer directly, not the HTTP API.

Served from the API's container at /mcp (streamable HTTP, behind the same auth as /v1; see app.py), and
over stdio for local use:

    python -m api.mcp_server        # the local profile: models from data/, no auth
"""

from __future__ import annotations

import contextvars
import logging
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from . import API_VERSION
from .schemas import (AnalyzeResponse, CatalogResponse, ClassifyResponse, DetectResponse, Subject, Text, TicketId,
                      TicketRequest, ZendeskOrgId)
from .service import NotReady, Service, TicketInput

log = logging.getLogger("api.mcp")
# The HTTP request id of a call over /mcp (set by the auth wrapper in app.py); stdio calls get a new id.
mcp_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_request_id", default=None)

INSTRUCTIONS = (
    "Analyze Fortified Data support tickets (SQL Server managed services). detect_servers finds the servers and "
    "instances a ticket names, checked against the client's known names when you pass zendeskOrgId; "
    "classify_ticket gives the taxonomy type and intent; analyze_ticket does both in one call; get_catalog "
    "explains every key, status, type, intent and source the others return. One ticket per call. Results "
    "are local model output: treat 'uncertain' types and 'possible_new_*' statuses as leads to check.")

DETECT = (
    "Find the SQL Server hosts, named instances and HOST\\INSTANCE names in one support ticket. Pass the ticket's "
    "text, its subject if you have it, and the client's zendeskOrgId so names are checked against that client's "
    "known servers (without it no client's names apply and every name is new). Returns 'entities' (each key's "
    "distinct names: host, instance, host_instance) and 'detections' (each occurrence with its position, the "
    "host an instance was paired with, and a status such as known, possible_new_host or typo_of_known). Use it "
    "when you need which servers a ticket is about; it does not classify the ticket.")
CLASSIFY = (
    "Classify one support ticket into the Fortified Data taxonomy: its primary type (such as backup_restore, "
    "access_security or performance) and its intent (such as fix_incident or request_change), with where each "
    "came from (the alert-name table, a keyword rule or the model), the model's top candidates, and whether the "
    "type is uncertain. Pass the text and, if you have it, the subject (alerts are recognized by it). Use it "
    "when you need what kind of work a ticket is; it does not look for servers and ignores zendeskOrgId.")
ANALYZE = (
    "Both detect_servers and classify_ticket for one ticket in a single call, with the same results as calling "
    "each: 'detection' holds the servers and instances, 'taxonomy' the type and intent. Prefer it when you need "
    "both for the same ticket.")
CATALOG = (
    "List every value the other tools can return, with its meaning: detection keys, every detection field and "
    "status, the taxonomy's types and intents with their definitions, and the type and intent sources (including "
    "each keyword rule). Takes no input. Call it to interpret a value you do not recognize.")


def _input(text, subject, zendeskOrgId, ticketId) -> TicketInput:
    try:
        req = TicketRequest(text=text, subject=subject, zendeskOrgId=zendeskOrgId, ticketId=ticketId)
    except ValidationError as e:  # field names only: the rejected values are the caller's data
        raise ToolError("Invalid arguments: " + ", ".join(sorted({".".join(map(str, err["loc"])) for err in e.errors()})))
    return TicketInput(req.text, req.subject or "", req.org_id(), req.ticket_id())


def build_mcp(service: Service) -> MCPServer:
    mcp = MCPServer("ticket-analysis", title="Fortified Data ticket analysis", version=API_VERSION,
                    instructions=INSTRUCTIONS, log_level="WARNING")

    async def call(tool: str, operation: str, inp: TicketInput | None = None) -> dict:
        request_id = mcp_request_id.get() or uuid.uuid4().hex
        extra = {"tool": tool, "requestId": request_id}
        if inp is not None:
            extra.update(textLength=len(inp.text), orgIdGiven=inp.org_id is not None)
        log.info("MCP %s", tool, extra=extra)
        try:
            if inp is None:
                return getattr(service, operation)(request_id)
            return await service.run(getattr(service, operation), inp, request_id)
        except NotReady:
            raise ToolError("The models are not loaded; try again shortly.") from None
        except Exception as e:  # as HTTP's 500: the type in the log, nothing internal to the caller
            log.error("MCP %s failed with %s", tool, type(e).__name__, extra={"requestId": request_id})
            raise ToolError("The request could not be completed.") from None

    @mcp.tool(name="detect_servers", title="Detect servers and instances", description=DETECT)
    async def detect_servers(text: Text, subject: Subject = None, zendeskOrgId: ZendeskOrgId = None,
                             ticketId: TicketId = None) -> DetectResponse:
        return await call("detect_servers", "detect", _input(text, subject, zendeskOrgId, ticketId))

    @mcp.tool(name="classify_ticket", title="Classify ticket type and intent", description=CLASSIFY)
    async def classify_ticket(text: Text, subject: Subject = None, zendeskOrgId: ZendeskOrgId = None,
                              ticketId: TicketId = None) -> ClassifyResponse:
        return await call("classify_ticket", "classify", _input(text, subject, zendeskOrgId, ticketId))

    @mcp.tool(name="analyze_ticket", title="Detect servers and classify", description=ANALYZE)
    async def analyze_ticket(text: Text, subject: Subject = None, zendeskOrgId: ZendeskOrgId = None,
                             ticketId: TicketId = None) -> AnalyzeResponse:
        return await call("analyze_ticket", "analyze", _input(text, subject, zendeskOrgId, ticketId))

    @mcp.tool(name="get_catalog", title="Catalog of keys and values", description=CATALOG)
    async def get_catalog() -> CatalogResponse:
        return await call("get_catalog", "catalog")

    return mcp


def main(argv=None) -> None:
    import argparse
    import os

    from . import logs
    from .refresh_directory import bootstrap_local_snapshot
    from .settings import load_settings, local_profile

    p = argparse.ArgumentParser(description="The ticket analysis MCP server over stdio, with the local profile "
                                            "(environment variables override it).")
    p.parse_args(argv)
    os.environ.update(local_profile("none"))
    settings = load_settings()
    logs.configure(settings.log_level, "text")  # stderr; stdout carries the protocol
    bootstrap_local_snapshot(settings)
    service = Service(settings)
    if not service.load():
        raise SystemExit(f"Not ready: {service.problem}")
    build_mcp(service).run("stdio")


if __name__ == "__main__":
    main()
