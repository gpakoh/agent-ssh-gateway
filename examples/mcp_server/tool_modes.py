"""Tool visibility modes for the experimental MCP server."""

from __future__ import annotations

import os
from typing import Literal, cast

ToolMode = Literal["minimal", "standard", "full", "mcp_client", "mcp_client_write"]

DEFAULT_TOOL_MODE: ToolMode = "standard"

TOOL_NAMES_BY_MODE: dict[ToolMode, set[str]] = {
    "minimal": {
        "health",
        "tools_manifest",
        "project_list",
        "scan_command",
        "session_health",
        "execute_restricted",
        "job_status",
        "job_result",
    },
    "standard": {
        "health",
        "tools_manifest",
        "project_list",
        "scan_command",
        "list_sessions",
        "session_health",
        "execute_restricted",
        "execute_argv",
        "job_status",
        "job_result",
        "wait_job",
        "read_file",
        "repo_status",
        "apply_patch",
        "workspace_file_write",
        "workspace_file_edit",
        "workspace_apply_patch",
        "workspace_preview_write",
        "workspace_preview_edit",
        "workspace_preview_patch",
        "workspace_verify",
    },
    "full": {
        "health",
        "tools_manifest",
        "project_list",
        "scan_command",
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
        "workspace_file_write",
        "workspace_file_edit",
        "workspace_apply_patch",
        "workspace_preview_write",
        "workspace_preview_edit",
        "workspace_preview_patch",
        "workspace_verify",
    },
    "mcp_client": {
        "project_list",
        "scan_command",
        "health",
        "tools_manifest",
        "session_health",
        "job_status",
        "job_result",
        "wait_job",
        "repo_status",
        "working_directory",
        "git_status",
        "recent_commits",
        "git_diff_stat",
        "show_changes",
        "run_tests",
        "run_lint",
        "run_compileall",
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
        "info",
        "read_file",
        "search_text",
        "find_files",
        "list_files",
        "tree",
        "list_tree",
        "git_diff",
        "git_diff_cached",
        "show_file_diff",
        "run_pytest",
        "run_ruff",
        "run_mypy",
        "remotes",
        "current_branch",
        "commit_head",
        "read_handoff",
        "write_handoff_plan",
        "show_handoff_status",
        "apply_patch",
        "docker_ps",
        "docker_images",
        "docker_inspect",
        "docker_logs",
        "docker_stats",
        "docker_compose_ps",
        "docker_compose_services",
        # "docker_start" deliberately absent: it has an impl function
        # (_docker_start_impl) and a _CONFIRM_HANDLERS entry in server.py,
        # but unlike every other docker_* action here, no @register_tool()
        # ever wraps it to actually create the pending confirmation --
        # there is no way to reach it through MCP at all. Listing it here
        # made both should_register_tool("docker_start", "mcp_client") and
        # the tools_manifest "modes" section lie about its availability.
        "docker_stop",
        "docker_restart",
        "docker_compose_up",
        "docker_compose_restart",
        "docker_compose_build",
        "docker_compose_logs",
        "docker_rm",
        "docker_compose_down",
        "docker_prune",
        "confirm_operation",
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
        "write_agent_task",
        "read_agent_status",
        "read_agent_report",
        "read_agent_diff",
        "read_agent_log",
        "list_agent_tasks",
        "archive_agent_task",
        "run_opencode",
        "run_agent",
        "run_agents",
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


# Tools that are NEVER exposed to ChatGPT in safe mode.
# These allow mutation, agent launch, or privileged operations.
MCP_CLIENT_BLOCKED_TOOLS: frozenset[str] = frozenset({
    # Agent launch — never safe for first attach
    "run_opencode",
    "run_agent",
    "run_agents",
    # Write/patch mutations
    "apply_patch",
    "workspace_file_write",
    "workspace_file_edit",
    "workspace_apply_patch",
    "workspace_preview_write",
    "workspace_preview_edit",
    "workspace_preview_patch",
    "workspace_verify",
    # Handoff write (mutates plan files)
    "write_handoff_plan",
    # Docker write/admin — dangerous
    "docker_start",
    "docker_stop",
    "docker_restart",
    "docker_compose_up",
    "docker_compose_restart",
    "docker_compose_build",
    "docker_rm",
    "docker_compose_down",
    "docker_prune",
    "confirm_operation",
    "docker_pending_actions",
    "docker_exec",
    "docker_run",
    "docker_rmi",
    "docker_volume_rm",
    # Agent task write
    "write_agent_task",
    "archive_agent_task",
})


# Tools still blocked in "mcp_client_write" mode.
# Deliberately EMPTY: the user granted the ChatGPT-connected client FULL
# rights -- project edits, git add/commit/push, agent launches, and
# infrastructure control (Docker/Postgres) alike. Nothing is filtered out
# of the mcp_client tool set for this mode anymore; keep the set empty as
# the explicit, auditable record of that decision (an empty frozenset is
# the "everything allowed" state, and is asserted by tests below).
MCP_CLIENT_WRITE_BLOCKED_TOOLS: frozenset[str] = frozenset()

# "mcp_client_write" starts from the same broad "mcp_client" tool set
# (project inspection, read-only git, gitea/github, tests/lint, workspace
# write/patch, docker/agent-launch/handoff-write) and removes only
# MCP_CLIENT_WRITE_BLOCKED_TOOLS, then adds the explicit git mutation tools
# (git_add/git_commit/git_create_branch/git_push; never present in any other
# mode's list).
TOOL_NAMES_BY_MODE["mcp_client_write"] = (
    TOOL_NAMES_BY_MODE["mcp_client"] - MCP_CLIENT_WRITE_BLOCKED_TOOLS
) | {
    "execute_argv",
    "git_add",
    "git_commit",
    "git_create_branch",
    "git_push",
    "gitea_create_pull_request",
    "gitea_merge_pull_request",
    "gitea_push_local_ref",
    # Supervisor-only integration tools are intentionally absent from the
    # broad mcp_client set and therefore from safe mode. They exist only in
    # the explicit write/admin mode and still require mcp:admin at runtime.
    "supervisor_integrate_file",
    "supervisor_recover_integrations",
    "supervisor_register_project",
}


def is_mcp_client_safe_mode() -> bool:
    """Return True when MCP_CLIENT_SAFE_MODE is enabled."""
    return os.environ.get("MCP_CLIENT_SAFE_MODE", "false").strip().lower() in {"1", "true", "yes"}


def get_mcp_client_safe_tools() -> frozenset[str]:
    """Return the set of tools allowed in ChatGPT safe mode.

    Starts from the full mcp_client mode set, removes blocked tools.
    """
    return frozenset(TOOL_NAMES_BY_MODE["mcp_client"] - MCP_CLIENT_BLOCKED_TOOLS)


def get_mcp_client_write_tools() -> frozenset[str]:
    """Return the set of tools allowed in "mcp_client_write" mode."""
    return frozenset(TOOL_NAMES_BY_MODE["mcp_client_write"])


def get_tool_mode() -> ToolMode:
    """Return configured MCP tool mode."""
    raw = os.environ.get("MCP_GATEWAY_TOOL_MODE", DEFAULT_TOOL_MODE).strip().lower()
    if raw not in TOOL_NAMES_BY_MODE:
        allowed = ", ".join(sorted(TOOL_NAMES_BY_MODE))
        raise ToolModeError(f"Invalid MCP_GATEWAY_TOOL_MODE={raw!r}; expected one of: {allowed}")
    return cast(ToolMode, raw)  # type: ignore[redundant-cast]


def should_register_tool(tool_name: str, mode: ToolMode | None = None) -> bool:
    """Return whether a tool should be registered for the selected mode.

    When MCP_CLIENT_SAFE_MODE=true and mode is mcp_client, only safe tools are registered.
    """
    selected_mode = mode or get_tool_mode()
    if selected_mode not in TOOL_NAMES_BY_MODE:
        allowed = ", ".join(sorted(TOOL_NAMES_BY_MODE))
        raise ToolModeError(
            f"Invalid MCP_GATEWAY_TOOL_MODE={selected_mode!r}; expected one of: {allowed}"
        )
    if selected_mode == "mcp_client" and is_mcp_client_safe_mode():
        return tool_name in get_mcp_client_safe_tools()
    return tool_name in TOOL_NAMES_BY_MODE[selected_mode]


def tools_for_mode(mode: ToolMode | None = None) -> frozenset[str]:
    """Return tool names for the selected mode."""
    selected_mode = mode or get_tool_mode()
    return frozenset(TOOL_NAMES_BY_MODE[selected_mode])
