"""Experimental MCP server for agent-ssh-gateway.

This server is intentionally kept outside the gateway core.
"""
# ruff: noqa: E402 — late imports intentional for --reload compat

from __future__ import annotations

import json
import os
import sys
import time as _time
from collections.abc import Callable
from typing import Any

_mcp_started_at = _time.time()

from agent_tasks import (
    archive_agent_task as _archive_agent_task,
)
from agent_tasks import (
    list_agent_tasks as _list_agent_tasks,
)
from agent_tasks import (
    read_agent_task_file as _read_agent_task_file,
)
from agent_tasks import (
    write_agent_task as _write_agent_task,
)
from agent_tools import (
    project_run_agent as _project_run_agent,
)
from chatgpt_tools import (
    git_diff_stat,
    git_status,
    project_commit_head,
    project_current_branch,
    project_find_files,
    project_git_diff,
    project_git_diff_cached,
    project_git_diff_stat,
    project_git_status,
    project_info,
    project_list_files,
    project_list_tree,
    project_read_file,
    project_read_handoff,
    project_recent_commits,
    project_remotes,
    project_run_compileall,
    project_run_lint,
    project_run_mypy,
    project_run_pytest,
    project_run_ruff,
    project_run_tests,
    project_search_text,
    project_show_changes,
    project_show_file_diff,
    project_show_handoff_status,
    project_tree,
    project_working_directory,
    project_write_handoff_plan,
    recent_commits,
    run_compileall,
    run_lint,
    run_project_command,
    run_tests,
    show_changes,
    working_directory,
)
from command_policy import CommandPolicyError
from docker_confirm import ConfirmAction, ConfirmStatus, ConfirmStore
from gateway_client import GatewayClient, GatewayClientError, resolve_file_path
from handoff import read_handoff, show_handoff_status, write_handoff_plan
from mcp.server.fastmcp import FastMCP
from mimo_tools import (
    project_run_mimo as _project_run_mimo,
)
from opencode_tools import (
    project_run_opencode as _project_run_opencode,
)
from self_test import run_self_test
from tool_modes import should_register_tool
from tool_results import build_command_result, error_result, text_result, tool_error, tool_success
from write_modes import WriteModeError, WritePermissionError

from examples.chatgpt_remote_mcp.fleet.context7_server import (
    _call_upstream as _call_context7_upstream,
)
from examples.chatgpt_remote_mcp.fleet.docker_client import DockerClient, RunResult
from examples.chatgpt_remote_mcp.fleet.gitea_client import GiteaClient
from examples.chatgpt_remote_mcp.fleet.github_client import (
    GitHubClient,
    normalize_list_response,
)
from examples.chatgpt_remote_mcp.fleet.postgres_client import PostgresClient

# OAuth provider and settings
from examples.mcp_server.latency_metrics import get_tracker
from examples.mcp_server.oauth_provider import (
    DEFAULT_SCOPES,
    SUPPORTED_SCOPES,
    GatewayOAuthProvider,
)

MCP_AUTH_MODE = os.environ.get("MCP_AUTH_MODE", "oauth").strip().lower()
if MCP_AUTH_MODE not in ("token", "oauth"):
    raise ValueError(f"Invalid MCP_AUTH_MODE={MCP_AUTH_MODE!r}; expected one of ('token', 'oauth')")

_auth_provider: GatewayOAuthProvider | None = None
_auth_settings = None

if MCP_AUTH_MODE == "oauth":
    _auth_provider = GatewayOAuthProvider()

    _health_token = os.environ.get("MCP_HEALTHCHECK_BEARER_TOKEN", "")
    if _health_token:
        from examples.mcp_server.oauth_provider import StoredToken as _StoredToken
        from examples.mcp_server.oauth_provider import hash_token as _hash_tok

        _at_hash = _hash_tok(_health_token)
        _auth_provider._tokens[_at_hash] = _StoredToken(
            token=_at_hash,
            client_id="mcp_healthcheck",
            scopes=list(SUPPORTED_SCOPES),
            expires_at=float("inf"),
            type="access",
        )

    _extra_tokens_all: dict[str, str] = {}

    _extra_tokens_json = os.environ.get("MCP_EXTRA_TOKENS_JSON", "")
    if _extra_tokens_json:
        import json

        try:
            _extra_tokens_all.update(json.loads(_extra_tokens_json))
        except Exception as _exc:
            print(f"  MCP_EXTRA_TOKENS_JSON error: {_exc}", file=sys.stderr)

    _extra_tokens_file = os.environ.get("MCP_EXTRA_TOKENS_FILE", "")
    if _extra_tokens_file:
        if os.path.isfile(_extra_tokens_file):
            import json

            try:
                with open(_extra_tokens_file) as _f:
                    _extra_tokens_all.update(json.load(_f))
            except Exception as _exc:
                print(f"  MCP_EXTRA_TOKENS_FILE error: {_exc}", file=sys.stderr)
        else:
            print(
                f"  MCP_EXTRA_TOKENS_FILE not found: {_extra_tokens_file}",
                file=sys.stderr,
            )

    if _extra_tokens_all:
        from examples.mcp_server.oauth_provider import StoredToken as _StoredToken
        from examples.mcp_server.oauth_provider import hash_token as _hash_tok
        from examples.mcp_server.tool_scopes import ACCESS_PROFILES as _ACCESS_PROFILES

        for _token_str, _profile in _extra_tokens_all.items():
            _at_hash = _hash_tok(_token_str)
            _profile_scopes = _ACCESS_PROFILES.get(_profile, list(SUPPORTED_SCOPES))
            _auth_provider._tokens[_at_hash] = _StoredToken(
                token=_at_hash,
                client_id=f"mcp_extras_{_profile}",
                scopes=list(_profile_scopes),
                expires_at=float("inf"),
                type="access",
            )
        print(f"  extra tokens: {len(_extra_tokens_all)} registered", file=sys.stderr)
        if _extra_tokens_file:
            print(f"  extra file  : {_extra_tokens_file}", file=sys.stderr)

    try:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
        from pydantic import AnyHttpUrl

        _auth_settings = AuthSettings(
            issuer_url=AnyHttpUrl(os.environ.get("MCP_ISSUER_URL", "https://ssh.xloud.ru")),
            resource_server_url=AnyHttpUrl(
                os.environ.get("MCP_RESOURCE_URL", "https://ssh.xloud.ru/mcp")
            ),
            service_documentation_url=AnyHttpUrl("https://github.com/gpakoh/agent-ssh-gateway"),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=SUPPORTED_SCOPES,
                default_scopes=list(SUPPORTED_SCOPES),
            ),
            required_scopes=None,
        )
    except ImportError:
        pass
elif MCP_AUTH_MODE == "token":
    _auth_provider = GatewayOAuthProvider()
    mcp_token = os.environ.get("MCP_PUBLIC_TOKEN", "")
    if not mcp_token:
        raise ValueError("MCP_PUBLIC_TOKEN is required in token mode")
    from examples.mcp_server.oauth_provider import StoredToken as _StoredToken
    from examples.mcp_server.oauth_provider import hash_token as _hash_tok

    _at_hash = _hash_tok(mcp_token)
    _auth_provider._tokens[_at_hash] = _StoredToken(
        token=_at_hash,
        client_id="mcp_static_client",
        scopes=list(DEFAULT_SCOPES),
        expires_at=float("inf"),
        type="access",
    )

# ── TokenStore: load persistent tokens from store ──────────────────
if _auth_provider is not None:
    try:
        from examples.mcp_server.token_store import TokenStore

        _token_store = TokenStore()
        _auth_provider.set_token_store(_token_store)
        _loaded = _auth_provider.load_tokens()
        if _loaded:
            print(
                f"  TokenStore: {_loaded} tokens loaded from {_token_store._path}", file=sys.stderr
            )
    except Exception as _exc:
        print(f"  TokenStore: error loading tokens: {_exc}", file=sys.stderr)

# ── Agent Backend Router ─────────────────────────────────────────────
_agent_router: AgentBackendRouter | None = None
if os.environ.get("MCP_AGENT_BACKEND_ROUTER_ENABLED", "false").strip().lower() == "true":
    try:
        from examples.mcp_server.agent_backend_router import AgentBackendRouter

        _agent_router = AgentBackendRouter(
            fallback_order=[
                x.strip()
                for x in os.environ.get("MCP_BACKEND_FALLBACK_ORDER", "opencode,mimo").split(",")
                if x.strip()
            ],
        )
        print(
            f"  backend router: enabled ({len(_agent_router._backends)} backends)", file=sys.stderr
        )
    except Exception as _exc:
        print(f"  backend router: init error: {_exc}", file=sys.stderr)

mcp = FastMCP(
    "agent-ssh-gateway",
    auth=_auth_settings,
    auth_server_provider=_auth_provider if _auth_settings else None,
)
client = GatewayClient()

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
PG_DSN: str | None = None
_pg_env = "/etc/agent-mcp-postgres.env"
if os.path.exists(_pg_env):
    _pg_vars: dict[str, str] = {}
    with open(_pg_env) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                _pg_vars[k] = v
    _h = _pg_vars.get("PGHOST", "")
    _p = _pg_vars.get("PGPORT", "5432")
    _d = _pg_vars.get("PGDATABASE", "")
    _u = _pg_vars.get("PGUSER", "")
    _pw = _pg_vars.get("PGPASSWORD", "")
    if all([_h, _d, _u, _pw]):
        _resolved_host = _resolve_docker_host(_h)
        if _resolved_host != _h:
            print(f"  resolved {_h} -> {_resolved_host} via docker inspect", file=sys.stderr)
        PG_DSN = (
            f"postgresql://{_u}:{_pw}@{_resolved_host}:{_p}/{_d}?sslmode=disable&application_name=mcp_gateway"
        )

_pg_client: PostgresClient | None = None


def _get_pg_client() -> PostgresClient | None:
    global _pg_client
    if _pg_client is None and PG_DSN is not None:
        _pg_client = PostgresClient(PG_DSN)
    return _pg_client


_confirm_store: ConfirmStore = ConfirmStore()


def register_tool(name: str):
    """Decorator: register MCP tool only if visible in the active mode."""

    def decorator(func):
        if should_register_tool(name):
            return mcp.tool(name=name)(func)
        return func

    return decorator


def instrumented(tool_name: str):
    """Decorator that wraps a tool function with latency tracking."""

    def decorator(func):
        import asyncio

        if asyncio.iscoroutinefunction(func):

            async def async_wrapper(*args, **kwargs):
                tracker = get_tracker()
                with tracker.measure(tool_name):
                    result = await func(*args, **kwargs)
                if isinstance(result, dict) and "meta" in result:
                    recs = tracker.records.get(tool_name, [])
                    if recs:
                        result["meta"]["duration_ms"] = int(recs[-1])
                return result

            return async_wrapper
        else:

            def sync_wrapper(*args, **kwargs):
                tracker = get_tracker()
                with tracker.measure(tool_name):
                    result = func(*args, **kwargs)
                if isinstance(result, dict) and "meta" in result:
                    recs = tracker.records.get(tool_name, [])
                    if recs:
                        result["meta"]["duration_ms"] = int(recs[-1])
                return result

            return sync_wrapper

    return decorator


def _validate_project(project: str) -> str:
    """Validate and return project name. Raises ValueError on invalid input."""
    if not project:
        raise ValueError("project argument is required")
    parts = project.strip("/").split("/")
    for p in parts:
        if p in ("..", ".", "~", ""):
            raise ValueError(f"Invalid project name: {project!r}")
    return "/".join(parts)


import hashlib as _hashlib


def compute_toolset_hash(mcp_instance: FastMCP) -> str:
    """Compute SHA-256 hash of the canonical tool manifest.

    Canonical form: sorted list of {name, inputSchema} objects as compact JSON.
    Uses items.sort(key=lambda item: item["name"]) — NOT sorted(dicts).
    """
    tools_dict = {}
    if hasattr(mcp_instance, "_tool_manager"):
        tm = mcp_instance._tool_manager
        if hasattr(tm, "_tools"):
            tools_dict = tm._tools

    items = []
    for name, tool_obj in tools_dict.items():
        schema = getattr(tool_obj, "parameters", None) or {}
        items.append({"name": name, "inputSchema": schema})

    items.sort(key=lambda item: item["name"])  # type: ignore[arg-type,return-value]
    canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return "sha256:" + _hashlib.sha256(canonical.encode()).hexdigest()


def run_tool(
    *,
    tool: str,
    title: str,
    fn: Callable[[], dict[str, Any]],
    success_text: str,
) -> dict[str, Any]:
    """Execute a tool call with structured error handling."""
    try:
        data = fn()
    except (GatewayClientError, CommandPolicyError, WritePermissionError, WriteModeError) as exc:
        if isinstance(exc, (CommandPolicyError, WritePermissionError, WriteModeError)):
            return error_result(tool=tool, title=title, error=str(exc))
        code, retryable = _classify_gateway_error(exc)
        hint = "The requested file does not exist at the specified path" if code == "FILE_NOT_FOUND" else None
        return tool_error(
            tool=tool,
            code=code,
            message=str(exc),
            retryable=retryable,
            hint=hint,
            source="gateway",
        )
    return text_result(tool=tool, title=title, text=success_text, data=data)


def _classify_gateway_error(exc: GatewayClientError) -> tuple[str, bool]:
    """Classify a GatewayClientError into (error_code, retryable)."""
    status = exc.status_code
    msg = str(exc).lower()

    if status == 404 and ("file not found" in msg or "cannot read" in msg):
        return "FILE_NOT_FOUND", False

    if status is not None and status >= 500:
        return "INTERNAL_ERROR", True

    if status == 404:
        return "INTERNAL_ERROR", False
    if status == 502:
        return "INTERNAL_ERROR", True
    if status == 504:
        return "INTERNAL_ERROR", True

    return "INTERNAL_ERROR", True


def _run_gateway(
    tool: str,
    fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Execute a read-only gateway tool with canonical response envelope."""
    try:
        data = fn()
    except (GatewayClientError, CommandPolicyError, WritePermissionError, WriteModeError) as exc:
        if isinstance(exc, (CommandPolicyError, WritePermissionError, WriteModeError)):
            code = "POLICY_VIOLATION"
            retryable = False
        else:
            code, retryable = _classify_gateway_error(exc)
        hint = None
        if code == "FILE_NOT_FOUND":
            hint = "The requested file does not exist at the specified path"
        return tool_error(
            tool=tool,
            code=code,
            message=str(exc),
            retryable=retryable,
            hint=hint,
            source="gateway",
            read_only=True,
        )
    return tool_success(
        tool=tool,
        result=data,
        source="gateway",
        read_only=True,
    )


def _split_lines(value: str | None) -> list[str] | None:
    """Split newline-separated string into list, or return None."""
    if value is None:
        return None
    return [line.strip() for line in value.split("\n") if line.strip()]


@register_tool("health")
@instrumented("health")
def gateway_health() -> dict[str, Any]:
    """Check gateway + MCP health with build metadata and toolset hash."""
    from datetime import UTC, datetime

    gateway_data = client.health()

    mcp_build_sha = os.environ.get("BUILD_SHA", "").strip() or "unknown"
    mcp_build_time = os.environ.get("BUILD_TIME", "").strip()
    mcp_started_at = ""
    if _mcp_started_at:
        mcp_started_at = datetime.fromtimestamp(
            _mcp_started_at, tz=UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    toolset_hash = compute_toolset_hash(mcp)

    tools_count = 0
    if hasattr(mcp, "_tool_manager"):
        tm = mcp._tool_manager
        if hasattr(tm, "_tools"):
            tools_count = len(tm._tools)

    return {
        "mcp": {
            "build_sha": mcp_build_sha,
            "build_time": mcp_build_time,
            "started_at": mcp_started_at,
            "toolset_hash": toolset_hash,
            "tools_count": tools_count,
            "contract_version": "1",
        },
        "gateway": gateway_data,
    }


@register_tool("list_sessions")
@instrumented("list_sessions")
def gateway_list_sessions() -> dict[str, Any]:
    """List current SSH sessions visible to the configured API key."""

    def _list() -> dict[str, Any]:
        data = client.list_sessions()
        return data

    return run_tool(
        tool="list_sessions",
        title="List sessions",
        fn=_list,
        success_text="Retrieved session list.",
    )


@register_tool("session_health")
def gateway_session_health(session_id: str | None = None) -> dict[str, Any]:
    """Check an SSH session health."""

    def _health() -> dict[str, Any]:
        return client.session_health(session_id=session_id)

    return run_tool(
        tool="session_health",
        title="Session health",
        fn=_health,
        success_text="Session health retrieved.",
    )


@register_tool("execute_restricted")
def gateway_execute_restricted(command: str, session_id: str | None = None) -> dict[str, Any]:
    """Execute an allowlisted read-only command as a redacted async job."""

    def _exec() -> dict[str, Any]:
        return client.execute_restricted(command, session_id=session_id)

    return run_tool(
        tool="execute_restricted",
        title="Restricted execute",
        fn=_exec,
        success_text="Command submitted as a background job.",
    )


@register_tool("execute_argv")
def gateway_execute_argv(
    session_id: str,
    argv: list[str],
    stdin: str = "",
    timeout_s: int = 30,
) -> dict[str, Any]:
    """Execute explicit argv serialized as a safely quoted POSIX command.

    Args:
        session_id: Active SSH session ID.
        argv: Command and arguments as a list.
        stdin: Optional stdin content (UTF-8 only).
        timeout_s: Execution timeout (1-3600).

    Returns:
        Contract v1 dict with stdout/stderr/exit_code (not a JSON string).
    """
    try:
        raw = client.execute_argv(
            argv=argv,
            stdin=stdin,
            timeout_s=timeout_s,
            session_id=session_id,
        )
    except GatewayClientError as e:
        return tool_error(
            tool="execute_argv",
            code="TOOL_EXECUTION_FAILED",
            message=str(e),
        )
    return tool_success(
        tool="execute_argv",
        result=build_command_result(
            outcome="passed" if raw.get("exit_code", 1) == 0 else "failed",
            exit_code=raw.get("exit_code", -1),
            stdout=raw.get("stdout", ""),
            stderr=raw.get("stderr", ""),
            execution_duration_ms=int(raw.get("duration", 0) * 1000),
        ),
        source="gateway",
    )


@register_tool("project_apply_patch")
@instrumented("project_apply_patch")
def gateway_project_apply_patch(
    session_id: str,
    project: str,
    patch: str,
    expected_hashes: dict[str, str],
    strip: int = 1,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a unified diff patch to project files.

    Args:
        session_id: Active SSH session ID.
        project: Project name (registered in MCP_GATEWAY_PROJECT_ROOT).
        patch: Unified diff content.
        expected_hashes: Per-file sha256 hashes for safety check.
        strip: Strip leading path components (default 1 for a/b prefix).
        dry_run: Preview changes without applying.

    Returns:
        Contract v1 dict with per-file status (not a JSON string).
    """
    _validate_project(project)
    try:
        raw = client.apply_patch(
            project=project,
            patch=patch,
            expected_hashes=expected_hashes,
            strip=strip,
            dry_run=dry_run,
            session_id=session_id,
        )
    except GatewayClientError as e:
        return tool_error(
            tool="project_apply_patch",
            code="TOOL_EXECUTION_FAILED",
            message=str(e),
        )
    return tool_success(
        tool="project_apply_patch",
        result={
            "success": raw.get("success", False),
            "files_applied": raw.get("files_applied", 0),
            "files_failed": raw.get("files_failed", 0),
            "hunks_applied": raw.get("hunks_applied", 0),
            "preview": raw.get("preview"),
            "errors": raw.get("errors", []),
            "files": raw.get("files", []),
        },
        source="gateway",
    )


@register_tool("job_status")
def gateway_job_status(job_id: str) -> dict[str, Any]:
    """Get background job status."""

    def _status() -> dict[str, Any]:
        data = client.job_status(job_id)
        return data

    return run_tool(
        tool="job_status",
        title="Job status",
        fn=_status,
        success_text=f"Job {job_id} status retrieved.",
    )


@register_tool("job_result")
def gateway_job_result(job_id: str, redact_output: bool = True) -> dict[str, Any]:
    """Get background job result."""

    def _result() -> dict[str, Any]:
        data = client.job_result(job_id, redact_output=redact_output)
        return data

    return run_tool(
        tool="job_result",
        title="Job result",
        fn=_result,
        success_text=f"Job {job_id} result retrieved.",
    )


@register_tool("wait_job")
def gateway_wait_job(job_id: str, timeout_sec: int | None = None) -> dict[str, Any]:
    """Wait for a background job and return its result."""

    def _wait() -> dict[str, Any]:
        return client.wait_job(job_id, timeout_sec=timeout_sec)

    return run_tool(
        tool="wait_job",
        title="Wait job",
        fn=_wait,
        success_text=f"Job {job_id} completed.",
    )


@register_tool("job_wait")
@instrumented("job_wait")
def gateway_job_wait(job_id: str, timeout_sec: int | None = None) -> dict[str, Any]:
    """Wait for a background job to complete using long-poll.

    Uses the Gateway long-poll endpoint. Falls back to polling if the
    Gateway does not support long-poll (multi-worker or old version).

    Args:
        job_id: Background job identifier.
        timeout_sec: Maximum seconds to wait (default: 180).

    Returns:
        Contract v1 dict with job result or WAIT_TIMEOUT error.
    """
    try:
        result = client.wait_job(job_id, timeout_sec=timeout_sec)
    except GatewayClientError as exc:
        code, retryable = _classify_gateway_error(exc)
        return tool_error(
            tool="job_wait",
            code=code,
            message=str(exc),
            retryable=retryable,
            source="gateway",
        )

    if result.get("wait_timed_out"):
        return tool_error(
            tool="job_wait",
            code="WAIT_TIMEOUT",
            message=f"Job {job_id} did not complete within timeout",
            retryable=True,
            details={"job_id": job_id, "status": result.get("status", "running")},
            source="gateway",
        )

    return tool_success(
        tool="job_wait",
        result=result,
        source="gateway",
    )


@register_tool("read_file")
def gateway_read_file(path: str, session_id: str | None = None) -> dict[str, Any]:
    """Read a file through the gateway file API.

    Relative paths are resolved under MCP_GATEWAY_PROJECT_ROOT.
    Absolute paths must be under the project root.
    """
    resolved = resolve_file_path(path)

    def _read() -> dict[str, Any]:
        return client.read_file(resolved, session_id=session_id)

    return run_tool(
        tool="read_file",
        title="Read file",
        fn=_read,
        success_text=f"File {resolved} read successfully.",
    )


@register_tool("repo_status")
def gateway_repo_status(
    session_id: str | None = None, project: str | None = None
) -> dict[str, Any]:
    """Collect basic repository status using read-only commands.

    Args:
        session_id: Optional existing session ID. A new one is created if omitted.
        project: Project subdirectory under MCP_GATEWAY_PROJECT_ROOT. Required when
            the SSH session working directory is not a git repository.
    """

    def _status() -> dict[str, Any]:
        return client.repo_status(session_id=session_id, project=project)

    return run_tool(
        tool="repo_status",
        title="Repository status",
        fn=_status,
        success_text="Collected repository status.",
    )


@register_tool("working_directory")
def gateway_working_directory(session_id: str | None = None) -> dict[str, Any]:
    """Print working directory on the SSH target."""
    return run_tool(
        tool="working_directory",
        title="Working directory",
        fn=lambda: working_directory(client, session_id=session_id),
        success_text="Collected current working directory.",
    )


@register_tool("git_status")
def gateway_git_status(session_id: str | None = None) -> dict[str, Any]:
    """Show git working tree status (short format)."""
    return run_tool(
        tool="git_status",
        title="Git status",
        fn=lambda: git_status(client, session_id=session_id),
        success_text="Collected git status.",
    )


@register_tool("recent_commits")
def gateway_recent_commits(session_id: str | None = None) -> dict[str, Any]:
    """List recent commits (git log --oneline -10)."""
    return run_tool(
        tool="recent_commits",
        title="Recent commits",
        fn=lambda: recent_commits(client, session_id=session_id),
        success_text="Collected recent commits.",
    )


@register_tool("git_diff_stat")
def gateway_git_diff_stat(session_id: str | None = None) -> dict[str, Any]:
    """Show uncommitted diff stat (git diff --stat)."""
    return run_tool(
        tool="git_diff_stat",
        title="Git diff stat",
        fn=lambda: git_diff_stat(client, session_id=session_id),
        success_text="Collected git diff stat.",
    )


@register_tool("show_changes")
def gateway_show_changes(session_id: str | None = None, project: str | None = None) -> dict[str, Any]:
    """Show combined git status and diff stat."""
    return run_tool(
        tool="show_changes",
        title="Show changes",
        fn=lambda: show_changes(client, session_id=session_id, project=project),
        success_text="Collected repository change summary.",
    )


@register_tool("run_tests")
def gateway_run_tests(session_id: str | None = None) -> dict[str, Any]:
    """Run test suite (pytest -q)."""
    return run_tool(
        tool="run_tests",
        title="Run tests",
        fn=lambda: run_tests(client, session_id=session_id),
        success_text="Ran test suite.",
    )


@register_tool("run_lint")
def gateway_run_lint(session_id: str | None = None) -> dict[str, Any]:
    """Run ruff linter on the project."""
    return run_tool(
        tool="run_lint",
        title="Run lint",
        fn=lambda: run_lint(client, session_id=session_id),
        success_text="Ran lint checks.",
    )


@register_tool("run_compileall")
def gateway_run_compileall(session_id: str | None = None) -> dict[str, Any]:
    """Run Python compileall on the project."""
    return run_tool(
        tool="run_compileall",
        title="Run compileall",
        fn=lambda: run_compileall(client, session_id=session_id),
        success_text="Ran Python compileall.",
    )


@register_tool("project_working_directory")
def gateway_project_working_directory(project: str) -> dict[str, Any]:
    """Print working directory within MCP_GATEWAY_PROJECT_ROOT/{project}."""
    return _run_gateway(
        tool="project_working_directory",
        fn=lambda: project_working_directory(client, project),
    )


@register_tool("project_info")
def gateway_project_info(project: str) -> dict[str, Any]:
    """Return resolved project metadata for a configured project name.
    Read-only. Does not execute user-provided shell commands.
    """
    return _run_gateway(
        tool="project_info",
        fn=lambda: project_info(client, project),
    )


@register_tool("project_git_status")
def gateway_project_git_status(project: str) -> dict[str, Any]:
    """Show git working tree status within a project directory."""
    return run_tool(
        tool="project_git_status",
        title="Project git status",
        fn=lambda: project_git_status(client, project),
        success_text="Collected project git status.",
    )


@register_tool("project_recent_commits")
def gateway_project_recent_commits(project: str) -> dict[str, Any]:
    """List recent commits within a project (git log --oneline -10)."""
    return run_tool(
        tool="project_recent_commits",
        title="Project recent commits",
        fn=lambda: project_recent_commits(client, project),
        success_text="Collected project recent commits.",
    )


@register_tool("project_git_diff_stat")
def gateway_project_git_diff_stat(project: str) -> dict[str, Any]:
    """Show uncommitted diff stat within a project."""
    return run_tool(
        tool="project_git_diff_stat",
        title="Project git diff stat",
        fn=lambda: project_git_diff_stat(client, project),
        success_text="Collected project git diff stat.",
    )


@register_tool("project_show_changes")
def gateway_project_show_changes(project: str) -> dict[str, Any]:
    """Show combined git status and diff stat within a project."""
    return run_tool(
        tool="project_show_changes",
        title="Project show changes",
        fn=lambda: project_show_changes(client, project),
        success_text="Collected project change summary.",
    )


@register_tool("project_run_tests")
def gateway_project_run_tests(project: str) -> dict[str, Any]:
    """Run test suite within a project (pytest -q)."""
    return run_tool(
        tool="project_run_tests",
        title="Project run tests",
        fn=lambda: project_run_tests(client, project),
        success_text="Ran project test suite.",
    )


@register_tool("project_run_lint")
def gateway_project_run_lint(project: str) -> dict[str, Any]:
    """Run ruff linter within a project."""
    return run_tool(
        tool="project_run_lint",
        title="Project run lint",
        fn=lambda: project_run_lint(client, project),
        success_text="Ran project lint checks.",
    )


@register_tool("project_run_compileall")
def gateway_project_run_compileall(project: str) -> dict[str, Any]:
    """Run Python compileall within a project."""
    return run_tool(
        tool="project_run_compileall",
        title="Project run compileall",
        fn=lambda: project_run_compileall(client, project),
        success_text="Ran project Python compileall.",
    )


# ── Phase 2 project tools ─────────────────────────────────────────


@register_tool("project_read_file")
def gateway_project_read_file(project: str, path: str) -> dict[str, Any]:
    """Read a file within MCP_GATEWAY_PROJECT_ROOT/{project}."""
    return run_tool(
        tool="project_read_file",
        title="Project read file",
        fn=lambda: project_read_file(client, project, path),
        success_text="Read project file.",
    )


@register_tool("project_search_text")
def gateway_project_search_text(
    project: str, query: str, glob: str | None = None
) -> dict[str, Any]:
    """Search for text across project files using grep."""
    return run_tool(
        tool="project_search_text",
        title="Project search text",
        fn=lambda: project_search_text(client, project, query, glob=glob),
        success_text="Searched project text.",
    )


@register_tool("project_find_files")
def gateway_project_find_files(project: str, pattern: str) -> dict[str, Any]:
    """Find files matching a glob pattern in the project."""
    return run_tool(
        tool="project_find_files",
        title="Project find files",
        fn=lambda: project_find_files(project, pattern),
        success_text="Found project files.",
    )


@register_tool("project_list_files")
def gateway_project_list_files(project: str, pattern: str) -> dict[str, Any]:
    """List files matching a glob pattern using Python pathlib — no shell execution."""
    return _run_gateway(
        tool="project_list_files",
        fn=lambda: project_list_files(client, project, pattern),
    )


@register_tool("project_tree")
def gateway_project_tree(project: str, depth: int = 2, glob: str | None = None) -> dict[str, Any]:
    """List project directory tree up to a given depth."""
    return _run_gateway(
        tool="project_tree",
        fn=lambda: project_tree(client, project, depth=depth, glob=glob),
    )


@register_tool("project_list_tree")
def gateway_project_list_tree(project: str, depth: int = 2) -> dict[str, Any]:
    """List project directory tree using Python pathlib — no shell execution."""
    return _run_gateway(
        tool="project_list_tree",
        fn=lambda: project_list_tree(client, project, depth=depth),
    )


@register_tool("project_git_diff")
def gateway_project_git_diff(project: str, path: str | None = None) -> dict[str, Any]:
    """Show git diff (uncommitted changes) in a project."""
    return run_tool(
        tool="project_git_diff",
        title="Project git diff",
        fn=lambda: project_git_diff(client, project, path=path),
        success_text="Collected project git diff.",
    )


@register_tool("project_git_diff_cached")
def gateway_project_git_diff_cached(project: str, path: str | None = None) -> dict[str, Any]:
    """Show git --cached diff (staged changes) in a project."""
    return run_tool(
        tool="project_git_diff_cached",
        title="Project git diff cached",
        fn=lambda: project_git_diff_cached(client, project, path=path),
        success_text="Collected project staged diff.",
    )


@register_tool("project_show_file_diff")
def gateway_project_show_file_diff(project: str, path: str) -> dict[str, Any]:
    """Show uncommitted diff for a specific file in the project."""
    return run_tool(
        tool="project_show_file_diff",
        title="Project show file diff",
        fn=lambda: project_show_file_diff(client, project, path),
        success_text="Collected file diff.",
    )


@register_tool("project_run_pytest")
def gateway_project_run_pytest(project: str, target: str) -> dict[str, Any]:
    """Run pytest on a specific target within the project."""
    return run_tool(
        tool="project_run_pytest",
        title="Project run pytest",
        fn=lambda: project_run_pytest(client, project, target),
        success_text="Ran project pytest.",
    )


@register_tool("project_run_ruff")
def gateway_project_run_ruff(project: str, target: str) -> dict[str, Any]:
    """Run ruff linter on a specific target within the project."""
    return run_tool(
        tool="project_run_ruff",
        title="Project run ruff",
        fn=lambda: project_run_ruff(client, project, target),
        success_text="Ran project ruff check.",
    )


@register_tool("project_run_mypy")
def gateway_project_run_mypy(project: str, target: str) -> dict[str, Any]:
    """Run mypy type checker on a specific target within the project."""
    return run_tool(
        tool="project_run_mypy",
        title="Project run mypy",
        fn=lambda: project_run_mypy(client, project, target),
        success_text="Ran project mypy.",
    )


@register_tool("project_remotes")
def gateway_project_remotes(project: str) -> dict[str, Any]:
    """List git remotes for the project."""
    return run_tool(
        tool="project_remotes",
        title="Project remotes",
        fn=lambda: project_remotes(client, project),
        success_text="Collected project remotes.",
    )


@register_tool("project_current_branch")
def gateway_project_current_branch(project: str) -> dict[str, Any]:
    """Show current git branch for the project."""
    return run_tool(
        tool="project_current_branch",
        title="Project current branch",
        fn=lambda: project_current_branch(client, project),
        success_text="Collected project current branch.",
    )


@register_tool("project_commit_head")
def gateway_project_commit_head(project: str) -> dict[str, Any]:
    """Show HEAD commit SHA for the project."""
    return run_tool(
        tool="project_commit_head",
        title="Project commit HEAD",
        fn=lambda: project_commit_head(client, project),
        success_text="Collected project HEAD commit.",
    )


@register_tool("project_read_handoff")
def gateway_project_read_handoff(project: str) -> dict[str, Any]:
    """Read .ai-bridge handoff files for a project."""
    return run_tool(
        tool="project_read_handoff",
        title="Project read handoff",
        fn=lambda: project_read_handoff(client, project),
        success_text="Read project handoff.",
    )


@register_tool("project_write_handoff_plan")
def gateway_project_write_handoff_plan(
    project: str, task: str, agent: str = "opencode", notes: str | None = None
) -> dict[str, Any]:
    """Write .ai-bridge/current-plan.md for a project (requires MCP_GATEWAY_WRITE_MODE=handoff)."""
    return run_tool(
        tool="project_write_handoff_plan",
        title="Project write handoff",
        fn=lambda: project_write_handoff_plan(client, project, task, agent=agent, notes=notes),
        success_text="Wrote project handoff plan.",
    )


@register_tool("project_show_handoff_status")
def gateway_project_show_handoff_status(project: str) -> dict[str, Any]:
    """Show .ai-bridge file listing for a project."""
    return run_tool(
        tool="project_show_handoff_status",
        title="Project handoff status",
        fn=lambda: project_show_handoff_status(client, project),
        success_text="Checked project handoff status.",
    )


@register_tool("self_test")
def gateway_self_test() -> dict[str, Any]:
    """Run read-only diagnostics for the MCP gateway example."""
    data = run_self_test(client)
    status = data.get("status", "unknown")
    return text_result(
        tool="self_test",
        title="Gateway self-test",
        text=f"Gateway MCP self-test status: {status}",
        data=data,
    )


@register_tool("latency_report")
def gateway_latency_report() -> dict[str, Any]:
    """Return accumulated per-tool latency statistics."""
    return tool_success(
        get_tracker().summary(),
        tool_name="latency_report",
    )


@register_tool("diagnostics_latency")
def gateway_diagnostics_latency() -> dict[str, Any]:
    """Return MCP-side latency breakdown and gateway latency summary."""
    tracker = get_tracker()
    mcp_summary = tracker.summary()

    try:
        gw_data = client._get("/api/diagnostics/latency")
    except Exception:
        gw_data = {"error": "gateway diagnostics unavailable"}

    return tool_success(
        {
            "mcp": mcp_summary,
            "gateway": gw_data,
        },
        tool_name="diagnostics_latency",
    )


@register_tool("read_handoff")
def gateway_read_handoff(session_id: str | None = None) -> dict[str, Any]:
    """Read .ai-bridge handoff files."""
    return run_tool(
        tool="read_handoff",
        title="Read handoff",
        fn=lambda: read_handoff(client, session_id=session_id),
        success_text="Read .ai-bridge handoff files.",
    )


@register_tool("show_handoff_status")
def gateway_show_handoff_status(session_id: str | None = None) -> dict[str, Any]:
    """Show compact handoff file availability."""
    return run_tool(
        tool="show_handoff_status",
        title="Handoff status",
        fn=lambda: show_handoff_status(client, session_id=session_id),
        success_text="Collected .ai-bridge handoff status.",
    )


@register_tool("write_handoff_plan")
def gateway_write_handoff_plan(
    task: str,
    agent: str = "opencode",
    notes: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Write .ai-bridge/current-plan.md given a task description."""
    return run_tool(
        tool="write_handoff_plan",
        title="Write handoff plan",
        fn=lambda: write_handoff_plan(
            client,
            task=task,
            agent=agent,
            notes=notes,
            session_id=session_id,
        ),
        success_text="Wrote .ai-bridge/current-plan.md.",
    )


# ── Gitea tools ──────────────────────────────────────────────────


@register_tool("gitea_get_repo")
async def gitea_get_repo(owner: str, repo: str) -> dict[str, Any]:
    """Get Gitea repository metadata including description, visibility, language, default branch."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_get_repo", title="Gitea get repo", error="GITEA_TOKEN not configured"
        )
    async with GiteaClient(token) as client:
        data = await client.get_repo(owner, repo)
    return text_result(
        tool="gitea_get_repo",
        title="Gitea repo",
        text=f"Repo: {data.get('full_name', 'unknown')}",
        data=data,
    )


@register_tool("gitea_list_branches")
async def gitea_list_branches(owner: str, repo: str, limit: int = 30) -> dict[str, Any]:
    """List branches in a Gitea repository."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_list_branches", title="Gitea branches", error="GITEA_TOKEN not configured"
        )
    async with GiteaClient(token) as client:
        data = normalize_list_response(await client.list_branches(owner, repo, limit=limit))
    return text_result(
        tool="gitea_list_branches",
        title="Gitea branches",
        text=f"Branches: {data['count']}",
        data=data,
    )


@register_tool("gitea_list_commits")
async def gitea_list_commits(
    owner: str, repo: str, sha: str | None = None, limit: int = 30
) -> dict[str, Any]:
    """List commits in a Gitea repository. Optionally filter by branch SHA."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_list_commits", title="Gitea commits", error="GITEA_TOKEN not configured"
        )
    async with GiteaClient(token) as client:
        data = normalize_list_response(await client.list_commits(owner, repo, sha=sha, limit=limit))
    return text_result(
        tool="gitea_list_commits",
        title="Gitea commits",
        text=f"Commits: {data['count']}",
        data=data,
    )


@register_tool("gitea_get_file")
async def gitea_get_file(
    owner: str, repo: str, path: str, branch: str | None = None
) -> dict[str, Any]:
    """Get a file or directory from a Gitea repository."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_get_file", title="Gitea file", error="GITEA_TOKEN not configured"
        )
    async with GiteaClient(token) as client:
        data = await client.get_file(owner, repo, path, branch=branch)
    return text_result(tool="gitea_get_file", title="Gitea file", text=f"File: {path}", data=data)


@register_tool("gitea_list_issues")
async def gitea_list_issues(
    owner: str, repo: str, state: str = "open", limit: int = 30
) -> dict[str, Any]:
    """List issues in a Gitea repository. State: open, closed, all."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_list_issues", title="Gitea issues", error="GITEA_TOKEN not configured"
        )
    async with GiteaClient(token) as client:
        data = normalize_list_response(
            await client.list_issues(owner, repo, state=state, limit=limit)
        )
    return text_result(
        tool="gitea_list_issues", title="Gitea issues", text=f"Issues: {data['count']}", data=data
    )


@register_tool("gitea_get_issue")
async def gitea_get_issue(owner: str, repo: str, issue_number: int) -> dict[str, Any]:
    """Get details of a specific Gitea issue by number."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_get_issue", title="Gitea issue", error="GITEA_TOKEN not configured"
        )
    async with GiteaClient(token) as client:
        data = await client.get_issue(owner, repo, issue_number)
    return text_result(
        tool="gitea_get_issue", title="Gitea issue", text=f"Issue #{issue_number}", data=data
    )


@register_tool("gitea_list_pull_requests")
async def gitea_list_pull_requests(
    owner: str, repo: str, state: str = "open", limit: int = 30
) -> dict[str, Any]:
    """List pull requests in a Gitea repository. State: open, closed, all."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_list_pull_requests", title="Gitea PRs", error="GITEA_TOKEN not configured"
        )
    async with GiteaClient(token) as client:
        data = normalize_list_response(
            await client.list_pull_requests(owner, repo, state=state, limit=limit)
        )
    return text_result(
        tool="gitea_list_pull_requests", title="Gitea PRs", text=f"PRs: {data['count']}", data=data
    )


@register_tool("gitea_get_pull_request")
async def gitea_get_pull_request(owner: str, repo: str, pull_number: int) -> dict[str, Any]:
    """Get details of a specific Gitea pull request by number."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_get_pull_request", title="Gitea PR", error="GITEA_TOKEN not configured"
        )
    async with GiteaClient(token) as client:
        data = await client.get_pull_request(owner, repo, pull_number)
    return text_result(
        tool="gitea_get_pull_request", title="Gitea PR", text=f"PR #{pull_number}", data=data
    )


@register_tool("gitea_list_action_runs")
async def gitea_list_action_runs(
    owner: str, repo: str, status: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """List Gitea Actions workflow runs. Optionally filter by status (completed, running, waiting)."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_list_action_runs", title="Gitea runs", error="GITEA_TOKEN not configured"
        )
    async with GiteaClient(token) as client:
        data = await client.list_action_runs(owner, repo, status=status, limit=limit)
    return text_result(
        tool="gitea_list_action_runs", title="Gitea runs", text="Action runs retrieved", data=data
    )


@register_tool("gitea_get_action_run")
async def gitea_get_action_run(owner: str, repo: str, run_id: int) -> dict[str, Any]:
    """Get details of a specific Gitea Actions workflow run by ID."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_get_action_run", title="Gitea run", error="GITEA_TOKEN not configured"
        )
    async with GiteaClient(token) as client:
        data = await client.get_action_run(owner, repo, run_id)
    return text_result(
        tool="gitea_get_action_run", title="Gitea run", text=f"Run #{run_id}", data=data
    )


@register_tool("gitea_list_action_run_jobs")
async def gitea_list_action_run_jobs(owner: str, repo: str, run_id: int) -> dict[str, Any]:
    """List jobs and steps for a Gitea Actions workflow run."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_list_action_run_jobs",
            title="Gitea jobs",
            error="GITEA_TOKEN not configured",
        )
    async with GiteaClient(token) as client:
        data = await client.list_action_run_jobs(owner, repo, run_id)
    return text_result(
        tool="gitea_list_action_run_jobs",
        title="Gitea jobs",
        text=f"Jobs for run #{run_id}",
        data=data,
    )


@register_tool("gitea_list_workflows")
async def gitea_list_workflows(owner: str, repo: str) -> dict[str, Any]:
    """List Gitea Actions workflow files in a repository."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return error_result(
            tool="gitea_list_workflows", title="Gitea workflows", error="GITEA_TOKEN not configured"
        )
    async with GiteaClient(token) as client:
        data = await client.list_workflows(owner, repo)
    return text_result(
        tool="gitea_list_workflows", title="Gitea workflows", text="Workflows retrieved", data=data
    )


# ── GitHub tools ─────────────────────────────────────────────────


@register_tool("github_get_repo")
async def github_get_repo(owner: str, repo: str) -> dict[str, Any]:
    """Get GitHub repository metadata."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return error_result(
            tool="github_get_repo", title="GitHub repo", error="GITHUB_TOKEN not configured"
        )
    async with GitHubClient(token) as client:
        data = await client.get_repo(owner, repo)
    return text_result(
        tool="github_get_repo",
        title="GitHub repo",
        text=f"Repo: {data.get('full_name', 'unknown')}",
        data=data,
    )


@register_tool("github_list_branches")
async def github_list_branches(owner: str, repo: str, per_page: int = 30) -> dict[str, Any]:
    """List branches in a GitHub repository."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return error_result(
            tool="github_list_branches",
            title="GitHub branches",
            error="GITHUB_TOKEN not configured",
        )
    async with GitHubClient(token) as client:
        data = normalize_list_response(
            await client.list_branches(owner, repo, per_page=per_page),
        )
    return text_result(
        tool="github_list_branches",
        title="GitHub branches",
        text=f"Branches: {data['count']}",
        data=data,
    )


@register_tool("github_list_commits")
async def github_list_commits(
    owner: str, repo: str, sha: str | None = None, per_page: int = 30
) -> dict[str, Any]:
    """List commits in a GitHub repository."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return error_result(
            tool="github_list_commits", title="GitHub commits", error="GITHUB_TOKEN not configured"
        )
    async with GitHubClient(token) as client:
        data = normalize_list_response(
            await client.list_commits(owner, repo, sha=sha, per_page=per_page),
        )
    return text_result(
        tool="github_list_commits",
        title="GitHub commits",
        text=f"Commits: {data['count']}",
        data=data,
    )


@register_tool("github_get_file")
async def github_get_file(
    owner: str, repo: str, path: str, branch: str | None = None
) -> dict[str, Any]:
    """Get a file or directory from a GitHub repository."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return error_result(
            tool="github_get_file", title="GitHub file", error="GITHUB_TOKEN not configured"
        )
    async with GitHubClient(token) as client:
        data = await client.get_file(owner, repo, path, branch=branch)
    return text_result(tool="github_get_file", title="GitHub file", text=f"File: {path}", data=data)


@register_tool("github_list_issues")
async def github_list_issues(
    owner: str, repo: str, state: str = "open", per_page: int = 30
) -> dict[str, Any]:
    """List issues in a GitHub repository. State: open, closed, all."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return error_result(
            tool="github_list_issues", title="GitHub issues", error="GITHUB_TOKEN not configured"
        )
    async with GitHubClient(token) as client:
        data = normalize_list_response(
            await client.list_issues(owner, repo, state=state, per_page=per_page),
        )
    return text_result(
        tool="github_list_issues", title="GitHub issues", text=f"Issues: {data['count']}", data=data
    )


@register_tool("github_get_issue")
async def github_get_issue(owner: str, repo: str, issue_number: int) -> dict[str, Any]:
    """Get details of a specific GitHub issue by number."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return error_result(
            tool="github_get_issue", title="GitHub issue", error="GITHUB_TOKEN not configured"
        )
    async with GitHubClient(token) as client:
        data = await client.get_issue(owner, repo, issue_number)
    return text_result(
        tool="github_get_issue", title="GitHub issue", text=f"Issue #{issue_number}", data=data
    )


@register_tool("github_list_pull_requests")
async def github_list_pull_requests(
    owner: str, repo: str, state: str = "open", per_page: int = 30
) -> dict[str, Any]:
    """List pull requests in a GitHub repository. State: open, closed, all."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return error_result(
            tool="github_list_pull_requests",
            title="GitHub PRs",
            error="GITHUB_TOKEN not configured",
        )
    async with GitHubClient(token) as client:
        data = normalize_list_response(
            await client.list_pull_requests(owner, repo, state=state, per_page=per_page),
        )
    return text_result(
        tool="github_list_pull_requests",
        title="GitHub PRs",
        text=f"PRs: {data['count']}",
        data=data,
    )


@register_tool("github_get_pull_request")
async def github_get_pull_request(owner: str, repo: str, pull_number: int) -> dict[str, Any]:
    """Get details of a specific GitHub pull request by number."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return error_result(
            tool="github_get_pull_request", title="GitHub PR", error="GITHUB_TOKEN not configured"
        )
    async with GitHubClient(token) as client:
        data = await client.get_pull_request(owner, repo, pull_number)
    return text_result(
        tool="github_get_pull_request", title="GitHub PR", text=f"PR #{pull_number}", data=data
    )


# ── Docker tools ──────────────────────────────────────────────────


@register_tool("docker_ps")
async def docker_ps(all: bool = False, format: str | None = None, limit: int = 50) -> str:
    """List running containers. Use all=True to include stopped containers. limit: max rows (default 50)."""
    return await DockerClient().ps(all=all, format=format, limit=limit)


@register_tool("docker_images")
async def docker_images(format: str | None = None, limit: int = 50) -> str:
    """List Docker images on the host. limit: max rows (default 50)."""
    return await DockerClient().images(format=format, limit=limit)


@register_tool("docker_inspect")
async def docker_inspect(name: str) -> str:
    """Inspect a container by name or ID. Returns JSON metadata (first 500 lines)."""
    return await DockerClient().inspect(name, max_lines=500)


@register_tool("docker_logs")
async def docker_logs(container: str, tail: int = 200) -> str:
    """Fetch logs from a running container. tail: number of recent lines (1-1000, default 200)."""
    return await DockerClient().logs(container, tail=tail)


@register_tool("docker_stats")
async def docker_stats(format: str | None = None, limit: int = 50) -> str:
    """Show live resource usage statistics for all running containers. limit: max rows (default 50)."""
    return await DockerClient().stats(format=format, limit=limit)


@register_tool("docker_compose_ps")
async def docker_compose_ps(
    project_dir: str | None = None, limit: int = 50
) -> str:
    """List containers in a Docker Compose project. limit: max rows (default 50)."""
    return await DockerClient().compose_ps(project_dir=project_dir, limit=limit)


@register_tool("docker_compose_services")
async def docker_compose_services(
    project_dir: str | None = None,
) -> str:
    """List service names defined in a Docker Compose project."""
    return await DockerClient().compose_services(project_dir=project_dir)


# ── Docker write tools (Session 160) ─────────────────────────────


@register_tool("docker_start")
async def docker_start(container: str, timeout: int | None = None) -> dict[str, Any]:
    """Start a stopped container. DANGEROUS: requires confirmation via confirm_operation(token)."""
    DockerClient()._validate_container_name(container)
    summary = f"Start container {container}"
    action = _confirm_store.create_action(
        "docker_start", {"container": container, "timeout": timeout}, summary, risk="medium"
    )
    return _confirmation_response(action)


@register_tool("docker_stop")
async def docker_stop(container: str, timeout: int = 10) -> dict[str, Any]:
    """Stop a running container. DANGEROUS: requires confirmation via confirm_operation(token).
    timeout: seconds before force kill (1-120, default 10)."""
    DockerClient()._validate_container_name(container)
    summary = f"Stop container {container}"
    action = _confirm_store.create_action(
        "docker_stop", {"container": container, "timeout": timeout}, summary, risk="medium"
    )
    return _confirmation_response(action)


@register_tool("docker_restart")
async def docker_restart(container: str, timeout: int = 10) -> dict[str, Any]:
    """Restart a container. DANGEROUS: requires confirmation via confirm_operation(token).
    timeout: seconds before force kill (1-120, default 10)."""
    DockerClient()._validate_container_name(container)
    summary = f"Restart container {container}"
    action = _confirm_store.create_action(
        "docker_restart", {"container": container, "timeout": timeout}, summary, risk="medium"
    )
    return _confirmation_response(action)


@register_tool("docker_compose_up")
async def docker_compose_up(
    project_dir: str | None = None,
    services: list[str] | None = None,
    detach: bool = True,
    build: bool = False,
    timeout: int = 120,
) -> str:
    """Start services in a Docker Compose project. detach=True by default."""
    return await DockerClient().compose_up(
        project_dir=project_dir,
        services=services,
        detach=detach,
        build=build,
        timeout=timeout,
    )


@register_tool("docker_compose_restart")
async def docker_compose_restart(
    project_dir: str | None = None,
    services: list[str] | None = None,
    timeout: int = 30,
) -> str:
    """Restart services in a Docker Compose project."""
    return await DockerClient().compose_restart(
        project_dir=project_dir,
        services=services,
        timeout=timeout,
    )


@register_tool("docker_compose_build")
async def docker_compose_build(
    project_dir: str | None = None,
    services: list[str] | None = None,
    no_cache: bool = False,
    timeout: int = 300,
) -> str:
    """Build (or rebuild) services in a Docker Compose project."""
    return await DockerClient().compose_build(
        project_dir=project_dir,
        services=services,
        no_cache=no_cache,
        timeout=timeout,
    )


@register_tool("docker_compose_logs")
async def docker_compose_logs(
    project_dir: str | None = None,
    services: list[str] | None = None,
    tail: int = 100,
    follow: bool = False,
    timestamps: bool = False,
) -> str:
    """Fetch logs from services in a Docker Compose project. tail: 1-1000 lines."""
    return await DockerClient().compose_logs(
        project_dir=project_dir,
        services=services,
        tail=tail,
        follow=follow,
        timestamps=timestamps,
    )


# ── Dangerous Docker operations (Session 164) ────────────────────


async def _docker_start_impl(container: str, timeout: int | None = None) -> str:
    return await DockerClient().start(container, timeout=timeout)


async def _docker_stop_impl(container: str, timeout: int = 10) -> str:
    return await DockerClient().stop(container, timeout=timeout)


async def _docker_restart_impl(container: str, timeout: int = 10) -> str:
    return await DockerClient().restart(container, timeout=timeout)


async def _docker_rm_impl(container: str, force: bool = False) -> RunResult:
    return await DockerClient().rm(container, force=force)


async def _docker_compose_down_impl(
    project_dir: str | None = None,
    remove_orphans: bool = False,
    timeout: int = 30,
    volumes: bool = False,
) -> RunResult:
    return await DockerClient().compose_down(
        project_dir=project_dir,
        remove_orphans=remove_orphans,
        timeout=timeout,
        volumes=volumes,
    )


async def _docker_prune_impl(type: str = "container") -> RunResult:
    return await DockerClient().prune(type)


async def _docker_exec_impl(container: str, command: list[str], timeout: int = 30) -> RunResult:
    return await DockerClient().exec(container, command, timeout=timeout)


async def _docker_run_impl(
    image: str,
    command: list[str],
    container_name: str | None = None,
    timeout: int = 60,
) -> RunResult:
    return await DockerClient().run(
        image, command, container_name=container_name, timeout=timeout
    )


async def _docker_rmi_impl(images: list[str]) -> RunResult:
    return await DockerClient().rmi(images)


async def _docker_volume_rm_impl(volumes: list[str]) -> RunResult:
    return await DockerClient().volume_rm(volumes)


_CONFIRM_HANDLERS: dict[str, Callable[..., Any]] = {
    "docker_start": _docker_start_impl,
    "docker_stop": _docker_stop_impl,
    "docker_restart": _docker_restart_impl,
    "docker_rm": _docker_rm_impl,
    "docker_compose_down": _docker_compose_down_impl,
    "docker_prune": _docker_prune_impl,
    "docker_exec": _docker_exec_impl,
    "docker_run": _docker_run_impl,
    "docker_rmi": _docker_rmi_impl,
    "docker_volume_rm": _docker_volume_rm_impl,
}


def _confirmation_response(action: ConfirmAction) -> dict[str, Any]:
    remaining = max(0, int(60 - (_time.monotonic() - action.created_at)))
    return tool_success(
        tool=action.tool,
        result={
            "status": "confirmation_required",
            "action_id": action.action_id,
            "confirm_token": action.confirm_token,
            "expires_in_sec": remaining,
            "summary": action.summary,
            "risk": action.risk,
        },
        source="docker",
        dangerous=True,
    )


@register_tool("docker_rm")
async def docker_rm(container: str, force: bool = False) -> dict[str, Any]:
    """Remove a container. DANGEROUS: requires confirmation via docker_confirm(token)."""
    DockerClient()._validate_container_name(container)
    summary = f"Remove container {container}"
    action = _confirm_store.create_action(
        "docker_rm", {"container": container, "force": force}, summary
    )
    return _confirmation_response(action)


def _get_token_scopes() -> list[str]:
    raw = os.environ.get("MCP_TOKEN_SCOPES", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


@register_tool("docker_compose_down")
async def docker_compose_down(
    project_dir: str | None = None,
    remove_orphans: bool = False,
    timeout: int = 30,
    volumes: bool = False,
) -> dict[str, Any]:
    """Stop and remove a Compose stack. DANGEROUS: requires confirmation.
    With mcp:docker:admin scope: use volumes=True to also remove named volumes."""
    if volumes:
        scopes = _get_token_scopes()
        if "mcp:docker:admin" not in scopes:
            return tool_error(
                tool="docker_compose_down",
                code="DOCKER_ADMIN_SCOPE_REQUIRED",
                message="volumes=true requires mcp:docker:admin scope.",
                source="docker",
            )
    dc = DockerClient()
    dc._validate_project_dir(project_dir)
    parts = []
    if project_dir:
        parts.append(f"project={project_dir}")
    if volumes:
        parts.append("--volumes")
    summary = f"Compose down {' '.join(parts)}"
    action = _confirm_store.create_action(
        "docker_compose_down",
        {
            "project_dir": project_dir,
            "remove_orphans": remove_orphans,
            "timeout": timeout,
            "volumes": volumes,
        },
        summary,
    )
    return _confirmation_response(action)


@register_tool("docker_prune")
async def docker_prune(type: str = "container") -> dict[str, Any]:
    """Prune Docker resources. DANGEROUS: requires confirmation. Allowed types: container, image, network.
    With mcp:docker:admin scope: also volume, system."""
    scopes = _get_token_scopes()
    has_admin = "mcp:docker:admin" in scopes
    if type in ("volume", "system") and not has_admin:
        return tool_error(
            tool="docker_prune",
            code="DOCKER_ADMIN_SCOPE_REQUIRED",
            message=f"Prune type '{type}' requires mcp:docker:admin scope.",
            hint="Request admin scope or use one of: container, image, network.",
            source="docker",
        )
    try:
        DockerClient()._validate_prune_type(type, admin_scope=has_admin)
    except ValueError as e:
        return tool_error(
            tool="docker_prune",
            code="INVALID_INPUT",
            message=str(e),
            source="docker",
        )
    summary = f"Prune {type}s"
    action = _confirm_store.create_action("docker_prune", {"type": type}, summary)
    return _confirmation_response(action)


# ── Docker admin operations (Session 165) ────────────────────────


@register_tool("docker_exec")
async def docker_exec(
    container: str,
    command: list[str],
    timeout: int = 30,
) -> dict[str, Any]:
    """Execute a command inside an existing container. ADMIN: requires mcp:docker:admin scope + confirmation.

    DANGEROUS: argv is checked against a safety denylist (env, shadow, shell launchers, etc.).
    This denylist is a safety guardrail, not a security boundary. docker_exec remains
    an admin-only dangerous operation and requires both mcp:docker:admin and confirmation.
    The system does not guarantee prevention of all data exfiltration through docker_exec.
    """
    dc = DockerClient()
    try:
        dc._validate_container_name(container)
    except ValueError as e:
        return tool_error(
            tool="docker_exec",
            code="INVALID_INPUT",
            message=str(e),
            source="docker",
        )
    try:
        dc._validate_exec_argv(command)
    except ValueError as e:
        return tool_error(
            tool="docker_exec",
            code="DOCKER_EXEC_COMMAND_BLOCKED",
            message=str(e),
            hint="Use a narrower diagnostic command that does not dump environment variables, SSH keys, or shadow files.",
            source="docker",
        )
    timeout = max(1, min(timeout, 300))
    summary = f"Exec in {container}: {' '.join(command)}"
    action = _confirm_store.create_action(
        "docker_exec",
        {"container": container, "command": command, "timeout": timeout},
        summary,
    )
    return _confirmation_response(action)


@register_tool("docker_run")
async def docker_run(
    image: str,
    command: list[str],
    container_name: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """Create and start a container from an image. ADMIN: requires mcp:docker:admin scope + confirmation.

    Image must be in the MCP_DOCKER_RUN_ALLOWED_IMAGES allowlist.
    Container runs with --rm and is removed after completion.
    """
    allowed_raw = os.environ.get("MCP_DOCKER_RUN_ALLOWED_IMAGES", "").strip()
    if not allowed_raw:
        return tool_error(
            tool="docker_run",
            code="DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED",
            message="docker_run requires MCP_DOCKER_RUN_ALLOWED_IMAGES environment variable.",
            hint="Set MCP_DOCKER_RUN_ALLOWED_IMAGES with comma-separated image:tag entries.",
            source="docker",
        )
    allowed_images = {ref.strip() for ref in allowed_raw.split(",") if ref.strip()}

    dc = DockerClient()
    try:
        dc._validate_image_tag(image)
    except ValueError as e:
        return tool_error(
            tool="docker_run",
            code="DOCKER_RUN_IMAGE_INVALID",
            message=str(e),
            source="docker",
        )
    if image not in allowed_images:
        return tool_error(
            tool="docker_run",
            code="DOCKER_RUN_IMAGE_NOT_ALLOWED",
            message=f"Image '{image}' is not in the configured allowlist.",
            hint="Only images listed in MCP_DOCKER_RUN_ALLOWED_IMAGES are permitted.",
            source="docker",
        )
    if container_name:
        try:
            dc._validate_container_name(container_name)
        except ValueError as e:
            return tool_error(
                tool="docker_run",
                code="INVALID_INPUT",
                message=str(e),
                source="docker",
            )
    try:
        dc._validate_exec_argv(command)
    except ValueError as e:
        return tool_error(
            tool="docker_run",
            code="DOCKER_EXEC_COMMAND_BLOCKED",
            message=str(e),
            source="docker",
        )
    timeout = max(1, min(timeout, 600))

    summary = f"Run {image}: {' '.join(command)}"
    if container_name:
        summary += f" (name={container_name})"
    action = _confirm_store.create_action(
        "docker_run",
        {
            "image": image,
            "command": command,
            "container_name": container_name,
            "timeout": timeout,
        },
        summary,
    )
    return _confirmation_response(action)


@register_tool("docker_rmi")
async def docker_rmi(images: list[str]) -> dict[str, Any]:
    """Remove one or more Docker images (1-5). ADMIN: requires mcp:docker:admin scope + confirmation."""
    dc = DockerClient()
    if not images or len(images) > 5:
        return tool_error(
            tool="docker_rmi",
            code="DOCKER_RMI_INVALID_REFERENCE",
            message="docker_rmi accepts 1-5 images.",
            source="docker",
        )
    for img in images:
        try:
            dc._validate_image_ref(img)
        except ValueError as e:
            return tool_error(
                tool="docker_rmi",
                code="DOCKER_RMI_INVALID_REFERENCE",
                message=str(e),
                source="docker",
            )
    summary = f"Remove image(s): {', '.join(images)}"
    action = _confirm_store.create_action("docker_rmi", {"images": images}, summary)
    return _confirmation_response(action)


@register_tool("docker_volume_rm")
async def docker_volume_rm(volumes: list[str]) -> dict[str, Any]:
    """Remove one or more Docker volumes (1-5). ADMIN: requires mcp:docker:admin scope + confirmation."""
    dc = DockerClient()
    if not volumes or len(volumes) > 5:
        return tool_error(
            tool="docker_volume_rm",
            code="DOCKER_VOLUME_RM_INVALID_NAME",
            message="docker_volume_rm accepts 1-5 volumes.",
            source="docker",
        )
    for vol in volumes:
        try:
            dc._validate_volume_name(vol)
        except ValueError as e:
            return tool_error(
                tool="docker_volume_rm",
                code="DOCKER_VOLUME_RM_INVALID_NAME",
                message=str(e),
                source="docker",
            )
    summary = f"Remove volume(s): {', '.join(volumes)}"
    action = _confirm_store.create_action("docker_volume_rm", {"volumes": volumes}, summary)
    return _confirmation_response(action)


@register_tool("confirm_operation")
async def confirm_operation(token: str) -> dict[str, Any]:
    """Confirm a pending dangerous Docker operation using the one-time token from the confirmation response."""
    action, status = _confirm_store.confirm_action(token)
    if action is None:
        code = {
            ConfirmStatus.INVALID: "CONFIRM_TOKEN_INVALID",
            ConfirmStatus.EXPIRED: "CONFIRM_TOKEN_EXPIRED",
            ConfirmStatus.CONSUMED: "CONFIRM_TOKEN_CONSUMED",
        }.get(status, "INTERNAL_ERROR")
        msg = {
            ConfirmStatus.INVALID: "Invalid confirmation token",
            ConfirmStatus.EXPIRED: "Confirmation token expired (TTL 60s)",
            ConfirmStatus.CONSUMED: "Confirmation token already used",
        }.get(status, "Unknown error")
        return tool_error(
            tool="confirm_operation",
            code=code,
            message=msg,
            hint="Call the dangerous tool again to get a new token.",
            retryable=False,
            source="docker",
        )

    handler = _CONFIRM_HANDLERS.get(action.tool)
    if not handler:
        return tool_error(
            tool="confirm_operation",
            code="INTERNAL_ERROR",
            message=f"No handler for {action.tool}",
            source="docker",
        )

    try:
        result = await handler(**action.kwargs)
    except Exception as exc:
        return tool_error(
            tool=action.tool,
            code="DOCKER_COMMAND_FAILED",
            message=str(exc),
            source="docker",
            retryable=False,
        )

    if isinstance(result, dict) and "ok" in result:
        return result

    if isinstance(result, str):
        return tool_success(
            tool=action.tool,
            result={"output": result},
            source="docker",
        )

    return tool_success(
        tool=action.tool,
        result=result,
        source="docker",
    )


@register_tool("docker_pending_actions")
async def docker_pending_actions() -> dict[str, Any]:
    """List all pending dangerous Docker operations awaiting confirmation."""
    _confirm_store.cleanup_expired()
    pending = _confirm_store.list_pending()
    count = len(pending)
    return tool_success(
        tool="docker_pending_actions",
        result={"count": count, "items": pending},
        source="docker",
    )


# ── Postgres tools ────────────────────────────────────────────────


@register_tool("postgres_health")
async def postgres_health() -> str:
    """Check Postgres connectivity. Returns DB name, user, version."""
    client = _get_pg_client()
    if client is None:
        return "error: Postgres not configured (PG DSN missing)"
    try:
        info = await client.health()
        return f"ok | db={info['db']} user={info['user']} version={info['version']}"
    except Exception as e:
        return f"error: {e}"


@register_tool("postgres_list_schemas")
async def postgres_list_schemas() -> str:
    """List non-system schemas in the database."""
    client = _get_pg_client()
    if client is None:
        return "error: Postgres not configured"
    schemas = await client.list_schemas()
    if not schemas:
        return "No user schemas found"
    lines = "\n".join(f"  {s}" for s in schemas)
    return f"Schemas ({len(schemas)}):\n{lines}"


@register_tool("postgres_list_tables")
async def postgres_list_tables(schema: str = "public") -> str:
    """List tables in a schema with type and row estimate."""
    client = _get_pg_client()
    if client is None:
        return "error: Postgres not configured"
    tables = await client.list_tables(schema=schema)
    if not tables:
        return f"No tables found in schema '{schema}'"
    lines = "\n".join(
        f"  {t['table_name']:30s} {t['table_type']:15s} rows={t.get('row_estimate', '?')}"
        for t in tables
    )
    return f"Tables in '{schema}' ({len(tables)}):\n{lines}"


@register_tool("postgres_describe_table")
async def postgres_describe_table(table_name: str, schema: str = "public") -> str:
    """Describe columns of a table."""
    client = _get_pg_client()
    if client is None:
        return "error: Postgres not configured"
    columns = await client.describe_table(schema=schema, table_name=table_name)
    if not columns:
        return f"Table '{schema}.{table_name}' not found or has no columns"
    lines = "\n".join(
        f"  {c['column_name']:30s} {c['data_type']:20s} nullable={c['is_nullable']:5s} default={c.get('column_default', 'NULL')}"
        for c in columns
    )
    return f"Columns of '{schema}.{table_name}' ({len(columns)}):\n{lines}"


@register_tool("postgres_select")
async def postgres_select(sql: str) -> str:
    """Execute a read-only SELECT or WITH query with enforced LIMIT 1000.
    Multi-statement not allowed, DDL/DML blocked."""
    client = _get_pg_client()
    if client is None:
        return "error: Postgres not configured"
    try:
        rows = await client.execute(sql)
    except ValueError as e:
        return f"error: {e}"
    except Exception as e:
        return f"error: query failed: {e}"
    import json

    return json.dumps(rows, default=str, ensure_ascii=False)


@register_tool("postgres_vector_status")
async def postgres_vector_status() -> str:
    """Check if pgvector extension is installed and its version."""
    client = _get_pg_client()
    if client is None:
        return "error: Postgres not configured"
    info = await client.vector_status()
    if info["installed"]:
        return f"pgvector is installed (version {info['version']})"
    return "pgvector is NOT installed"


# ── Context7 tools ────────────────────────────────────────────────


@register_tool("resolve_library_id")
async def resolve_library_id(query: str, libraryName: str) -> str:
    """Resolve a package/product name to a Context7-compatible library ID."""
    return await _call_context7_upstream(
        "resolve-library-id", {"query": query, "libraryName": libraryName}
    )


@register_tool("query_docs")
async def query_docs(libraryId: str, query: str) -> str:
    """Query Context7 for documentation on a resolved library."""
    return await _call_context7_upstream("query-docs", {"libraryId": libraryId, "query": query})


# ── Agent Handoff v2 tools ──────────────────────────────────────────


@register_tool("project_write_agent_task")
def gateway_project_write_agent_task(
    project: str,
    task_id: str,
    agent: str,
    task: str,
    scope: str = "",
    allowed_files: str | None = None,
    forbidden_files: str | None = None,
    required_checks: str | None = None,
    acceptance_criteria: str | None = None,
    commit_message: str | None = None,
    constraints: str | None = None,
    worktree_path: str | None = None,
) -> dict[str, Any]:
    """Write task.json + current-plan.md to .ai-bridge/tasks/<task_id>/."""

    def _fn() -> dict[str, Any]:
        return _write_agent_task(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            agent=agent,
            task=task,
            scope=scope,
            allowed_files=_split_lines(allowed_files),
            forbidden_files=_split_lines(forbidden_files),
            required_checks=_split_lines(required_checks),
            acceptance_criteria=_split_lines(acceptance_criteria),
            commit_message=commit_message,
            constraints=constraints,
            worktree_path=worktree_path,
        )

    return run_tool(
        tool="project_write_agent_task",
        title="Write agent task",
        fn=_fn,
        success_text="Wrote agent task.",
    )


@register_tool("project_read_agent_status")
def gateway_project_read_agent_status(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-status.md."""
    return run_tool(
        tool="project_read_agent_status",
        title="Read agent status",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            filename="agent-status.md",
        ),
        success_text="Read agent status.",
    )


@register_tool("project_read_agent_report")
def gateway_project_read_agent_report(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-report.md."""
    return run_tool(
        tool="project_read_agent_report",
        title="Read agent report",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            filename="agent-report.md",
        ),
        success_text="Read agent report.",
    )


@register_tool("project_read_agent_diff")
def gateway_project_read_agent_diff(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/implementation-diff.patch."""
    return run_tool(
        tool="project_read_agent_diff",
        title="Read agent diff",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            filename="implementation-diff.patch",
        ),
        success_text="Read agent diff.",
    )


@register_tool("project_list_agent_tasks")
def gateway_project_list_agent_tasks(project: str) -> dict[str, Any]:
    """List task directories under .ai-bridge/tasks/."""
    return run_tool(
        tool="project_list_agent_tasks",
        title="List agent tasks",
        fn=lambda: _list_agent_tasks(
            lambda p, c: run_project_command(client, p, c),
            project=project,
        ),
        success_text="Listed agent tasks.",
    )


@register_tool("project_archive_agent_task")
def gateway_project_archive_agent_task(project: str, task_id: str) -> dict[str, Any]:
    """Move .ai-bridge/tasks/<task_id>/ -> .ai-bridge/archive/<task_id>/."""
    return run_tool(
        tool="project_archive_agent_task",
        title="Archive agent task",
        fn=lambda: _archive_agent_task(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
        ),
        success_text="Archived agent task.",
    )


@register_tool("project_run_opencode")
def project_run_opencode(
    project: str,
    task_id: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Execute an existing handoff task via agent CLI.
    Requires write mode handoff or full."""
    from write_modes import assert_handoff_write_allowed

    assert_handoff_write_allowed()
    return run_tool(
        tool="project_run_opencode",
        title="Run opencode task",
        fn=lambda: _project_run_opencode(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            model=model,
        ),
        success_text="Submitted opencode task.",
    )


@register_tool("project_run_mimo")
def gateway_project_run_mimo(
    project: str,
    task_id: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Execute an existing handoff task via Mimo CLI inside a disposable git worktree.
    Requires write mode handoff or full. See spec for 11 pre-flight guards.
    Mimo runs with --dangerously-skip-permissions — only valid in disposable worktrees."""
    from write_modes import assert_handoff_write_allowed

    assert_handoff_write_allowed()
    return run_tool(
        tool="project_run_mimo",
        title="Run mimo task",
        fn=lambda: _project_run_mimo(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            model=model,
        ),
        success_text="Submitted mimo task.",
    )


@register_tool("project_run_agent")
def gateway_project_run_agent(
    project: str,
    task_id: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Execute a handoff task via the agent backend router — auto-selects OpenCode or Mimo.
    Requires write mode handoff or full. Router enabled by MCP_AGENT_BACKEND_ROUTER_ENABLED.
    Task must have task.json with agent='auto' and worktree_path if mimo may be selected."""
    from write_modes import assert_handoff_write_allowed

    assert_handoff_write_allowed()
    return run_tool(
        tool="project_run_agent",
        title="Run agent task (router)",
        fn=lambda: _project_run_agent(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            model=model,
            router=_agent_router,
        ),
        success_text="Submitted agent task via router.",
    )


# ── Tools Manifest ──────────────────────────────────────────────

from tools_manifest import build_manifest as _build_manifest  # noqa: E402

_scope_enforcement = os.environ.get("MCP_SCOPE_ENFORCEMENT", "off").strip().lower()
if _scope_enforcement not in ("off", "audit", "enforce"):
    _scope_enforcement = "off"


@register_tool("tools_manifest")
def gateway_tools_manifest() -> dict[str, Any]:
    """Return a read-only manifest of all registered tools, modes, scopes, and access profiles.
    No secrets, no env dumps, no network calls, no tool execution."""
    return _run_gateway(
        tool="tools_manifest",
        fn=lambda: _build_manifest(
            registered_tools=mcp._tool_manager.list_tools(),
            scope_enforcement=_scope_enforcement,
        ),
    )


# ── Main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
