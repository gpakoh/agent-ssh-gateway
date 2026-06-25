"""Shared utilities for fleet MCP adapters."""

from __future__ import annotations

import os

from starlette.requests import Request


def extract_auth_token(request: Request, valid_tokens: set[str]) -> str | None:
    """Extract and validate auth token from Bearer header or mcp_token query param.
    Returns the token string if valid, None otherwise.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ")
        if token in valid_tokens:
            return token
        return None
    token = request.query_params.get("mcp_token", "")
    if token and token in valid_tokens:
        return token
    return None


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
