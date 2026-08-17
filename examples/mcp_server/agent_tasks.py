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
ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
BASE_REF_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
AGENT_LOG_FILENAME = "opencode-output.log"
AGENT_LOG_MAX_BYTES = 64 * 1024
AGENT_LOG_MAX_TAIL_LINES = 1000

_SENTENCE_ENDINGS = (".", "?", "!")
_TRAILING_OPERATORS = frozenset({"&&", "||", "|", ">", ">>", "<", "<&", ">&", "2>"})
_FUNCTION_WORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "as", "at", "be", "been", "being",
        "but", "by", "can", "could", "did", "do", "does", "for", "from", "has",
        "have", "having", "he", "her", "his", "how", "i", "if", "in", "into", "is",
        "it", "its", "me", "my", "no", "not", "of", "on", "only", "or", "our",
        "please", "shall", "should", "so", "such", "than", "that", "the", "their",
        "them", "then", "there", "these", "they", "this", "those", "to", "was", "we",
        "were", "what", "when", "where", "which", "who", "why", "will", "with", "would",
        "you", "your",
    }
)

TASKS_REL_DIR = ".ai-bridge/tasks"
ARCHIVE_REL_DIR = ".ai-bridge/archive"


def validate_task_id(task_id: str) -> None:
    """Raise ValueError if task_id is malformed."""
    if not TASK_ID_RE.match(task_id):
        raise ValueError(f"Invalid task_id: {task_id!r}. Must match {TASK_ID_RE.pattern}")


def validate_filename(filename: str) -> None:
    """Raise ValueError if filename is malformed."""
    if not FILENAME_RE.match(filename):
        raise ValueError(f"Invalid filename: {filename!r}. Must match {FILENAME_RE.pattern}")


def validate_base_ref(base_ref: str | None) -> None:
    """Reject a non-empty base_ref that is not a full 40- or 64-hex commit id."""
    if base_ref is None:
        return
    if not isinstance(base_ref, str):
        raise TypeError(f"base_ref must be a string or None, got {type(base_ref).__name__}")
    if not base_ref:
        return
    if not BASE_REF_RE.fullmatch(base_ref):
        raise ValueError(
            f"Invalid base_ref: {base_ref!r}. Must be a full 40- or 64-character hex commit id"
        )


def _is_valid_shell_command(entry: str) -> bool:
    """Return True if entry tokenizes as a shell command without dangling operators."""
    try:
        tokens = shlex.split(entry, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False
    return tokens[-1] not in _TRAILING_OPERATORS


def _has_command_shape(tokens: list[str]) -> bool:
    """Return True if any token looks command-like."""
    for tok in tokens:
        if "/" in tok or tok.startswith("-") or ENV_ASSIGNMENT_RE.match(tok):
            return True
    return False


def _looks_like_prose(entry: str) -> bool:
    """Return True if entry reads as natural-language prose, not a shell command."""
    words = entry.split()
    if len(words) < 4:
        return False
    if _has_command_shape(words):
        return False
    last = words[-1]
    if len(last) > 1 and last.endswith(_SENTENCE_ENDINGS):
        return True
    function_words = sum(1 for w in words if w.strip(".,;:!?()[]\"'").lower() in _FUNCTION_WORDS)
    return function_words >= 3


def validate_required_checks(required_checks: list[str] | None) -> None:
    """Reject prose/invalid shell syntax before a worker is launched."""
    if required_checks is None:
        return
    if not isinstance(required_checks, list):
        raise TypeError("required_checks must be a list of non-empty shell command strings")
    for idx, check in enumerate(required_checks):
        label = f"required_checks[{idx}]"
        if not isinstance(check, str):
            raise TypeError(f"{label} must be a string, got {type(check).__name__}")
        stripped = check.strip()
        if not stripped:
            raise ValueError(f"{label} must be a non-empty shell command string")
        if not _is_valid_shell_command(stripped):
            raise ValueError(f"{label} is not valid shell syntax: {check!r}")
        if _looks_like_prose(stripped):
            raise ValueError(
                f"{label} looks like acceptance prose, not a shell command: {check!r}. "
                "Put descriptive acceptance criteria in acceptance_criteria, not required_checks."
            )


def validate_scope_contract(
    allowed_files: list[str] | None,
    forbidden_files: list[str] | None,
) -> None:
    """Reject obviously contradictory file-scope contracts before launch."""
    allowed = [item.strip() for item in (allowed_files or []) if isinstance(item, str) and item.strip()]
    forbidden = [item.strip() for item in (forbidden_files or []) if isinstance(item, str) and item.strip()]
    if allowed and any(pattern in {"**", "**/*", "*"} for pattern in forbidden):
        raise ValueError("forbidden_files blocks every allowed file; fix the task scope before launch")
    overlap = sorted(set(allowed).intersection(forbidden))
    if overlap:
        raise ValueError(f"allowed_files and forbidden_files overlap: {', '.join(overlap)}")


def _encoded_write(path: str, content: str) -> str:
    """Build one shell-safe file write without interpolating raw content."""
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"printf %s {shlex.quote(payload)} | base64 -d > {shlex.quote(path)}"


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
    base_ref: str | None = None,
) -> str:
    """Build machine-readable task.json content."""
    validate_task_id(task_id)
    validate_required_checks(required_checks)
    validate_scope_contract(allowed_files, forbidden_files)
    validate_base_ref(base_ref)
    data: dict[str, Any] = {
        "task_id": task_id,
        "agent": agent,
        "allowed_files": allowed_files or [],
        "forbidden_files": forbidden_files or [],
        "required_checks": required_checks or [],
        "worktree_path": worktree_path or "",
        "base_ref": base_ref or "",
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
    validate_required_checks(required_checks)
    validate_scope_contract(allowed_files, forbidden_files)
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
    result = run_cmd(project, f"ls -1t {shlex.quote(tasks_dir)}/")
    if result.get("exit_code") != 0:
        return {"stdout": "(no tasks)", "stderr": "", "exit_code": 0}
    all_lines = result.get("stdout", "").splitlines()
    visible = all_lines[:50]
    if len(all_lines) > len(visible):
        visible.append(f"(truncated: showing {len(visible)} of {len(all_lines)} tasks)")
    result["stdout"] = "\n".join(visible)
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
    """Read a file from .ai-bridge/tasks/<task_id>/ via shell."""
    validate_task_id(task_id)
    validate_filename(filename)
    path = f"{task_dir(project, task_id)}/{filename}"
    result = run_cmd(project, f"cat {shlex.quote(path)}")
    if result.get("exit_code") != 0:
        return {"stdout": "(not found)", "stderr": "", "exit_code": 0}
    return result


def read_agent_log_tail(
    run_cmd,
    *,
    project: str,
    task_id: str,
    tail_lines: int = 200,
) -> dict[str, Any]:
    """Read a bounded tail of the live OpenCode stdout/stderr log.

    The filename is fixed so callers can never choose an arbitrary path under
    the coordination directory. Remote output is byte-bounded before it crosses
    the gateway boundary.
    """
    validate_task_id(task_id)
    if isinstance(tail_lines, bool) or not isinstance(tail_lines, int):
        raise TypeError("tail_lines must be an integer")
    if not 1 <= tail_lines <= AGENT_LOG_MAX_TAIL_LINES:
        raise ValueError(
            f"tail_lines must be between 1 and {AGENT_LOG_MAX_TAIL_LINES}"
        )

    path = f"{task_dir(project, task_id)}/{AGENT_LOG_FILENAME}"
    result = run_cmd(
        project,
        f"tail -c {AGENT_LOG_MAX_BYTES + 1} -- {shlex.quote(path)}",
    )
    if result.get("exit_code") != 0:
        return {
            "stdout": "(not found)",
            "stderr": "",
            "exit_code": 0,
            "truncated": False,
        }

    stdout = str(result.get("stdout", ""))
    encoded = stdout.encode("utf-8", errors="replace")
    byte_truncated = len(encoded) > AGENT_LOG_MAX_BYTES
    if byte_truncated:
        stdout = encoded[-AGENT_LOG_MAX_BYTES:].decode("utf-8", errors="replace")
    lines = stdout.splitlines(keepends=True)
    line_truncated = len(lines) > tail_lines
    stdout = "".join(lines[-tail_lines:])
    return {
        "stdout": stdout,
        "stderr": str(result.get("stderr", "")),
        "exit_code": 0,
        "truncated": byte_truncated or line_truncated,
        "tail_lines": tail_lines,
        "max_bytes": AGENT_LOG_MAX_BYTES,
    }


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
    base_ref: str | None = None,
) -> dict[str, Any]:
    """Write task.json + current-plan.md + agent-status.md to .ai-bridge/tasks/<task_id>/."""
    validate_task_id(task_id)

    task_json = build_task_json(
        task_id=task_id,
        agent=agent,
        allowed_files=allowed_files,
        forbidden_files=forbidden_files,
        required_checks=required_checks,
        worktree_path=worktree_path,
        base_ref=base_ref,
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
    if base_ref:
        parts.append(_encoded_write(f"{td}/base-ref.txt", base_ref))
    cmd = "\n".join(parts)
    return run_cmd(project, cmd)
