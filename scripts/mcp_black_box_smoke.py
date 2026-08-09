#!/usr/bin/env python3
"""Authenticated black-box smoke check for the deployed mcp-server
container (bearer-token mode), run via `docker exec mcp-server python3`
from deploy-from-registry.sh after a real deploy.

P1 BLOCKER audit finding: the only post-deploy check was
wait_docker_health() (docker inspect's own HEALTHCHECK status), and
mcp-server's own Dockerfile HEALTHCHECK hits /healthz -- deliberately
exempt from bearer auth (see scripts/mcp_sse_serve.py's
BearerAuthMiddleware), so it never exercised the real MCP protocol or
the auth boundary at all. This does a real MCP initialize + tools/list
handshake authenticated with MCP_STREAMABLE_HTTP_BEARER_TOKEN, mirroring
scripts/mcp_oauth_healthcheck.py's approach for the OAuth-mode service.

Exit code:
    0 - initialize + tools/list succeeded, at least one tool returned
    1 - missing token, transport error, JSON-RPC error, or no tools
"""

from __future__ import annotations

import http.client
import json
import os
import sys

TIMEOUT = float(os.environ.get("MCP_SMOKE_TIMEOUT", "10"))


def _mcp_request(body: dict, token: str, sid: str | None = None) -> tuple[dict, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid

    conn = http.client.HTTPConnection("127.0.0.1", 8087, timeout=TIMEOUT)
    try:
        conn.request("POST", "/mcp", json.dumps(body), headers)
        resp = conn.getresponse()
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n\n" in buf:
                break
        raw = buf.decode("utf-8", errors="replace")
        result: dict = {}
        for line in raw.split("\n"):
            if line.startswith("data:"):
                result = json.loads(line[5:])
                break
        ret_sid = resp.getheader("mcp-session-id", "")
        resp.close()
        return result, ret_sid
    finally:
        conn.close()


def main() -> int:
    token = os.environ.get("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "").strip()
    if not token:
        print("mcp_black_box_smoke: MCP_STREAMABLE_HTTP_BEARER_TOKEN not set", file=sys.stderr)
        return 1

    try:
        result, sid = _mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "black-box-smoke", "version": "1.0"},
                },
            },
            token,
        )
        if not sid:
            print("mcp_black_box_smoke: no session ID in initialize response", file=sys.stderr)
            return 1
        if "error" in result:
            print(f"mcp_black_box_smoke: initialize error: {result['error']}", file=sys.stderr)
            return 1

        result2, _ = _mcp_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            token,
            sid=sid,
        )
        if "error" in result2:
            print(f"mcp_black_box_smoke: tools/list error: {result2['error']}", file=sys.stderr)
            return 1

        tools = result2.get("result", {}).get("tools", [])
        if not tools:
            print("mcp_black_box_smoke: tools/list returned no tools", file=sys.stderr)
            return 1

        return 0
    except Exception as exc:
        print(f"mcp_black_box_smoke: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
