"""Tool visibility modes for the experimental MCP server."""

from __future__ import annotations

import os
from typing import Literal, cast

ToolMode = Literal["minimal", "standard", "full", "chatgpt"]

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
        "gateway_read_handoff",
        "gateway_write_handoff_plan",
        "gateway_show_handoff_status",
    },
    "chatgpt": {
        "gateway_health",
        "gateway_session_health",
        "gateway_job_status",
        "gateway_job_result",
        "gateway_wait_job",
        "gateway_read_file",
        "gateway_repo_status",
        "gateway_working_directory",
        "gateway_git_status",
        "gateway_recent_commits",
        "gateway_git_diff_stat",
        "gateway_show_changes",
        "gateway_run_tests",
        "gateway_run_lint",
        "gateway_run_compileall",
        "gateway_project_working_directory",
        "gateway_project_git_status",
        "gateway_project_recent_commits",
        "gateway_project_git_diff_stat",
        "gateway_project_show_changes",
        "gateway_project_run_tests",
        "gateway_project_run_lint",
        "gateway_project_run_compileall",
        "gateway_read_handoff",
        "gateway_write_handoff_plan",
        "gateway_show_handoff_status",
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
