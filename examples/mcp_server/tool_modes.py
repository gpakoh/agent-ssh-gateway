"""Tool visibility modes for the experimental MCP server."""

from __future__ import annotations

import os
from typing import Literal, cast

ToolMode = Literal["minimal", "standard", "full"]

DEFAULT_TOOL_MODE: ToolMode = "standard"

TOOL_NAMES_BY_MODE: dict[ToolMode, set[str]] = {
    "minimal": {
        "gateway_health",
        "gateway_session_health",
        "gateway_execute_restricted",
        "gateway_job_status",
        "gateway_job_result",
    },
    "standard": {
        "gateway_health",
        "gateway_list_sessions",
        "gateway_session_health",
        "gateway_execute_restricted",
        "gateway_job_status",
        "gateway_job_result",
        "gateway_wait_job",
        "gateway_read_file",
        "gateway_repo_status",
    },
    "full": {
        "gateway_health",
        "gateway_list_sessions",
        "gateway_session_health",
        "gateway_execute_restricted",
        "gateway_job_status",
        "gateway_job_result",
        "gateway_wait_job",
        "gateway_read_file",
        "gateway_repo_status",
        "gateway_self_test",
    },
}


class ToolModeError(ValueError):
    """Raised when the MCP tool mode is invalid."""


def get_tool_mode() -> ToolMode:
    """Return configured MCP tool mode."""
    raw = os.environ.get("MCP_GATEWAY_TOOL_MODE", DEFAULT_TOOL_MODE).strip().lower()
    if raw not in TOOL_NAMES_BY_MODE:
        allowed = ", ".join(sorted(TOOL_NAMES_BY_MODE))
        raise ToolModeError(
            f"Invalid MCP_GATEWAY_TOOL_MODE={raw!r}; expected one of: {allowed}"
        )
    return cast(ToolMode, raw)


def should_register_tool(
    tool_name: str, mode: ToolMode | None = None
) -> bool:
    """Return whether a tool should be registered for the selected mode."""
    selected_mode = mode or get_tool_mode()
    if selected_mode not in TOOL_NAMES_BY_MODE:
        allowed = ", ".join(sorted(TOOL_NAMES_BY_MODE))
        raise ToolModeError(
            f"Invalid MCP_GATEWAY_TOOL_MODE={selected_mode!r}; expected one of: {allowed}"
        )
    return tool_name in TOOL_NAMES_BY_MODE[selected_mode]


def tools_for_mode(mode: ToolMode | None = None) -> set[str]:
    """Return tool names for the selected mode."""
    selected_mode = mode or get_tool_mode()
    return set(TOOL_NAMES_BY_MODE[selected_mode])
