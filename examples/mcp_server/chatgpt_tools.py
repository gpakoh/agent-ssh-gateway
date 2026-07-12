"""ChatGPT-safe high-level MCP tools.

These tools avoid exposing a generic SSH execute surface to ChatGPT
by wrapping fixed allowlisted commands in semantic functions.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from gateway_client import GatewayClient
from tool_results import build_command_result


def _project_root() -> Path:
    root = os.environ.get("MCP_GATEWAY_PROJECT_ROOT", "").strip().rstrip("/")
    if not root:
        raise ValueError("MCP_GATEWAY_PROJECT_ROOT is not set")
    return Path(root)


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
    project_dir = _project_root() / project
    if not project_dir.is_dir():
        raise ValueError(f"Project directory not found: {project_dir}")

    from app.services.project_search import search_text

    return search_text(
        root=project_dir,
        query=query,
        glob=glob,
    )


def project_find_files(
    client: GatewayClient,
    project: str,
    pattern: str,
) -> dict[str, Any]:
    if not _ALLOWED_PATH_RE.match(pattern):
        raise ValueError(f"invalid pattern: {pattern!r}")
    cmd = (
        f"find . -not -path '*/.git/*' -not -path '*/__pycache__/*'"
        f" -not -path '*/.venv/*' -not -path '*/node_modules/*'"
        f" -type f -name '{pattern}' | sort | head -200"
    )
    return run_project_command(client, project, cmd)


def project_list_files(client: GatewayClient, project: str, pattern: str) -> dict[str, Any]:
    """Find files by glob pattern using Python pathlib — no shell execution."""
    _validate_project(project)
    if not pattern or ".." in pattern:
        raise ValueError(f"Invalid pattern: {pattern!r}")

    project_dir = _project_root() / _validate_project(project)
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
    project_dir = _project_root() / project

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
    project_dir = _project_root() / project

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


def project_run_pytest(
    client: GatewayClient,
    project: str,
    target: str,
) -> dict[str, Any]:
    safe = _safe_test_target(target)
    return run_project_command(client, project, f"pytest -q {safe}")


def project_run_ruff(
    client: GatewayClient,
    project: str,
    target: str,
) -> dict[str, Any]:
    safe = _safe_test_target(target)
    return run_project_command(client, project, f"ruff check {safe}")


def project_run_mypy(
    client: GatewayClient,
    project: str,
    target: str,
) -> dict[str, Any]:
    safe = _safe_test_target(target)
    return run_project_command(client, project, f"mypy {safe}")


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
    resolved = _project_root() / project
    return {
        "project": project,
        "root": str(_project_root()),
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


def project_run_compileall(client: GatewayClient, project: str) -> dict[str, Any]:
    return run_project_command(client, project, "python -m compileall app tests examples")
