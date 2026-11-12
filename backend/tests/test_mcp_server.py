"""MCP server over Streamable HTTP, plus the stdio bridge."""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_catalog import ALICE, catalog, create_product  # noqa: F401

from content_service import agent_tools, mcp_server

URL = "/api/v1/mcp"


@pytest.fixture
def mcp(catalog):  # noqa: F811
    client, _, _ = catalog
    app = FastAPI()
    app.include_router(mcp_server.router)
    app.dependency_overrides[mcp_server.get_identity] = lambda: ALICE
    with TestClient(app) as rpc:
        yield rpc, client


def call(rpc, method, params=None, message_id=1):
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    response = rpc.post(URL, json=message)
    assert response.status_code == 200, response.text
    return response.json()


def test_handshake_and_tool_list(mcp):
    rpc, _ = mcp
    hello = call(rpc, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "1"}})
    assert hello["result"]["protocolVersion"] == "2025-06-18"
    assert hello["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert hello["result"]["serverInfo"]["name"] == "yifabao-workbench"
    older = call(rpc, "initialize", {"protocolVersion": "2099-01-01"})
    assert older["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]
    assert rpc.post(URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert call(rpc, "ping")["result"] == {}

    tools = {tool["name"]: tool for tool in call(rpc, "tools/list")["result"]["tools"]}
    assert set(tools) == set(agent_tools.TOOLS)
    assert tools["search_products"]["inputSchema"]["type"] == "object"
    assert tools["search_products"]["annotations"]["readOnlyHint"] is True
    assert tools["start_generation"]["annotations"]["readOnlyHint"] is False


def test_tools_call_returns_text_and_structured_content(mcp):
    rpc, client = mcp
    product = create_product(client, "雅集酒（遇雅集）")
    reply = call(rpc, "tools/call", {"name": "search_products", "arguments": {"keyword": "雅集"}})["result"]
    assert reply["isError"] is False
    assert reply["structuredContent"]["products"][0]["id"] == product["id"]
    assert json.loads(reply["content"][0]["text"]) == reply["structuredContent"]

    started = call(rpc, "tools/call", {"name": "start_generation", "arguments": {"product_ids": [product["id"]]}})
    assert started["result"]["structuredContent"]["queued"] == 1

    refused = call(rpc, "tools/call", {"name": "prepare_publish", "arguments": {"workflow": "nope", "content_ids": []}})
    assert refused["result"]["isError"] is True and "workflow" in refused["result"]["content"][0]["text"]


def test_protocol_errors(mcp):
    rpc, _ = mcp
    assert call(rpc, "tools/call", {"name": "missing"})["error"]["code"] == mcp_server.INVALID_PARAMS
    assert call(rpc, "resources/list")["error"]["code"] == mcp_server.METHOD_NOT_FOUND
    bad = rpc.post(URL, content=b"{not json", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == mcp_server.PARSE_ERROR
    batch = rpc.post(URL, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert batch.status_code == 400 and batch.json()["error"]["code"] == mcp_server.INVALID_REQUEST
    assert rpc.post(URL, json={"id": 1, "method": "ping"}).json()["error"]["code"] == mcp_server.INVALID_REQUEST
    assert rpc.get(URL).status_code == 405


def load_bridge():
    path = Path(__file__).resolve().parents[1] / "scripts" / "mcp_stdio.py"
    spec = importlib.util.spec_from_file_location("mcp_stdio_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeOpener:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.sent = reply, error, []

    def open(self, request, timeout):
        self.sent.append((request.full_url, json.loads(request.data), dict(request.header_items())))
        if self.error:
            raise self.error
        return io.BytesIO(self.reply)


def test_stdio_bridge_forwards_messages_and_reports_a_stopped_service():
    bridge = load_bridge()
    opener = FakeOpener(b'{"jsonrpc":"2.0","id":7,"result":{}}')
    assert bridge.forward('{"jsonrpc":"2.0","id":7,"method":"ping"}', opener, "http://local/mcp") == \
        '{"jsonrpc":"2.0","id":7,"result":{}}'
    url, body, headers = opener.sent[0]
    assert url == "http://local/mcp" and body["method"] == "ping" and headers["Content-type"] == "application/json"

    accepted = FakeOpener(b"")
    assert bridge.forward('{"jsonrpc":"2.0","method":"notifications/initialized"}', accepted, "http://x") is None

    down = FakeOpener(error=urllib.error.URLError("refused"))
    reply = json.loads(bridge.forward('{"jsonrpc":"2.0","id":3,"method":"tools/list"}', down, "http://x"))
    assert reply["id"] == 3 and reply["error"]["code"] == -32000 and "启动" in reply["error"]["message"]
    assert json.loads(bridge.forward("not json", down, "http://x"))["error"]["code"] == -32700
