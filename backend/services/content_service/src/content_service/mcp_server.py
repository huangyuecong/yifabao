"""MCP server: the workbench tools over the Model Context Protocol (2025-06-18).

Streamable HTTP endpoint at POST /api/v1/mcp, answering each JSON-RPC message
with a single JSON response (no server-initiated stream). It uses the same
identity dependency as the pages, so MCP clients see only the caller's data and
write tools keep their permission checks. `scripts/mcp_stdio.py` bridges stdio
clients (e.g. desktop assistants) to this endpoint.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from agent_core import Identity

from . import agent_tools
from .access_control import get_content_identity as get_identity
from .access_routes import PrivateAccessRoute

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "yifabao-workbench", "title": "易发宝工作台", "version": "0.1.0"}
INSTRUCTIONS = (
    "易发宝工作台：商品库 → 批量生成 → 人工审核 → 内容库 → 闲鱼上架 / 抖音小红书快手宣传。"
    "先用查询工具获取 ID 再操作；审核通过只能由人在工作台完成；prepare_publish 只返回发布页链接，不会直接发布。"
)

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"], route_class=PrivateAccessRoute)


def _error(message_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _result(message_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def tool_list() -> list[dict]:
    return [{
        "name": tool.name,
        "title": tool.label,
        "description": tool.description,
        "inputSchema": tool.parameters,
        "annotations": {"title": tool.label, "readOnlyHint": not tool.writes, "destructiveHint": False,
                        "idempotentHint": not tool.writes, "openWorldHint": False},
    } for tool in agent_tools.TOOLS.values()]


def handle(message: Any, identity: Identity) -> dict | None:
    """Answer one JSON-RPC message; notifications and responses get no reply."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "Invalid JSON-RPC message")
    method = message.get("method")
    if not isinstance(method, str):
        return None  # a response from the client: nothing to answer
    if "id" not in message:
        return None  # notification, e.g. notifications/initialized
    message_id = message["id"]
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(message_id, INVALID_PARAMS, "params must be an object")
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(message_id, {"protocolVersion": version,
                                    "capabilities": {"tools": {"listChanged": False}},
                                    "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, {"tools": tool_list()})
    if method == "tools/call":
        name = params.get("name")
        if name not in agent_tools.TOOLS:
            return _error(message_id, INVALID_PARAMS, f"Unknown tool: {name}")
        outcome = agent_tools.call(identity, name, params.get("arguments") or {})
        if not outcome["ok"]:
            return _result(message_id, {"content": [{"type": "text", "text": outcome["error"]}], "isError": True})
        result = outcome["result"]
        text = json.dumps(result, ensure_ascii=False, default=str)
        return _result(message_id, {"content": [{"type": "text", "text": text}],
                                    "structuredContent": json.loads(text), "isError": False})
    return _error(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")


@router.post("")
async def endpoint(request: Request, identity: Identity = Depends(get_identity)):
    try:
        message = json.loads(await request.body())
    except ValueError:
        return JSONResponse(_error(None, PARSE_ERROR, "Parse error"), status_code=400)
    if isinstance(message, list):
        return JSONResponse(_error(None, INVALID_REQUEST, "Batch requests are not supported"), status_code=400)
    reply = await run_in_threadpool(handle, message, identity)
    if reply is None:
        return Response(status_code=202)
    return JSONResponse(reply)


@router.get("")
def no_stream():
    # This server never opens a server-to-client stream.
    return Response(status_code=405, headers={"Allow": "POST"})
