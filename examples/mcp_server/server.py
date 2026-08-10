"""Experimental MCP server for agent-ssh-gateway.

This server is intentionally kept outside the gateway core.
"""
# ruff: noqa: E402 — late imports intentional for --reload compat

from __future__ import annotations

import os
import shutil
import sys
import time as _time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

_mcp_started_at = _time.time()

from docker_confirm import ConfirmStore
from gateway_client import (
    GatewayClient,
    GatewayClientError,  # noqa: F401 (facade: tests raise this class by server-module identity)
)
from mcp.server.fastmcp import FastMCP
from mcp_client_tools import (
    read_file,  # noqa: F401 (facade: tests patch this name)
    )

from examples.mcp_client_remote.fleet.context7_server import (
    _call_upstream as _call_context7_upstream,  # noqa: F401  (facade: tests monkeypatch this name)
)
from examples.mcp_client_remote.fleet.docker_client import (
    DockerClient,  # noqa: F401  (facade: tests patch this name)
)
from examples.mcp_client_remote.fleet.gitea_client import (
    GiteaClient,  # noqa: F401  (facade: tests monkeypatch this name)
)
from examples.mcp_client_remote.fleet.github_client import (
    GitHubClient,  # noqa: F401  (facade: tests monkeypatch this name)
    normalize_list_response,  # noqa: F401  (facade)
)
from examples.mcp_client_remote.fleet.postgres_client import PostgresClient
from examples.mcp_client_remote.fleet.shared import (
    list_pagination_meta,  # noqa: F401  (facade)
    minimize_issue_payload,  # noqa: F401  (facade)
)

# OAuth provider and settings
from examples.mcp_server.mcp_audit import (
    McpAuditEvent,  # noqa: F401 (facade: tests import this name)
    get_audit_logger,  # noqa: F401 (facade: tests patch this name)
)
from examples.mcp_server.mcp_infra import auth_setup, gateway_errors, runtime, tool_registry

_auth_settings, _auth_provider, _agent_router = auth_setup.setup()

mcp = FastMCP(
    "agent-ssh-gateway",
    auth=_auth_settings,
    auth_server_provider=_auth_provider if _auth_settings else None,
)
runtime.set_mcp(mcp)
client = GatewayClient()

register_tool = tool_registry.register_tool
instrumented = tool_registry.instrumented
_envelope_to_call_tool_result = tool_registry._envelope_to_call_tool_result
compute_toolset_hash = tool_registry.compute_toolset_hash
run_tool = tool_registry.run_tool
_validate_project = tool_registry._validate_project
_GATEWAY_ERROR_CODE_MAP = gateway_errors._GATEWAY_ERROR_CODE_MAP
_gateway_error_message = gateway_errors._gateway_error_message
_gateway_error_hint = gateway_errors._gateway_error_hint
_classify_gateway_error = gateway_errors._classify_gateway_error

# ── Docker hostname resolver ────────────────────────────────────────


def _resolve_docker_host(hostname: str, network: str = "internal_net") -> str:
    """Resolve a Docker container name to its IP on a given network.

    Falls back to the hostname as-is when resolution fails (off-host,
    no Docker, different network, etc.).
    """
    import subprocess

    try:
        fmt = f"{{{{.NetworkSettings.Networks.{network}.IPAddress}}}}"
        result = subprocess.run(
            ["docker", "inspect", "-f", fmt, hostname],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            ip = result.stdout.strip()
            if ip:
                return ip
    except Exception:
        pass
    return hostname


# ── Postgres DSN ────────────────────────────────────────────────────
def _build_pg_dsn(pg_vars: dict[str, str]) -> str | None:
    h = pg_vars.get("PGHOST", "")
    p = pg_vars.get("PGPORT", "5432")
    d = pg_vars.get("PGDATABASE", "")
    u = pg_vars.get("PGUSER", "")
    pw = pg_vars.get("PGPASSWORD", "")
    if not all([h, d, u, pw]):
        return None
    resolved_host = _resolve_docker_host(h)
    if resolved_host != h:
        print(f"  resolved {h} -> {resolved_host} via docker inspect", file=sys.stderr)
    from urllib.parse import quote_plus

    return (
        f"postgresql://{quote_plus(u)}:{quote_plus(pw)}@{resolved_host}:{p}/{d}"
        f"?sslmode=disable&application_name=mcp_gateway"
    )


PG_DSN: str | None = None
_pg_env = "/etc/agent-mcp-postgres.env"
if os.path.exists(_pg_env):
    # Legacy systemd fleet-of-adapters convention: a separate systemd unit
    # (agent-mcp-postgres) owns this file, deliberately scoped down (e.g.
    # a read-only DB user) independently of whatever PG* vars this
    # process's own env file happens to carry -- preserved as-is,
    # unchanged, so that scoping can't silently regress.
    _pg_vars: dict[str, str] = {}
    with open(_pg_env) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                _pg_vars[k] = v
    PG_DSN = _build_pg_dsn(_pg_vars)

if PG_DSN is None:
    # Docker deployment: docker-compose.yml's mcp-oauth/mcp-server services
    # already set PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD directly as
    # container env vars -- there's no separate host file to read inside a
    # container. Without this fallback PG_DSN stayed None forever there
    # (confirmed live: postgres_* tools always failed with "Postgres not
    # configured" despite every PG* var being correctly set) even though
    # tools_manifest still advertised postgres_* as enabled (MAJOR audit
    # finding -- the manifest-accuracy half of this bug; this is the
    # functional half underneath it).
    PG_DSN = _build_pg_dsn(dict(os.environ))

_pg_client: PostgresClient | None = None


def _get_pg_client() -> PostgresClient | None:
    global _pg_client
    if _pg_client is None and PG_DSN is not None:
        _pg_client = PostgresClient(PG_DSN)
    return _pg_client


_confirm_store: ConfirmStore = ConfirmStore()







# ── Tools Manifest ──────────────────────────────────────────────

from tools_manifest import build_manifest as _build_manifest  # noqa: E402

_scope_enforcement = os.environ.get("MCP_SCOPE_ENFORCEMENT", "off").strip().lower()
if _scope_enforcement not in ("off", "audit", "enforce"):
    _scope_enforcement = "off"


def _unavailable_tool_reasons() -> dict[str, str]:
    """Map tool name -> reason it's registered but won't actually work.

    MAJOR audit finding: tools_manifest reported "enabled": True
    unconditionally for docker_*/resolve_library_id/query_docs/postgres_*
    even in images/deployments missing their actual runtime dependency
    (confirmed live: this image has neither `docker` nor `npx` on PATH) --
    a client had no way to distinguish "registered" from "will actually
    work" short of calling the tool and getting a runtime error. Computed
    here (not inside tools_manifest.build_manifest(), which stays pure
    registry introspection with no I/O) from cheap, no-network-call
    signals: PATH lookups and PG_DSN's own resolution (see _build_pg_dsn).
    """
    from tool_scopes import TOOL_SCOPES

    reasons: dict[str, str] = {}
    if shutil.which("docker") is None:
        msg = "docker CLI not present in this image"
        for name, scopes in TOOL_SCOPES.items():
            if "mcp:docker" in scopes or "mcp:docker:admin" in scopes:
                reasons[name] = msg
    if shutil.which("npx") is None:
        msg = "npx not present in this image"
        for name, scopes in TOOL_SCOPES.items():
            if "mcp:docs" in scopes:
                reasons[name] = msg
    if PG_DSN is None:
        msg = "Postgres not configured (PG_DSN unresolved)"
        for name, scopes in TOOL_SCOPES.items():
            if "mcp:postgres" in scopes:
                reasons[name] = msg
    return reasons


@register_tool("tools_manifest")
def gateway_tools_manifest(
    scope: str | None = None,
    mode: str | None = None,
    name_prefix: str | None = None,
    include_descriptions: bool = True,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return a read-only manifest of registered tools, modes, scopes, and access profiles.
    No secrets, no env dumps, no network calls, no tool execution.

    Optional filters (scope/mode/name_prefix) and include_descriptions/
    offset/limit keep the response small -- the unfiltered manifest lists
    every registered tool's full description, which is expensive context
    for an agent that only needs, say, the docker_* tool names. Each
    entry also reports "available" (and "unavailable_reason" when false)
    -- distinct from "enabled": a tool can be registered/enabled for this
    mode yet still not actually work in this deployment (missing docker/
    npx binary, Postgres not configured).
    """
    return _run_gateway(
        tool="tools_manifest",
        fn=lambda: _build_manifest(
            registered_tools=mcp._tool_manager.list_tools(),
            scope_enforcement=_scope_enforcement,
            scope=scope,
            mode=mode,
            name_prefix=name_prefix,
            include_descriptions=include_descriptions,
            offset=offset,
            limit=limit,
            unavailable_tool_reasons=_unavailable_tool_reasons(),
        ),
    )


# ── Main ─────────────────────────────────────────────────────────

# Import adapters and register their tools into the live FastMCP
# instance. Explicit register_all() (not import-time decorator side
# effects): server.py may be importlib.reloaded, and the adapters are
# cached in sys.modules, so import-time registration would miss a fresh
# FastMCP instance.
from examples.mcp_server.mcp_infra.adapters import (  # noqa: E402
    agent,
    context7,
    docker,
    gateway,
    postgres,
    remote,
    workspace,
)

_get_workspace_registry = workspace._get_workspace_registry
gateway_workspace_file_write = workspace.gateway_workspace_file_write
gateway_workspace_file_edit = workspace.gateway_workspace_file_edit
gateway_workspace_apply_patch = workspace.gateway_workspace_apply_patch
gateway_workspace_preview_write = workspace.gateway_workspace_preview_write
gateway_workspace_preview_edit = workspace.gateway_workspace_preview_edit
gateway_workspace_preview_patch = workspace.gateway_workspace_preview_patch
gateway_workspace_verify = workspace.gateway_workspace_verify

gitea_get_repo = remote.gitea_get_repo
gitea_list_branches = remote.gitea_list_branches
gitea_list_commits = remote.gitea_list_commits
gitea_get_file = remote.gitea_get_file
gitea_list_issues = remote.gitea_list_issues
gitea_get_issue = remote.gitea_get_issue
gitea_list_pull_requests = remote.gitea_list_pull_requests
gitea_get_pull_request = remote.gitea_get_pull_request
gitea_list_action_runs = remote.gitea_list_action_runs
gitea_get_action_run = remote.gitea_get_action_run
gitea_list_action_run_jobs = remote.gitea_list_action_run_jobs
gitea_list_workflows = remote.gitea_list_workflows
github_get_repo = remote.github_get_repo
github_list_branches = remote.github_list_branches
github_list_commits = remote.github_list_commits
github_get_file = remote.github_get_file
github_list_issues = remote.github_list_issues
github_get_issue = remote.github_get_issue
github_list_pull_requests = remote.github_list_pull_requests
github_get_pull_request = remote.github_get_pull_request
gateway_write_agent_task = agent.gateway_write_agent_task
gateway_read_agent_status = agent.gateway_read_agent_status
gateway_read_agent_report = agent.gateway_read_agent_report
gateway_read_agent_diff = agent.gateway_read_agent_diff
gateway_list_agent_tasks = agent.gateway_list_agent_tasks
gateway_archive_agent_task = agent.gateway_archive_agent_task
gateway_run_opencode = agent.gateway_run_opencode
gateway_run_agent = agent.gateway_run_agent

agent.register_all()
context7.register_all()
workspace.register_all()
docker.register_all()
gateway.register_all()
remote.register_all()
postgres.register_all()

_run_gateway = gateway._run_gateway
_split_lines = gateway._split_lines
gateway_health = gateway.gateway_health
gateway_project_list = gateway.gateway_project_list
gateway_scan_command = gateway.gateway_scan_command
gateway_simulate = gateway.gateway_simulate
gateway_scan_file = gateway.gateway_scan_file
gateway_project_scan_destructive = gateway.gateway_project_scan_destructive
gateway_explain_pattern = gateway.gateway_explain_pattern
gateway_list_sessions = gateway.gateway_list_sessions
gateway_session_health = gateway.gateway_session_health
gateway_execute_restricted = gateway.gateway_execute_restricted
gateway_execute_argv = gateway.gateway_execute_argv
gateway_apply_patch = gateway.gateway_apply_patch
gateway_job_status = gateway.gateway_job_status
gateway_job_result = gateway.gateway_job_result
gateway_wait_job = gateway.gateway_wait_job
gateway_job_wait = gateway.gateway_job_wait
gateway_repo_status = gateway.gateway_repo_status
gateway_working_directory = gateway.gateway_working_directory
gateway_info = gateway.gateway_info
gateway_git_status = gateway.gateway_git_status
gateway_recent_commits = gateway.gateway_recent_commits
gateway_git_diff_stat = gateway.gateway_git_diff_stat
gateway_show_changes = gateway.gateway_show_changes
gateway_git_add = gateway.gateway_git_add
gateway_git_commit = gateway.gateway_git_commit
gateway_git_push = gateway.gateway_git_push
gateway_run_tests = gateway.gateway_run_tests
gateway_run_lint = gateway.gateway_run_lint
gateway_run_compileall = gateway.gateway_run_compileall
gateway_read_file = gateway.gateway_read_file
gateway_search_text = gateway.gateway_search_text
gateway_find_files = gateway.gateway_find_files
gateway_list_files = gateway.gateway_list_files
gateway_tree = gateway.gateway_tree
gateway_list_tree = gateway.gateway_list_tree
gateway_git_diff = gateway.gateway_git_diff
gateway_git_diff_cached = gateway.gateway_git_diff_cached
gateway_show_file_diff = gateway.gateway_show_file_diff
gateway_run_pytest = gateway.gateway_run_pytest
gateway_run_ruff = gateway.gateway_run_ruff
gateway_run_mypy = gateway.gateway_run_mypy
gateway_remotes = gateway.gateway_remotes
gateway_current_branch = gateway.gateway_current_branch
gateway_commit_head = gateway.gateway_commit_head
gateway_read_handoff = gateway.gateway_read_handoff
gateway_write_handoff_plan = gateway.gateway_write_handoff_plan
gateway_show_handoff_status = gateway.gateway_show_handoff_status
gateway_self_test = gateway.gateway_self_test
gateway_latency_report = gateway.gateway_latency_report
gateway_diagnostics_latency = gateway.gateway_diagnostics_latency

resolve_library_id = context7.resolve_library_id
query_docs = context7.query_docs

postgres_health = postgres.postgres_health
postgres_list_schemas = postgres.postgres_list_schemas
postgres_list_tables = postgres.postgres_list_tables
postgres_describe_table = postgres.postgres_describe_table
postgres_select = postgres.postgres_select
postgres_vector_status = postgres.postgres_vector_status

docker_ps = docker.docker_ps
docker_images = docker.docker_images
docker_inspect = docker.docker_inspect
docker_logs = docker.docker_logs
docker_stats = docker.docker_stats
docker_compose_ps = docker.docker_compose_ps
docker_compose_services = docker.docker_compose_services
docker_compose_logs = docker.docker_compose_logs
docker_stop = docker.docker_stop
docker_restart = docker.docker_restart
docker_compose_up = docker.docker_compose_up
docker_compose_restart = docker.docker_compose_restart
docker_compose_build = docker.docker_compose_build
docker_rm = docker.docker_rm
docker_compose_down = docker.docker_compose_down
docker_prune = docker.docker_prune
docker_exec = docker.docker_exec
docker_run = docker.docker_run
docker_rmi = docker.docker_rmi
docker_volume_rm = docker.docker_volume_rm
confirm_operation = docker.confirm_operation
docker_pending_actions = docker.docker_pending_actions
_CONFIRM_HANDLERS = docker._CONFIRM_HANDLERS
_confirmation_response = docker._confirmation_response
_get_token_scopes = docker._get_token_scopes
_docker_start_impl = docker._docker_start_impl
_docker_stop_impl = docker._docker_stop_impl
_docker_restart_impl = docker._docker_restart_impl
_docker_rm_impl = docker._docker_rm_impl
_docker_compose_down_impl = docker._docker_compose_down_impl
_docker_prune_impl = docker._docker_prune_impl
_docker_exec_impl = docker._docker_exec_impl
_docker_run_impl = docker._docker_run_impl
_docker_compose_up_impl = docker._docker_compose_up_impl
_docker_compose_restart_impl = docker._docker_compose_restart_impl
_docker_compose_build_impl = docker._docker_compose_build_impl
_docker_rmi_impl = docker._docker_rmi_impl
_docker_volume_rm_impl = docker._docker_volume_rm_impl

if __name__ == "__main__":
    mcp.run()
