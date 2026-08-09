#!/usr/bin/env python3
"""Docker HEALTHCHECK for the mcp-oauth Compose service.

examples/mcp_client_remote/server.py (the script this container runs)
has no /healthz REST route: unlike scripts/mcp_streamable_http_serve.py's
BearerAuthMiddleware (which special-cases an exempt path with a
synthetic 200 response), this entrypoint's outer proxy authenticates
everything -- including any would-be /healthz -- through the real
GatewayOAuthProvider, and its internal FastMCP instance never registers
such a route at all. A curl to /healthz here is expected to 404, not a
bug to route around.

MCP_HEALTHCHECK_BEARER_TOKEN (examples/mcp_server/server.py) exists for
exactly this: a pre-registered access token, scoped to mcp:read only,
whose sole purpose is authenticating a liveness probe. This script uses
it to do a real MCP handshake (initialize + tools/list) against the
container's own public port, mirroring scripts/mcp_fleet_healthcheck.py's
check_mcp_endpoint() -- trimmed to a single adapter, single shot, with a
plain exit code instead of a printed report, since this runs as Docker's
HEALTHCHECK CMD every interval.

Exit code:
    0 - initialize + tools/list succeeded, at least one tool returned
    1 - missing token, transport error, JSON-RPC error, or no tools
"""

from __future__ import annotations

import http.client
import json
import os
import sys

TIMEOUT = float(os.environ.get("MCP_HEALTHCHECK_TIMEOUT", "4"))


def _mcp_request(port: int, body: dict, token: str, sid: str | None = None) -> tuple[dict, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
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
    token = os.environ.get("MCP_HEALTHCHECK_BEARER_TOKEN", "").strip()
    if not token:
        print("mcp_oauth_healthcheck: MCP_HEALTHCHECK_BEARER_TOKEN not set", file=sys.stderr)
        return 1

    port = int(os.environ.get("MCP_PORT", "8788"))

    try:
        result, sid = _mcp_request(
            port,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "docker-healthcheck", "version": "1.0"},
                },
            },
            token,
        )
        if not sid:
            print("mcp_oauth_healthcheck: no session ID in initialize response", file=sys.stderr)
            return 1
        if "error" in result:
            print(f"mcp_oauth_healthcheck: initialize error: {result['error']}", file=sys.stderr)
            return 1

        result2, _ = _mcp_request(
            port,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            token,
            sid=sid,
        )
        if "error" in result2:
            print(f"mcp_oauth_healthcheck: tools/list error: {result2['error']}", file=sys.stderr)
            return 1

        tools = result2.get("result", {}).get("tools", [])
        if not tools:
            print("mcp_oauth_healthcheck: tools/list returned no tools", file=sys.stderr)
            return 1

        return 0
    except Exception as exc:
        print(f"mcp_oauth_healthcheck: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
