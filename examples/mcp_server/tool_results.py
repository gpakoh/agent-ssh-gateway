"""Structured MCP tool result helpers."""

from __future__ import annotations

from typing import Any


def text_result(
    *,
    tool: str,
    title: str,
    text: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a structured MCP-compatible tool result."""
    structured = data or {}
    return {
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
        "structuredContent": structured,
        "_meta": {
            "agent_ssh_gateway_tool": tool,
            "agent_ssh_gateway_title": title,
        },
    }


def error_result(
    *,
    tool: str,
    title: str,
    error: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a structured MCP-compatible error result."""
    structured = {
        "error": error,
        **(data or {}),
    }
    return {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": f"Error: {error}",
            }
        ],
        "structuredContent": structured,
        "_meta": {
            "agent_ssh_gateway_tool": tool,
            "agent_ssh_gateway_title": title,
        },
    }
