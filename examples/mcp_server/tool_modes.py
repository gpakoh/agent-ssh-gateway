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
        "gitea_get_repo",
        "gitea_list_branches",
        "gitea_list_commits",
        "gitea_get_file",
        "gitea_list_issues",
        "gitea_get_issue",
        "gitea_list_pull_requests",
        "gitea_get_pull_request",
        "gitea_list_action_runs",
        "gitea_get_action_run",
        "gitea_list_action_run_jobs",
        "gitea_list_workflows",
        "github_get_repo",
        "github_list_branches",
        "github_list_commits",
        "github_get_file",
        "github_list_issues",
        "github_get_issue",
        "github_list_pull_requests",
        "github_get_pull_request",
        "gateway_project_info",
        "gateway_project_read_file",
        "gateway_project_search_text",
        "gateway_project_find_files",
        "gateway_project_list_files",
        "gateway_project_tree",
        "gateway_project_list_tree",
        "gateway_project_git_diff",
        "gateway_project_git_diff_cached",
        "gateway_project_show_file_diff",
        "gateway_project_run_pytest",
        "gateway_project_run_ruff",
        "gateway_project_run_mypy",
        "gateway_project_remotes",
        "gateway_project_current_branch",
        "gateway_project_commit_head",
        "gateway_project_read_handoff",
        "gateway_project_write_handoff_plan",
        "gateway_project_show_handoff_status",
        "docker_ps",
        "docker_images",
        "docker_inspect",
        "docker_logs",
        "docker_stats",
        "docker_compose_ps",
        "docker_compose_services",
        "docker_start",
        "docker_stop",
        "docker_restart",
        "docker_compose_up",
        "docker_compose_restart",
        "docker_compose_build",
        "docker_compose_logs",
        "postgres_health",
        "postgres_list_schemas",
        "postgres_list_tables",
        "postgres_describe_table",
        "postgres_select",
        "postgres_vector_status",
        "resolve_library_id",
        "query_docs",
        "gateway_project_write_agent_task",
        "gateway_project_read_agent_status",
        "gateway_project_read_agent_report",
        "gateway_project_read_agent_diff",
        "gateway_project_list_agent_tasks",
        "gateway_project_archive_agent_task",
        "project_run_opencode",
        "gateway_project_run_mimo",
        "gateway_project_run_agent",
    },
}


class ToolModeError(ValueError):
    """Raised when the MCP tool mode is invalid."""


def get_tool_mode() -> ToolMode:
    """Return configured MCP tool mode."""
    raw = os.environ.get("MCP_GATEWAY_TOOL_MODE", DEFAULT_TOOL_MODE).strip().lower()
    if raw not in TOOL_NAMES_BY_MODE:
        allowed = ", ".join(sorted(TOOL_NAMES_BY_MODE))
        raise ToolModeError(f"Invalid MCP_GATEWAY_TOOL_MODE={raw!r}; expected one of: {allowed}")
    return cast(ToolMode, raw)


def should_register_tool(tool_name: str, mode: ToolMode | None = None) -> bool:
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
