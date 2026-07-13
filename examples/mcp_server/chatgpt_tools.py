"""ChatGPT-safe high-level MCP tools.

These tools avoid exposing a generic SSH execute surface to ChatGPT
by wrapping fixed allowlisted commands in semantic functions.
"""

from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from typing import Any

from gateway_client import GatewayClient, GatewayClientError
from project_registry import get_project_registry
from tool_results import build_command_result, tool_error, tool_success


def _resolve_project(project_name: str) -> Path:
    """Resolve a project name to a validated filesystem path via the registry."""
    try:
        return get_project_registry().resolve(project_name)
    except ValueError as e:
        raise GatewayClientError(str(e), status_code=404) from e


def _validate_project(project: str) -> str:
    if not project:
        raise ValueError("Project name must not be empty")
    if not re.match(r"^[A-Za-z0-9._\-\/]+$", project):
        raise ValueError(f"Invalid project name: {project!r}")
    if ".." in project.split("/"):
        raise ValueError(f"Path traversal blocked: {project!r}")
    return project.strip("/")


# ── Safety helpers ──────────────────────────────────────────────

_OUTPUT_LINE_LIMIT = 2000
_ALLOWED_PATH_RE = re.compile(r"^[a-zA-Z0-9_./-]+$")


def _safe_relpath(path: str) -> str:
    if not path:
        raise ValueError("path is required")
    if path.startswith("/"):
        raise ValueError(f"absolute path not allowed: {path!r}")
    if ".." in path.split("/"):
        raise ValueError(f"path traversal not allowed: {path!r}")
    if not _ALLOWED_PATH_RE.match(path):
        raise ValueError(f"invalid characters in path: {path!r}")
    return path


def _safe_test_target(target: str) -> str:
    if not target:
        raise ValueError("target is required")
    if target.startswith("/"):
        raise ValueError(f"absolute target not allowed: {target!r}")
    if ".." in target.split("/"):
        raise ValueError(f"path traversal not allowed: {target!r}")
    if ";" in target or "|" in target or "`" in target or "$" in target:
        raise ValueError(f"shell metacharacters not allowed: {target!r}")
    return target


def _limit_output(output: str) -> str:
    lines = output.splitlines()
    if len(lines) > _OUTPUT_LINE_LIMIT:
        lines = lines[:_OUTPUT_LINE_LIMIT]
        lines.append(f"[... truncated to {_OUTPUT_LINE_LIMIT} lines]")
    return "\n".join(lines)


# ── uv runner helpers ───────────────────────────────────────────

_UV_TOOL_MAP: dict[str, list[str]] = {
    "ruff": ["ruff", "check"],
    "mypy": ["mypy"],
    "pytest": ["pytest"],
    "compileall": ["python", "-m", "compileall"],
}


def _validate_targets(project_dir: str, targets: list[str]) -> list[str]:
    """Validate all targets: relative, no traversal, inside project root."""
    root = Path(project_dir).resolve()
    validated = []
    for t in targets:
        p = Path(t)
        if p.is_absolute():
            raise ValueError(f"POLICY_DENIED: absolute target not allowed: {t}")
        if ".." in p.parts:
            raise ValueError(f"POLICY_DENIED: path traversal in target: {t}")
        resolved = (root / p).resolve()
        resolved.relative_to(root)
        validated.append(t)
    return validated


def _build_uv_argv(tool: str, project_dir: str, targets: list[str]) -> list[str]:
    """Build argv list for ``uv run <tool>``. Validates targets first."""
    if tool not in _UV_TOOL_MAP:
        raise ValueError(f"INVALID_INPUT: unknown tool '{tool}'")
    if not targets:
        raise ValueError("INVALID_INPUT: at least one target required")
    validated = _validate_targets(project_dir, targets)
    cmd = _UV_TOOL_MAP[tool]
    return ["uv", "run", "--frozen", "--directory", project_dir, "--"] + cmd + ["--"] + validated


def _map_uv_exit_code(tool: str, exit_code: int) -> tuple[str | None, str | None]:
    """Map uv exit code to (outcome, error_code).

    Returns ``(outcome, None)`` for success/check-failed and
    ``(None, error_code)`` for infrastructure errors.
    """
    if tool == "pytest":
        if exit_code == 0:
            return ("passed", None)
        elif exit_code == 1:
            return ("failed", None)
        elif exit_code == 5:
            return ("failed", None)  # NO_TESTS
        else:
            return (None, "TOOL_EXECUTION_FAILED")

    if exit_code == 0:
        return ("passed", None)
    elif exit_code == 1:
        return ("failed", None)
    else:
        return (None, "TOOL_EXECUTION_FAILED")


# ── Basic read-only helpers ─────────────────────────────────────


def run_readonly_command(
    client: GatewayClient, command: str, session_id: str | None = None
) -> dict[str, Any]:
    job = client.execute_restricted(command, session_id=session_id)
    raw = client.wait_job(job["job_id"])
    return build_command_result(
        outcome="passed" if raw.get("exit_code", 1) == 0 else "failed",
        exit_code=raw.get("exit_code", -1),
        stdout=raw.get("stdout") or raw.get("output", ""),
        stderr=raw.get("stderr", ""),
        execution_duration_ms=raw.get("execution_duration_ms"),
        job_id=job.get("job_id"),
    )


def working_directory(client: GatewayClient, session_id: str | None = None) -> dict[str, Any]:
    return run_readonly_command(client, "pwd", session_id=session_id)


def git_status(client: GatewayClient, session_id: str | None = None) -> dict[str, Any]:
    return run_readonly_command(client, "git status --short", session_id=session_id)


def recent_commits(client: GatewayClient, session_id: str | None = None) -> dict[str, Any]:
    return run_readonly_command(client, "git log --oneline -10", session_id=session_id)


def git_diff_stat(client: GatewayClient, session_id: str | None = None) -> dict[str, Any]:
    return run_readonly_command(client, "git diff --stat", session_id=session_id)


def _is_git_repo(client: GatewayClient, session_id: str | None = None) -> bool:
    """Check if the current SSH session working directory is a git repository."""
    result = run_readonly_command(client, "git rev-parse --git-dir 2>/dev/null", session_id=session_id)
    output = result.get("output", "") or result.get("stdout", "")
    exit_code = result.get("exit_code", 1)
    return exit_code == 0 and ".git" in output or output.strip().endswith(".git")


def show_changes(
    client: GatewayClient,
    session_id: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    if project:
        return project_show_changes(client, project)

    if not _is_git_repo(client, session_id=session_id):
        raise ValueError(
            "SSH session working directory is not a git repository. "
            "Use gateway_project_show_changes(project=...) to specify a project."
        )

    return {
        "git_status": git_status(client, session_id=session_id),
        "git_diff_stat": git_diff_stat(client, session_id=session_id),
    }


def run_tests(client: GatewayClient, session_id: str | None = None) -> dict[str, Any]:
    return run_readonly_command(client, "pytest -q", session_id=session_id)


def run_lint(client: GatewayClient, session_id: str | None = None) -> dict[str, Any]:
    return run_readonly_command(client, "ruff check app tests examples", session_id=session_id)


def run_compileall(client: GatewayClient, session_id: str | None = None) -> dict[str, Any]:
    return run_readonly_command(
        client,
        "python -m compileall app tests examples",
        session_id=session_id,
    )


# ── Project file tools ──────────────────────────────────────────


def project_read_file(
    client: GatewayClient,
    project: str,
    path: str,
) -> dict[str, Any]:
    safe = _safe_relpath(path)
    return run_project_command(client, project, f"cat {safe}")


def project_search_text(
    client: GatewayClient,
    project: str,
    query: str,
    glob: str | None = None,
) -> dict[str, Any]:
    """Search for text across project files using pure Python pathlib — no shell execution."""
    _validate_project(project)
    project_dir = _resolve_project(project)
    if not project_dir.is_dir():
        raise ValueError(f"Project directory not found: {project_dir}")

    from app.services.project_search import search_text

    return search_text(
        root=project_dir,
        query=query,
        glob=glob,
    )


EXCLUDE_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__"})
MAX_GLOB_RESULTS = 200
MAX_GLOB_DEPTH = 20
GLOB_TIMEOUT_S = 5


def _safe_glob(
    project_dir: Path,
    pattern: str,
    max_results: int = MAX_GLOB_RESULTS,
) -> dict[str, Any]:
    """Safe glob: returns {"files": [...], "count": N, "truncated": bool}."""
    if pattern.startswith("/"):
        raise ValueError("POLICY_DENIED: absolute pattern not allowed")
    if ".." in Path(pattern).parts:
        raise ValueError("POLICY_DENIED: path traversal in pattern")

    project_root = project_dir.resolve()
    results: list[str] = []
    start = time.monotonic()

    for path in project_root.glob(pattern):
        if time.monotonic() - start > GLOB_TIMEOUT_S:
            break
        try:
            resolved = path.resolve()
            rel = resolved.relative_to(project_root)
        except ValueError:
            continue
        if not resolved.is_file():
            continue
        if len(rel.parts) > MAX_GLOB_DEPTH:
            continue
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        results.append(str(rel))
        if len(results) >= max_results:
            break

    results.sort()
    return {
        "files": results,
        "count": len(results),
        "truncated": len(results) >= max_results,
    }


def project_find_files(
    project: str,
    pattern: str,
) -> dict[str, Any]:
    project_dir = _resolve_project(project)
    try:
        glob_result = _safe_glob(project_dir, pattern)
    except ValueError as e:
        code, msg = str(e).split(":", 1) if ":" in str(e) else ("INVALID_INPUT", str(e))
        return tool_error(
            "project_find_files",
            code=code.strip(),
            message=msg.strip(),
            tool_name="project_find_files",
        )

    result = {
        "pattern": pattern,
        "files": glob_result["files"],
        "count": glob_result["count"],
    }
    meta = tool_success("project_find_files", result, tool_name="project_find_files")["meta"]
    meta["truncated"] = glob_result.get("truncated", False)
    return {
        "ok": True,
        "result": result,
        "error": None,
        "meta": meta,
    }


def project_list_files(client: GatewayClient, project: str, pattern: str) -> dict[str, Any]:
    """Find files by glob pattern using Python pathlib — no shell execution."""
    _validate_project(project)
    if not pattern or ".." in pattern:
        raise ValueError(f"Invalid pattern: {pattern!r}")

    project_dir = _resolve_project(_validate_project(project))
    exclude_dirs = {
        ".git",
        "__pycache__",
        ".venv",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }

    files: list[str] = []
    for p in project_dir.rglob(pattern):
        if any(part in exclude_dirs for part in p.relative_to(project_dir).parts):
            continue
        if p.is_file():
            files.append(str(p.relative_to(project_dir)))

    files.sort()
    files = files[:200]

    return {
        "project": project,
        "pattern": pattern,
        "root": str(project_dir),
        "files": files,
        "count": len(files),
    }


_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".benchmarks",
    }
)


def project_list_tree(client: GatewayClient, project: str, depth: int = 2) -> dict[str, Any]:
    """List project directory tree using Python pathlib — no shell execution."""
    project = _validate_project(project)
    depth = min(max(depth, 1), 5)
    project_dir = _resolve_project(project)

    entries: list[str] = []
    for p in sorted(project_dir.rglob("*")):
        rel = p.relative_to(project_dir)
        if any(part in _EXCLUDE_DIRS for part in rel.parts):
            continue
        if len(rel.parts) > depth:
            continue
        suffix = "/" if p.is_dir() else ""
        entries.append(f"{rel}{suffix}")

    return {
        "project": project,
        "root": str(project_dir),
        "depth": depth,
        "entries": entries,
        "count": len(entries),
    }


def project_tree(
    client: GatewayClient,
    project: str,
    depth: int = 2,
    glob: str | None = None,
) -> dict[str, Any]:
    """List project directory tree using Python pathlib — no shell execution."""
    if glob and not _ALLOWED_PATH_RE.match(glob):
        raise ValueError(f"invalid glob: {glob!r}")
    project = _validate_project(project)
    depth = min(max(depth, 1), 5)
    project_dir = _resolve_project(project)

    entries: list[str] = []
    for p in sorted(project_dir.rglob("*")):
        rel = p.relative_to(project_dir)
        if any(part in _EXCLUDE_DIRS for part in rel.parts):
            continue
        if len(rel.parts) > depth:
            continue
        suffix = "/" if p.is_dir() else ""
        name = str(rel)
        if glob and not p.match(glob):
            continue
        entries.append(f"{name}{suffix}")

    return {
        "project": project,
        "root": str(project_dir),
        "depth": depth,
        "entries": entries,
        "count": len(entries),
    }


# ── Project git diff tools ──────────────────────────────────────


def project_git_diff(
    client: GatewayClient,
    project: str,
    path: str | None = None,
) -> dict[str, Any]:
    cmd = "git diff --no-color"
    if path:
        cmd += f" -- {_safe_relpath(path)}"
    cmd += " | head -500"
    return run_project_command(client, project, cmd)


def project_git_diff_cached(
    client: GatewayClient,
    project: str,
    path: str | None = None,
) -> dict[str, Any]:
    cmd = "git diff --cached --no-color"
    if path:
        cmd += f" -- {_safe_relpath(path)}"
    cmd += " | head -500"
    return run_project_command(client, project, cmd)


def project_show_file_diff(
    client: GatewayClient,
    project: str,
    path: str,
) -> dict[str, Any]:
    safe = _safe_relpath(path)
    cmd = f"git diff --no-color -- {safe} | head -500"
    return run_project_command(client, project, cmd)


# ── Project test target tools ───────────────────────────────────


def _run_uv_tool(
    client: GatewayClient,
    project: str,
    tool_key: str,
    tool_name: str,
    target: list[str] | None = None,
) -> dict[str, Any]:
    """Run a uv-backed tool via SSH gateway. Shared by all uv runner tools."""
    project_dir = _resolve_project(project)
    targets = target or ["."]
    try:
        argv = _build_uv_argv(tool_key, str(project_dir), targets)
    except ValueError as e:
        code, msg = str(e).split(":", 1) if ":" in str(e) else ("INVALID_INPUT", str(e))
        return tool_error(code=code.strip(), message=msg.strip(), tool_name=tool_name)

    check_result = client.execute_project_command(str(project_dir), "command -v uv")
    if check_result.get("exit_code", 1) != 0:
        return tool_error(
            code="DEPENDENCY_MISSING",
            message="Required executable 'uv' was not found",
            hint="Install uv on the SSH target or configure another backend",
            retryable=False,
            details={"required_binary": "uv"},
            tool_name=tool_name,
        )

    command = " ".join(shlex.quote(a) for a in argv)
    result = client.execute_project_command(str(project_dir), command)
    raw = client.wait_job(result["job_id"])
    outcome, error_code = _map_uv_exit_code(tool_key, raw.get("exit_code", -1))
    if error_code:
        return tool_error(
            code=error_code,
            message=f"{tool_key} failed with exit code {raw.get('exit_code')}",
            details={"exit_code": raw.get("exit_code"), "stderr": raw.get("stderr", "")},
            tool_name=tool_name,
        )
    return tool_success(
        build_command_result(
            outcome=outcome,
            exit_code=raw.get("exit_code", 0),
            stdout=raw.get("stdout") or raw.get("output", ""),
            stderr=raw.get("stderr", ""),
            execution_duration_ms=raw.get("execution_duration_ms"),
            job_id=raw.get("job_id"),
        ),
        tool_name=tool_name,
    )


def project_run_pytest(
    client: GatewayClient,
    project: str,
    target: list[str] | None = None,
) -> dict[str, Any]:
    return _run_uv_tool(client, project, "pytest", "project_run_pytest", target)


def project_run_ruff(
    client: GatewayClient,
    project: str,
    target: list[str] | None = None,
) -> dict[str, Any]:
    return _run_uv_tool(client, project, "ruff", "project_run_ruff", target)


def project_run_mypy(
    client: GatewayClient,
    project: str,
    target: list[str] | None = None,
) -> dict[str, Any]:
    return _run_uv_tool(client, project, "mypy", "project_run_mypy", target)


# ── Project git info tools ──────────────────────────────────────


def project_remotes(
    client: GatewayClient,
    project: str,
) -> dict[str, Any]:
    return run_project_command(client, project, "git remote -v")


def project_current_branch(
    client: GatewayClient,
    project: str,
) -> dict[str, Any]:
    return run_project_command(client, project, "git rev-parse --abbrev-ref HEAD")


def project_commit_head(
    client: GatewayClient,
    project: str,
) -> dict[str, Any]:
    return run_project_command(client, project, "git rev-parse HEAD")


# ── Project-scoped handoff ──────────────────────────────────────


def project_read_handoff(
    client: GatewayClient,
    project: str,
) -> dict[str, Any]:
    return run_project_command(
        client,
        project,
        "cat .ai-bridge/current-plan.md 2>/dev/null || echo '(no handoff plan)'",
    )


def project_write_handoff_plan(
    client: GatewayClient,
    project: str,
    task: str,
    agent: str = "opencode",
    notes: str | None = None,
) -> dict[str, Any]:
    from handoff import assert_handoff_write_allowed, build_handoff_plan

    assert_handoff_write_allowed()
    plan = build_handoff_plan(task=task, agent=agent, notes=notes)
    cmd = f"mkdir -p .ai-bridge && cat > .ai-bridge/current-plan.md << 'PLANEOF'\n{plan}\nPLANEOF"
    return run_project_command(client, project, cmd)


def project_show_handoff_status(
    client: GatewayClient,
    project: str,
) -> dict[str, Any]:
    cmd = (
        "echo '--- .ai-bridge files ---' && "
        "ls -la .ai-bridge/ 2>/dev/null || echo '(no .ai-bridge directory)'"
    )
    return run_project_command(client, project, cmd)


# ── Shell escape helper ─────────────────────────────────────────


def _shell_escape(text: str) -> str:
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"


# ── Project-aware tools (cd into MCP_GATEWAY_PROJECT_ROOT/{project}) ──


def run_project_command(
    client: GatewayClient,
    project: str,
    command: str,
) -> dict[str, Any]:
    job = client.execute_project_command(project, command)
    raw = client.wait_job(job["job_id"])
    return build_command_result(
        outcome="passed" if raw.get("exit_code", 1) == 0 else "failed",
        exit_code=raw.get("exit_code", -1),
        stdout=raw.get("stdout") or raw.get("output", ""),
        stderr=raw.get("stderr", ""),
        execution_duration_ms=raw.get("execution_duration_ms"),
        job_id=job.get("job_id"),
    )


def project_working_directory(client: GatewayClient, project: str) -> dict[str, Any]:
    return run_project_command(client, project, "pwd")


def project_info(client: GatewayClient, project: str) -> dict[str, Any]:
    """Resolve project path metadata — no shell execution."""
    project = _validate_project(project)
    resolved = _resolve_project(project)
    return {
        "project": project,
        "root": str(resolved),
        "resolved_path": str(resolved),
        "exists": resolved.exists(),
        "is_dir": resolved.is_dir(),
        "is_git_repo": (resolved / ".git").exists(),
    }


def project_git_status(client: GatewayClient, project: str) -> dict[str, Any]:
    return run_project_command(client, project, "git status --short")


def project_recent_commits(client: GatewayClient, project: str) -> dict[str, Any]:
    return run_project_command(client, project, "git log --oneline -10")


def project_git_diff_stat(client: GatewayClient, project: str) -> dict[str, Any]:
    return run_project_command(client, project, "git diff --stat")


def project_show_changes(client: GatewayClient, project: str) -> dict[str, Any]:
    return {
        "git_status": project_git_status(client, project),
        "git_diff_stat": project_git_diff_stat(client, project),
    }


def project_run_tests(client: GatewayClient, project: str) -> dict[str, Any]:
    return run_project_command(client, project, "pytest -q")


def project_run_lint(client: GatewayClient, project: str) -> dict[str, Any]:
    return run_project_command(client, project, "ruff check app tests examples")


def project_run_compileall(
    client: GatewayClient,
    project: str,
    target: list[str] | None = None,
) -> dict[str, Any]:
    return _run_uv_tool(client, project, "compileall", "project_run_compileall", target)
