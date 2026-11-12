"""Bridge a stdio MCP client to the running workbench's MCP endpoint.

MCP clients that launch local servers (desktop assistants, IDEs) speak
newline-delimited JSON-RPC over stdin/stdout. This script forwards each message
to http://127.0.0.1:8002/api/v1/mcp (override with YIFABAO_MCP_URL) and writes
the reply back, so the tools always run inside the workbench service with its
own data, permissions and background workers. Only the standard library is used.

Example client entry:
  {"mcpServers": {"yifabao": {"command": "python", "args": ["<repo>/backend/scripts/mcp_stdio.py"]}}}
"""

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.getenv("YIFABAO_MCP_URL", "http://127.0.0.1:8002/api/v1/mcp")
TIMEOUT_SECONDS = 120
# Never send local traffic through a system proxy.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def forward(line: str, opener=OPENER, url: str = URL) -> str | None:
    """Send one message; return the reply line, or None for notifications."""
    try:
        message = json.loads(line)
    except ValueError:
        return json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    request = urllib.request.Request(url, data=line.encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18"})
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        if not body.strip().startswith("{"):
            body = ""
        if not body and isinstance(message, dict) and "id" in message:
            return json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {
                "code": -32000, "message": f"易发宝工作台拒绝了请求（HTTP {error.code}）"}}, ensure_ascii=False)
    except (urllib.error.URLError, TimeoutError, OSError):
        if isinstance(message, dict) and "id" in message:
            return json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {
                "code": -32000, "message": "连不上易发宝工作台，请先用启动器启动服务"}}, ensure_ascii=False)
        return None
    body = body.strip()
    return body or None


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = forward(line.strip())
        if reply is not None:
            sys.stdout.write(reply.replace("\n", " ") + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    main()
