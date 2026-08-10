"""Gateway adapter: gateway _server_client() tools.

_server_client(), mcp, _mcp_started_at, _get_workspace_registry and get_audit_logger
are resolved through the server module at call time: tests patch
examples.mcp_server.server._server_client() and expect the patched _server_client() here
(test_mcp_job_wait, test_mcp_opencode).

Tools are registered explicitly via register_all() (called by server.py
after runtime.set_mcp) instead of import-time decorator side effects:
server.py may be importlib.reloaded, and the adapters are cached in
sys.modules, so import-time registration would miss the new FastMCP
instance.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from command_policy import CommandPolicyError
from gateway_client import GatewayClientError
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
    read_handoff,
    recent_commits,
    remotes,
    run_compileall,
    run_lint,
    run_mypy,
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
from self_test import run_self_test
from tool_results import (
    build_command_result,
    text_result,
    tool_error,
    tool_success,
)
from write_modes import WriteModeError, WritePermissionError

from examples.mcp_server.latency_metrics import get_tracker
from examples.mcp_server.mcp_audit import McpAuditEvent
from examples.mcp_server.mcp_infra import gateway_errors
from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.tool_registry import (
    _validate_project,
    compute_toolset_hash,
    instrumented,
    register_tool,
    run_tool,
)

_classify_gateway_error = gateway_errors._classify_gateway_error
_gateway_error_message = gateway_errors._gateway_error_message
_gateway_error_hint = gateway_errors._gateway_error_hint


def _server_client():
    return server_attr("client")


def _server_read_file():
    return server_attr("read_file")


def _server_mcp():
    return server_attr("mcp")


def _server_mcp_started_at():
    return server_attr("_mcp_started_at")


def _server_workspace_registry():
    return server_attr("_get_workspace_registry")()


def _server_audit_logger():
    return server_attr("get_audit_logger")()


def _server_file():
    return server_attr("__file__")


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
                audit_logger = _server_audit_logger()
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


def gateway_health() -> dict[str, Any]:
    """Check gateway + MCP health with build metadata and toolset hash."""
    from datetime import UTC, datetime

    gateway_data = _server_client().health()

    mcp_build_sha = os.environ.get("BUILD_SHA", "").strip() or "unknown"
    mcp_build_time = os.environ.get("BUILD_TIME", "").strip()

    # Fallback: read git HEAD from source tree (MCP server runs on host, not in Docker)
    if mcp_build_sha == "unknown" or not mcp_build_time:
        try:
            import subprocess as _sp

            _git_dir = Path(_server_file()).resolve().parents[2]
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
    if _server_mcp_started_at():
        mcp_started_at = datetime.fromtimestamp(
            _server_mcp_started_at(), tz=UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    toolset_hash = compute_toolset_hash(_server_mcp())

    tools_count = 0
    if hasattr(_server_mcp(), "_tool_manager"):
        tm = _server_mcp()._tool_manager
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


def gateway_project_list() -> dict[str, Any]:
    """List all registered projects with their type, description and tags."""
    registry = _server_workspace_registry()
    projects = registry.list_projects()
    return tool_success(
        tool="project_list",
        result={
            "count": len(projects),
            "projects": projects,
        },
    )


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


def gateway_scan_file(project: str, path: str) -> dict[str, Any]:
    """Scan a file for destructive command patterns.

    Reads the file through the project workspace and runs each line through
    the destructive pattern scanner. Returns findings with line numbers.
    """
    from app.command_policy import scan_command as _scan

    file_result = _server_read_file()(_server_client(), project=project, path=path)
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


def gateway_list_sessions() -> dict[str, Any]:
    """List current SSH sessions visible to the configured API key."""

    def _list() -> dict[str, Any]:
        data = _server_client().list_sessions()
        return data

    return run_tool(
        tool="list_sessions",
        title="List sessions",
        fn=_list,
        success_text="Retrieved session list.",
    )


def gateway_session_health(session_id: str | None = None) -> dict[str, Any]:
    """Check an SSH session health."""

    def _health() -> dict[str, Any]:
        return _server_client().session_health(session_id=session_id)

    return run_tool(
        tool="session_health",
        title="Session health",
        fn=_health,
        success_text="Session health retrieved.",
    )


def gateway_execute_restricted(command: str, session_id: str | None = None) -> dict[str, Any]:
    """Execute an allowlisted read-only command as a redacted async job."""

    def _exec() -> dict[str, Any]:
        return _server_client().execute_restricted(command, session_id=session_id)

    return run_tool(
        tool="execute_restricted",
        title="Restricted execute",
        fn=_exec,
        success_text="Command submitted as a background job.",
    )


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
        raw = _server_client().execute_argv(
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
        raw = _server_client().apply_patch(
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


def gateway_job_status(job_id: str) -> dict[str, Any]:
    """Get background job status."""

    def _status() -> dict[str, Any]:
        data = _server_client().job_status(job_id)
        return data

    return run_tool(
        tool="job_status",
        title="Job status",
        fn=_status,
        success_text=f"Job {job_id} status retrieved.",
    )


def gateway_job_result(job_id: str, redact_output: bool = True) -> dict[str, Any]:
    """Get background job result."""

    def _result() -> dict[str, Any]:
        data = _server_client().job_result(job_id, redact_output=redact_output)
        return data

    return run_tool(
        tool="job_result",
        title="Job result",
        fn=_result,
        success_text=f"Job {job_id} result retrieved.",
    )


def gateway_wait_job(job_id: str, timeout_sec: int | None = None) -> dict[str, Any]:
    """Wait for a background job and return its result."""

    def _wait() -> dict[str, Any]:
        return _server_client().wait_job(job_id, timeout_sec=timeout_sec)

    return run_tool(
        tool="wait_job",
        title="Wait job",
        fn=_wait,
        success_text=f"Job {job_id} completed.",
    )


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
        result = _server_client().wait_job(job_id, timeout_sec=timeout_sec)
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


def gateway_repo_status(
    session_id: str | None = None, project: str | None = None
) -> dict[str, Any]:
    """Collect basic repository status using read-only commands.

    Args:
        session_id: Optional existing session ID. No new session is ever
            created here -- there's no host/credentials in this tool's
            signature to connect with. When omitted, falls back to the
            _server_client()'s configured default session (GATEWAY_SESSION_ID); if
            that isn't set either, the call fails with
            "GATEWAY_SESSION_ID is required".
        project: Project subdirectory under MCP_GATEWAY_PROJECT_ROOT. Required when
            the SSH session working directory is not a git repository.
    """

    def _status() -> dict[str, Any]:
        return _server_client().repo_status(session_id=session_id, project=project)

    return run_tool(
        tool="repo_status",
        title="Repository status",
        fn=_status,
        success_text="Collected repository status.",
    )


def gateway_working_directory(project: str) -> dict[str, Any]:
    """Print working directory within MCP_GATEWAY_PROJECT_ROOT/{project}."""
    return _run_gateway(
        tool="working_directory",
        fn=lambda: working_directory(_server_client(), project),
    )


def gateway_info(project: str) -> dict[str, Any]:
    """Return resolved project metadata for a configured project name.
    Read-only. Does not execute user-provided shell commands.
    """
    return _run_gateway(
        tool="info",
        fn=lambda: info(_server_client(), project),
    )


def gateway_git_status(project: str) -> dict[str, Any]:
    """Show git working tree status within a project directory."""
    return run_tool(
        tool="git_status",
        title="git status",
        fn=lambda: git_status(_server_client(), project),
        success_text="Collected project git status.",
    )


def gateway_recent_commits(project: str) -> dict[str, Any]:
    """List recent commits within a project (git log --oneline -10)."""
    return run_tool(
        tool="recent_commits",
        title="recent commits",
        fn=lambda: recent_commits(_server_client(), project),
        success_text="Collected project recent commits.",
    )


def gateway_git_diff_stat(project: str) -> dict[str, Any]:
    """Show uncommitted diff stat within a project."""
    return run_tool(
        tool="git_diff_stat",
        title="git diff stat",
        fn=lambda: git_diff_stat(_server_client(), project),
        success_text="Collected project git diff stat.",
    )


def gateway_show_changes(project: str) -> dict[str, Any]:
    """Show combined git status and diff stat within a project."""
    return run_tool(
        tool="show_changes",
        title="show changes",
        fn=lambda: show_changes(_server_client(), project),
        success_text="Collected project change summary.",
    )


def gateway_git_add(project: str, paths: list[str]) -> dict[str, Any]:
    """Stage files for commit (git add)."""
    return run_tool(
        tool="git_add",
        title="git add",
        fn=lambda: git_add(_server_client(), project, paths),
        success_text="Staged files.",
    )


def gateway_git_commit(project: str, message: str) -> dict[str, Any]:
    """Commit staged changes with a message (git commit -m)."""
    return run_tool(
        tool="git_commit",
        title="git commit",
        fn=lambda: git_commit(_server_client(), project, message),
        success_text="Committed changes.",
    )


def gateway_git_push(
    project: str,
    remote: str = "origin",
    branch: str | None = None,
) -> dict[str, Any]:
    """Push commits to remote (git push)."""
    return run_tool(
        tool="git_push",
        title="git push",
        fn=lambda: git_push(_server_client(), project, remote=remote, branch=branch),
        success_text="Pushed to remote.",
    )


def gateway_run_tests(project: str) -> dict[str, Any]:
    """Submit the project's full test suite (pytest -q) as a background job.

    Returns immediately with the job_id (status running); poll with
    gateway_job_status / gateway_job_result. Use run_pytest for a targeted
    synchronous run.
    """
    return run_tool(
        tool="run_tests",
        title="run tests",
        fn=lambda: run_tests(_server_client(), project),
        success_text="Submitted project test suite.",
    )


def gateway_run_lint(project: str) -> dict[str, Any]:
    """Run ruff linter within a project."""
    return run_tool(
        tool="run_lint",
        title="run lint",
        fn=lambda: run_lint(_server_client(), project),
        success_text="Ran project lint checks.",
    )


def gateway_run_compileall(project: str, target: list[str] | str | None = None) -> dict[str, Any]:
    """Run Python compileall within a project (syntax-check the tree).

    ``target`` may be a single file/dir string or a list of targets.
    Omitting it walks the whole project (service dirs pruned).
    """
    return run_tool(
        tool="run_compileall",
        title="run compileall",
        fn=lambda: run_compileall(_server_client(), project, target),
        success_text="Ran project Python compileall.",
    )


# ── Phase 2 project tools ─────────────────────────────────────────


def gateway_read_file(project: str, path: str) -> dict[str, Any]:
    """Read a file within MCP_GATEWAY_PROJECT_ROOT/{project}."""
    return run_tool(
        tool="read_file",
        title="read file",
        fn=lambda: _server_read_file()(_server_client(), project, path),
        success_text="Read project file.",
    )


def gateway_search_text(
    project: str, query: str, glob: str | None = None
) -> dict[str, Any]:
    """Search for text across project files using grep."""
    return run_tool(
        tool="search_text",
        title="search text",
        fn=lambda: search_text(_server_client(), project, query, glob=glob),
        success_text="Searched project text.",
    )


def gateway_find_files(project: str, pattern: str) -> dict[str, Any]:
    """Find files matching a glob pattern in the project."""
    return run_tool(
        tool="find_files",
        title="find files",
        fn=lambda: find_files(project, pattern),
        success_text="Found project files.",
    )


def gateway_list_files(project: str, pattern: str) -> dict[str, Any]:
    """List files matching a glob pattern using Python pathlib — no shell execution."""
    return _run_gateway(
        tool="list_files",
        fn=lambda: list_files(_server_client(), project, pattern),
    )


def gateway_tree(
    project: str,
    depth: int = 2,
    glob: str | None = None,
    max_results: int = MAX_GLOB_RESULTS,
) -> dict[str, Any]:
    """List project directory tree up to a given depth."""
    return _run_gateway(
        tool="tree",
        fn=lambda: tree(_server_client(), project, depth=depth, glob=glob, max_results=max_results),
    )


def gateway_list_tree(
    project: str,
    depth: int = 2,
    max_results: int = MAX_GLOB_RESULTS,
) -> dict[str, Any]:
    """List project directory tree using Python pathlib — no shell execution."""
    return _run_gateway(
        tool="list_tree",
        fn=lambda: list_tree(_server_client(), project, depth=depth, max_results=max_results),
    )


def gateway_git_diff(project: str, path: str | None = None) -> dict[str, Any]:
    """Show git diff (uncommitted changes) in a project."""
    return run_tool(
        tool="git_diff",
        title="git diff",
        fn=lambda: git_diff(_server_client(), project, path=path),
        success_text="Collected project git diff.",
    )


def gateway_git_diff_cached(project: str, path: str | None = None) -> dict[str, Any]:
    """Show git --cached diff (staged changes) in a project."""
    return run_tool(
        tool="git_diff_cached",
        title="git diff cached",
        fn=lambda: git_diff_cached(_server_client(), project, path=path),
        success_text="Collected project staged diff.",
    )


def gateway_show_file_diff(project: str, path: str) -> dict[str, Any]:
    """Show uncommitted diff for a specific file in the project."""
    return run_tool(
        tool="show_file_diff",
        title="show file diff",
        fn=lambda: show_file_diff(_server_client(), project, path),
        success_text="Collected file diff.",
    )


def gateway_run_pytest(project: str, target: list[str] | str | None = None) -> dict[str, Any]:
    """Run pytest on one or more targets within the project.

    ``target`` may be a single file/dir string or a list of targets.
    Omitting it runs the whole suite.
    """
    return run_tool(
        tool="run_pytest",
        title="run pytest",
        fn=lambda: run_pytest(_server_client(), project, target),
        success_text="Ran project pytest.",
    )


def gateway_run_ruff(project: str, target: list[str] | str | None = None) -> dict[str, Any]:
    """Run ruff linter on one or more targets within the project."""
    return run_tool(
        tool="run_ruff",
        title="run ruff",
        fn=lambda: run_ruff(_server_client(), project, target),
        success_text="Ran project ruff.",
    )


def gateway_run_mypy(project: str, target: list[str] | str | None = None) -> dict[str, Any]:
    """Run mypy type checks on one or more targets within the project."""
    return run_tool(
        tool="run_mypy",
        title="run mypy",
        fn=lambda: run_mypy(_server_client(), project, target),
        success_text="Ran project mypy.",
    )


def gateway_remotes(project: str) -> dict[str, Any]:
    """List git remotes for the project."""
    return run_tool(
        tool="remotes",
        title="remotes",
        fn=lambda: remotes(_server_client(), project),
        success_text="Collected project remotes.",
    )


def gateway_current_branch(project: str) -> dict[str, Any]:
    """Show current git branch for the project."""
    return run_tool(
        tool="current_branch",
        title="current branch",
        fn=lambda: current_branch(_server_client(), project),
        success_text="Collected project current branch.",
    )


def gateway_commit_head(project: str) -> dict[str, Any]:
    """Show HEAD commit SHA for the project."""
    return run_tool(
        tool="commit_head",
        title="commit HEAD",
        fn=lambda: commit_head(_server_client(), project),
        success_text="Collected project HEAD commit.",
    )


def gateway_read_handoff(project: str) -> dict[str, Any]:
    """Read .ai-bridge handoff files for a project."""
    return run_tool(
        tool="read_handoff",
        title="read handoff",
        fn=lambda: read_handoff(_server_client(), project),
        success_text="Read project handoff.",
    )


def gateway_write_handoff_plan(
    project: str, task: str, agent: str = "opencode", notes: str | None = None
) -> dict[str, Any]:
    """Write .ai-bridge/current-plan.md for a project (requires MCP_GATEWAY_WRITE_MODE=handoff)."""
    return run_tool(
        tool="write_handoff_plan",
        title="write handoff",
        fn=lambda: write_handoff_plan(_server_client(), project, task, agent=agent, notes=notes),
        success_text="Wrote project handoff plan.",
    )


def gateway_show_handoff_status(project: str) -> dict[str, Any]:
    """Show .ai-bridge file listing for a project."""
    return run_tool(
        tool="show_handoff_status",
        title="handoff status",
        fn=lambda: show_handoff_status(_server_client(), project),
        success_text="Checked project handoff status.",
    )


def gateway_self_test() -> dict[str, Any]:
    """Run read-only diagnostics for the MCP gateway example."""
    data = run_self_test(_server_client())
    status = data.get("status", "unknown")
    return text_result(
        tool="self_test",
        title="Gateway self-test",
        text=f"Gateway MCP self-test status: {status}",
        data=data,
    )


def gateway_latency_report() -> dict[str, Any]:
    """Return accumulated per-tool latency statistics."""
    return tool_success(
        tool="latency_report",
        result=get_tracker().summary(),
    )


def gateway_diagnostics_latency() -> dict[str, Any]:
    """Return MCP-side latency breakdown and gateway latency summary."""
    tracker = get_tracker()
    mcp_summary = tracker.summary()

    try:
        gw_data = _server_client()._get("/api/diagnostics/latency")
    except Exception:
        gw_data = {"error": "gateway diagnostics unavailable"}

    return tool_success(
        tool="diagnostics_latency",
        result={
            "mcp": mcp_summary,
            "gateway": gw_data,
        },
    )

def register_all() -> None:
    register_tool("health")(instrumented("health")(gateway_health))
    register_tool("project_list")(instrumented("project_list")(gateway_project_list))
    register_tool("scan_command")(instrumented("scan_command")(gateway_scan_command))
    register_tool("simulate")(instrumented("simulate")(gateway_simulate))
    register_tool("scan_file")(instrumented("scan_file")(gateway_scan_file))
    register_tool("project_scan_destructive")(instrumented("project_scan_destructive")(gateway_project_scan_destructive))
    register_tool("explain_pattern")(instrumented("explain_pattern")(gateway_explain_pattern))
    register_tool("list_sessions")(instrumented("list_sessions")(gateway_list_sessions))
    register_tool("session_health")(gateway_session_health)
    register_tool("execute_restricted")(gateway_execute_restricted)
    register_tool("execute_argv")(gateway_execute_argv)
    register_tool("apply_patch")(instrumented("apply_patch")(gateway_apply_patch))
    register_tool("job_status")(gateway_job_status)
    register_tool("job_result")(gateway_job_result)
    register_tool("wait_job")(gateway_wait_job)
    register_tool("job_wait")(instrumented("job_wait")(gateway_job_wait))
    register_tool("repo_status")(gateway_repo_status)
    register_tool("working_directory")(gateway_working_directory)
    register_tool("info")(gateway_info)
    register_tool("git_status")(gateway_git_status)
    register_tool("recent_commits")(gateway_recent_commits)
    register_tool("git_diff_stat")(gateway_git_diff_stat)
    register_tool("show_changes")(gateway_show_changes)
    register_tool("git_add")(gateway_git_add)
    register_tool("git_commit")(gateway_git_commit)
    register_tool("git_push")(gateway_git_push)
    register_tool("run_tests")(gateway_run_tests)
    register_tool("run_lint")(gateway_run_lint)
    register_tool("run_compileall")(gateway_run_compileall)
    register_tool("read_file")(gateway_read_file)
    register_tool("search_text")(gateway_search_text)
    register_tool("find_files")(gateway_find_files)
    register_tool("list_files")(gateway_list_files)
    register_tool("tree")(gateway_tree)
    register_tool("list_tree")(gateway_list_tree)
    register_tool("git_diff")(gateway_git_diff)
    register_tool("git_diff_cached")(gateway_git_diff_cached)
    register_tool("show_file_diff")(gateway_show_file_diff)
    register_tool("run_pytest")(gateway_run_pytest)
    register_tool("run_ruff")(gateway_run_ruff)
    register_tool("run_mypy")(gateway_run_mypy)
    register_tool("remotes")(gateway_remotes)
    register_tool("current_branch")(gateway_current_branch)
    register_tool("commit_head")(gateway_commit_head)
    register_tool("read_handoff")(gateway_read_handoff)
    register_tool("write_handoff_plan")(gateway_write_handoff_plan)
    register_tool("show_handoff_status")(gateway_show_handoff_status)
    register_tool("self_test")(gateway_self_test)
    register_tool("latency_report")(gateway_latency_report)
    register_tool("diagnostics_latency")(gateway_diagnostics_latency)
