"""Context7 MCP adapter — stdio-to-HTTP bridge for ChatGPT remote access."""

from __future__ import annotations

import os
import threading
from typing import Any

import httpx
import uvicorn
from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .shared import get_fleet_env

# ── Config ────────────────────────────────────────────────────────────
INTERNAL_PORT = 8780  # FastMCP streamable-http (no auth, localhost only)
CONTEXT7_ENV = {
    "CONTEXT7_MCP_URL": os.environ.get(
        "CONTEXT7_MCP_URL", "https://mcp.context7.com/mcp"
    ),
}

# ── FastMCP with tools ────────────────────────────────────────────────
mcp = FastMCP("context7-remote")

# Reusable stdio session to Context7 subprocess
_session: ClientSession | None = None
_lock = threading.Lock()


async def _get_session() -> ClientSession:
    global _session
    if _session is not None:
        return _session
    params = StdioServerParameters(
        command="npx",
        args=["-y", "@upstash/context7-mcp"],
        env=CONTEXT7_ENV,
    )
    read, write = await stdio_client(params).__aenter__()
    _session = await ClientSession(read, write).__aenter__()
    await _session.initialize()
    return _session


@mcp.tool()
async def resolve_library_id(query: str, libraryName: str) -> Any:
    """Resolve a package/product name to a Context7-compatible library ID."""
    session = await _get_session()
    result = await session.call_tool(
        "resolve-library-id", {"query": query, "libraryName": libraryName}
    )
    return result.content[0].text


@mcp.tool()
async def query_docs(libraryId: str, query: str) -> Any:
    """Query Context7 for documentation on a resolved library."""
    session = await _get_session()
    result = await session.call_tool(
        "query-docs", {"libraryId": libraryId, "query": query}
    )
    return result.content[0].text


# ── Auth proxy ────────────────────────────────────────────────────
def create_auth_proxy(
    *, upstream_port: int, valid_tokens: set[str]
) -> Starlette:
    """Return an ASGI app that proxies /mcp to the internal FastMCP
    with mcp_token auth."""
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{upstream_port}")

    async def proxy(request: Request) -> Response:
        token = request.query_params.get("mcp_token")
        if not token:
            return JSONResponse({"error": "missing mcp_token"}, 401)
        if token not in valid_tokens:
            return JSONResponse({"error": "invalid mcp_token"}, 403)

        body = await request.body()
        headers = dict(request.headers)
        headers.pop("host", None)
        resp = await client.post(
            "/mcp",
            content=body,
            headers=headers,
            params={k: v for k, v in request.query_params.items() if k != "mcp_token"},
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
        )

    return Starlette(routes=[{"path": "/mcp", "endpoint": proxy, "methods": ["POST"]}])


# ── Main ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    env = get_fleet_env()

    # Start internal FastMCP (streamable HTTP, no auth, localhost only)
    threading.Thread(
        target=mcp.run,
        kwargs={
            "transport": "streamable-http",
            "host": "127.0.0.1",
            "port": INTERNAL_PORT,
        },
        daemon=True,
    ).start()

    # External auth proxy
    app = create_auth_proxy(
        upstream_port=INTERNAL_PORT, valid_tokens={env["token"]}
    )
    uvicorn.run(app, host=env["host"], port=env["port"])
