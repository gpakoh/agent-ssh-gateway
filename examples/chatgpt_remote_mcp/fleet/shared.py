"""Shared utilities for fleet MCP adapters."""

from __future__ import annotations

import os
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid mcp_token query parameter."""

    def __init__(self, app: Any, valid_tokens: set[str]) -> None:
        super().__init__(app)
        self._valid_tokens = valid_tokens

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        token = request.query_params.get("mcp_token")
        if not token:
            return JSONResponse(
                {"error": "missing mcp_token"}, status_code=401
            )
        if token not in self._valid_tokens:
            return JSONResponse(
                {"error": "invalid mcp_token"}, status_code=403
            )
        return await call_next(request)


def get_fleet_env() -> dict[str, str]:
    """Read standard fleet env vars, raise if missing."""
    token = os.environ.get("MCP_PUBLIC_TOKEN", "")
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = os.environ.get("MCP_PORT", "")
    if not token:
        raise RuntimeError("MCP_PUBLIC_TOKEN is required")
    if not port:
        raise RuntimeError("MCP_PORT is required")
    return {"token": token, "host": host, "port": int(port)}
