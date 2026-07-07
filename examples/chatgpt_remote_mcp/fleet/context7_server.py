"""Context7 MCP adapter — stdio-to-HTTP bridge for ChatGPT remote access."""

from __future__ import annotations

import asyncio
import contextlib
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
from starlette.routing import Route

from .shared import extract_auth_token, get_fleet_env

HTTP_TIMEOUT = httpx.Timeout(120.0, connect=15.0)

# ── Config ────────────────────────────────────────────────────────────
INTERNAL_PORT = 8780  # FastMCP streamable-http (no auth, localhost only)
CONTEXT7_ENV = {
    "CONTEXT7_MCP_URL": os.environ.get("CONTEXT7_MCP_URL", "https://mcp.context7.com/mcp"),
}

# ── FastMCP with tools ────────────────────────────────────────────────
mcp = FastMCP("context7-remote")

_session: ClientSession | None = None
_exit_stack: contextlib.AsyncExitStack | None = None
_lock = asyncio.Lock()


def _reset_session() -> None:
    """Drop stale session so next call reconnects."""
    global _session, _exit_stack
    _session = None
    _exit_stack = None


async def _get_session() -> ClientSession:
    global _session, _exit_stack
    if _session is not None:
        return _session
    async with _lock:
        if _session is not None:
            return _session
        _exit_stack = contextlib.AsyncExitStack()
        params = StdioServerParameters(
            command="npx",
            args=["-y", "@upstash/context7-mcp"],
            env=CONTEXT7_ENV,
        )
        streams = await _exit_stack.enter_async_context(stdio_client(params))
        _session = await _exit_stack.enter_async_context(ClientSession(*streams))
        await _session.initialize()
        return _session


async def _call_upstream(name: str, args: dict) -> str:
    """Call a Context7 tool with one reconnect retry on failure."""
    for attempt in range(2):
        session = await _get_session()
        try:
            result = await session.call_tool(name, args)
            return result.content[0].text
        except Exception:
            if attempt == 0:
                _reset_session()
                continue
            raise


@mcp.tool()
async def resolve_library_id(query: str, libraryName: str) -> Any:
    """Resolve a package/product name to a Context7-compatible library ID."""
    return await _call_upstream("resolve-library-id", {"query": query, "libraryName": libraryName})


@mcp.tool()
async def query_docs(libraryId: str, query: str) -> Any:
    """Query Context7 for documentation on a resolved library."""
    return await _call_upstream("query-docs", {"libraryId": libraryId, "query": query})


# ── Auth proxy ────────────────────────────────────────────────────
def create_auth_proxy(*, upstream_port: int, valid_tokens: set[str]) -> Starlette:
    """Return an ASGI app that proxies /mcp to the internal FastMCP
    with Bearer header or mcp_token auth."""
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{upstream_port}",
        timeout=HTTP_TIMEOUT,
    )

    async def proxy(request: Request) -> Response:
        token = extract_auth_token(request, valid_tokens)
        if not token:
            return JSONResponse({"error": "missing or invalid auth"}, 401)

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

    return Starlette(routes=[Route("/mcp", endpoint=proxy, methods=["POST"])])


# ── Main ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    env = get_fleet_env()

    # Start internal FastMCP (streamable HTTP, no auth, localhost only)
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = INTERNAL_PORT
    threading.Thread(
        target=mcp.run,
        kwargs={"transport": "streamable-http"},
        daemon=True,
    ).start()

    # External auth proxy
    app = create_auth_proxy(upstream_port=INTERNAL_PORT, valid_tokens={env["token"]})
    uvicorn.run(app, host=env["host"], port=env["port"])
