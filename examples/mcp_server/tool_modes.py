"""Tool visibility modes for the experimental MCP server."""

from __future__ import annotations

import os
from typing import Literal, cast

ToolMode = Literal["minimal", "standard", "full", "chatgpt"]

DEFAULT_TOOL_MODE: ToolMode = "standard"

TOOL_NAMES_BY_MODE: dict[ToolMode, set[str]] = {
    "minimal": {
        "health",
        "tools_manifest",
        "session_health",
        "execute_restricted",
        "job_status",
        "job_result",
    },
    "standard": {
        "health",
        "tools_manifest",
        "list_sessions",
        "session_health",
        "execute_restricted",
        "execute_argv",
        "job_status",
        "job_result",
        "wait_job",
        "read_file",
        "repo_status",
        "project_apply_patch",
    },
    "full": {
        "health",
        "tools_manifest",
        "list_sessions",
        "session_health",
        "execute_restricted",
        "execute_argv",
        "job_status",
        "job_result",
        "wait_job",
        "read_file",
        "repo_status",
        "self_test",
        "read_handoff",
        "write_handoff_plan",
        "show_handoff_status",
    },
    "chatgpt": {
        "health",
        "tools_manifest",
        "session_health",
        "job_status",
        "job_result",
        "wait_job",
        "read_file",
        "repo_status",
        "working_directory",
        "git_status",
        "recent_commits",
        "git_diff_stat",
        "show_changes",
        "run_tests",
        "run_lint",
        "run_compileall",
        "project_working_directory",
        "project_git_status",
        "project_recent_commits",
        "project_git_diff_stat",
        "project_show_changes",
        "project_run_tests",
        "project_run_lint",
        "project_run_compileall",
        "read_handoff",
        "write_handoff_plan",
        "show_handoff_status",
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
        "project_info",
        "project_read_file",
        "project_search_text",
        "project_find_files",
        "project_list_files",
        "project_tree",
        "project_list_tree",
        "project_git_diff",
        "project_git_diff_cached",
        "project_show_file_diff",
        "project_run_pytest",
        "project_run_ruff",
        "project_run_mypy",
        "project_remotes",
        "project_current_branch",
        "project_commit_head",
        "project_read_handoff",
        "project_write_handoff_plan",
        "project_show_handoff_status",
        "project_apply_patch",
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
        "docker_rm",
        "docker_compose_down",
        "docker_prune",
        "docker_confirm",
        "docker_pending_actions",
        "docker_exec",
        "docker_run",
        "docker_rmi",
        "docker_volume_rm",
        "postgres_health",
        "postgres_list_schemas",
        "postgres_list_tables",
        "postgres_describe_table",
        "postgres_select",
        "postgres_vector_status",
        "resolve_library_id",
        "query_docs",
        "project_write_agent_task",
        "project_read_agent_status",
        "project_read_agent_report",
        "project_read_agent_diff",
        "project_list_agent_tasks",
        "project_archive_agent_task",
        "project_run_opencode",
        "project_run_mimo",
        "project_run_agent",
        "workspace_file_write",
        "workspace_file_edit",
        "workspace_apply_patch",
        "workspace_preview_write",
        "workspace_preview_edit",
        "workspace_preview_patch",
        "workspace_verify",
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
