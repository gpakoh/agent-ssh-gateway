"""Agent Handoff v2 — .ai-bridge task management for parallel agent execution."""

from __future__ import annotations

import base64
import json
import re
import shlex
from datetime import UTC, datetime
from typing import Any

from examples.mcp_server.agent_paths import (
    task_archive_dir,
    task_archive_path,
    task_dir,
    task_tasks_dir,
)

TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{10,120}$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")

# Legacy constants remain import-compatible for callers/tests. Production uses
# agent_paths.task_* helpers when MCP_AGENT_STATE_ROOT is configured.
TASKS_REL_DIR = ".ai-bridge/tasks"
ARCHIVE_REL_DIR = ".ai-bridge/archive"


def validate_task_id(task_id: str) -> None:
    """Raise ValueError if task_id is malformed."""
    if not TASK_ID_RE.match(task_id):
        raise ValueError(f"Invalid task_id: {task_id!r}. Must match {TASK_ID_RE.pattern}")


def validate_filename(filename: str) -> None:
    """Raise ValueError if filename is malformed.

    Every current caller of read_agent_task_file() passes a hardcoded
    literal, but the function interpolates filename directly into a shell
    command with no escaping — this rejects path separators (path
    traversal) and shell metacharacters (injection) so it stays safe if a
    future caller ever passes anything less trusted.
    """
    if not FILENAME_RE.match(filename):
        raise ValueError(f"Invalid filename: {filename!r}. Must match {FILENAME_RE.pattern}")


def _encoded_write(path: str, content: str) -> str:
    """Build one shell-safe file write without interpolating raw content."""
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return (
        f"printf %s {shlex.quote(payload)} | base64 -d > {shlex.quote(path)}"
    )


def build_task_json(
    *,
    task_id: str,
    agent: str,
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    required_checks: list[str] | None = None,
    worktree_path: str | None = None,
    commit_allowed: bool = False,
    push_allowed: bool = False,
) -> str:
    """Build machine-readable task.json content."""
    validate_task_id(task_id)
    data: dict[str, Any] = {
        "task_id": task_id,
        "agent": agent,
        "allowed_files": allowed_files or [],
        "forbidden_files": forbidden_files or [],
        "required_checks": required_checks or [],
        "worktree_path": worktree_path or "",
        "commit_allowed": commit_allowed,
        "push_allowed": push_allowed,
        "created": datetime.now(UTC).isoformat(),
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def build_initial_status(agent: str, task_id: str) -> str:
    """Build initial agent-status.md with Status: created."""
    validate_task_id(task_id)
    return (
        f"Status: created\n\n"
        f"## Task\n\n"
        f"- Task ID: {task_id}\n"
        f"- Agent: {agent}\n"
        f"- Started: {datetime.now(UTC).isoformat()}\n\n"
        f"## Progress\n\n"
        f"Task created, awaiting executor.\n"
    )


def build_current_plan(
    *,
    task_id: str,
    task: str,
    scope: str = "",
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    required_checks: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    commit_message: str | None = None,
    constraints: str | None = None,
    artifact_dir: str | None = None,
) -> str:
    """Build human-readable current-plan.md content."""
    validate_task_id(task_id)
    allow = "\n".join(f"- {f}" for f in (allowed_files or []))
    forbid = "\n".join(f"- {f}" for f in (forbidden_files or []))
    checks = "\n".join(f"- `{c}`" for c in (required_checks or []))
    criteria = "\n".join(f"- {c}" for c in (acceptance_criteria or []))
    notes = f"\n## Constraints\n\n{constraints}\n" if constraints else ""
    artifacts = artifact_dir or f"{TASKS_REL_DIR}/{task_id}"

    return (
        f"# {task}\n\n"
        f"## Metadata\n\n"
        f"- Task ID: {task_id}\n"
        f"- Created: {datetime.now(UTC).isoformat()}\n\n"
        f"## Scope\n\n{scope}\n\n"
        f"## Allowed files\n\n{allow}\n\n"
        f"## Forbidden\n\n{forbid}\n\n"
        f"## Required checks\n\n{checks}\n\n"
        f"## Acceptance criteria\n\n{criteria}\n"
        + (f"\n## Commit message\n\n```\n{commit_message}\n```\n" if commit_message else "")
        + notes
        + "\n## Agent instructions\n\n"
        + "Read this plan and execute it in small, reviewable steps.\n"
        + f"After each meaningful change, update `{artifacts}/agent-status.md`.\n"
        + f"Save final diff to `{artifacts}/implementation-diff.patch`.\n"
        + "Do not commit or push unless explicitly instructed.\n"
    )


def list_agent_tasks(run_cmd, *, project: str) -> dict[str, Any]:
    """List task directories from the configured coordination plane."""
    tasks_dir = task_tasks_dir(project)
    result = run_cmd(project, f"ls -1 {shlex.quote(tasks_dir)}/")
    if result.get("exit_code") != 0:
        return {"stdout": "(no tasks)", "stderr": "", "exit_code": 0}
    lines = result.get("stdout", "").splitlines()[:50]
    result["stdout"] = "\n".join(lines)
    return result


def archive_agent_task(run_cmd, *, project: str, task_id: str) -> dict[str, Any]:
    """Move a task into the configured archive; never physically delete it."""
    validate_task_id(task_id)
    src = task_dir(project, task_id)
    archive_dir = task_archive_dir(project)
    dst = task_archive_path(project, task_id)
    result = run_cmd(project, f"mkdir -p {shlex.quote(archive_dir)}")
    if result.get("exit_code") != 0:
        return result
    result = run_cmd(project, f"mv {shlex.quote(src)} {shlex.quote(dst)}")
    if result.get("exit_code") != 0:
        return {"stdout": f"task {task_id} not found", "stderr": result.get("stderr", ""), "exit_code": 1}
    return {"stdout": f"archived {task_id}", "stderr": "", "exit_code": 0}


def read_agent_task_file(run_cmd, *, project: str, task_id: str, filename: str) -> dict[str, Any]:
    """Read a file from .ai-bridge/tasks/<task_id>/ via shell.

    run_cmd is a callable(project, command) that executes a shell command
    and returns dict with at least {'stdout': str, 'stderr': str, 'exit_code': int}.
    """
    validate_task_id(task_id)
    validate_filename(filename)
    path = f"{task_dir(project, task_id)}/{filename}"
    result = run_cmd(project, f"cat {shlex.quote(path)}")
    if result.get("exit_code") != 0:
        return {"stdout": "(not found)", "stderr": "", "exit_code": 0}
    return result


def write_agent_task(
    run_cmd,
    *,
    project: str,
    task_id: str,
    agent: str,
    task: str,
    scope: str = "",
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    required_checks: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    commit_message: str | None = None,
    constraints: str | None = None,
    worktree_path: str | None = None,
) -> dict[str, Any]:
    """Write task.json + current-plan.md + agent-status.md to .ai-bridge/tasks/<task_id>/.

    Also writes worktree-path.txt if worktree_path is provided.
    """
    validate_task_id(task_id)

    task_json = build_task_json(
        task_id=task_id,
        agent=agent,
        allowed_files=allowed_files,
        forbidden_files=forbidden_files,
        required_checks=required_checks,
        worktree_path=worktree_path,
    )
    td = task_dir(project, task_id)
    current_plan = build_current_plan(
        task_id=task_id,
        task=task,
        scope=scope,
        allowed_files=allowed_files,
        forbidden_files=forbidden_files,
        required_checks=required_checks,
        acceptance_criteria=acceptance_criteria,
        commit_message=commit_message,
        constraints=constraints,
        artifact_dir=td,
    )
    initial_status = build_initial_status(agent=agent, task_id=task_id)

    parts = [
        f"mkdir -p {shlex.quote(td)}",
        _encoded_write(f"{td}/task.json", task_json),
        _encoded_write(f"{td}/current-plan.md", current_plan),
        _encoded_write(f"{td}/agent-status.md", initial_status),
    ]
    if worktree_path:
        parts.append(_encoded_write(f"{td}/worktree-path.txt", worktree_path))
    cmd = "\n".join(parts)
    return run_cmd(project, cmd)
