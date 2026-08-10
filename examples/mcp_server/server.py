"""Experimental MCP server for agent-ssh-gateway.

This server is intentionally kept outside the gateway core.
"""
# ruff: noqa: E402 — late imports intentional for --reload compat

from __future__ import annotations

import json
import os
import shutil
import sys
import time as _time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

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
from command_policy import CommandPolicyError
from docker_confirm import ConfirmStore
from gateway_client import GatewayClient, GatewayClientError
from mcp.server.fastmcp import FastMCP
from mcp_client_tools import (
    MAX_GLOB_RESULTS,
    commit_head,
    current_branch,
    find_files,
    git_add,
    git_commit,
    git_diff,
    git_diff_cached,
    git_diff_stat,
    git_push,
    git_status,
    info,
    list_files,
    list_tree,
    read_file,
    read_handoff,
    recent_commits,
    remotes,
    run_compileall,
    run_lint,
    run_mypy,
    run_project_command,
    run_pytest,
    run_ruff,
    run_tests,
    search_text,
    show_changes,
    show_file_diff,
    show_handoff_status,
    tree,
    working_directory,
    write_handoff_plan,
)
from opencode_tools import (
    project_run_opencode as _project_run_opencode,
)
from self_test import run_self_test
from tool_results import (
    build_command_result,
    text_result,
    tool_error,
    tool_success,
    validate_pagination,
)
from write_modes import WriteModeError, WritePermissionError

from examples.mcp_client_remote.fleet.context7_server import (
    _call_upstream as _call_context7_upstream,  # noqa: F401  (facade: tests monkeypatch this name)
)
from examples.mcp_client_remote.fleet.docker_client import (
    DockerClient,  # noqa: F401  (facade: tests patch this name)
)
from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_client_remote.fleet.github_client import (
    GitHubClient,
    normalize_list_response,
)
from examples.mcp_client_remote.fleet.postgres_client import PostgresClient
from examples.mcp_client_remote.fleet.shared import list_pagination_meta, minimize_issue_payload

# OAuth provider and settings
from examples.mcp_server.latency_metrics import get_tracker
from examples.mcp_server.mcp_audit import McpAuditEvent, get_audit_logger
from examples.mcp_server.mcp_infra import gateway_errors, runtime, tool_registry
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
            # health (the only tool this credential exists to call) only
            # requires "mcp:read" (see tool_scopes.py) -- granting the full
            # SUPPORTED_SCOPES set (admin/execute/docker included) made a
            # credential whose entire purpose is an unauthenticated-adjacent
            # liveness probe as powerful as any operator token if it leaked.
            scopes=["mcp:read"],
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
        from examples.mcp_server.tool_scopes import get_profile_scopes as _get_profile_scopes

        for _token_str, _profile in _extra_tokens_all.items():
            _at_hash = _hash_tok(_token_str)
            _profile_scopes = _get_profile_scopes(_profile)
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
            issuer_url=AnyHttpUrl(os.environ.get("MCP_ISSUER_URL", "https://gateway.example.com")),
            resource_server_url=AnyHttpUrl(
                os.environ.get("MCP_RESOURCE_URL", "https://gateway.example.com/mcp")
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

# ── ClientStore: load persisted dynamically-registered OAuth clients ──
# Without this, GatewayOAuthProvider._clients was purely in-memory --
# every restart forgot every client a connector had ever registered via
# DCR, so the next reconnection attempt failed with "Client ID ... not
# found" even though nothing about the connection itself had changed.
if _auth_provider is not None and MCP_AUTH_MODE == "oauth":
    try:
        from examples.mcp_server.client_store import ClientStore

        _client_store = ClientStore()
        _auth_provider.set_client_store(_client_store)
        _clients_loaded = _auth_provider.load_clients()
        if _clients_loaded:
            print(
                f"  ClientStore: {_clients_loaded} clients loaded from {_client_store._path}",
                file=sys.stderr,
            )
    except Exception as _exc:
        print(f"  ClientStore: error loading clients: {_exc}", file=sys.stderr)

# ── Agent Backend Router ─────────────────────────────────────────────
_agent_router: AgentBackendRouter | None = None
if os.environ.get("MCP_AGENT_BACKEND_ROUTER_ENABLED", "false").strip().lower() == "true":
    try:
        from examples.mcp_server.agent_backend_router import AgentBackendRouter

        _agent_router = AgentBackendRouter(
            fallback_order=[
                x.strip()
                for x in os.environ.get("MCP_BACKEND_FALLBACK_ORDER", "opencode").split(",")
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


def _run_gateway(
    tool: str,
    fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Execute a read-only gateway tool with canonical response envelope."""
    try:
        data = fn()
    except (GatewayClientError, CommandPolicyError, WritePermissionError, WriteModeError) as exc:
        if isinstance(exc, CommandPolicyError | WritePermissionError | WriteModeError):
            code = "POLICY_VIOLATION"
            retryable = False
            # Emit structured audit event
            try:
                audit_logger = get_audit_logger()
                audit_logger.append(McpAuditEvent(
                    event_type="mcp.command_denied",
                    tool=tool,
                    action="gateway_call",
                    decision="deny",
                    reason=str(exc),
                    error_code=code,
                ))
            except Exception:
                pass  # audit failure must not change tool behavior
        else:
            code, retryable = _classify_gateway_error(exc)
        message = _gateway_error_message(exc) if isinstance(exc, GatewayClientError) else str(exc)
        hint = _gateway_error_hint(exc, code) if isinstance(exc, GatewayClientError) else None
        return tool_error(
            tool=tool,
            code=code,
            message=message,
            retryable=retryable,
            hint=hint,
            source="gateway",
            read_only=True,
        )
    except ValueError as exc:
        return tool_error(
            tool=tool,
            code="INVALID_INPUT",
            message=str(exc),
            retryable=False,
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

    # Fallback: read git HEAD from source tree (MCP server runs on host, not in Docker)
    if mcp_build_sha == "unknown" or not mcp_build_time:
        try:
            import subprocess as _sp

            _git_dir = Path(__file__).resolve().parents[2]
            if mcp_build_sha == "unknown":
                _sha = _sp.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=str(_git_dir),
                    stderr=_sp.DEVNULL,
                    timeout=2,
                ).decode().strip()
                if _sha:
                    mcp_build_sha = _sha
            if not mcp_build_time:
                _ts = _sp.check_output(
                    ["git", "log", "-1", "--format=%ci"],
                    cwd=str(_git_dir),
                    stderr=_sp.DEVNULL,
                    timeout=2,
                ).decode().strip()
                if _ts:
                    mcp_build_time = _ts
        except Exception:
            pass
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


@register_tool("project_list")
@instrumented("project_list")
def gateway_project_list() -> dict[str, Any]:
    """List all registered projects with their type, description and tags."""
    registry = _get_workspace_registry()
    projects = registry.list_projects()
    return tool_success(
        tool="project_list",
        result={
            "count": len(projects),
            "projects": projects,
        },
    )


@register_tool("scan_command")
@instrumented("scan_command")
def gateway_scan_command(command: str) -> dict[str, Any]:
    """Scan a command string against all destructive patterns and return findings.

    Unlike the policy engine (which returns allow/block based on profile),
    scan_command returns ALL matching destructive patterns regardless of
    profile — for introspection, debugging, and CI use. The scan is
    informational: enforcement happens in the policy gates. Commands are
    normalized first (shell variable assignments resolved, nested inline
    scripts like ``bash -c '...'`` extracted) so obfuscated destructive
    commands are not silently reported as clean.
    """
    from app.command_policy import scan_command as _scan

    report = _scan(command)
    return tool_success(
        tool="scan_command",
        result={
            "findings": [
                {
                    "pattern_name": f.pattern_name,
                    "severity": f.severity,
                    "reason": f.reason,
                    "suggestion": f.suggestion,
                }
                for f in report.findings
            ],
            "total": report.total,
        },
    )


@register_tool("simulate")
@instrumented("simulate")
def gateway_simulate(
    content: str,
    mode: str = "audit",
    profile: str = "default",
    agent: str | None = None,
    project: str | None = None,
    max_lines: int | None = None,
) -> dict[str, Any]:
    """Replay commands through the policy engine for testing.

    Parses multiple input formats (plain text, hook JSON, DCG decision log),
    evaluates each command against the policy, and returns structured results.

    Args:
        content: Command log content (one command per line, or hook JSON).
        mode: Policy mode (audit, enforce, ask). Default: audit (no blocking).
        profile: Policy profile to evaluate against. Default: default.
        agent: Optional agent name for agent-specific mode overrides.
        project: Optional project name.
        max_lines: Maximum lines to process. Default: no limit.
    """
    from app.simulate import simulate as _simulate

    limits = None
    if max_lines is not None:
        from app.simulate import SimLimits
        limits = SimLimits(max_lines=max_lines)

    result = _simulate(
        content,
        mode=mode,
        profile=profile,
        agent=agent,
        project=project,
        limits=limits,
    )

    return tool_success(
        tool="simulate",
        result=result,
    )


@register_tool("scan_file")
@instrumented("scan_file")
def gateway_scan_file(project: str, path: str) -> dict[str, Any]:
    """Scan a file for destructive command patterns.

    Reads the file through the project workspace and runs each line through
    the destructive pattern scanner. Returns findings with line numbers.
    """
    from app.command_policy import scan_command as _scan

    file_result = read_file(client, project=project, path=path)
    if not file_result.get("ok"):
        return tool_error(
            tool="scan_file",
            code="READ_ERROR",
            message=f"Failed to read file: {file_result.get('error', {}).get('message', 'unknown error')}",
        )
    content = file_result.get("result", {}).get("content", "")
    if not isinstance(content, str):
        return tool_success(
            tool="scan_file",
            result={"path": path, "lines_scanned": 0, "findings": [], "total": 0},
        )

    lines = content.splitlines()
    findings: list[dict[str, Any]] = []
    for lineno, line in enumerate(lines, 1):
        report = _scan(line)
        for f in report.findings:
            findings.append({
                "line": lineno,
                "content": line.strip(),
                "pattern_name": f.pattern_name,
                "severity": f.severity,
                "reason": f.reason,
                "suggestion": f.suggestion,
            })

    return tool_success(
        tool="scan_file",
        result={
            "path": path,
            "lines_scanned": len(lines),
            "findings": findings,
            "total": len(findings),
        },
    )


@register_tool("project_scan_destructive")
@instrumented("project_scan_destructive")
def gateway_project_scan_destructive(
    project: str,
    pattern: str = "*",
    max_files: int = 100,
    fmt: str = "dict",
) -> dict[str, Any]:
    """Scan a project directory for destructive command patterns.

    Walks files matching ``pattern`` (glob), skips binary/vendor/cache,
    reads each file, runs ``scan_command`` per line, and returns findings
    grouped by file path.

    Parameters:
        project: Registered project name.
        pattern: Glob pattern to filter files (default ``*``).
        max_files: Maximum files to scan (default 100).
        fmt: Output format — ``dict`` (default), ``json``, or ``sarif``.
    """
    from app.workspace.scan_project import scan_project as _scan

    try:
        result = _scan(project, pattern=pattern, max_files=max_files, fmt=fmt)
    except Exception as exc:
        return tool_error(
            tool="project_scan_destructive",
            code="SCAN_ERROR",
            message=str(exc),
        )

    return tool_success(
        tool="project_scan_destructive",
        result=result,
    )


@register_tool("explain_pattern")
@instrumented("explain_pattern")
def gateway_explain_pattern(pattern_name: str) -> dict[str, Any]:
    """Look up a destructive pattern by name and return its full details.

    Searches across all 9 registered packs (docker, filesystem, kubernetes,
    cloud, database, git, firewall, loadbalancer, system).
    """
    from app.packs.registry import get_registry as _get_registry

    registry = _get_registry()
    for pack in registry.all_packs:
        for dp in pack.destructive_patterns:
            if dp.name == pattern_name:
                return tool_success(
                    tool="explain_pattern",
                    result={
                        "name": dp.name,
                        "pack": {"id": pack.id, "name": pack.name},
                        "regex": dp.regex,
                        "severity": dp.severity,
                        "reason": dp.reason,
                        "description": dp.description,
                        "suggestions": [
                            {"command": s.command, "description": s.description}
                            for s in dp.suggestions
                        ],
                    },
                )

    return tool_error(
        tool="explain_pattern",
        code="PATTERN_NOT_FOUND",
        message=f"Pattern '{pattern_name}' not found in any pack",
    )


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


@register_tool("apply_patch")
@instrumented("apply_patch")
def gateway_apply_patch(
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
            tool="apply_patch",
            code="TOOL_EXECUTION_FAILED",
            message=str(e),
        )
    return tool_success(
        tool="apply_patch",
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


@register_tool("repo_status")
def gateway_repo_status(
    session_id: str | None = None, project: str | None = None
) -> dict[str, Any]:
    """Collect basic repository status using read-only commands.

    Args:
        session_id: Optional existing session ID. No new session is ever
            created here -- there's no host/credentials in this tool's
            signature to connect with. When omitted, falls back to the
            client's configured default session (GATEWAY_SESSION_ID); if
            that isn't set either, the call fails with
            "GATEWAY_SESSION_ID is required".
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
def gateway_working_directory(project: str) -> dict[str, Any]:
    """Print working directory within MCP_GATEWAY_PROJECT_ROOT/{project}."""
    return _run_gateway(
        tool="working_directory",
        fn=lambda: working_directory(client, project),
    )


@register_tool("info")
def gateway_info(project: str) -> dict[str, Any]:
    """Return resolved project metadata for a configured project name.
    Read-only. Does not execute user-provided shell commands.
    """
    return _run_gateway(
        tool="info",
        fn=lambda: info(client, project),
    )


@register_tool("git_status")
def gateway_git_status(project: str) -> dict[str, Any]:
    """Show git working tree status within a project directory."""
    return run_tool(
        tool="git_status",
        title="git status",
        fn=lambda: git_status(client, project),
        success_text="Collected project git status.",
    )


@register_tool("recent_commits")
def gateway_recent_commits(project: str) -> dict[str, Any]:
    """List recent commits within a project (git log --oneline -10)."""
    return run_tool(
        tool="recent_commits",
        title="recent commits",
        fn=lambda: recent_commits(client, project),
        success_text="Collected project recent commits.",
    )


@register_tool("git_diff_stat")
def gateway_git_diff_stat(project: str) -> dict[str, Any]:
    """Show uncommitted diff stat within a project."""
    return run_tool(
        tool="git_diff_stat",
        title="git diff stat",
        fn=lambda: git_diff_stat(client, project),
        success_text="Collected project git diff stat.",
    )


@register_tool("show_changes")
def gateway_show_changes(project: str) -> dict[str, Any]:
    """Show combined git status and diff stat within a project."""
    return run_tool(
        tool="show_changes",
        title="show changes",
        fn=lambda: show_changes(client, project),
        success_text="Collected project change summary.",
    )


@register_tool("git_add")
def gateway_git_add(project: str, paths: list[str]) -> dict[str, Any]:
    """Stage files for commit (git add)."""
    return run_tool(
        tool="git_add",
        title="git add",
        fn=lambda: git_add(client, project, paths),
        success_text="Staged files.",
    )


@register_tool("git_commit")
def gateway_git_commit(project: str, message: str) -> dict[str, Any]:
    """Commit staged changes with a message (git commit -m)."""
    return run_tool(
        tool="git_commit",
        title="git commit",
        fn=lambda: git_commit(client, project, message),
        success_text="Committed changes.",
    )


@register_tool("git_push")
def gateway_git_push(
    project: str,
    remote: str = "origin",
    branch: str | None = None,
) -> dict[str, Any]:
    """Push commits to remote (git push)."""
    return run_tool(
        tool="git_push",
        title="git push",
        fn=lambda: git_push(client, project, remote=remote, branch=branch),
        success_text="Pushed to remote.",
    )


@register_tool("run_tests")
def gateway_run_tests(project: str) -> dict[str, Any]:
    """Submit the project's full test suite (pytest -q) as a background job.

    Returns immediately with the job_id (status running); poll with
    gateway_job_status / gateway_job_result. Use run_pytest for a targeted
    synchronous run.
    """
    return run_tool(
        tool="run_tests",
        title="run tests",
        fn=lambda: run_tests(client, project),
        success_text="Submitted project test suite.",
    )


@register_tool("run_lint")
def gateway_run_lint(project: str) -> dict[str, Any]:
    """Run ruff linter within a project."""
    return run_tool(
        tool="run_lint",
        title="run lint",
        fn=lambda: run_lint(client, project),
        success_text="Ran project lint checks.",
    )


@register_tool("run_compileall")
def gateway_run_compileall(project: str, target: list[str] | str | None = None) -> dict[str, Any]:
    """Run Python compileall within a project (syntax-check the tree).

    ``target`` may be a single file/dir string or a list of targets.
    Omitting it walks the whole project (service dirs pruned).
    """
    return run_tool(
        tool="run_compileall",
        title="run compileall",
        fn=lambda: run_compileall(client, project, target),
        success_text="Ran project Python compileall.",
    )


# ── Phase 2 project tools ─────────────────────────────────────────


@register_tool("read_file")
def gateway_read_file(project: str, path: str) -> dict[str, Any]:
    """Read a file within MCP_GATEWAY_PROJECT_ROOT/{project}."""
    return run_tool(
        tool="read_file",
        title="read file",
        fn=lambda: read_file(client, project, path),
        success_text="Read project file.",
    )


@register_tool("search_text")
def gateway_search_text(
    project: str, query: str, glob: str | None = None
) -> dict[str, Any]:
    """Search for text across project files using grep."""
    return run_tool(
        tool="search_text",
        title="search text",
        fn=lambda: search_text(client, project, query, glob=glob),
        success_text="Searched project text.",
    )


@register_tool("find_files")
def gateway_find_files(project: str, pattern: str) -> dict[str, Any]:
    """Find files matching a glob pattern in the project."""
    return run_tool(
        tool="find_files",
        title="find files",
        fn=lambda: find_files(project, pattern),
        success_text="Found project files.",
    )


@register_tool("list_files")
def gateway_list_files(project: str, pattern: str) -> dict[str, Any]:
    """List files matching a glob pattern using Python pathlib — no shell execution."""
    return _run_gateway(
        tool="list_files",
        fn=lambda: list_files(client, project, pattern),
    )


@register_tool("tree")
def gateway_tree(
    project: str,
    depth: int = 2,
    glob: str | None = None,
    max_results: int = MAX_GLOB_RESULTS,
) -> dict[str, Any]:
    """List project directory tree up to a given depth."""
    return _run_gateway(
        tool="tree",
        fn=lambda: tree(client, project, depth=depth, glob=glob, max_results=max_results),
    )


@register_tool("list_tree")
def gateway_list_tree(
    project: str,
    depth: int = 2,
    max_results: int = MAX_GLOB_RESULTS,
) -> dict[str, Any]:
    """List project directory tree using Python pathlib — no shell execution."""
    return _run_gateway(
        tool="list_tree",
        fn=lambda: list_tree(client, project, depth=depth, max_results=max_results),
    )


@register_tool("git_diff")
def gateway_git_diff(project: str, path: str | None = None) -> dict[str, Any]:
    """Show git diff (uncommitted changes) in a project."""
    return run_tool(
        tool="git_diff",
        title="git diff",
        fn=lambda: git_diff(client, project, path=path),
        success_text="Collected project git diff.",
    )


@register_tool("git_diff_cached")
def gateway_git_diff_cached(project: str, path: str | None = None) -> dict[str, Any]:
    """Show git --cached diff (staged changes) in a project."""
    return run_tool(
        tool="git_diff_cached",
        title="git diff cached",
        fn=lambda: git_diff_cached(client, project, path=path),
        success_text="Collected project staged diff.",
    )


@register_tool("show_file_diff")
def gateway_show_file_diff(project: str, path: str) -> dict[str, Any]:
    """Show uncommitted diff for a specific file in the project."""
    return run_tool(
        tool="show_file_diff",
        title="show file diff",
        fn=lambda: show_file_diff(client, project, path),
        success_text="Collected file diff.",
    )


@register_tool("run_pytest")
def gateway_run_pytest(project: str, target: list[str] | str | None = None) -> dict[str, Any]:
    """Run pytest on one or more targets within the project.

    ``target`` may be a single file/dir string or a list of targets.
    Omitting it runs the whole suite.
    """
    return run_tool(
        tool="run_pytest",
        title="run pytest",
        fn=lambda: run_pytest(client, project, target),
        success_text="Ran project pytest.",
    )


@register_tool("run_ruff")
def gateway_run_ruff(project: str, target: list[str] | str | None = None) -> dict[str, Any]:
    """Run ruff linter on one or more targets within the project."""
    return run_tool(
        tool="run_ruff",
        title="run ruff",
        fn=lambda: run_ruff(client, project, target),
        success_text="Ran project ruff.",
    )


@register_tool("run_mypy")
def gateway_run_mypy(project: str, target: list[str] | str | None = None) -> dict[str, Any]:
    """Run mypy type checks on one or more targets within the project."""
    return run_tool(
        tool="run_mypy",
        title="run mypy",
        fn=lambda: run_mypy(client, project, target),
        success_text="Ran project mypy.",
    )


@register_tool("remotes")
def gateway_remotes(project: str) -> dict[str, Any]:
    """List git remotes for the project."""
    return run_tool(
        tool="remotes",
        title="remotes",
        fn=lambda: remotes(client, project),
        success_text="Collected project remotes.",
    )


@register_tool("current_branch")
def gateway_current_branch(project: str) -> dict[str, Any]:
    """Show current git branch for the project."""
    return run_tool(
        tool="current_branch",
        title="current branch",
        fn=lambda: current_branch(client, project),
        success_text="Collected project current branch.",
    )


@register_tool("commit_head")
def gateway_commit_head(project: str) -> dict[str, Any]:
    """Show HEAD commit SHA for the project."""
    return run_tool(
        tool="commit_head",
        title="commit HEAD",
        fn=lambda: commit_head(client, project),
        success_text="Collected project HEAD commit.",
    )


@register_tool("read_handoff")
def gateway_read_handoff(project: str) -> dict[str, Any]:
    """Read .ai-bridge handoff files for a project."""
    return run_tool(
        tool="read_handoff",
        title="read handoff",
        fn=lambda: read_handoff(client, project),
        success_text="Read project handoff.",
    )


@register_tool("write_handoff_plan")
def gateway_write_handoff_plan(
    project: str, task: str, agent: str = "opencode", notes: str | None = None
) -> dict[str, Any]:
    """Write .ai-bridge/current-plan.md for a project (requires MCP_GATEWAY_WRITE_MODE=handoff)."""
    return run_tool(
        tool="write_handoff_plan",
        title="write handoff",
        fn=lambda: write_handoff_plan(client, project, task, agent=agent, notes=notes),
        success_text="Wrote project handoff plan.",
    )


@register_tool("show_handoff_status")
def gateway_show_handoff_status(project: str) -> dict[str, Any]:
    """Show .ai-bridge file listing for a project."""
    return run_tool(
        tool="show_handoff_status",
        title="handoff status",
        fn=lambda: show_handoff_status(client, project),
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
        tool="latency_report",
        result=get_tracker().summary(),
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
        tool="diagnostics_latency",
        result={
            "mcp": mcp_summary,
            "gateway": gw_data,
        },
    )


# ── Gitea/GitHub tools ───────────────────────────────────────────


def _remote_api_error(tool: str, source: str, exc: Exception) -> dict[str, Any]:
    """Map a GiteaClient/GitHubClient exception to a Contract v1 error.

    Both clients raise ValueError (bad endpoint/owner/repo/path input),
    PermissionError (401/403 from the remote API), httpx.HTTPStatusError
    (any other non-2xx, e.g. 404 for a typo'd repo/issue number), or
    httpx.TransportError (connect/read/write timeout, DNS failure,
    connection refused -- no HTTP response was ever received) -- see
    gitea_client.py/github_client.py's _get(). Their messages are already
    scrubbed of the resolved base URL by those clients.

    httpx.TransportError used to fall through to the generic
    INTERNAL_ERROR/retryable=False branch below -- P2 audit finding: a
    transient network problem (Gitea/GitHub briefly unreachable) looked
    identical to an internal program defect, and retryable=False told a
    calling agent not to bother retrying a condition that was, in fact,
    exactly the kind of thing a retry fixes.
    """
    if isinstance(exc, ValueError):
        return tool_error(tool=tool, code="INVALID_INPUT", message=str(exc), source=source)
    if isinstance(exc, PermissionError):
        return tool_error(tool=tool, code="AUTH_ERROR", message=str(exc), source=source)
    if isinstance(exc, httpx.HTTPStatusError):
        return tool_error(
            tool=tool,
            code="REMOTE_API_ERROR",
            message=str(exc),
            hint="Check that owner/repo/number exist and the token has access.",
            source=source,
        )
    if isinstance(exc, httpx.TransportError):
        return tool_error(
            tool=tool,
            code="REMOTE_UNAVAILABLE",
            message=str(exc),
            retryable=True,
            hint="The remote API host did not respond -- transient network issue, retry later.",
            source=source,
        )
    return tool_error(tool=tool, code="INTERNAL_ERROR", message=str(exc), source=source)


def _minimize_gitea_repo(data: dict[str, Any]) -> dict[str, Any]:
    """Trim a Gitea repo payload to non-PII fields.

    The raw Gitea API response embeds the full owner user object, including
    their email address, in every repo lookup -- unnecessary for the tool's
    purpose and a PII leak. Keep only login/name/visibility/default_branch/
    permissions/counters/topics.
    """
    owner = data.get("owner") or {}
    if data.get("private"):
        visibility = "private"
    elif data.get("internal"):
        visibility = "internal"
    else:
        visibility = "public"
    return {
        "owner": {"login": owner.get("login")},
        "name": data.get("name"),
        "full_name": data.get("full_name"),
        "description": data.get("description"),
        "visibility": visibility,
        "default_branch": data.get("default_branch"),
        "permissions": data.get("permissions"),
        "counters": {
            "stars": data.get("stars_count"),
            "forks": data.get("forks_count"),
            "watchers": data.get("watchers_count"),
            "open_issues": data.get("open_issues_count"),
        },
        "topics": data.get("topics", []),
        "archived": data.get("archived"),
        "html_url": data.get("html_url"),
    }


def _minimize_github_repo(data: dict[str, Any]) -> dict[str, Any]:
    """Trim a GitHub repo payload to non-PII fields (mirrors _minimize_gitea_repo)."""
    owner = data.get("owner") or {}
    return {
        "owner": {"login": owner.get("login")},
        "name": data.get("name"),
        "full_name": data.get("full_name"),
        "description": data.get("description"),
        "visibility": data.get("visibility") or ("private" if data.get("private") else "public"),
        "default_branch": data.get("default_branch"),
        "permissions": data.get("permissions"),
        "counters": {
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "watchers": data.get("watchers_count"),
            "open_issues": data.get("open_issues_count"),
        },
        "topics": data.get("topics", []),
        "archived": data.get("archived"),
        "html_url": data.get("html_url"),
    }


@register_tool("gitea_get_repo")
async def gitea_get_repo(owner: str, repo: str) -> dict[str, Any]:
    """Get Gitea repository metadata (login, visibility, default branch, permissions, counters, topics)."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_repo",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with GiteaClient(token) as client:
            data = await client.get_repo(owner, repo)
    except Exception as exc:
        return _remote_api_error("gitea_get_repo", "gitea", exc)
    return tool_success("gitea_get_repo", result=_minimize_gitea_repo(data), source="gitea")


@register_tool("gitea_list_branches")
async def gitea_list_branches(owner: str, repo: str, limit: int = 30) -> dict[str, Any]:
    """List branches in a Gitea repository."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_branches",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        validate_pagination(limit, "limit")
        async with GiteaClient(token) as client:
            raw = await client.list_branches(owner, repo, limit=limit)
            data = normalize_list_response(raw, meta=list_pagination_meta(len(raw), limit))
    except Exception as exc:
        return _remote_api_error("gitea_list_branches", "gitea", exc)
    return tool_success("gitea_list_branches", result=data, source="gitea")


@register_tool("gitea_list_commits")
async def gitea_list_commits(
    owner: str, repo: str, sha: str | None = None, limit: int = 30
) -> dict[str, Any]:
    """List commits in a Gitea repository. Optionally filter by branch SHA."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_commits",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        validate_pagination(limit, "limit")
        async with GiteaClient(token) as client:
            raw = await client.list_commits(owner, repo, sha=sha, limit=limit)
            data = normalize_list_response(raw, meta=list_pagination_meta(len(raw), limit))
    except Exception as exc:
        return _remote_api_error("gitea_list_commits", "gitea", exc)
    return tool_success("gitea_list_commits", result=data, source="gitea")


@register_tool("gitea_get_file")
async def gitea_get_file(
    owner: str, repo: str, path: str = "", branch: str | None = None
) -> dict[str, Any]:
    """Get a file or directory from a Gitea repository. Omit path (or
    pass "") to list the repository root."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_file",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with GiteaClient(token) as client:
            data = await client.get_file(owner, repo, path, branch=branch)
    except Exception as exc:
        return _remote_api_error("gitea_get_file", "gitea", exc)
    return tool_success("gitea_get_file", result=data, source="gitea")


@register_tool("gitea_list_issues")
async def gitea_list_issues(
    owner: str, repo: str, state: str = "open", limit: int = 30
) -> dict[str, Any]:
    """List issues in a Gitea repository. State: open, closed, all."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_issues",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        validate_pagination(limit, "limit")
        async with GiteaClient(token) as client:
            raw = await client.list_issues(owner, repo, state=state, limit=limit)
            data = normalize_list_response(
                [minimize_issue_payload(i, provider="gitea") for i in raw],
                meta=list_pagination_meta(len(raw), limit),
            )
    except Exception as exc:
        return _remote_api_error("gitea_list_issues", "gitea", exc)
    return tool_success("gitea_list_issues", result=data, source="gitea")


@register_tool("gitea_get_issue")
async def gitea_get_issue(owner: str, repo: str, issue_number: int) -> dict[str, Any]:
    """Get details of a specific Gitea issue by number."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_issue",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with GiteaClient(token) as client:
            raw = await client.get_issue(owner, repo, issue_number)
            data = minimize_issue_payload(raw, provider="gitea")
    except Exception as exc:
        return _remote_api_error("gitea_get_issue", "gitea", exc)
    return tool_success("gitea_get_issue", result=data, source="gitea")


@register_tool("gitea_list_pull_requests")
async def gitea_list_pull_requests(
    owner: str, repo: str, state: str = "open", limit: int = 30
) -> dict[str, Any]:
    """List pull requests in a Gitea repository. State: open, closed, all."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_pull_requests",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        validate_pagination(limit, "limit")
        async with GiteaClient(token) as client:
            raw = await client.list_pull_requests(owner, repo, state=state, limit=limit)
            data = normalize_list_response(
                [minimize_issue_payload(i, provider="gitea") for i in raw],
                meta=list_pagination_meta(len(raw), limit),
            )
    except Exception as exc:
        return _remote_api_error("gitea_list_pull_requests", "gitea", exc)
    return tool_success("gitea_list_pull_requests", result=data, source="gitea")


@register_tool("gitea_get_pull_request")
async def gitea_get_pull_request(owner: str, repo: str, pull_number: int) -> dict[str, Any]:
    """Get details of a specific Gitea pull request by number."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_pull_request",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with GiteaClient(token) as client:
            raw = await client.get_pull_request(owner, repo, pull_number)
            data = minimize_issue_payload(raw, provider="gitea")
    except Exception as exc:
        return _remote_api_error("gitea_get_pull_request", "gitea", exc)
    return tool_success("gitea_get_pull_request", result=data, source="gitea")


@register_tool("gitea_list_action_runs")
async def gitea_list_action_runs(
    owner: str, repo: str, status: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """List Gitea Actions workflow runs. Optionally filter by status (completed, running, waiting)."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_action_runs",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        validate_pagination(limit, "limit")
        async with GiteaClient(token) as client:
            data = await client.list_action_runs(owner, repo, status=status, limit=limit)
    except Exception as exc:
        return _remote_api_error("gitea_list_action_runs", "gitea", exc)
    return tool_success("gitea_list_action_runs", result=data, source="gitea")


@register_tool("gitea_get_action_run")
async def gitea_get_action_run(owner: str, repo: str, run_id: int) -> dict[str, Any]:
    """Get details of a specific Gitea Actions workflow run by ID."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_get_action_run",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with GiteaClient(token) as client:
            data = await client.get_action_run(owner, repo, run_id)
    except Exception as exc:
        return _remote_api_error("gitea_get_action_run", "gitea", exc)
    return tool_success("gitea_get_action_run", result=data, source="gitea")


@register_tool("gitea_list_action_run_jobs")
async def gitea_list_action_run_jobs(owner: str, repo: str, run_id: int) -> dict[str, Any]:
    """List jobs and steps for a Gitea Actions workflow run."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_action_run_jobs",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with GiteaClient(token) as client:
            data = await client.list_action_run_jobs(owner, repo, run_id)
    except Exception as exc:
        return _remote_api_error("gitea_list_action_run_jobs", "gitea", exc)
    return tool_success("gitea_list_action_run_jobs", result=data, source="gitea")


@register_tool("gitea_list_workflows")
async def gitea_list_workflows(owner: str, repo: str) -> dict[str, Any]:
    """List Gitea Actions workflow files in a repository."""
    token = os.environ.get("GITEA_TOKEN", "")
    if not token:
        return tool_error(
            tool="gitea_list_workflows",
            code="DEPENDENCY_MISSING",
            message="GITEA_TOKEN not configured",
            source="gitea",
        )
    try:
        async with GiteaClient(token) as client:
            data = await client.list_workflows(owner, repo)
    except Exception as exc:
        return _remote_api_error("gitea_list_workflows", "gitea", exc)
    return tool_success("gitea_list_workflows", result=data, source="gitea")


# ── GitHub tools ─────────────────────────────────────────────────


@register_tool("github_get_repo")
async def github_get_repo(owner: str, repo: str) -> dict[str, Any]:
    """Get GitHub repository metadata (login, visibility, default branch, permissions, counters, topics)."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_get_repo",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        async with GitHubClient(token) as client:
            data = await client.get_repo(owner, repo)
    except Exception as exc:
        return _remote_api_error("github_get_repo", "github", exc)
    return tool_success("github_get_repo", result=_minimize_github_repo(data), source="github")


@register_tool("github_list_branches")
async def github_list_branches(owner: str, repo: str, per_page: int = 30) -> dict[str, Any]:
    """List branches in a GitHub repository."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_list_branches",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        validate_pagination(per_page, "per_page")
        async with GitHubClient(token) as client:
            raw = await client.list_branches(owner, repo, per_page=per_page)
            data = normalize_list_response(
                raw,
                meta=list_pagination_meta(len(raw), per_page),
            )
    except Exception as exc:
        return _remote_api_error("github_list_branches", "github", exc)
    return tool_success("github_list_branches", result=data, source="github")


@register_tool("github_list_commits")
async def github_list_commits(
    owner: str, repo: str, sha: str | None = None, per_page: int = 30
) -> dict[str, Any]:
    """List commits in a GitHub repository."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_list_commits",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        validate_pagination(per_page, "per_page")
        async with GitHubClient(token) as client:
            raw = await client.list_commits(owner, repo, sha=sha, per_page=per_page)
            data = normalize_list_response(
                raw,
                meta=list_pagination_meta(len(raw), per_page),
            )
    except Exception as exc:
        return _remote_api_error("github_list_commits", "github", exc)
    return tool_success("github_list_commits", result=data, source="github")


@register_tool("github_get_file")
async def github_get_file(
    owner: str, repo: str, path: str = "", branch: str | None = None
) -> dict[str, Any]:
    """Get a file or directory from a GitHub repository. Omit path (or
    pass "") to list the repository root."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_get_file",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        async with GitHubClient(token) as client:
            data = await client.get_file(owner, repo, path, branch=branch)
    except Exception as exc:
        return _remote_api_error("github_get_file", "github", exc)
    return tool_success("github_get_file", result=data, source="github")


@register_tool("github_list_issues")
async def github_list_issues(
    owner: str, repo: str, state: str = "open", per_page: int = 30
) -> dict[str, Any]:
    """List issues in a GitHub repository. State: open, closed, all."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_list_issues",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        validate_pagination(per_page, "per_page")
        async with GitHubClient(token) as client:
            raw = await client.list_issues(owner, repo, state=state, per_page=per_page)
            data = normalize_list_response(
                [minimize_issue_payload(i, provider="github") for i in raw],
                meta=list_pagination_meta(len(raw), per_page),
            )
    except Exception as exc:
        return _remote_api_error("github_list_issues", "github", exc)
    return tool_success("github_list_issues", result=data, source="github")


@register_tool("github_get_issue")
async def github_get_issue(owner: str, repo: str, issue_number: int) -> dict[str, Any]:
    """Get details of a specific GitHub issue by number."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_get_issue",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        async with GitHubClient(token) as client:
            raw = await client.get_issue(owner, repo, issue_number)
            data = minimize_issue_payload(raw, provider="github")
    except Exception as exc:
        return _remote_api_error("github_get_issue", "github", exc)
    return tool_success("github_get_issue", result=data, source="github")


@register_tool("github_list_pull_requests")
async def github_list_pull_requests(
    owner: str, repo: str, state: str = "open", per_page: int = 30
) -> dict[str, Any]:
    """List pull requests in a GitHub repository. State: open, closed, all."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_list_pull_requests",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        validate_pagination(per_page, "per_page")
        async with GitHubClient(token) as client:
            raw = await client.list_pull_requests(owner, repo, state=state, per_page=per_page)
            data = normalize_list_response(
                [minimize_issue_payload(i, provider="github") for i in raw],
                meta=list_pagination_meta(len(raw), per_page),
            )
    except Exception as exc:
        return _remote_api_error("github_list_pull_requests", "github", exc)
    return tool_success("github_list_pull_requests", result=data, source="github")


@register_tool("github_get_pull_request")
async def github_get_pull_request(owner: str, repo: str, pull_number: int) -> dict[str, Any]:
    """Get details of a specific GitHub pull request by number."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return tool_error(
            tool="github_get_pull_request",
            code="DEPENDENCY_MISSING",
            message="GITHUB_TOKEN not configured",
            source="github",
        )
    try:
        async with GitHubClient(token) as client:
            raw = await client.get_pull_request(owner, repo, pull_number)
            data = minimize_issue_payload(raw, provider="github")
    except Exception as exc:
        return _remote_api_error("github_get_pull_request", "github", exc)
    return tool_success("github_get_pull_request", result=data, source="github")


# ── Agent Handoff v2 tools ──────────────────────────────────────────


@register_tool("write_agent_task")
def gateway_write_agent_task(
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
        tool="write_agent_task",
        title="Write agent task",
        fn=_fn,
        success_text="Wrote agent task.",
    )


@register_tool("read_agent_status")
def gateway_read_agent_status(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-status.md."""
    return run_tool(
        tool="read_agent_status",
        title="Read agent status",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            filename="agent-status.md",
        ),
        success_text="Read agent status.",
    )


@register_tool("read_agent_report")
def gateway_read_agent_report(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-report.md."""
    return run_tool(
        tool="read_agent_report",
        title="Read agent report",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            filename="agent-report.md",
        ),
        success_text="Read agent report.",
    )


@register_tool("read_agent_diff")
def gateway_read_agent_diff(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/implementation-diff.patch."""
    return run_tool(
        tool="read_agent_diff",
        title="Read agent diff",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            filename="implementation-diff.patch",
        ),
        success_text="Read agent diff.",
    )


@register_tool("list_agent_tasks")
def gateway_list_agent_tasks(project: str) -> dict[str, Any]:
    """List task directories under .ai-bridge/tasks/."""
    return run_tool(
        tool="list_agent_tasks",
        title="List agent tasks",
        fn=lambda: _list_agent_tasks(
            lambda p, c: run_project_command(client, p, c),
            project=project,
        ),
        success_text="Listed agent tasks.",
    )


@register_tool("archive_agent_task")
def gateway_archive_agent_task(project: str, task_id: str) -> dict[str, Any]:
    """Move .ai-bridge/tasks/<task_id>/ -> .ai-bridge/archive/<task_id>/."""
    return run_tool(
        tool="archive_agent_task",
        title="Archive agent task",
        fn=lambda: _archive_agent_task(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
        ),
        success_text="Archived agent task.",
    )


@register_tool("run_opencode")
def gateway_run_opencode(
    project: str,
    task_id: str,
    model: str | None = None,
    async_submit: bool = False,
) -> dict[str, Any]:
    """Execute an existing handoff task via OpenCode CLI (--dangerously-skip-permissions).
    Requires write mode handoff or full.

    async_submit=True returns a job_id immediately instead of waiting for
    the full run -- poll with job_status/job_result/job_wait. Fleet mode:
    call this repeatedly with async_submit=True to launch several agents
    without blocking on each one."""
    from write_modes import assert_handoff_write_allowed

    assert_handoff_write_allowed()
    return run_tool(
        tool="run_opencode",
        title="Run opencode task",
        fn=lambda: _project_run_opencode(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            model=model,
            run_script=lambda p, s: client.execute_project_script(p, s),
            run_script_async=lambda p, s: client.execute_project_script_async(p, s),
            async_submit=async_submit,
        ),
        success_text="Submitted opencode task.",
    )


@register_tool("run_agent")
def gateway_run_agent(
    project: str,
    task_id: str,
    model: str | None = None,
    async_submit: bool = False,
) -> dict[str, Any]:
    """Execute a handoff task via the agent backend router — selects OpenCode
    (--dangerously-skip-permissions).
    Requires write mode handoff or full. Router enabled by MCP_AGENT_BACKEND_ROUTER_ENABLED.
    Task must have task.json with agent='auto' or agent='opencode'.

    async_submit=True returns a job_id immediately instead of waiting for
    the full run -- poll with job_status/job_result/job_wait. Fleet mode:
    call this repeatedly with async_submit=True to launch several agents
    without blocking on each one; the router's cooldown tracking is not
    fed by async-submitted jobs (no completion callback into this
    process), only by synchronous (async_submit=False) runs."""
    from write_modes import assert_handoff_write_allowed

    assert_handoff_write_allowed()
    return run_tool(
        tool="run_agent",
        title="Run agent task (router)",
        fn=lambda: _project_run_agent(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            model=model,
            router=_agent_router,
            run_script=lambda p, s: client.execute_project_script(p, s),
            run_script_async=lambda p, s: client.execute_project_script_async(p, s),
            async_submit=async_submit,
        ),
        success_text="Submitted agent task via router.",
    )


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


# ── Workspace write tools (Phase C1) ─────────────────────────────

_workspace_registry_cache = None


def _get_workspace_registry():
    """Get or create the workspace registry, resolving projects.yaml path.

    Uses a lazy cache to avoid re-parsing YAML on every call. The root is
    resolved by the shared resolve_registry_root() — the exact same
    deterministic resolution the REST app pins at startup — so MCP and
    REST can never drift apart again.
    """
    global _workspace_registry_cache
    if _workspace_registry_cache is not None:
        return _workspace_registry_cache

    from app.workspace.policy import ALL_SCOPES
    from app.workspace.registry import WorkspaceRegistry, resolve_registry_root, set_registry_root

    root = resolve_registry_root()
    if not (root / "projects.yaml").exists():
        raise RuntimeError(
            "Cannot find projects.yaml. Set WORKSPACE_REGISTRY_ROOT or "
            "ensure projects.yaml exists in the repo root."
        )
    set_registry_root(root)
    _workspace_registry_cache = WorkspaceRegistry.load(
        root / "projects.yaml", granted_scopes=ALL_SCOPES
    )
    return _workspace_registry_cache


@register_tool("workspace_file_write")
@instrumented("workspace_file_write")
def gateway_workspace_file_write(
    project_id: str,
    relative_path: str,
    content: str,
    max_bytes: int = 1_000_000,
    safe: bool = False,
) -> dict[str, Any]:
    """Write (create or overwrite) a UTF-8 text file inside a project.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        content: UTF-8 text content to write.
        max_bytes: Maximum content size in bytes (default 1MB).
        safe: If True, include change receipt in response for rollback.

    Returns:
        Contract v1 dict with project_id, path, size, encoding.
        If safe=True, includes nested receipt dict.
    """
    from app.config import settings as _settings

    if _settings.workspace_readonly:
        return tool_error(
            tool="workspace_file_write",
            code="WORKSPACE_READONLY",
            message="Workspace is in read-only mode",
        )
    try:
        from app.workspace.edit import project_file_write

        registry = _get_workspace_registry()
        result = project_file_write(
            project_id=project_id,
            relative_path=relative_path,
            content=content,
            max_bytes=max_bytes,
            registry=registry,
            safe=safe,
        )
        return tool_success(tool="workspace_file_write", result=result)
    except Exception as exc:
        return tool_error(
            tool="workspace_file_write",
            code="TOOL_EXECUTION_FAILED",
            message=str(exc),
        )


@register_tool("workspace_file_edit")
@instrumented("workspace_file_edit")
def gateway_workspace_file_edit(
    project_id: str,
    relative_path: str,
    old_string: str,
    new_string: str,
    max_bytes: int = 1_000_000,
    safe: bool = False,
) -> dict[str, Any]:
    """Edit a file by replacing the first occurrence of old_string with new_string.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        old_string: Literal string to find and replace (must not be empty).
        new_string: Replacement string.
        max_bytes: Maximum file size in bytes (default 1MB).
        safe: If True, include change receipt in response for rollback.

    Returns:
        Contract v1 dict with project_id, path, size, diff, replaced.
        If safe=True, includes nested receipt dict.
    """
    from app.config import settings as _settings

    if _settings.workspace_readonly:
        return tool_error(
            tool="workspace_file_edit",
            code="WORKSPACE_READONLY",
            message="Workspace is in read-only mode",
        )
    try:
        from app.workspace.edit import project_file_edit

        registry = _get_workspace_registry()
        result = project_file_edit(
            project_id=project_id,
            relative_path=relative_path,
            old_string=old_string,
            new_string=new_string,
            max_bytes=max_bytes,
            registry=registry,
            safe=safe,
        )
        return tool_success(tool="workspace_file_edit", result=result)
    except Exception as exc:
        return tool_error(
            tool="workspace_file_edit",
            code="TOOL_EXECUTION_FAILED",
            message=str(exc),
        )


@register_tool("workspace_apply_patch")
@instrumented("workspace_apply_patch")
def gateway_workspace_apply_patch(
    project_id: str,
    relative_path: str,
    patch: str,
    max_bytes: int = 1_000_000,
    safe: bool = False,
) -> dict[str, Any]:
    """Apply a unified diff patch to a file inside a project.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        patch: Unified diff text (single file).
        max_bytes: Maximum file size in bytes (default 1MB).
        safe: If True, include change receipt in response for rollback.

    Returns:
        Dict with project_id, path, size, applied, backup_hash (patch stripped).
        If safe=True, includes nested receipt dict.
    """
    from app.config import settings as _settings

    if _settings.workspace_readonly:
        return tool_error(
            tool="workspace_apply_patch",
            code="WORKSPACE_READONLY",
            message="Workspace is in read-only mode",
        )
    try:
        from app.workspace.edit import project_apply_patch

        registry = _get_workspace_registry()
        result = project_apply_patch(
            project_id=project_id,
            relative_path=relative_path,
            patch=patch,
            max_bytes=max_bytes,
            registry=registry,
            safe=safe,
        )
        # Strip patch content from response to avoid leaking input
        result.pop("patch", None)
        return tool_success(tool="workspace_apply_patch", result=result)
    except Exception as exc:
        return tool_error(
            tool="workspace_apply_patch",
            code="TOOL_EXECUTION_FAILED",
            message=str(exc),
        )


@register_tool("workspace_preview_write")
@instrumented("workspace_preview_write")
def gateway_workspace_preview_write(
    project_id: str,
    relative_path: str,
    content: str,
    max_bytes: int = 1_000_000,
) -> dict[str, Any]:
    """Preview a file write without writing to disk.

    Returns diff, hashes, and size changes. No disk mutation.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        content: UTF-8 text content to write.
        max_bytes: Maximum content size in bytes (default 1MB).

    Returns:
        Contract v1 dict with before_hash, after_hash, size_before,
        size_after, diff, changed, file_exists_before, encoding.
    """
    try:
        from app.workspace.preview import project_file_preview_write

        registry = _get_workspace_registry()
        result = project_file_preview_write(
            project_id=project_id,
            relative_path=relative_path,
            content=content,
            max_bytes=max_bytes,
            registry=registry,
        )
        return tool_success(tool="workspace_preview_write", result=result)
    except Exception as exc:
        return tool_error(
            tool="workspace_preview_write",
            code="TOOL_EXECUTION_FAILED",
            message=str(exc),
        )


@register_tool("workspace_preview_edit")
@instrumented("workspace_preview_edit")
def gateway_workspace_preview_edit(
    project_id: str,
    relative_path: str,
    old_string: str,
    new_string: str,
    max_bytes: int = 1_000_000,
) -> dict[str, Any]:
    """Preview a file edit without writing to disk.

    Returns diff, hashes, and size changes. No disk mutation.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        old_string: Literal string to find and replace (must not be empty).
        new_string: Replacement string.
        max_bytes: Maximum file size in bytes (default 1MB).

    Returns:
        Contract v1 dict with before_hash, after_hash, size_before,
        size_after, diff, changed, replaced, encoding.
    """
    try:
        from app.workspace.preview import project_file_preview_edit

        registry = _get_workspace_registry()
        result = project_file_preview_edit(
            project_id=project_id,
            relative_path=relative_path,
            old_string=old_string,
            new_string=new_string,
            max_bytes=max_bytes,
            registry=registry,
        )
        return tool_success(tool="workspace_preview_edit", result=result)
    except Exception as exc:
        return tool_error(
            tool="workspace_preview_edit",
            code="TOOL_EXECUTION_FAILED",
            message=str(exc),
        )


@register_tool("workspace_preview_patch")
@instrumented("workspace_preview_patch")
def gateway_workspace_preview_patch(
    project_id: str,
    relative_path: str,
    patch: str,
    max_bytes: int = 1_000_000,
) -> dict[str, Any]:
    """Preview a patch application without writing to disk.

    Returns diff, hashes, and size changes. No disk mutation.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        patch: Unified diff text (single file).
        max_bytes: Maximum file size in bytes (default 1MB).

    Returns:
        Contract v1 dict with before_hash, after_hash, size_before,
        size_after, diff, changed, applied, encoding.
    """
    try:
        from app.workspace.preview import project_file_preview_patch

        registry = _get_workspace_registry()
        result = project_file_preview_patch(
            project_id=project_id,
            relative_path=relative_path,
            patch=patch,
            max_bytes=max_bytes,
            registry=registry,
        )
        return tool_success(tool="workspace_preview_patch", result=result)
    except Exception as exc:
        return tool_error(
            tool="workspace_preview_patch",
            code="TOOL_EXECUTION_FAILED",
            message=str(exc),
        )


@register_tool("workspace_verify")
@instrumented("workspace_verify")
def gateway_workspace_verify(
    project_id: str,
    relative_path: str,
    expected_hash: str,
) -> dict[str, Any]:
    """Verify a file's current SHA-256 hash matches expected hash.

    Args:
        project_id: Registered project identifier.
        relative_path: Project-relative file path.
        expected_hash: Expected SHA-256 hash (e.g. "sha256:abc...").

    Returns:
        Contract v1 dict with project_id, path, matches, current_hash,
        file_exists.
    """
    try:
        from app.workspace.preview import project_file_verify

        registry = _get_workspace_registry()
        result = project_file_verify(
            project_id=project_id,
            relative_path=relative_path,
            expected_hash=expected_hash,
            registry=registry,
        )
        return tool_success(tool="workspace_verify", result=result)
    except Exception as exc:
        return tool_error(
            tool="workspace_verify",
            code="TOOL_EXECUTION_FAILED",
            message=str(exc),
        )


# ── Main ─────────────────────────────────────────────────────────

# Import adapters and register their tools into the live FastMCP
# instance. Explicit register_all() (not import-time decorator side
# effects): server.py may be importlib.reloaded, and the adapters are
# cached in sys.modules, so import-time registration would miss a fresh
# FastMCP instance.
from examples.mcp_server.mcp_infra.adapters import context7, docker, postgres  # noqa: E402

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

context7.register_all()
docker.register_all()
postgres.register_all()

if __name__ == "__main__":
    mcp.run()
