"""Agent Handoff v2 — .ai-bridge task management for parallel agent execution."""

from __future__ import annotations

import base64
import json
import re
import shlex
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from examples.mcp_server.agent_paths import (
    managed_workspace_path,
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
AGENT_HEARTBEAT_FILENAME = "agent-heartbeat.json"
AGENT_LOG_MAX_BYTES = 64 * 1024
AGENT_LOG_MAX_TAIL_LINES = 1000
AGENT_STALE_AFTER_SECONDS = 600
ATTEMPT_STATE_FILENAME = "attempt-state.json"
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

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

KNOWN_CONCRETE_BACKENDS = frozenset({"opencode"})
WORKFLOW_PHASES = frozenset(
    {"discovery", "validation", "implementation", "verification", "cleanup"}
)
WORKFLOW_PHASE_TRANSITIONS = {
    "discovery": "Move to validation with evidence, risks, and a GO/NO-GO recommendation.",
    "validation": "Move to implementation or terminal blocked; do not keep discussing.",
    "implementation": "Make the smallest scoped change, then move to verification.",
    "verification": "Run required checks, fix scoped failures, then move to cleanup/final report.",
    "cleanup": "Tighten the diff and write the final handoff; do not start new scope.",
}


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


def validate_workflow_phase(workflow_phase: str | None) -> str:
    """Return a normalized workflow phase or raise for unknown phases."""
    if workflow_phase is None or workflow_phase == "":
        return "implementation"
    if not isinstance(workflow_phase, str):
        raise TypeError(
            f"workflow_phase must be a string or None, got {type(workflow_phase).__name__}"
        )
    phase = workflow_phase.strip().lower()
    if phase not in WORKFLOW_PHASES:
        choices = ", ".join(sorted(WORKFLOW_PHASES))
        raise ValueError(f"Invalid workflow_phase: {workflow_phase!r}. Must be one of: {choices}")
    return phase


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


class AttemptStateError(RuntimeError):
    """Raised when the durable attempt state exists but cannot be trusted.

    The durable record binds (attempt_id, fingerprint, job_id) for one logical
    execution attempt. A ``None`` return from the reader means the record is
    definitely ABSENT and it is safe to create the first attempt. Any record
    that exists but cannot be fully validated fails CLOSED with this error:
    treating it as absent would spin up a second execution that can never see
    the first one's fleet job, double-charging the caller.
    """


class AttemptConflictError(AttemptStateError):
    """A task_id is immutable: the record already binds the task to a
    DIFFERENT execution fingerprint, so the request must create a NEW task_id.

    Raised by the identity resolver when the task's durable record and the
    requested fingerprint disagree. Carries both fingerprints and the recorded
    binding so callers can surface a typed ``immutable-task-conflict`` error.
    """

    def __init__(
        self,
        *,
        project: str,
        task_id: str,
        attempt_id: str,
        job_id: str | None,
        recorded_fingerprint: str,
        requested_fingerprint: str,
    ) -> None:
        self.project = project
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.job_id = job_id
        self.recorded_fingerprint = recorded_fingerprint
        self.requested_fingerprint = requested_fingerprint
        super().__init__(
            f"task {task_id} is immutable: it is already bound to attempt "
            f"{attempt_id} (fingerprint {recorded_fingerprint}), which does not "
            f"match the requested fingerprint {requested_fingerprint}; "
            "create a NEW task_id for this different execution"
        )


def _attempt_state_record_errors(record: Any) -> str | None:
    """Return a human-readable reason when ``record`` is not a valid attempt.

    Valid records are dicts with a non-empty string ``attempt_id``, a
    non-empty string ``fingerprint``, and a ``job_id`` that is absent, null,
    or a string. Returns None when the record is acceptable.
    """
    if not isinstance(record, dict):
        return "must be a JSON object"
    attempt_id = record.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return "must contain a non-empty string 'attempt_id'"
    fingerprint = record.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return "must contain a non-empty string 'fingerprint'"
    job_id = record.get("job_id")
    if job_id is not None and not isinstance(job_id, str):
        return "'job_id' must be absent, null, or a string"
    return None


def _atomic_encoded_write(
    path: str,
    content: str,
    *,
    tmp_path: str | None = None,
) -> list[str]:
    """Build trusted-script lines that atomically replace ``path``.

    Production acquires the temp via ``mktemp "$base.XXXXXX"`` in the SAME
    directory: O_EXCL + an unpredictable name, so a planted symlink or a stale
    partial file at a plausible temp name is never followed or reused -- the
    freshly acquired exclusive temp supersedes it. The full write lands in
    that temp and the canonical ``path`` is only ever the destination of an
    atomic ``mv``, so the canonical file is always a complete record (old or
    new), never a partially-written one.

    ``tmp_path`` pins the temp location for tests ONLY: the pinned path must
    collide with nothing (guard exit 51) so a test-induced collision fails
    closed before any write.
    """
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    lines = [
        f"base={shlex.quote(path)}",
    ]
    if tmp_path is None:
        lines.append('tmp=$(mktemp "$base.XXXXXX") || exit 50')
    else:
        lines.append(f"tmp={tmp_path}")
        lines.append('if [ -e "$tmp" ] || [ -L "$tmp" ]; then exit 51; fi')
    lines += [
        f"printf %s {shlex.quote(payload)} | base64 -d > \"$tmp\" "
        '|| { rm -f -- "$tmp"; exit 48; }',
        f"mv -f -- \"$tmp\" {shlex.quote(path)} "
        '|| { rm -f -- "$tmp"; exit 49; }',
    ]
    return lines


def _coordination_path_prefixes(path: str) -> list[str]:
    """Return every lexical component from the trust anchor to ``path``.

    Legacy relative paths are anchored at the gateway-selected project cwd, so
    ``.ai-bridge`` is the first component that must be protected. Configured
    absolute paths are stricter: every component below ``/`` must be a real
    path object, including the configured state root and its parents. This
    deliberately rejects symlink-based aliases for the coordination root.
    """
    parsed = PurePosixPath(path)
    if any(part == ".." for part in parsed.parts):
        raise ValueError("coordination paths must not contain '..'")

    prefixes: list[str] = []
    if parsed.is_absolute():
        current = PurePosixPath("/")
        parts = parsed.parts[1:]
    else:
        current = PurePosixPath()
        parts = parsed.parts

    for part in parts:
        if part in {"", "."}:
            continue
        current /= part
        prefixes.append(str(current))
    return prefixes


def _coordination_guard_paths(paths: list[str]) -> list[str]:
    """Return de-duplicated path prefixes for one coordination operation."""
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        for prefix in _coordination_path_prefixes(path):
            if prefix not in seen:
                seen.add(prefix)
                ordered.append(prefix)
    return ordered


def _symlink_guard_lines(paths: list[str]) -> list[str]:
    """Build trusted-script guards for every component in ``paths``."""
    return [
        f"if [ -L {shlex.quote(prefix)} ]; then exit 46; fi"
        for prefix in _coordination_guard_paths(paths)
    ]


def _readonly_path_is_safe(
    run_cmd,
    *,
    project: str,
    path: str,
) -> bool:
    """Return True only when the full coordination path chain is non-symlink.

    Readonly operations stay on the generic project-command transport. One
    literal ``ls -ld`` probes every lexical prefix; ``-d`` reports each named
    object without following that final component. Naming every ancestor as a
    separate operand exposes an ancestor symlink that would otherwise be hidden
    by an ordinary descendant. Missing paths, malformed output, or any symlink
    fail closed.
    """
    prefixes = _coordination_path_prefixes(path)
    if not prefixes:
        return False
    for prefix in prefixes:
        result = run_cmd(project, f"ls -ld -- {shlex.quote(prefix)}")
        if result.get("exit_code") != 0:
            return False
        stdout = str(result.get("stdout", ""))
        if not stdout or stdout.startswith("l"):
            return False
    return True


def _validate_allowed_backends(
    agent: str,
    allowed_backends: list[str] | None,
) -> list[str]:
    """Validate and normalize allowed_backends at task creation time.

    ``agent="auto"`` is a selection mode, not a concrete backend.  An empty
    allowlist must NOT be treated as a wildcard — it means no backends are
    permitted.  Unknown concrete backend names are rejected at creation.
    """
    if allowed_backends is not None:
        unknown = sorted(set(allowed_backends) - KNOWN_CONCRETE_BACKENDS)
        if unknown:
            raise ValueError(
                f"unknown backend(s) in allowed_backends: {', '.join(unknown)}; "
                f"known concrete backends: {', '.join(sorted(KNOWN_CONCRETE_BACKENDS))}"
            )

    if agent != "auto" and agent not in KNOWN_CONCRETE_BACKENDS:
        raise ValueError(
            f"agent={agent!r} is not a known concrete backend; "
            f"known: {', '.join(sorted(KNOWN_CONCRETE_BACKENDS))}"
        )

    if allowed_backends is None:
        if agent == "auto":
            return ["opencode"]
        return [agent]

    if not allowed_backends:
        raise ValueError(
            "allowed_backends is empty; an explicit empty allowlist cannot "
            "become a wildcard — provide at least one concrete backend"
        )

    return list(allowed_backends)


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
    allowed_backends: list[str] | None = None,
    managed_source_sha256: str | None = None,
    workflow_phase: str | None = None,
) -> str:
    """Build machine-readable task.json content."""
    validate_task_id(task_id)
    validate_required_checks(required_checks)
    validate_scope_contract(allowed_files, forbidden_files)
    validate_base_ref(base_ref)
    normalized_workflow_phase = validate_workflow_phase(workflow_phase)
    if managed_source_sha256 is not None and (
        not isinstance(managed_source_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", managed_source_sha256)
    ):
        raise ValueError(
            "managed_source_sha256 must be a 64-character lowercase hex digest"
        )
    normalized_backends = _validate_allowed_backends(agent, allowed_backends)
    data: dict[str, Any] = {
        "task_id": task_id,
        "agent": agent,
        "allowed_backends": normalized_backends,
        "allowed_files": allowed_files or [],
        "forbidden_files": forbidden_files or [],
        "required_checks": required_checks or [],
        "worktree_path": worktree_path or "",
        "base_ref": base_ref or "",
        "managed_source_sha256": managed_source_sha256 or "",
        "workflow_phase": normalized_workflow_phase,
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


def build_task_consensus(
    *,
    task_id: str,
    task: str,
    workflow_phase: str | None = None,
    artifact_dir: str | None = None,
) -> str:
    """Build operator-readable consensus.md baton-state content."""
    validate_task_id(task_id)
    phase = validate_workflow_phase(workflow_phase)
    artifacts = artifact_dir or f"{TASKS_REL_DIR}/{task_id}"
    next_action = WORKFLOW_PHASE_TRANSITIONS[phase]
    return (
        "# Agent consensus\n\n"
        "This file is the durable baton state for long-running agent work. "
        "Keep it short, factual, and updated when decisions change.\n\n"
        "## Task\n\n"
        f"- Task ID: {task_id}\n"
        f"- Title: {task}\n"
        f"- Workflow phase: {phase}\n"
        f"- Created: {datetime.now(UTC).isoformat()}\n\n"
        "## Current consensus\n\n"
        "- Initial state: read current-plan.md, preserve scope, and record material decisions here.\n"
        "- Do not re-litigate settled decisions unless new evidence appears.\n\n"
        "## Convergence rule\n\n"
        "- Discovery must produce evidence and a validation question.\n"
        "- Validation must produce GO/NO-GO and an implementation plan.\n"
        "- Implementation must produce a scoped diff, not more discussion.\n"
        "- Verification must run required checks or explain a hard blocker.\n"
        "- Cleanup must finalize the handoff and avoid new scope.\n\n"
        "## Next action\n\n"
        f"- {next_action}\n"
        f"- Update `{artifacts}/agent-status.md` for progress and this file for decisions.\n"
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
    workflow_phase: str | None = None,
) -> str:
    """Build human-readable current-plan.md content."""
    validate_task_id(task_id)
    validate_required_checks(required_checks)
    validate_scope_contract(allowed_files, forbidden_files)
    phase = validate_workflow_phase(workflow_phase)
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
        f"- Created: {datetime.now(UTC).isoformat()}\n"
        f"- Workflow phase: {phase}\n\n"
        f"## Workflow phase\n\n"
        f"Current phase: `{phase}`. {WORKFLOW_PHASE_TRANSITIONS[phase]}\n\n"
        f"Forced convergence: discovery → validation → implementation → "
        f"verification → cleanup. After validation, pure discussion is not "
        f"a sufficient deliverable.\n\n"
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
        + f"Keep durable decisions and handoff state in `{artifacts}/consensus.md`.\n"
        + f"Save final diff to `{artifacts}/implementation-diff.patch`.\n"
        + "Do not commit or push unless explicitly instructed.\n"
    )


def list_agent_tasks(run_cmd, *, project: str) -> dict[str, Any]:
    """List task directories from the configured coordination plane."""
    tasks_dir = task_tasks_dir(project)
    if not _readonly_path_is_safe(run_cmd, project=project, path=tasks_dir):
        return {"stdout": "(no tasks)", "stderr": "", "exit_code": 0}
    result = run_cmd(project, f"ls -1t {shlex.quote(tasks_dir)}/")
    if result.get("exit_code") != 0:
        return {"stdout": "(no tasks)", "stderr": "", "exit_code": 0}
    all_lines = result.get("stdout", "").splitlines()
    visible = all_lines[:50]
    if len(all_lines) > len(visible):
        visible.append(f"(truncated: showing {len(visible)} of {len(all_lines)} tasks)")
    result["stdout"] = "\n".join(visible)
    return result


def archive_agent_task(run_script, *, project: str, task_id: str) -> dict[str, Any]:
    """Move a task into the configured archive; never physically delete it."""
    validate_task_id(task_id)
    src = task_dir(project, task_id)
    archive_dir = task_archive_dir(project)
    dst = task_archive_path(project, task_id)
    guard_lines = _symlink_guard_lines([src, archive_dir, dst])
    script = "\n".join(
        [
            f"src={shlex.quote(src)}",
            f"archive_dir={shlex.quote(archive_dir)}",
            f"dst={shlex.quote(dst)}",
            *guard_lines,
            # A repeated archive is idempotent only when the source is gone
            # and the destination is an existing directory. Any other partial
            # state fails closed instead of guessing which copy is canonical.
            'if [ ! -e "$src" ]; then',
            '  if [ -d "$dst" ]; then exit 45; fi',
            '  if [ -e "$dst" ]; then exit 46; fi',
            '  exit 44',
            'fi',
            'if [ ! -d "$src" ]; then exit 46; fi',
            'if [ -e "$dst" ]; then exit 48; fi',
            'mkdir -p "$archive_dir" || exit 47',
            *guard_lines,
            'if [ -e "$dst" ]; then exit 48; fi',
            # -T prevents a raced-in destination directory from changing mv
            # semantics into "move src inside dst". -- terminates options.
            'mv -T -- "$src" "$dst" || exit 47',
        ]
    )
    result = run_script(project, script)
    exit_code = result.get("exit_code")
    if exit_code == 44:
        return {"stdout": f"task {task_id} not found", "stderr": "", "exit_code": 1}
    if exit_code == 45:
        return {"stdout": f"already archived {task_id}", "stderr": "", "exit_code": 0}
    if exit_code == 48:
        return {"stdout": "", "stderr": f"archive already contains task {task_id}", "exit_code": 1}
    if exit_code != 0:
        return {"stdout": "", "stderr": f"failed to archive task {task_id}", "exit_code": 1}
    return {"stdout": f"archived {task_id}", "stderr": "", "exit_code": 0}


def read_agent_task_file(run_cmd, *, project: str, task_id: str, filename: str) -> dict[str, Any]:
    """Read a file from .ai-bridge/tasks/<task_id>/ without following symlinks."""
    validate_task_id(task_id)
    validate_filename(filename)
    td = task_dir(project, task_id)
    path = f"{td}/{filename}"
    if not _readonly_path_is_safe(run_cmd, project=project, path=path):
        return {"stdout": "(not found)", "stderr": "", "exit_code": 0}
    result = run_cmd(project, f"cat {shlex.quote(path)}")
    if result.get("exit_code") != 0:
        return {"stdout": "(not found)", "stderr": "", "exit_code": 0}
    return result


def _normalize_agent_log_text(project: str, task_id: str, text: str) -> str:
    """Remove terminal control sequences and executor-owned absolute paths."""
    normalized = _ANSI_ESCAPE_RE.sub("", text)
    prefixes = [
        (task_dir(project, task_id), "<agent-task>"),
        (managed_workspace_path(project, task_id), "<agent-workspace>"),
    ]
    for prefix, replacement in prefixes:
        if prefix:
            normalized = normalized.replace(prefix, replacement)
    return normalized


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

    td = task_dir(project, task_id)
    path = f"{td}/{AGENT_LOG_FILENAME}"
    if not _readonly_path_is_safe(run_cmd, project=project, path=path):
        return {
            "stdout": "(not found)",
            "stderr": "",
            "exit_code": 0,
            "truncated": False,
        }
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
    stdout = _normalize_agent_log_text(project, task_id, stdout)
    stderr = _normalize_agent_log_text(
        project,
        task_id,
        str(result.get("stderr", "")),
    )
    lines = stdout.splitlines(keepends=True)
    line_truncated = len(lines) > tail_lines
    stdout = "".join(lines[-tail_lines:])
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": 0,
        "truncated": byte_truncated or line_truncated,
        "tail_lines": tail_lines,
        "max_bytes": AGENT_LOG_MAX_BYTES,
    }



_AGENT_TERMINAL_STATUSES = frozenset(
    {
        "needs-review",
        "needs-review-warning",
        "blocked",
        "rate-limited",
        "resource-exhausted",
        "startup-timeout",
        "run-timeout",
        "evidence-failed",
        "scope-failed",
        "checks-failed",
        "parent-guard-failed",
        "supervisor-failed",
        "failed",
        "completed",
        "cancelled",
    }
)
_AGENT_ACTIVE_STATUSES = frozenset({"created", "pending", "processing", "running", "cancelling"})
_STARTUP_STALLED_RE = re.compile(r"OpenCode startup stalled; rotating proxy \(attempt (\d+)/(\d+)\)")
_USEFUL_AGENT_ACTIVITY_MARKERS = (
    "← Write ",
    "Wrote file successfully",
    "$ cd ",
    "# Todos",
    "Implementation",
    "agent-report.md",
    "implementation-diff.patch",
)


def _parse_agent_status(text: str) -> str | None:
    """Extract the first-line ``Status: ...`` token from agent-status.md."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.lower().startswith("status:"):
            return None
        value = stripped.split(":", 1)[1].strip().split()
        return value[0].lower() if value else None
    return None


def _task_file_stat(run_cmd, *, project: str, task_id: str, filename: str) -> dict[str, Any]:
    """Return path-safe size/mtime metadata for a fixed task artifact."""
    validate_task_id(task_id)
    validate_filename(filename)
    path = f"{task_dir(project, task_id)}/{filename}"
    if not _readonly_path_is_safe(run_cmd, project=project, path=path):
        return {"exists": False}
    result = run_cmd(project, f"stat -c '%s %Y' -- {shlex.quote(path)}")
    if result.get("exit_code") != 0:
        return {"exists": False}
    parts = str(result.get("stdout", "")).strip().split()
    if len(parts) < 2:
        return {"exists": True, "size_bytes": None, "mtime_epoch": None}
    try:
        size = int(parts[0])
        mtime = int(float(parts[1]))
    except ValueError:
        return {"exists": True, "size_bytes": None, "mtime_epoch": None}
    return {"exists": True, "size_bytes": size, "mtime_epoch": mtime}



def _read_agent_heartbeat(
    run_cmd,
    *,
    project: str,
    task_id: str,
    now_epoch: int,
) -> dict[str, Any]:
    """Read and sanitize the runner heartbeat record, if present."""
    result = read_agent_task_file(
        run_cmd, project=project, task_id=task_id, filename=AGENT_HEARTBEAT_FILENAME
    )
    text = str(result.get("stdout", ""))
    if text == "(not found)":
        return {"exists": False}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {"exists": True, "valid": False, "error": "heartbeat is not valid JSON"}
    if not isinstance(data, dict):
        return {"exists": True, "valid": False, "error": "heartbeat is not a JSON object"}

    summary: dict[str, Any] = {"exists": True, "valid": True}
    for key in ("state", "phase", "updated_at"):
        value = data.get(key)
        if isinstance(value, str) and value:
            summary[key] = value[:120]
    for key in ("updated_epoch", "runner_pid", "exit_code"):
        value = data.get(key)
        if isinstance(value, int) or value is None:
            summary[key] = value
    updated_epoch = summary.get("updated_epoch")
    if isinstance(updated_epoch, int):
        summary["age_seconds"] = max(0, now_epoch - updated_epoch)
    else:
        summary["age_seconds"] = None
    return summary


def _safe_attempt_summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    summary: dict[str, Any] = {}
    for key in ("attempt_id", "job_id", "created_at", "submitted_at", "status"):
        value = record.get(key)
        if isinstance(value, str) and value:
            summary[key] = value
    return summary or None


def _safe_job_summary(job_status, job_id: str | None) -> dict[str, Any] | None:
    if not job_id or job_status is None:
        return None
    try:
        snapshot = job_status(job_id)
    except Exception as exc:
        return {"job_id": job_id, "known": False, "error": str(exc)[:500]}
    if not isinstance(snapshot, dict):
        return {"job_id": job_id, "known": False, "error": "job_status returned non-object"}
    summary: dict[str, Any] = {"job_id": job_id, "known": True}
    for key in ("status", "exit_code", "created_at", "started_at", "finished_at"):
        value = snapshot.get(key)
        if isinstance(value, (str, int)) or value is None:
            summary[key] = value
    return summary


def _job_status_token(job: dict[str, Any] | None) -> str | None:
    value = (job or {}).get("status")
    return value.lower() if isinstance(value, str) and value else None


def _latest_activity(files: dict[str, dict[str, Any]], now_epoch: int) -> dict[str, Any]:
    latest_name: str | None = None
    latest_mtime: int | None = None
    for name, meta in files.items():
        mtime = meta.get("mtime_epoch")
        if isinstance(mtime, int) and (latest_mtime is None or mtime > latest_mtime):
            latest_name = name
            latest_mtime = mtime
    if latest_mtime is None:
        return {"source": None, "mtime_epoch": None, "age_seconds": None}
    return {
        "source": latest_name,
        "mtime_epoch": latest_mtime,
        "age_seconds": max(0, now_epoch - latest_mtime),
    }



def _agent_startup_diagnostics(
    *,
    status: str | None,
    status_text: str,
    log_stdout: str,
    files: dict[str, dict[str, Any]],
    active: bool,
) -> dict[str, Any]:
    """Classify OpenCode startup/proxy dead time separately from useful work."""
    combined = f"{status_text}\n{log_stdout}"
    matches = list(_STARTUP_STALLED_RE.finditer(combined))
    attempts = [int(match.group(1)) for match in matches]
    max_attempts = [int(match.group(2)) for match in matches]
    opencode_startup_stalled = bool(matches or "OpenCode startup stalled" in combined)
    startup_timeout = status == "startup-timeout" or "opencode-startup-timeout" in combined
    useful_agent_activity_seen = bool(
        (files.get("report") or {}).get("exists")
        or (files.get("diff") or {}).get("exists")
        or any(marker in combined for marker in _USEFUL_AGENT_ACTIVITY_MARKERS)
    )
    dead_time_kind = None
    if active and opencode_startup_stalled and not useful_agent_activity_seen:
        dead_time_kind = "opencode_startup"
    return {
        "startup_timeout": startup_timeout,
        "opencode_startup_stalled": opencode_startup_stalled,
        "proxy_rotation": {
            "observed": bool(matches),
            "attempt": max(attempts) if attempts else None,
            "max_attempts": max(max_attempts) if max_attempts else None,
            "count": len(matches),
        },
        "useful_agent_activity_seen": useful_agent_activity_seen,
        "dead_time_kind": dead_time_kind,
    }

def inspect_agent_task(
    run_cmd,
    *,
    project: str,
    task_id: str,
    tail_lines: int = 120,
    stale_after_seconds: int = AGENT_STALE_AFTER_SECONDS,
    job_status=None,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    """Inspect one agent task and classify whether it is active, done, or stale.

    This is a read-only operator diagnostic: it aggregates agent-status.md,
    attempt-state.json, gateway job status, fixed artifact metadata and a
    bounded log tail into one path-safe result so a caller does not need to
    infer "hung" from several separate tools.
    """
    validate_task_id(task_id)
    if isinstance(stale_after_seconds, bool) or not isinstance(stale_after_seconds, int):
        raise TypeError("stale_after_seconds must be an integer")
    if not 60 <= stale_after_seconds <= 86_400:
        raise ValueError("stale_after_seconds must be between 60 and 86400")
    now = int(time.time()) if now_epoch is None else int(now_epoch)

    td = task_dir(project, task_id)
    if not _readonly_path_is_safe(run_cmd, project=project, path=td):
        return {
            "project": project,
            "task_id": task_id,
            "exists": False,
            "verdict": "missing",
            "terminal": False,
            "likely_hung": False,
            "stale_after_seconds": stale_after_seconds,
        }

    status_result = read_agent_task_file(
        run_cmd, project=project, task_id=task_id, filename="agent-status.md"
    )
    status_text = str(status_result.get("stdout", ""))
    status_token = None if status_text == "(not found)" else _parse_agent_status(status_text)

    try:
        attempt_record = read_agent_attempt_state(run_cmd, project=project, task_id=task_id)
        attempt_error = None
    except AttemptStateError as exc:
        attempt_record = None
        attempt_error = str(exc)[:500]
    attempt = _safe_attempt_summary(attempt_record)
    job_id = attempt.get("job_id") if attempt else None
    job = _safe_job_summary(job_status, job_id if isinstance(job_id, str) else None)
    job_token = _job_status_token(job)

    files = {
        "status": _task_file_stat(run_cmd, project=project, task_id=task_id, filename="agent-status.md"),
        "log": _task_file_stat(run_cmd, project=project, task_id=task_id, filename=AGENT_LOG_FILENAME),
        "heartbeat": _task_file_stat(run_cmd, project=project, task_id=task_id, filename=AGENT_HEARTBEAT_FILENAME),
        "report": _task_file_stat(run_cmd, project=project, task_id=task_id, filename="agent-report.md"),
        "diff": _task_file_stat(run_cmd, project=project, task_id=task_id, filename="implementation-diff.patch"),
        "attempt_state": _task_file_stat(run_cmd, project=project, task_id=task_id, filename=ATTEMPT_STATE_FILENAME),
    }
    # Heartbeat proves the wrapper process is alive, but it is deliberately
    # excluded from semantic activity so a stuck/silent agent is not hidden by
    # the runner's periodic keepalive.
    activity = _latest_activity(
        {name: meta for name, meta in files.items() if name != "heartbeat"}, now
    )
    age = activity.get("age_seconds")
    heartbeat = _read_agent_heartbeat(
        run_cmd, project=project, task_id=task_id, now_epoch=now
    )
    heartbeat_age = heartbeat.get("age_seconds")
    runner_heartbeat_fresh = bool(
        heartbeat.get("state") == "running"
        and isinstance(heartbeat_age, int)
        and heartbeat_age < stale_after_seconds
    )

    terminal = bool(status_token in _AGENT_TERMINAL_STATUSES or job_token in _AGENT_TERMINAL_STATUSES)
    active = bool(status_token in _AGENT_ACTIVE_STATUSES or job_token in _AGENT_ACTIVE_STATUSES)
    likely_hung = bool(active and not terminal and isinstance(age, int) and age >= stale_after_seconds)

    log = read_agent_log_tail(run_cmd, project=project, task_id=task_id, tail_lines=tail_lines)
    startup = _agent_startup_diagnostics(
        status=status_token,
        status_text=status_text if status_text != "(not found)" else "",
        log_stdout=str(log.get("stdout", "")),
        files=files,
        active=active,
    )

    if terminal:
        verdict = "finished"
    elif startup.get("dead_time_kind") == "opencode_startup":
        verdict = "startup_stalled"
    elif likely_hung:
        verdict = "likely_hung"
    elif active:
        verdict = "running"
    elif status_token is None and job is None and attempt_error is None:
        verdict = "unknown"
    else:
        verdict = "needs_attention"

    result: dict[str, Any] = {
        "project": project,
        "task_id": task_id,
        "exists": True,
        "status": status_token,
        "job": job,
        "attempt": attempt,
        "attempt_state_error": attempt_error,
        "files": files,
        "last_activity": activity,
        "runner_heartbeat": heartbeat,
        "runner_heartbeat_fresh": runner_heartbeat_fresh,
        "startup": startup,
        "stale_after_seconds": stale_after_seconds,
        "terminal": terminal,
        "likely_hung": likely_hung,
        "verdict": verdict,
        "log": {
            "stdout": log.get("stdout", ""),
            "stderr": log.get("stderr", ""),
            "truncated": bool(log.get("truncated", False)),
            "tail_lines": log.get("tail_lines", tail_lines),
        },
    }
    if status_text != "(not found)":
        result["status_text"] = status_text
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
    base_ref: str | None = None,
    allowed_backends: list[str] | None = None,
    managed_source_sha256: str | None = None,
    workflow_phase: str | None = None,
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
        allowed_backends=allowed_backends,
        managed_source_sha256=managed_source_sha256,
        workflow_phase=workflow_phase,
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
        workflow_phase=workflow_phase,
    )
    consensus = build_task_consensus(
        task_id=task_id,
        task=task,
        workflow_phase=workflow_phase,
        artifact_dir=td,
    )
    initial_status = build_initial_status(agent=agent, task_id=task_id)

    tasks_dir = task_tasks_dir(project)
    targets = [
        f"{td}/task.json",
        f"{td}/current-plan.md",
        f"{td}/consensus.md",
        f"{td}/agent-status.md",
    ]
    if worktree_path:
        targets.append(f"{td}/worktree-path.txt")
    if base_ref:
        targets.append(f"{td}/base-ref.txt")

    # This operation is deliberately a trusted generated script: it mutates
    # several server-owned files and needs pipes/redirections. The same full
    # ancestry invariant as readonly operations applies before and after mkdir.
    guard_lines = _symlink_guard_lines([tasks_dir, td, *targets])
    parts = [
        f"tasks_dir={shlex.quote(tasks_dir)}",
        f"td={shlex.quote(td)}",
        *guard_lines,
        'mkdir -p "$td" || exit 47',
        *guard_lines,
    ]
    parts.extend(
        [
            _encoded_write(f"{td}/task.json", task_json),
            _encoded_write(f"{td}/current-plan.md", current_plan),
            _encoded_write(f"{td}/consensus.md", consensus),
            _encoded_write(f"{td}/agent-status.md", initial_status),
        ]
    )
    if worktree_path:
        parts.append(_encoded_write(f"{td}/worktree-path.txt", worktree_path))
    if base_ref:
        parts.append(_encoded_write(f"{td}/base-ref.txt", base_ref))
    result = run_cmd(project, "\n".join(parts))
    if result.get("exit_code") != 0:
        return {
            "stdout": "",
            "stderr": f"failed to write task {task_id}",
            "exit_code": 1,
        }
    return result



def cancel_agent_task(
    run_cmd,
    *,
    project: str,
    task_id: str,
    cancel_job,
) -> dict[str, Any]:
    """Request cancellation for the gateway job bound to an agent task.

    The caller supplies the gateway cancel primitive; this helper only resolves
    the durable attempt record and refuses to guess a job identity from logs or
    status text.
    """
    validate_task_id(task_id)
    record = read_agent_attempt_state(run_cmd, project=project, task_id=task_id)
    if record is None:
        return {
            "task_id": task_id,
            "status": "missing",
            "cancel_requested": False,
            "message": "agent attempt state not found",
        }
    job_id = record.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return {
            "task_id": task_id,
            "attempt_id": record.get("attempt_id"),
            "status": "not-submitted",
            "cancel_requested": False,
            "message": "agent attempt exists but has no bound job_id",
        }
    cancelled = cancel_job(job_id)
    if not isinstance(cancelled, dict):
        cancelled = {"status": "unknown", "job_id": job_id}
    return {
        "task_id": task_id,
        "attempt_id": record.get("attempt_id"),
        "job_id": job_id,
        "status": cancelled.get("status"),
        "cancel_requested": True,
        "gateway": cancelled,
        "diagnostics": {
            "inspect_agent_task": {
                "project": project,
                "task_id": task_id,
                "purpose": "verify cancellation outcome, artifact mtimes, heartbeat, and log tail",
            },
            "job_status": {"job_id": job_id, "purpose": "gateway job state after cancellation"},
        },
    }


def _retry_plan_text(plan: str, *, source_task_id: str, retry_task_id: str) -> str:
    prefix = (
        f"# Retry of {source_task_id}\n\n"
        f"- Source task ID: {source_task_id}\n"
        f"- Retry task ID: {retry_task_id}\n"
        f"- Prepared: {datetime.now(UTC).isoformat()}\n\n"
    )
    return prefix + (plan if plan.endswith("\n") else plan + "\n")


def _load_retry_task_contract(
    run_cmd,
    *,
    project: str,
    source_task_id: str,
) -> dict[str, Any]:
    result = read_agent_task_file(
        run_cmd,
        project=project,
        task_id=source_task_id,
        filename="task.json",
    )
    text = str(result.get("stdout", ""))
    if text == "(not found)":
        raise AttemptStateError(f"source task {source_task_id} has no task.json")
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise AttemptStateError(f"source task {source_task_id} task.json is invalid") from exc
    if not isinstance(data, dict):
        raise AttemptStateError(f"source task {source_task_id} task.json is invalid")
    return data


def prepare_agent_task_retry(
    run_cmd,
    run_script,
    *,
    project: str,
    source_task_id: str,
    retry_task_id: str,
    job_status=None,
) -> dict[str, Any]:
    """Prepare a new immutable task from a terminal/cancelled source task.

    The retry receives a fresh task directory and intentionally does not copy
    attempt-state.json, logs, reports, diffs, or heartbeats. The caller must run
    the returned retry_task_id explicitly through run_agent/run_opencode.
    """
    validate_task_id(source_task_id)
    validate_task_id(retry_task_id)
    if source_task_id == retry_task_id:
        raise ValueError("retry_task_id must be different from source_task_id")

    inspection = inspect_agent_task(
        run_cmd,
        project=project,
        task_id=source_task_id,
        tail_lines=20,
        job_status=job_status,
    )
    if not inspection.get("exists"):
        return {
            "stdout": "",
            "stderr": f"source task {source_task_id} not found",
            "exit_code": 1,
            "code": "TASK_NOT_FOUND",
        }
    attempt = inspection.get("attempt") if isinstance(inspection.get("attempt"), dict) else None
    not_submitted = bool(attempt and not attempt.get("job_id"))
    if not inspection.get("terminal") and not not_submitted:
        return {
            "stdout": "",
            "stderr": "source task is not terminal; cancel it and wait for terminal state before retrying",
            "exit_code": 1,
            "code": "AGENT_TASK_NOT_TERMINAL",
            "source": {
                "task_id": source_task_id,
                "status": inspection.get("status"),
                "verdict": inspection.get("verdict"),
                "job": inspection.get("job"),
            },
        }

    contract = _load_retry_task_contract(
        run_cmd,
        project=project,
        source_task_id=source_task_id,
    )
    agent = str(contract.get("agent") or "opencode")
    retry_contract = dict(contract)
    retry_contract["task_id"] = retry_task_id
    retry_contract["created"] = datetime.now(UTC).isoformat()
    # Validate the copied immutable contract before persisting it.
    validate_required_checks(retry_contract.get("required_checks") or [])
    validate_scope_contract(
        retry_contract.get("allowed_files") or [],
        retry_contract.get("forbidden_files") or [],
    )
    validate_base_ref(retry_contract.get("base_ref") or None)

    plan_result = read_agent_task_file(
        run_cmd,
        project=project,
        task_id=source_task_id,
        filename="current-plan.md",
    )
    plan = str(plan_result.get("stdout", ""))
    if plan == "(not found)":
        plan = ""
    phase = validate_workflow_phase(str(retry_contract.get("workflow_phase") or "implementation"))
    status = build_initial_status(agent, retry_task_id) + f"\nRetry of: {source_task_id}\n"
    td = task_dir(project, retry_task_id)
    consensus = build_task_consensus(
        task_id=retry_task_id,
        task=f"Retry of {source_task_id}",
        workflow_phase=phase,
        artifact_dir=td,
    )
    files = {
        f"{td}/task.json": json.dumps(retry_contract, indent=2, ensure_ascii=False),
        f"{td}/current-plan.md": _retry_plan_text(
            plan,
            source_task_id=source_task_id,
            retry_task_id=retry_task_id,
        ),
        f"{td}/consensus.md": consensus,
        f"{td}/agent-status.md": status,
    }
    tasks_dir = task_tasks_dir(project)
    guard_paths = [tasks_dir, td, *files]
    parts = [
        f"tasks_dir={shlex.quote(tasks_dir)}",
        f"td={shlex.quote(td)}",
        *_symlink_guard_lines(guard_paths),
        'if [ -e "$td" ]; then exit 48; fi',
        'mkdir -p "$td" || exit 47',
        *_symlink_guard_lines(guard_paths),
    ]
    for target, content in files.items():
        parts.append(_encoded_write(target, content))
    result = run_script(project, "\n".join(parts) + "\n")
    exit_code = int(result.get("exit_code", 1))
    if exit_code == 0:
        return {
            "stdout": f"prepared retry task {retry_task_id} from {source_task_id}",
            "stderr": "",
            "exit_code": 0,
            "source_task_id": source_task_id,
            "retry_task_id": retry_task_id,
            "source": {
                "status": inspection.get("status"),
                "verdict": inspection.get("verdict"),
                "job": inspection.get("job"),
            },
            "next": {
                "run_agent": {"project": project, "task_id": retry_task_id},
                "run_opencode": {"project": project, "task_id": retry_task_id},
            },
        }
    if exit_code == 48:
        return {"stdout": "", "stderr": f"retry task {retry_task_id} already exists", "exit_code": 1, "code": "ALREADY_EXISTS"}
    if exit_code == 46:
        return {"stdout": "", "stderr": "retry task path rejected by symlink guard", "exit_code": 1, "code": "POLICY_DENIED"}
    return {"stdout": "", "stderr": f"failed to prepare retry task {retry_task_id}", "exit_code": 1, "code": "TOOL_EXECUTION_FAILED"}


def read_agent_attempt_state(
    run_cmd,
    *,
    project: str,
    task_id: str,
) -> dict[str, Any] | None:
    """Read the durable execution-attempt record.

    The record binds (attempt_id, command fingerprint, job_id) for one
    logical execution attempt so a retry / reconnect / process restart
    converges on the SAME gateway job instead of launching a second agent.
    Returns None ONLY when the record is definitely absent (the whole task
    directory chain does not exist yet); a first attempt is then safe to
    create. Any existing-but-untrustworthy record (symlink-unsafe path,
    permission failure, unparseable/truncated JSON, missing required fields,
    invalid types) fails CLOSED with AttemptStateError: the durable sync
    path surfaces kind=durable-state-error instead of launching a second,
    identity-less execution.
    """
    validate_task_id(task_id)
    td = task_dir(project, task_id)
    path = f"{td}/{ATTEMPT_STATE_FILENAME}"
    for prefix in _coordination_path_prefixes(path):
        result = run_cmd(project, f"ls -ld -- {shlex.quote(prefix)}")
        if result.get("exit_code") == 0:
            if str(result.get("stdout", "")).startswith("l"):
                raise AttemptStateError(
                    f"attempt state path for task {task_id} resolves through a symlink"
                )
            continue
        if "No such file" in str(result.get("stderr", "")):
            return None
        raise AttemptStateError(
            f"cannot verify attempt state path for task {task_id}: remote probe failed"
        )
    result = run_cmd(project, f"cat {shlex.quote(path)}")
    if result.get("exit_code") != 0:
        raise AttemptStateError(
            f"attempt state for task {task_id} exists but cannot be read"
        )
    try:
        record = json.loads(str(result.get("stdout", "")))
    except (ValueError, TypeError) as exc:
        raise AttemptStateError(
            f"attempt state for task {task_id} is not valid JSON"
        ) from exc
    reason = _attempt_state_record_errors(record)
    if reason:
        raise AttemptStateError(
            f"invalid attempt state for task {task_id}: record {reason}"
        )
    return record


def write_agent_attempt_state(
    run_cmd,
    *,
    project: str,
    task_id: str,
    record: dict[str, Any],
) -> None:
    """Persist the attempt record BEFORE the first submission.

    Mirrors write_agent_task's trusted-generated-script invariants: the
    write is symlink-guarded around mkdir, the content travels base64-encoded
    so EOF/quoting can never corrupt it, and the canonical file is replaced
    atomically (a full write to a unique same-dir temp file, then an atomic
    mv). Raises RuntimeError when the remote write fails so the caller fails
    closed instead of submitting a second, identity-less execution.
    """
    validate_task_id(task_id)
    td = task_dir(project, task_id)
    tasks_dir = task_tasks_dir(project)
    target = f"{td}/{ATTEMPT_STATE_FILENAME}"
    payload = json.dumps(record, sort_keys=True)
    guard_lines = _symlink_guard_lines([tasks_dir, td, target])
    parts = [
        f"td={shlex.quote(td)}",
        *guard_lines,
        'mkdir -p "$td" || exit 47',
        *guard_lines,
        *_atomic_encoded_write(target, payload),
    ]
    result = run_cmd(project, "\n".join(parts))
    if result.get("exit_code") != 0:
        raise RuntimeError(f"failed to persist attempt state for task {task_id}")


def claim_agent_attempt_state(
    run_cmd,
    *,
    project: str,
    task_id: str,
    record: dict[str, Any],
) -> bool:
    """Atomically create the attempt record, create-if-absent (CAS).

    The producer of the concurrent first-attempt race -- two callers BOTH
    observed the task absent on their initial read -- is resolved remotely and
    atomically: a same-directory exclusive ``mktemp`` file is fully written
    (its name is opaque, so a pre-planted symlink is ignored), then hard-linked
    into place with ``ln``. ``ln`` fails exactly when the canonical already
    exists, so exactly ONE caller wins the claim; the loser re-reads the winner
    instead of creating a parallel execution. The claim never overwrites.

    Returns True when THIS call created the record (winner); False when the
    canonical already belonged to someone else (the caller must re-read the
    winner). Any other remote failure raises AttemptStateError (fail closed).
    """
    validate_task_id(task_id)
    td = task_dir(project, task_id)
    tasks_dir = task_tasks_dir(project)
    target = f"{td}/{ATTEMPT_STATE_FILENAME}"
    payload = base64.b64encode(
        json.dumps(record, sort_keys=True).encode("utf-8")
    ).decode("ascii")
    guard_lines = _symlink_guard_lines([tasks_dir, td, target])
    parts = [
        f"td={shlex.quote(td)}",
        *guard_lines,
        'mkdir -p "$td" || exit 47',
        *guard_lines,
        f'payload={shlex.quote(payload)}',
        f'tmp=$(mktemp {shlex.quote(target + ".XXXXXX")}) || exit 50',
        'printf %s "$payload" | base64 -d > "$tmp" || { rm -f -- "$tmp"; exit 48; }',
        f'if ln "$tmp" {shlex.quote(target)} 2> /dev/null; then '
        'rm -f -- "$tmp"; exit 0; fi',
        f'if [ -e {shlex.quote(target)} ] || [ -L {shlex.quote(target)} ]; then '
        'rm -f -- "$tmp"; exit 52; fi',
        'rm -f -- "$tmp"; exit 53',
    ]
    result = run_cmd(project, "\n".join(parts))
    code = result.get("exit_code")
    if code == 0:
        return True
    if code == 52:
        return False
    stderr = str(result.get("stderr", "")).strip()
    detail = stderr or (f"remote claim exited {code}" if code is not None else "no response")
    raise AttemptStateError(f"attempt claim for task {task_id} failed: {detail}")
