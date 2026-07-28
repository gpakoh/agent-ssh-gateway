"""Shared utilities for fleet MCP adapters."""

from __future__ import annotations

import os
from typing import Any, TypedDict

from starlette.requests import Request


class FleetEnv(TypedDict):
    token: str
    host: str
    port: int


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


def normalize_list_response(
    value: Any,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a bare list in a stable dict for MCP tool output.
    MCP protocol expects tool results to be JSON objects (dicts), not bare arrays.
    This helper normalises list data to {"items": [...], "count": N}.
    """
    if isinstance(value, dict):
        if "items" in value:
            if "count" not in value:
                value["count"] = len(value["items"])
            if meta:
                value.update(meta)
            return value
        if meta:
            value.update(meta)
        return value
    if isinstance(value, list):
        result: dict[str, Any] = {"items": value, "count": len(value)}
        if meta:
            result.update(meta)
        return result
    return {"items": [], "count": 0, "error": "unexpected response type"}


def get_fleet_env() -> FleetEnv:
    """Read standard fleet env vars, raise if missing."""
    token = os.environ.get("MCP_PUBLIC_TOKEN", "")
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = os.environ.get("MCP_PORT", "")
    if not token:
        raise RuntimeError("MCP_PUBLIC_TOKEN is required")
    if not port:
        raise RuntimeError("MCP_PORT is required")
    return {"token": token, "host": host, "port": int(port)}
