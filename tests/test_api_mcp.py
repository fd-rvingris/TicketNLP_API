"""The MCP tools, in-process with the SDK's client and over the /mcp endpoint; each returns what its
HTTP endpoint returns."""

import asyncio

import pytest
from mcp import Client

from api.mcp_server import build_mcp
from tests.api_support import ORG, TICKET, make_client

TOOLS = {"detect_servers": "/v1/detect", "classify_ticket": "/v1/classify", "analyze_ticket": "/v1/analyze",
         "get_catalog": "/v1/catalog"}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def without_request_id(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "requestId"}


def http(client, tool: str, args: dict) -> dict:
    path = TOOLS[tool]
    r = client.get(path) if tool == "get_catalog" else client.post(path, json=args)
    assert r.status_code == 200
    return r.json()


def test_tools_are_listed_with_descriptions_and_the_http_inputs(client):
    async def run():
        async with Client(build_mcp(client.app.state.service)) as mcp:
            return (await mcp.list_tools()).tools
    tools = {t.name: t for t in asyncio.run(run())}
    assert set(tools) == set(TOOLS)
    for name, tool in tools.items():
        assert len(tool.description) > 100 and tool.output_schema
    schema = tools["detect_servers"].input_schema
    assert schema["required"] == ["text"] and set(schema["properties"]) == {"text", "subject", "zendeskOrgId", "ticketId"}
    assert tools["get_catalog"].input_schema.get("properties", {}) == {}


def test_each_tool_returns_what_its_endpoint_returns(client):
    args = {**TICKET, "ticketId": "T-9"}

    async def run():
        out = {}
        async with Client(build_mcp(client.app.state.service)) as mcp:
            for tool in TOOLS:
                result = await mcp.call_tool(tool, {} if tool == "get_catalog" else args)
                assert not result.is_error, result.content
                out[tool] = result.structured_content
        return out
    results = asyncio.run(run())
    for tool, body in results.items():
        assert body["requestId"] and body["apiVersion"]
        assert without_request_id(body) == without_request_id(http(client, tool, args)), tool


def test_tool_inputs_are_validated_like_the_http_body(client):
    async def run():
        async with Client(build_mcp(client.app.state.service)) as mcp:
            return [await mcp.call_tool("detect_servers", a) for a in
                    ({"subject": "no text"}, {"text": ""}, {"text": "x", "zendeskOrgId": "Fake Co"})]
    for result in asyncio.run(run()):
        assert result.is_error


def test_tools_over_the_mcp_endpoint(client):
    r = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 200 and {t["name"] for t in r.json()["result"]["tools"]} == set(TOOLS)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "detect_servers", "arguments": {"text": TICKET["text"], "zendeskOrgId": int(ORG)}}}
    r = client.post("/mcp", headers=HEADERS, json=call)
    assert r.status_code == 200 and r.headers["x-request-id"]
    body = r.json()["result"]["structuredContent"]
    expected = http(client, "detect_servers", {"text": TICKET["text"], "zendeskOrgId": int(ORG)})
    assert without_request_id(body) == without_request_id(expected)


def test_tool_results_never_name_another_clients_servers(client):
    """othsql* hosts belong to the other made-up client; asking as either client never returns them unasked."""
    async def run():
        async with Client(build_mcp(client.app.state.service)) as mcp:
            return [await mcp.call_tool(tool, {"text": "The REPORTS instance is slow", "zendeskOrgId": org})
                    for tool in ("detect_servers", "analyze_ticket") for org in (ORG, "999999")]
    for result in asyncio.run(run()):
        assert "othsql" not in str(result.structured_content)
    catalog = asyncio.run(_catalog(client))
    assert "fakesql" not in str(catalog) and "othsql" not in str(catalog)


async def _catalog(client):
    async with Client(build_mcp(client.app.state.service)) as mcp:
        return (await mcp.call_tool("get_catalog", {})).structured_content


def test_a_call_over_the_mcp_endpoint_reports_the_http_request_id(client):
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_catalog", "arguments": {}}}
    r = client.post("/mcp", headers={**HEADERS, "X-Request-Id": "mcp-req-7"}, json=call)
    assert r.headers["x-request-id"] == "mcp-req-7"
    assert r.json()["result"]["structuredContent"]["requestId"] == "mcp-req-7"


def test_an_unexpected_tool_error_reveals_nothing(client, monkeypatch):
    service = client.app.state.service

    def broken(inp, request_id):
        raise ValueError("internal detail FAKESQL01")
    monkeypatch.setattr(service, "detect", broken)

    async def run():
        async with Client(build_mcp(service)) as mcp:
            return await mcp.call_tool("detect_servers", {"text": "FAKESQL01 is down"})
    result = asyncio.run(run())
    assert result.is_error and "FAKESQL01" not in str(result.content) and "could not be completed" in str(result.content)
