"""Remote Streamable HTTP MCP server for ChatGPT Developer Mode.

Architecture:
  public :8788  →  MixedAuthMiddleware  →  reverse proxy  →  internal MCP :8789

Auth modes (MCP_AUTH_MODE env var):
  token  — only ?mcp_token= query param (current behavior)
  mixed  — Authorization: Bearer preferred, ?mcp_token= fallback
  oauth  — only Bearer token (mcp_token rejected)

OAuth paths (/.well-known/, /oauth/) are always public to enable
the OAuth authorization flow.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

EXAMPLES_DIR = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"

sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))

_spec = importlib.util.spec_from_file_location(
    "mcp_server_module", MCP_SERVER_DIR / "server.py"
)
_mcp_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mcp_mod)
mcp = _mcp_mod.mcp

MCP_INTERNAL_HOST = os.environ.get("MCP_INTERNAL_HOST", "127.0.0.1")
MCP_INTERNAL_PORT = int(os.environ.get("MCP_INTERNAL_PORT", "8789"))
MCP_INTERNAL_URL = f"http://{MCP_INTERNAL_HOST}:{MCP_INTERNAL_PORT}"

MCP_AUTH_MODE = os.environ.get("MCP_AUTH_MODE", "token").strip().lower()
MCP_PUBLIC_TOKEN = os.environ.get("MCP_PUBLIC_TOKEN", "")

OAUTH_PUBLIC_PREFIXES = ("/.well-known/", "/oauth/")


def _is_oauth_public_path(path: str) -> bool:
    """OAuth endpoints don't require mcp_token or Bearer."""
    return path.startswith(OAUTH_PUBLIC_PREFIXES)


class MixedAuthMiddleware(BaseHTTPMiddleware):
    """Accept Bearer token (header) or mcp_token (query param).

    mode=token: only mcp_token
    mode=mixed: Bearer preferred, mcp_token fallback
    mode=oauth: only Bearer (mcp_token rejected)
    """

    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path

        # OAuth public paths — always pass through
        if _is_oauth_public_path(path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        mcp_token = request.query_params.get("mcp_token", "")
        has_bearer = auth_header.startswith("Bearer ")
        has_mcp_token = bool(mcp_token)

        if MCP_AUTH_MODE == "token":
            if not has_mcp_token or mcp_token != MCP_PUBLIC_TOKEN:
                return JSONResponse(
                    {"error": "Invalid or missing mcp_token"},
                    status_code=401,
                )
            return await call_next(request)

        if MCP_AUTH_MODE == "oauth":
            if has_bearer:
                # Let FastMCP validate the Bearer token internally
                return await call_next(request)
            if has_mcp_token:
                return JSONResponse(
                    {"error": "mcp_token is not accepted in oauth mode"},
                    status_code=401,
                )
            return JSONResponse(
                {"error": "Missing Authorization: Bearer header"},
                status_code=401,
            )

        # mixed mode: Bearer preferred, mcp_token fallback
        if has_bearer:
            return await call_next(request)

        if has_mcp_token:
            if mcp_token != MCP_PUBLIC_TOKEN:
                return JSONResponse(
                    {"error": "invalid mcp_token"},
                    status_code=403,
                )
            return await call_next(request)

        return JSONResponse(
            {"error": "Missing Authorization header or mcp_token"},
            status_code=401,
        )


async def proxy_request(request: Request) -> StreamingResponse | JSONResponse:
    """Proxy an HTTP request to the internal MCP server."""
    url = f"{MCP_INTERNAL_URL}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)

    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.request(
            method=request.method,
            url=url,
            content=body,
            headers=headers,
        )
        return StreamingResponse(
            content=resp.aiter_bytes(),
            status_code=resp.status_code,
            headers=dict(resp.headers),
        )


def create_proxy_app() -> Starlette:
    """Create auth-guarded proxy to internal MCP server."""
    proxy = Starlette()
    proxy.add_middleware(MixedAuthMiddleware)
    proxy.add_route("/", proxy_request, methods=["GET", "POST", "DELETE"])
    proxy.add_route("/{path:path}", proxy_request, methods=["GET", "POST", "DELETE"])
    return proxy


proxy_app = create_proxy_app()


def run():
    """Start both internal MCP server and public proxy."""
    import threading

    internal_host = MCP_INTERNAL_HOST
    internal_port = MCP_INTERNAL_PORT
    public_host = os.environ.get("MCP_HOST", "127.0.0.1")
    public_port = int(os.environ.get("MCP_PORT", "8788"))

    mcp.settings.host = internal_host
    mcp.settings.port = internal_port

    t = threading.Thread(
        target=mcp.run,
        kwargs={"transport": "streamable-http"},
        daemon=True,
    )
    t.start()

    print(f"  MCP internal : {internal_host}:{internal_port}", file=sys.stderr)
    print(f"  MCP public   : {public_host}:{public_port}", file=sys.stderr)
    print(f"  auth mode    : {MCP_AUTH_MODE}", file=sys.stderr)
    tok_display = MCP_PUBLIC_TOKEN[:8] + "..." if MCP_PUBLIC_TOKEN else "(not set)"
    print(f"  mcp_token    : {tok_display}", file=sys.stderr)

    uvicorn.run(proxy_app, host=public_host, port=public_port)


if __name__ == "__main__":
    run()
