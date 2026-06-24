"""Agent Handoff v2 — .ai-bridge task management for parallel agent execution."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{10,120}$")

TASKS_REL_DIR = ".ai-bridge/tasks"
ARCHIVE_REL_DIR = ".ai-bridge/archive"


def validate_task_id(task_id: str) -> None:
    """Raise ValueError if task_id is malformed."""
    if not TASK_ID_RE.match(task_id):
        raise ValueError(
            f"Invalid task_id: {task_id!r}. Must match {TASK_ID_RE.pattern}"
        )


def _task_dir(task_id: str) -> str:
    return f"{TASKS_REL_DIR}/{task_id}"


def _archive_dir(task_id: str) -> str:
    return f"{ARCHIVE_REL_DIR}/{task_id}"


def _shell_escape(text: str) -> str:
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"


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
) -> str:
    """Build human-readable current-plan.md content."""
    validate_task_id(task_id)
    allow = "\n".join(f"- {f}" for f in (allowed_files or []))
    forbid = "\n".join(f"- {f}" for f in (forbidden_files or []))
    checks = "\n".join(f"- `{c}`" for c in (required_checks or []))
    criteria = "\n".join(f"- {c}" for c in (acceptance_criteria or []))
    notes = f"\n## Constraints\n\n{constraints}\n" if constraints else ""

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
        + f"After each meaningful change, update `.ai-bridge/tasks/{task_id}/agent-status.md`.\n"
        + f"Save final diff to `.ai-bridge/tasks/{task_id}/implementation-diff.patch`.\n"
        + "Do not commit or push unless explicitly instructed.\n"
    )


def write_agent_task(
    run_cmd, *,
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
    )
    initial_status = build_initial_status(agent=agent, task_id=task_id)

    td = _task_dir(task_id)
    parts = [
        f"mkdir -p {td}",
        f"cat > {td}/task.json << 'JEOF'\n{task_json}\nJEOF",
        f"cat > {td}/current-plan.md << 'PEOF'\n{current_plan}\nPEOF",
        f"cat > {td}/agent-status.md << 'SEOF'\n{initial_status}\nSEOF",
    ]
    if worktree_path:
        parts.append(
            f"cat > {td}/worktree-path.txt << 'WEOF'\n{worktree_path}\nWEOF"
        )
    cmd = " && ".join(parts)
    return run_cmd(project, cmd)
