"""Tool-level scope enforcement for MCP Gateway.

ACCESS_PROFILES — named capability bundles for different operating modes.
TOOL_SCOPES — mapping from tool name to list of required scopes (fail-closed).
"""

from __future__ import annotations

ACCESS_PROFILES: dict[str, list[str]] = {
    "viewer": [
        "mcp:read",
        "mcp:repo",
        "mcp:docs",
    ],
    "operator": [
        "mcp:read",
        "mcp:project",
        "mcp:handoff",
        "mcp:repo",
        "mcp:docs",
    ],
    "agent-runner": [
        "mcp:read",
        "mcp:project",
        "mcp:handoff",
        "mcp:agent-run",
        "mcp:repo",
        "mcp:docs",
    ],
    "infra": [
        "mcp:read",
        "mcp:docker",
        "mcp:docker:admin",
        "mcp:postgres",
        "mcp:repo",
    ],
    "full": [
        "mcp:read",
        "mcp:project",
        "mcp:handoff",
        "mcp:agent-run",
        "mcp:execute",
        "mcp:repo",
        "mcp:docker",
        "mcp:docker:admin",
        "mcp:postgres",
        "mcp:docs",
        "mcp:admin",
    ],
}

TOOL_SCOPES: dict[str, list[str]] = {
    # ops — mcp:read / mcp:execute
    "gateway_health": ["mcp:read"],
    "gateway_tools_manifest": ["mcp:read"],
    "gateway_list_sessions": ["mcp:read"],
    "gateway_session_health": ["mcp:read"],
    "gateway_execute_restricted": ["mcp:execute"],
    "gateway_job_status": ["mcp:read"],
    "gateway_job_result": ["mcp:read"],
    "gateway_wait_job": ["mcp:read"],
    "gateway_read_file": ["mcp:read", "mcp:project"],
    "gateway_repo_status": ["mcp:read"],
    "gateway_working_directory": ["mcp:read", "mcp:project"],
    "gateway_git_status": ["mcp:read", "mcp:project"],
    "gateway_recent_commits": ["mcp:read", "mcp:project"],
    "gateway_git_diff_stat": ["mcp:read", "mcp:project"],
    "gateway_show_changes": ["mcp:read", "mcp:project"],
    "gateway_run_tests": ["mcp:project"],
    "gateway_run_lint": ["mcp:project"],
    "gateway_run_compileall": ["mcp:project"],
    "gateway_self_test": ["mcp:read"],
    # project — mcp:project
    "gateway_project_working_directory": ["mcp:project"],
    "gateway_project_info": ["mcp:read", "mcp:project"],
    "gateway_project_git_status": ["mcp:project"],
    "gateway_project_recent_commits": ["mcp:project"],
    "gateway_project_git_diff_stat": ["mcp:project"],
    "gateway_project_show_changes": ["mcp:project"],
    "gateway_project_run_tests": ["mcp:project"],
    "gateway_project_run_lint": ["mcp:project"],
    "gateway_project_run_compileall": ["mcp:project"],
    "gateway_project_read_file": ["mcp:project"],
    "gateway_project_search_text": ["mcp:project"],
    "gateway_project_find_files": ["mcp:project"],
    "gateway_project_list_files": ["mcp:read", "mcp:project"],
    "gateway_project_tree": ["mcp:project"],
    "gateway_project_list_tree": ["mcp:read", "mcp:project"],
    "gateway_project_git_diff": ["mcp:project"],
    "gateway_project_git_diff_cached": ["mcp:project"],
    "gateway_project_show_file_diff": ["mcp:project"],
    "gateway_project_run_pytest": ["mcp:project"],
    "gateway_project_run_ruff": ["mcp:project"],
    "gateway_project_run_mypy": ["mcp:project"],
    "gateway_project_remotes": ["mcp:project"],
    "gateway_project_current_branch": ["mcp:project"],
    "gateway_project_commit_head": ["mcp:project"],
    # handoff — mcp:handoff
    "gateway_read_handoff": ["mcp:handoff"],
    "gateway_show_handoff_status": ["mcp:handoff"],
    "gateway_write_handoff_plan": ["mcp:handoff"],
    "gateway_project_read_handoff": ["mcp:handoff"],
    "gateway_project_write_handoff_plan": ["mcp:handoff"],
    "gateway_project_show_handoff_status": ["mcp:handoff"],
    "gateway_project_write_agent_task": ["mcp:handoff"],
    "gateway_project_read_agent_status": ["mcp:handoff"],
    "gateway_project_read_agent_report": ["mcp:handoff"],
    "gateway_project_read_agent_diff": ["mcp:handoff"],
    "gateway_project_list_agent_tasks": ["mcp:handoff"],
    "gateway_project_archive_agent_task": ["mcp:handoff"],
    # agent-run — mcp:agent-run
    "project_run_opencode": ["mcp:agent-run"],
    "gateway_project_run_mimo": ["mcp:agent-run"],
    "gateway_project_run_agent": ["mcp:agent-run"],
    # repo — mcp:repo
    "gitea_get_repo": ["mcp:repo"],
    "gitea_list_branches": ["mcp:repo"],
    "gitea_list_commits": ["mcp:repo"],
    "gitea_get_file": ["mcp:repo"],
    "gitea_list_issues": ["mcp:repo"],
    "gitea_get_issue": ["mcp:repo"],
    "gitea_list_pull_requests": ["mcp:repo"],
    "gitea_get_pull_request": ["mcp:repo"],
    "gitea_list_action_runs": ["mcp:repo"],
    "gitea_get_action_run": ["mcp:repo"],
    "gitea_list_action_run_jobs": ["mcp:repo"],
    "gitea_list_workflows": ["mcp:repo"],
    "github_get_repo": ["mcp:repo"],
    "github_list_branches": ["mcp:repo"],
    "github_list_commits": ["mcp:repo"],
    "github_get_file": ["mcp:repo"],
    "github_list_issues": ["mcp:repo"],
    "github_get_issue": ["mcp:repo"],
    "github_list_pull_requests": ["mcp:repo"],
    "github_get_pull_request": ["mcp:repo"],
    # docker — mcp:docker
    "docker_ps": ["mcp:docker"],
    "docker_images": ["mcp:docker"],
    "docker_inspect": ["mcp:docker"],
    "docker_logs": ["mcp:docker"],
    "docker_stats": ["mcp:docker"],
    "docker_compose_ps": ["mcp:docker"],
    "docker_compose_services": ["mcp:docker"],
    # docker write operations (Session 160) — mcp:docker
    "docker_start": ["mcp:docker"],
    "docker_stop": ["mcp:docker"],
    "docker_restart": ["mcp:docker"],
    "docker_compose_up": ["mcp:docker"],
    "docker_compose_restart": ["mcp:docker"],
    "docker_compose_build": ["mcp:docker"],
    "docker_compose_logs": ["mcp:docker"],
    # dangerous docker operations (Session 164) — mcp:docker
    "docker_rm": ["mcp:docker"],
    "docker_compose_down": ["mcp:docker"],
    "docker_prune": ["mcp:docker"],
    "docker_confirm": ["mcp:docker"],
    "docker_pending_actions": ["mcp:docker"],
    # docker admin operations (Session 165) — mcp:docker:admin
    "docker_exec": ["mcp:docker:admin"],
    "docker_run": ["mcp:docker:admin"],
    "docker_rmi": ["mcp:docker:admin"],
    "docker_volume_rm": ["mcp:docker:admin"],
    # postgres — mcp:postgres
    "postgres_health": ["mcp:postgres"],
    "postgres_list_schemas": ["mcp:postgres"],
    "postgres_list_tables": ["mcp:postgres"],
    "postgres_describe_table": ["mcp:postgres"],
    "postgres_select": ["mcp:postgres"],
    "postgres_vector_status": ["mcp:postgres"],
    # docs — mcp:docs
    "resolve_library_id": ["mcp:docs"],
    "query_docs": ["mcp:docs"],
}

FLEET_ROUTE_SCOPES: dict[str, list[str]] = {
    "/mcp/gitea": ["mcp:repo"],
    "/mcp/github": ["mcp:repo"],
    "/mcp/docker": ["mcp:docker"],
    "/mcp/postgres": ["mcp:postgres"],
    "/mcp/context7": ["mcp:docs"],
}

FAIL_CLOSED_SCOPE = "mcp:admin"


def get_required_scopes(tool_name: str) -> list[str]:
    """Return required scopes for a tool.

    Unknown tools get [FAIL_CLOSED_SCOPE] (= only full profile).
    """
    return TOOL_SCOPES.get(tool_name, [FAIL_CLOSED_SCOPE])


def get_profile_scopes(profile: str) -> list[str]:
    """Return scopes for a named access profile."""
    return ACCESS_PROFILES.get(profile, list(ACCESS_PROFILES.get("operator", [])))


def has_required_scope(token_scopes: list[str], tool_name: str) -> bool:
    """Check if token scopes satisfy tool's requirements."""
    required = get_required_scopes(tool_name)
    return any(s in token_scopes for s in required)


def check_fleet_route(path: str, token_scopes: list[str]) -> tuple[bool, str | None]:
    """Check if token scopes allow access to a fleet route.

    Returns (allowed, matched_scope_or_None).
    """
    for route_prefix, required_scopes in FLEET_ROUTE_SCOPES.items():
        if path.startswith(route_prefix):
            allowed = any(s in token_scopes for s in required_scopes)
            return allowed, required_scopes[0] if not allowed else None
    return True, None


def extract_tool_from_body(body: bytes) -> str | None:
    """Extract tool name from a JSON-RPC tools/call request body.

    Returns None if not a tools/call request (e.g. initialize, tools/list).
    """
    import json

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    method = data.get("method")
    if method != "tools/call":
        return None
    params = data.get("params")
    if not isinstance(params, dict):
        return None
    return params.get("name")
