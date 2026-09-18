"""Agent Handoff v2 — .ai-bridge task management for parallel agent execution."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import stat
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
AGENT_PROXY_STATUS_FILENAME = "proxy-status.json"
AGENT_FAILURE_STATUS_FILENAME = "failure-status.json"
GATES_MD_FILENAME = "GATES.md"
GATE_LEDGER_FILENAME = "gate-ledger.json"
AGENT_LOG_MAX_BYTES = 64 * 1024
AGENT_LOG_MAX_TAIL_LINES = 1000
AGENT_ARTIFACT_MAX_BYTES = 64 * 1024
AGENT_ARTIFACT_MAX_TAIL_LINES = 1000
AGENT_ARTIFACT_FILENAMES: dict[str, str] = {
    "status": "agent-status.md",
    "report": "agent-report.md",
    "diff": "implementation-diff.patch",
    "log": AGENT_LOG_FILENAME,
    "heartbeat": AGENT_HEARTBEAT_FILENAME,
    "proxy_status": AGENT_PROXY_STATUS_FILENAME,
    "failure_status": AGENT_FAILURE_STATUS_FILENAME,
    "worker_status": "worker-status.md",
    "required_checks": "required-checks.log",
    "consensus": "consensus.md",
    "task": "task.json",
    "gates": GATES_MD_FILENAME,
    "gate_ledger": GATE_LEDGER_FILENAME,
}
_AGENT_ARTIFACTS_BY_FILENAME = {filename: key for key, filename in AGENT_ARTIFACT_FILENAMES.items()}
AGENT_STALE_AFTER_SECONDS = 600
AGENT_REASONING_LOOP_AFTER_SECONDS = 120
AGENT_REASONING_LOOP_MIN_LINES = 10
AGENT_REASONING_LOOP_CONTINUATION_PROMPT = "Продолжай"
AGENT_TRAILING_COLON_AFTER_SECONDS = 120
AGENT_EMITTED_INVOKE_AFTER_SECONDS = 120
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
    {"discovery", "validation", "implementation", "verification", "review", "cleanup"}
)
SOURCE_MODE_COMMITTED_HEAD = "committed_head"
SOURCE_MODE_DIRTY_WORKTREE_SNAPSHOT = "dirty_worktree_snapshot"
SOURCE_MODES = frozenset(
    {SOURCE_MODE_COMMITTED_HEAD, SOURCE_MODE_DIRTY_WORKTREE_SNAPSHOT}
)
WORKFLOW_PHASE_TRANSITIONS = {
    "discovery": "Move to validation with evidence, risks, and a GO/NO-GO recommendation.",
    "validation": "Move to implementation or terminal blocked; do not keep discussing.",
    "implementation": "Make the smallest scoped change, then move to verification.",
    "verification": "Run required checks, fix scoped failures, then move to cleanup/final report.",
    "review": "Produce evidence and the final review report; do not implement, commit, or push changes in this task.",
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


def validate_source_mode(source_mode: str | None) -> str:
    """Return the normalized immutable source mode, defaulting old tasks safely."""
    if source_mode is None or source_mode == "":
        return SOURCE_MODE_COMMITTED_HEAD
    if not isinstance(source_mode, str):
        raise TypeError(
            f"source_mode must be a string or None, got {type(source_mode).__name__}"
        )
    mode = source_mode.strip().lower()
    if mode not in SOURCE_MODES:
        choices = ", ".join(sorted(SOURCE_MODES))
        raise ValueError(f"Invalid source_mode: {source_mode!r}. Must be one of: {choices}")
    return mode


def resolve_task_source_contract(task_json: dict[str, Any]) -> dict[str, str | None]:
    """Validate and normalize one task's immutable source provenance contract."""
    raw_base_ref = task_json.get("base_ref")
    validate_base_ref(raw_base_ref)
    base_ref = raw_base_ref.strip() if isinstance(raw_base_ref, str) and raw_base_ref.strip() else None

    mode = validate_source_mode(task_json.get("source_mode"))
    raw_source_ref = task_json.get("source_ref")
    if raw_source_ref is not None and not isinstance(raw_source_ref, str):
        raise TypeError("source_ref must be a string or None")
    source_ref = raw_source_ref.strip() if isinstance(raw_source_ref, str) and raw_source_ref.strip() else None
    validate_base_ref(source_ref)

    raw_tree_sha = task_json.get("source_tree_sha")
    if raw_tree_sha is not None and not isinstance(raw_tree_sha, str):
        raise TypeError("source_tree_sha must be a string or None")
    source_tree_sha = raw_tree_sha.strip() if isinstance(raw_tree_sha, str) and raw_tree_sha.strip() else None
    validate_base_ref(source_tree_sha)

    raw_digest = task_json.get("managed_source_sha256")
    if raw_digest is not None and not isinstance(raw_digest, str):
        raise TypeError("managed_source_sha256 must be a string or None")
    managed_source_sha256 = raw_digest.strip() if isinstance(raw_digest, str) and raw_digest.strip() else None
    if managed_source_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", managed_source_sha256) is None:
        raise ValueError(
            "managed_source_sha256 must be a 64-character lowercase hex digest"
        )

    if mode == SOURCE_MODE_COMMITTED_HEAD:
        resolved_ref = source_ref or base_ref
        if source_ref and base_ref and source_ref.lower() != base_ref.lower():
            raise ValueError("committed_head source_ref must equal base_ref")
        if source_tree_sha:
            raise ValueError("committed_head must not set source_tree_sha")
    else:
        if not base_ref:
            raise ValueError("dirty_worktree_snapshot requires an exact base_ref")
        if not source_ref:
            raise ValueError("dirty_worktree_snapshot requires an exact source_ref")
        if not source_tree_sha:
            raise ValueError("dirty_worktree_snapshot requires an exact source_tree_sha")
        if not managed_source_sha256:
            raise ValueError("dirty_worktree_snapshot requires managed source digest metadata")
        resolved_ref = source_ref

    return {
        "source_mode": mode,
        "base_ref": base_ref,
        "source_ref": resolved_ref,
        "source_tree_sha": source_tree_sha,
        "managed_source_sha256": managed_source_sha256,
    }


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


GATE_KINDS = frozenset(
    {"scope", "execution", "required_check", "acceptance", "supervisor_review"}
)
GATE_OWNERS = frozenset({"gateway", "supervisor"})
GATE_ID_RE = re.compile(
    r"^(?:scope|execution|check-[1-9][0-9]*|acceptance-[1-9][0-9]*|supervisor-review)$"
)
_GATE_LEDGER_STATUSES = frozenset(
    {"passed", "failed", "warning", "skipped", "pending_supervisor"}
)
_GATE_LEDGER_ACCEPTANCE_STATES = frozenset({"needs_supervisor", "machine_unmet"})


def validate_acceptance_criteria(
    acceptance_criteria: list[str] | None,
) -> list[str]:
    """Normalize bounded acceptance prose used for supervisor-owned gates."""
    if acceptance_criteria is None:
        return []
    if not isinstance(acceptance_criteria, list):
        raise TypeError("acceptance_criteria must be a list of non-empty strings")
    if len(acceptance_criteria) > 64:
        raise ValueError("acceptance_criteria may contain at most 64 items")
    normalized: list[str] = []
    for idx, item in enumerate(acceptance_criteria):
        label = f"acceptance_criteria[{idx}]"
        if not isinstance(item, str):
            raise TypeError(f"{label} must be a string, got {type(item).__name__}")
        value = item.strip()
        if not value:
            raise ValueError(f"{label} must be non-empty")
        if len(value) > 1000:
            raise ValueError(f"{label} must be at most 1000 characters")
        normalized.append(value)
    return normalized


def build_gate_specs(
    *,
    required_checks: list[str] | None,
    acceptance_criteria: list[str] | None,
) -> list[dict[str, Any]]:
    """Build deterministic machine and supervisor-owned acceptance gates."""
    validate_required_checks(required_checks)
    criteria = validate_acceptance_criteria(acceptance_criteria)
    checks = [item.strip() for item in (required_checks or [])]
    gates: list[dict[str, Any]] = [
        {
            "id": "scope",
            "kind": "scope",
            "owner": "gateway",
            "outcome": (
                "Implementation remains inside the declared file scope and "
                "preserves guarded parent/source integrity."
            ),
        },
        {
            "id": "execution",
            "kind": "execution",
            "owner": "gateway",
            "outcome": "The bounded worker execution reaches a terminal zero exit code.",
        },
    ]
    for index, check in enumerate(checks, start=1):
        gates.append(
            {
                "id": f"check-{index}",
                "kind": "required_check",
                "owner": "gateway",
                "outcome": f"Required check {index} passes in the isolated verification workspace.",
                "check_index": index - 1,
                "check_sha256": hashlib.sha256(check.encode("utf-8")).hexdigest(),
            }
        )
    for index, criterion in enumerate(criteria, start=1):
        gates.append(
            {
                "id": f"acceptance-{index}",
                "kind": "acceptance",
                "owner": "supervisor",
                "outcome": criterion,
            }
        )
    gates.append(
        {
            "id": "supervisor-review",
            "kind": "supervisor_review",
            "owner": "supervisor",
            "outcome": (
                "Supervisor independently reviews call sites, security and "
                "transactional semantics and decides closure/readiness."
            ),
        }
    )
    return gates


def validate_gate_specs(
    value: Any,
    *,
    required_checks: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate an immutable task gate contract exactly and fail closed."""
    if not isinstance(value, list) or not value:
        raise ValueError("task gate contract must be a non-empty list")
    checks = [item.strip() for item in (required_checks or [])]
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for idx, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise TypeError(f"gates[{idx}] must be an object")
        gate_id = raw.get("id")
        kind = raw.get("kind")
        owner = raw.get("owner")
        outcome = raw.get("outcome")
        if not isinstance(gate_id, str) or GATE_ID_RE.fullmatch(gate_id) is None:
            raise ValueError(f"gates[{idx}].id is invalid")
        if gate_id in seen:
            raise ValueError(f"duplicate gate id: {gate_id}")
        seen.add(gate_id)
        if kind not in GATE_KINDS:
            raise ValueError(f"gates[{idx}].kind is invalid")
        if owner not in GATE_OWNERS:
            raise ValueError(f"gates[{idx}].owner is invalid")
        if not isinstance(outcome, str) or not outcome.strip() or len(outcome.strip()) > 1000:
            raise ValueError(
                f"gates[{idx}].outcome must be a non-empty string up to 1000 characters"
            )
        gate: dict[str, Any] = {
            "id": gate_id,
            "kind": kind,
            "owner": owner,
            "outcome": outcome.strip(),
        }
        if kind == "required_check":
            check_index = raw.get("check_index")
            digest = raw.get("check_sha256")
            if isinstance(check_index, bool) or not isinstance(check_index, int):
                raise TypeError(f"gates[{idx}].check_index must be an integer")
            if check_index < 0 or check_index >= len(checks):
                raise ValueError(f"gates[{idx}].check_index is out of range")
            expected = hashlib.sha256(checks[check_index].encode("utf-8")).hexdigest()
            if digest != expected:
                raise ValueError(
                    f"gates[{idx}].check_sha256 does not bind required_checks[{check_index}]"
                )
            gate["check_index"] = check_index
            gate["check_sha256"] = expected
        elif "check_index" in raw or "check_sha256" in raw:
            raise ValueError(
                f"gates[{idx}] has required-check fields on non-check gate"
            )
        normalized.append(gate)

    if len(normalized) < 3:
        raise ValueError(
            "gate contract must contain scope, execution, and supervisor-review gates"
        )
    if normalized[0].get("id") != "scope" or normalized[0].get("kind") != "scope" or normalized[0].get("owner") != "gateway":
        raise ValueError("gate contract must start with the gateway-owned scope gate")
    if normalized[1].get("id") != "execution" or normalized[1].get("kind") != "execution" or normalized[1].get("owner") != "gateway":
        raise ValueError(
            "gate contract must contain the gateway-owned execution gate second"
        )

    cursor = 2
    for check_index in range(len(checks)):
        if cursor >= len(normalized) - 1:
            raise ValueError("gate contract is missing required-check gates")
        gate = normalized[cursor]
        if (
            gate.get("id") != f"check-{check_index + 1}"
            or gate.get("kind") != "required_check"
            or gate.get("owner") != "gateway"
            or gate.get("check_index") != check_index
        ):
            raise ValueError(
                "gate contract required-check gates must exactly match required_checks order"
            )
        cursor += 1

    acceptance_index = 1
    while cursor < len(normalized) - 1:
        gate = normalized[cursor]
        if (
            gate.get("id") != f"acceptance-{acceptance_index}"
            or gate.get("kind") != "acceptance"
            or gate.get("owner") != "supervisor"
        ):
            raise ValueError(
                "gate contract acceptance gates must be contiguous and supervisor-owned"
            )
        acceptance_index += 1
        cursor += 1

    final_gate = normalized[-1]
    if (
        final_gate.get("id") != "supervisor-review"
        or final_gate.get("kind") != "supervisor_review"
        or final_gate.get("owner") != "supervisor"
    ):
        raise ValueError(
            "gate contract must end with the supervisor-owned review gate"
        )
    return normalized


def resolve_gate_specs(task_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve persisted gates; only legacy tasks with no gates may derive."""
    required_checks_raw = task_json.get("required_checks", [])
    if required_checks_raw is None:
        required_checks: list[str] = []
    elif isinstance(required_checks_raw, list) and all(
        isinstance(item, str) for item in required_checks_raw
    ):
        required_checks = [item.strip() for item in required_checks_raw if item.strip()]
    else:
        raise TypeError("task.json field 'required_checks' must be a list of strings")
    raw = task_json.get("gates")
    if raw is None:
        return build_gate_specs(
            required_checks=required_checks,
            acceptance_criteria=[],
        )
    return validate_gate_specs(raw, required_checks=required_checks)


def _gate_contract_sha256(gates: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        gates, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _task_contract_sha256(task_json: dict[str, Any]) -> str:
    canonical = json.dumps(
        task_json, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_gates_markdown(
    task_id: str,
    gates: list[dict[str, Any]],
    *,
    required_checks: list[str] | None = None,
) -> str:
    """Render readable gates without making GATES.md an authority."""
    validate_task_id(task_id)
    validated = validate_gate_specs(gates, required_checks=required_checks)
    lines = [
        "# Acceptance gates",
        "",
        f"Task: `{task_id}`",
        "",
        "This is a readable projection of the immutable `task.json[gates]` contract.",
        "Agent evidence is useful, but supervisor-owned gates remain pending until",
        "independent supervisor review; the agent must never declare the finding",
        "or project closed/ready on its own.",
        "",
    ]
    for gate in validated:
        lines.append(
            f"- [ ] `{gate['id']}` — owner=`{gate['owner']}` "
            f"kind=`{gate['kind']}` — {gate['outcome']}"
        )
    lines.append("")
    return "\n".join(lines)


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


_PROJECT_STATE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}-[0-9a-f]{12}$")
_ATTEMPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ATTEMPT_HINT_MAX_BYTES = 8192


def _read_local_state_file_nofollow(
    root: str,
    *relative_parts: str,
) -> bytes | None:
    """Read one state-volume file without following any path-component symlink."""
    parsed_root = PurePosixPath(root)
    if not parsed_root.is_absolute() or str(parsed_root) == "/":
        raise AttemptStateError("MCP_AGENT_STATE_ROOT must be an absolute non-root path")
    if not relative_parts or any(
        not part or part in {".", ".."} or "/" in part or "\x00" in part
        for part in relative_parts
    ):
        raise AttemptStateError("attempt hint path components are invalid")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open("/", directory_flags)
    file_fd: int | None = None
    try:
        components = [part for part in parsed_root.parts[1:] if part] + list(relative_parts)
        for component in components[:-1]:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise AttemptStateError("attempt hint path is not safely readable") from exc
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            file_fd = os.open(components[-1], file_flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AttemptStateError("attempt hint file is not safely readable") from exc
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise AttemptStateError("attempt hint must be a regular file")
        if metadata.st_size > _ATTEMPT_HINT_MAX_BYTES:
            raise AttemptStateError("attempt hint exceeds the bounded size limit")
        payload = os.read(file_fd, _ATTEMPT_HINT_MAX_BYTES + 1)
        if len(payload) > _ATTEMPT_HINT_MAX_BYTES:
            raise AttemptStateError("attempt hint exceeds the bounded size limit")
        return payload
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def read_agent_attempt_hint_by_state_key(
    *,
    project_key: str,
    task_id: str,
) -> str | None:
    """Return a worker-state attempt id only as an exact Gateway lookup hint.

    ``attempt-state.json`` lives on the shared executor coordination volume and
    is therefore evidence, not an authenticity boundary.  This helper never
    returns ``job_id`` and must not be used to release or bind fleet capacity by
    itself.  Its only safe consumer resolves the resulting attempt id through
    Gateway's authoritative exact submission key, which also embeds the durable
    fleet task identity.

    Active and archived copies are mutually exclusive under the normal atomic
    archive transition. Seeing both is ambiguous and fails closed. Every path
    component is opened with ``O_NOFOLLOW`` so a worker-controlled symlink
    cannot redirect the control-plane reader outside the state volume.
    """
    if not isinstance(project_key, str) or _PROJECT_STATE_KEY_RE.fullmatch(project_key) is None:
        # Historical fleet rows may predate project_state_key's current
        # slug+digest format. They cannot address this filesystem fallback at
        # all, so report no hint and let the caller use only its pre-existing
        # authoritative legacy Gateway lookup.
        return None
    validate_task_id(task_id)
    root = os.environ.get("MCP_AGENT_STATE_ROOT", "").strip()
    if not root:
        return None

    active = _read_local_state_file_nofollow(
        root,
        project_key,
        "tasks",
        task_id,
        ATTEMPT_STATE_FILENAME,
    )
    archived = _read_local_state_file_nofollow(
        root,
        project_key,
        "archive",
        task_id,
        ATTEMPT_STATE_FILENAME,
    )
    if active is not None and archived is not None:
        raise AttemptStateError("attempt hint exists in both active and archive state")
    payload = active if active is not None else archived
    if payload is None:
        return None
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise AttemptStateError("attempt hint is not valid UTF-8 JSON") from exc
    reason = _attempt_state_record_errors(record)
    if reason:
        raise AttemptStateError(f"invalid attempt hint: record {reason}")
    attempt_id = record["attempt_id"]
    if _ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
        raise AttemptStateError("attempt hint has an invalid attempt_id")
    return attempt_id


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
    acceptance_criteria: list[str] | None = None,
    worktree_path: str | None = None,
    commit_allowed: bool = False,
    push_allowed: bool = False,
    base_ref: str | None = None,
    allowed_backends: list[str] | None = None,
    managed_source_sha256: str | None = None,
    source_mode: str | None = None,
    source_ref: str | None = None,
    source_tree_sha: str | None = None,
    workflow_phase: str | None = None,
) -> str:
    """Build machine-readable task.json content."""
    validate_task_id(task_id)
    validate_required_checks(required_checks)
    validate_scope_contract(allowed_files, forbidden_files)
    normalized_workflow_phase = validate_workflow_phase(workflow_phase)
    if normalized_workflow_phase == "review" and (commit_allowed or push_allowed):
        raise ValueError("review workflow tasks must not allow commit or push mutations")
    # Preserve the public builder's pre-existing strict input contract: an
    # explicitly supplied digest must be valid, including rejecting "". The
    # runtime resolver remains tolerant of legacy task.json files that stored
    # an empty string to mean "no managed digest".
    if managed_source_sha256 is not None and (
        not isinstance(managed_source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", managed_source_sha256) is None
    ):
        raise ValueError(
            "managed_source_sha256 must be a 64-character lowercase hex digest"
        )
    source_contract = resolve_task_source_contract(
        {
            "base_ref": base_ref or "",
            "source_mode": source_mode or "",
            "source_ref": source_ref or "",
            "source_tree_sha": source_tree_sha or "",
            "managed_source_sha256": managed_source_sha256 or "",
        }
    )
    normalized_backends = _validate_allowed_backends(agent, allowed_backends)
    gates = build_gate_specs(
        required_checks=required_checks,
        acceptance_criteria=acceptance_criteria,
    )
    data: dict[str, Any] = {
        "task_id": task_id,
        "agent": agent,
        "allowed_backends": normalized_backends,
        "allowed_files": allowed_files or [],
        "forbidden_files": forbidden_files or [],
        "required_checks": required_checks or [],
        "gates": gates,
        "worktree_path": worktree_path or "",
        "base_ref": source_contract["base_ref"] or "",
        "source_mode": source_contract["source_mode"] or SOURCE_MODE_COMMITTED_HEAD,
        "source_ref": source_contract["source_ref"] or "",
        "source_tree_sha": source_contract["source_tree_sha"] or "",
        "managed_source_sha256": source_contract["managed_source_sha256"] or "",
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
    if phase == "review":
        convergence_rules = (
            "- Review must produce evidence, findings, and a final review report.\n"
            "- Review must not modify source files, commit, push, or transition to implementation.\n"
            "- The final implementation-diff.patch must be empty as evidence that no source mutation occurred.\n"
        )
    else:
        convergence_rules = (
            "- Discovery must produce evidence and a validation question.\n"
            "- Validation must produce GO/NO-GO and an implementation plan.\n"
            "- Implementation must produce a scoped diff, not more discussion.\n"
            "- Verification must run required checks or explain a hard blocker.\n"
            "- Cleanup must finalize the handoff and avoid new scope.\n"
        )
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
        + convergence_rules
        + "\n## Next action\n\n"
        + f"- {next_action}\n"
        + f"- Update `{artifacts}/agent-status.md` for progress and this file for decisions.\n"
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
    if phase == "review" and commit_message:
        raise ValueError("review workflow tasks must not define a commit message")
    allow = "\n".join(f"- {f}" for f in (allowed_files or []))
    forbid = "\n".join(f"- {f}" for f in (forbidden_files or []))
    checks = "\n".join(f"- `{c}`" for c in (required_checks or []))
    criteria = "\n".join(f"- {c}" for c in (acceptance_criteria or []))
    notes = f"\n## Constraints\n\n{constraints}\n" if constraints else ""
    artifacts = artifact_dir or f"{TASKS_REL_DIR}/{task_id}"
    if phase == "review":
        convergence = (
            "This review phase is evidence-only and terminal for this task. "
            "Do not transition to implementation or modify source files.\n\n"
        )
        agent_instructions = (
            "Read this plan and inspect the existing code without modifying source files.\n"
            f"Update `{artifacts}/agent-status.md` as evidence is collected.\n"
            f"Keep durable decisions and handoff state in `{artifacts}/consensus.md`.\n"
            f"Write the final findings to `{artifacts}/agent-report.md`.\n"
            f"Leave `{artifacts}/implementation-diff.patch` empty as evidence that no source mutation occurred.\n"
            "Do not implement, commit, push, or create branches in this task.\n"
        )
    else:
        convergence = (
            "Forced convergence: discovery → validation → implementation → "
            "verification → cleanup. After validation, pure discussion is not "
            "a sufficient deliverable.\n\n"
        )
        agent_instructions = (
            "Read this plan and execute it in small, reviewable steps.\n"
            f"After each meaningful change, update `{artifacts}/agent-status.md`.\n"
            f"Keep durable decisions and handoff state in `{artifacts}/consensus.md`.\n"
            f"Save final diff to `{artifacts}/implementation-diff.patch`.\n"
            "Do not commit or push unless explicitly instructed.\n"
        )

    return (
        f"# {task}\n\n"
        f"## Metadata\n\n"
        f"- Task ID: {task_id}\n"
        f"- Created: {datetime.now(UTC).isoformat()}\n"
        f"- Workflow phase: {phase}\n\n"
        f"## Workflow phase\n\n"
        f"Current phase: `{phase}`. {WORKFLOW_PHASE_TRANSITIONS[phase]}\n\n"
        + convergence
        + f"## Scope\n\n{scope}\n\n"
        + f"## Allowed files\n\n{allow}\n\n"
        + f"## Forbidden\n\n{forbid}\n\n"
        + f"## Required checks\n\n{checks}\n\n"
        + f"## Acceptance criteria\n\n{criteria}\n"
        + (f"\n## Commit message\n\n```\n{commit_message}\n```\n" if commit_message else "")
        + notes
        + "\n## Agent instructions\n\n"
        + agent_instructions
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
    # The public log is runner-owned, but worker output is still untrusted and
    # can contain proxy URLs, credentials, or tokens.  Use the same strict
    # surface sanitizer as read_agent_artifact_tail so the dedicated live-log
    # API never becomes a weaker disclosure path.
    stdout = _sanitize_agent_surface_text(project, task_id, stdout)
    stderr = _sanitize_agent_surface_text(
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
        "ambiguous",
        "cancelled",
    }
)
_AGENT_ACTIVE_STATUSES = frozenset({"created", "pending", "processing", "running", "cancelling"})
_STARTUP_STALLED_RE = re.compile(r"OpenCode startup stalled; rotating proxy \(attempt (\d+)/(\d+)\)")
_PRE_USEFUL_SERVER_RETRY_RE = re.compile(
    r"OpenCode upstream server error before useful work; rotating proxy \(attempt (\d+)/(\d+)\)"
)
_USEFUL_AGENT_ACTIVITY_MARKERS = (
    "← Write ",
    "Wrote file successfully",
    "$ cd ",
    "# Todos",
    "Implementation",
    "agent-report.md",
    "implementation-diff.patch",
)
_OPENCODE_TOOL_ACTIVITY_RE = re.compile(r"(?m)^\s*(?:→|←)\s+\S")


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



def _read_agent_gate_ledger(
    run_cmd,
    *,
    project: str,
    task_id: str,
) -> dict[str, Any]:
    """Validate shared runner evidence against the current immutable task contract."""
    result = read_agent_task_file(
        run_cmd, project=project, task_id=task_id, filename=GATE_LEDGER_FILENAME
    )
    text = str(result.get("stdout", ""))
    if text == "(not found)":
        return {"exists": False, "authoritative": False}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {
            "exists": True,
            "valid": False,
            "authoritative": False,
            "error": "gate ledger is not valid JSON",
        }
    if not isinstance(data, dict):
        return {
            "exists": True,
            "valid": False,
            "authoritative": False,
            "error": "gate ledger is not a JSON object",
        }
    if data.get("version") != 1 or data.get("generated_by") != "gateway-runner":
        return {
            "exists": True,
            "valid": False,
            "authoritative": False,
            "error": "gate ledger provenance is invalid",
        }

    machine_gates_met = data.get("machine_gates_met")
    acceptance_state = data.get("acceptance_state")
    gate_digest = data.get("gate_contract_sha256")
    task_digest = data.get("task_contract_sha256")
    if not isinstance(machine_gates_met, bool):
        return {
            "exists": True,
            "valid": False,
            "authoritative": False,
            "error": "gate ledger machine state is invalid",
        }
    expected_state = "needs_supervisor" if machine_gates_met else "machine_unmet"
    if (
        acceptance_state not in _GATE_LEDGER_ACCEPTANCE_STATES
        or acceptance_state != expected_state
    ):
        return {
            "exists": True,
            "valid": False,
            "authoritative": False,
            "error": "gate ledger acceptance state is inconsistent",
        }
    for name, digest in (
        ("contract", gate_digest),
        ("task-contract", task_digest),
    ):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return {
                "exists": True,
                "valid": False,
                "authoritative": False,
                "error": f"gate ledger {name} digest is invalid",
            }

    task_result = read_agent_task_file(
        run_cmd, project=project, task_id=task_id, filename="task.json"
    )
    task_text = str(task_result.get("stdout", ""))
    if task_text == "(not found)":
        return {
            "exists": True,
            "valid": False,
            "authoritative": False,
            "error": "task contract is missing",
        }
    try:
        task_data = json.loads(task_text)
        if not isinstance(task_data, dict):
            raise TypeError("task contract must be an object")
        task_gates = resolve_gate_specs(task_data)
    except (TypeError, ValueError):
        return {
            "exists": True,
            "valid": False,
            "authoritative": False,
            "error": "task gate contract is invalid",
        }
    if _task_contract_sha256(task_data) != task_digest:
        return {
            "exists": True,
            "valid": False,
            "authoritative": False,
            "error": "gate ledger does not match current task contract",
        }
    if _gate_contract_sha256(task_gates) != gate_digest:
        return {
            "exists": True,
            "valid": False,
            "authoritative": False,
            "error": "gate ledger does not match current task gate contract",
        }

    raw_gates = data.get("gates")
    if not isinstance(raw_gates, list) or len(raw_gates) != len(task_gates):
        return {
            "exists": True,
            "valid": False,
            "authoritative": False,
            "error": "gate ledger entries are invalid",
        }

    safe_gates: list[dict[str, Any]] = []
    for index, (raw, contract_gate) in enumerate(
        zip(raw_gates, task_gates, strict=True)
    ):
        if not isinstance(raw, dict):
            return {
                "exists": True,
                "valid": False,
                "authoritative": False,
                "error": f"gate ledger entry {index} is invalid",
            }
        if any(
            raw.get(key) != contract_gate.get(key)
            for key in ("id", "kind", "owner", "outcome")
        ):
            return {
                "exists": True,
                "valid": False,
                "authoritative": False,
                "error": f"gate ledger entry {index} does not match task contract",
            }
        status = raw.get("status")
        if status not in _GATE_LEDGER_STATUSES:
            return {
                "exists": True,
                "valid": False,
                "authoritative": False,
                "error": f"gate ledger entry {index} status is invalid",
            }
        owner = contract_gate["owner"]
        if owner == "supervisor" and status != "pending_supervisor":
            return {
                "exists": True,
                "valid": False,
                "authoritative": False,
                "error": "supervisor-owned gate cannot be completed by runner",
            }
        if owner == "gateway" and status == "pending_supervisor":
            return {
                "exists": True,
                "valid": False,
                "authoritative": False,
                "error": "gateway-owned gate has invalid pending-supervisor status",
            }

        safe: dict[str, Any] = {
            "id": contract_gate["id"],
            "kind": contract_gate["kind"],
            "owner": owner,
            "outcome": contract_gate["outcome"],
            "status": status,
        }
        exit_code = raw.get("exit_code")
        if exit_code is not None:
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                return {
                    "exists": True,
                    "valid": False,
                    "authoritative": False,
                    "error": f"gate ledger entry {index} exit code is invalid",
                }
            safe["exit_code"] = exit_code
        if contract_gate["kind"] == "required_check":
            if (
                raw.get("check_index") != contract_gate.get("check_index")
                or raw.get("check_sha256") != contract_gate.get("check_sha256")
            ):
                return {
                    "exists": True,
                    "valid": False,
                    "authoritative": False,
                    "error": (
                        f"gate ledger check entry {index} is not bound to task contract"
                    ),
                }
            safe["check_index"] = contract_gate["check_index"]
            safe["check_sha256"] = contract_gate["check_sha256"]
        safe_gates.append(safe)

    computed_machine_met = all(
        gate["status"] == "passed"
        for gate in safe_gates
        if gate["owner"] == "gateway"
    )
    if computed_machine_met != machine_gates_met:
        return {
            "exists": True,
            "valid": False,
            "authoritative": False,
            "error": (
                "gate ledger machine summary does not match gateway-owned gate entries"
            ),
        }

    passed = sum(1 for gate in safe_gates if gate["status"] == "passed")
    pending = sum(
        1 for gate in safe_gates if gate["status"] == "pending_supervisor"
    )
    unmet = len(safe_gates) - passed - pending
    return {
        "exists": True,
        "valid": True,
        "authoritative": False,
        "generated_by": "gateway-runner",
        "gate_contract_sha256": gate_digest,
        "task_contract_sha256": task_digest,
        "machine_gates_met": machine_gates_met,
        "acceptance_state": acceptance_state,
        "counts": {
            "passed": passed,
            "pending_supervisor": pending,
            "unmet": unmet,
        },
        "gates": safe_gates,
    }


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


_DIAGNOSTIC_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s)\]}>\"']+")
_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?i)(\b(?:token|password|passwd|secret|api[_-]?key|proxy[_-]?url)\b\s*[:=]\s*)[^\s,;]+"
)
_AGENT_PROXY_STATUS_STRING_FIELDS = (
    "provider_kind",
    "last_error_class",
    "started_at",
    "updated_at",
    "finished_at",
    "final_outcome",
)
_AGENT_PROXY_STATUS_INT_FIELDS = (
    "attempt",
    "max_attempts",
    "started_epoch",
    "updated_epoch",
    "finished_epoch",
)


def _sanitize_agent_diagnostic_text(value: str, *, max_chars: int = 240) -> str:
    """Return a bounded diagnostic string without URLs, credentials, or tokens."""
    text = _ANSI_ESCAPE_RE.sub("", value)
    text = _DIAGNOSTIC_URL_RE.sub("<redacted-url>", text)
    text = _DIAGNOSTIC_SECRET_RE.sub(r"\1<redacted>", text)
    return text[:max_chars]


def _resolve_agent_artifact(artifact: str) -> tuple[str, str]:
    """Resolve a public artifact alias or filename to a fixed safe filename."""
    if not isinstance(artifact, str):
        raise TypeError("artifact must be a string")
    value = artifact.strip()
    if not value:
        raise ValueError("artifact must be a non-empty fixed artifact name")
    normalized = value.replace("-", "_")
    filename = AGENT_ARTIFACT_FILENAMES.get(normalized)
    key = normalized if filename is not None else ""
    if filename is None:
        validate_filename(value)
        filename = value
        key = _AGENT_ARTIFACTS_BY_FILENAME.get(filename, "")
    if not key or filename not in _AGENT_ARTIFACTS_BY_FILENAME:
        choices = sorted({*AGENT_ARTIFACT_FILENAMES, *AGENT_ARTIFACT_FILENAMES.values()})
        raise ValueError(
            f"unsupported agent artifact: {artifact!r}. Expected one of: {', '.join(choices)}"
        )
    return key, filename


def _validate_agent_artifact_limits(*, tail_lines: int, max_bytes: int) -> None:
    if isinstance(tail_lines, bool) or not isinstance(tail_lines, int):
        raise TypeError("tail_lines must be an integer")
    if not 1 <= tail_lines <= AGENT_ARTIFACT_MAX_TAIL_LINES:
        raise ValueError(
            f"tail_lines must be between 1 and {AGENT_ARTIFACT_MAX_TAIL_LINES}"
        )
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be an integer")
    if not 1 <= max_bytes <= AGENT_ARTIFACT_MAX_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {AGENT_ARTIFACT_MAX_BYTES}")


def _sanitize_agent_surface_text(project: str, task_id: str, text: str) -> str:
    """Normalize one task artifact without leaking control-plane paths or secrets."""
    redacted = _normalize_agent_log_text(project, task_id, text)
    redacted = _DIAGNOSTIC_URL_RE.sub("<redacted-url>", redacted)
    return _DIAGNOSTIC_SECRET_RE.sub(r"\1<redacted>", redacted)


def _agent_artifact_unavailable(
    *,
    artifact: str,
    filename: str,
    reason: str,
    tail_lines: int,
    max_bytes: int,
) -> dict[str, Any]:
    return {
        "artifact": artifact,
        "filename": filename,
        "available": False,
        "stdout": "",
        "stderr": "",
        "exit_code": 0,
        "truncated": False,
        "redacted": False,
        "tail_lines": tail_lines,
        "max_bytes": max_bytes,
        "log_unavailable": {"reason": reason},
        "artifact_unavailable": {"reason": reason},
    }


def read_agent_artifact_tail(
    run_cmd,
    *,
    project: str,
    task_id: str,
    artifact: str,
    tail_lines: int = 200,
    max_bytes: int = AGENT_ARTIFACT_MAX_BYTES,
) -> dict[str, Any]:
    """Read a bounded, redacted tail from one fixed agent task artifact.

    Callers choose only from a small allowlist of task-owned artifact aliases
    or filenames. The remote path is derived from task_id + that allowlist;
    unsupported names are rejected before any command executes. Missing or
    symlink-unsafe files are returned as structured ``log_unavailable`` /
    ``artifact_unavailable`` records, not transport/tool failures.
    """
    validate_task_id(task_id)
    key, filename = _resolve_agent_artifact(artifact)
    _validate_agent_artifact_limits(tail_lines=tail_lines, max_bytes=max_bytes)

    path = f"{task_dir(project, task_id)}/{filename}"
    if not _readonly_path_is_safe(run_cmd, project=project, path=path):
        return _agent_artifact_unavailable(
            artifact=key,
            filename=filename,
            reason="not_found_or_unsafe_path",
            tail_lines=tail_lines,
            max_bytes=max_bytes,
        )
    result = run_cmd(project, f"tail -c {max_bytes + 1} -- {shlex.quote(path)}")
    if result.get("exit_code") != 0:
        return _agent_artifact_unavailable(
            artifact=key,
            filename=filename,
            reason="read_failed",
            tail_lines=tail_lines,
            max_bytes=max_bytes,
        )

    raw_stdout = str(result.get("stdout", ""))
    encoded = raw_stdout.encode("utf-8", errors="replace")
    byte_truncated = len(encoded) > max_bytes
    if byte_truncated:
        raw_stdout = encoded[-max_bytes:].decode("utf-8", errors="replace")
    sanitized_stdout = _sanitize_agent_surface_text(project, task_id, raw_stdout)
    raw_stderr = str(result.get("stderr", ""))
    sanitized_stderr = _sanitize_agent_surface_text(project, task_id, raw_stderr)
    redacted = sanitized_stdout != raw_stdout or sanitized_stderr != raw_stderr
    lines = sanitized_stdout.splitlines(keepends=True)
    line_truncated = len(lines) > tail_lines
    if line_truncated:
        sanitized_stdout = "".join(lines[-tail_lines:])
    return {
        "artifact": key,
        "filename": filename,
        "available": True,
        "stdout": sanitized_stdout,
        "stderr": sanitized_stderr,
        "exit_code": 0,
        "truncated": byte_truncated or line_truncated,
        "redacted": redacted,
        "tail_lines": tail_lines,
        "max_bytes": max_bytes,
        "bytes_returned": len(sanitized_stdout.encode("utf-8", errors="replace")),
    }


def _last_startup_message(text: str) -> str | None:
    """Return the latest bounded startup/proxy line from status/log text."""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        lowered = stripped.lower()
        if stripped and ("startup" in lowered or "proxy" in lowered):
            return _sanitize_agent_diagnostic_text(stripped)
    return None


def _elapsed_since_earliest_artifact(
    files: dict[str, dict[str, Any]],
    now_epoch: int,
    names: tuple[str, ...],
) -> int | None:
    mtimes: list[int] = []
    for name, meta in files.items():
        if name not in names:
            continue
        mtime = meta.get("mtime_epoch")
        if isinstance(mtime, int):
            mtimes.append(mtime)
    if not mtimes:
        return None
    return max(0, now_epoch - min(mtimes))


def _read_agent_proxy_status(
    run_cmd,
    *,
    project: str,
    task_id: str,
    now_epoch: int,
) -> dict[str, Any]:
    """Read and sanitize the optional proxy rotation sidecar.

    Only a strict allowlist of concise, non-secret fields is returned.  Any
    secret-bearing or raw proxy URL fields in the JSON are reported by key name
    only, never by value.
    """
    result = read_agent_task_file(
        run_cmd, project=project, task_id=task_id, filename=AGENT_PROXY_STATUS_FILENAME
    )
    text = str(result.get("stdout", ""))
    if text == "(not found)":
        return {"exists": False}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {"exists": True, "valid": False, "error": "proxy status is not valid JSON"}
    if not isinstance(data, dict):
        return {"exists": True, "valid": False, "error": "proxy status is not a JSON object"}

    summary: dict[str, Any] = {"exists": True, "valid": True}
    for key in _AGENT_PROXY_STATUS_STRING_FIELDS:
        value = data.get(key)
        if isinstance(value, str) and value:
            summary[key] = _sanitize_agent_diagnostic_text(value, max_chars=120)
    for key in _AGENT_PROXY_STATUS_INT_FIELDS:
        value = data.get(key)
        if isinstance(value, int) or value is None:
            summary[key] = value
    updated_epoch = summary.get("updated_epoch")
    if isinstance(updated_epoch, int):
        summary["age_seconds"] = max(0, now_epoch - updated_epoch)
    else:
        summary["age_seconds"] = None
    redacted_fields = sorted(
        key
        for key in data
        if re.search(r"(?i)(url|token|secret|password|passwd|credential|api[_-]?key)", str(key))
    )
    if redacted_fields:
        summary["redacted_fields"] = redacted_fields
    return summary


_AGENT_FAILURE_REASONS = frozenset({"opencode_server_error"})
_AGENT_FAILURE_PHASES = frozenset({"pre_useful_work"})
_AGENT_UPSTREAM_REF_RE = re.compile(r"^err_[A-Za-z0-9]{8,64}$")


def _read_agent_failure_status(
    run_cmd,
    *,
    project: str,
    task_id: str,
) -> dict[str, Any]:
    """Read a fail-honest, redacted worker failure sidecar."""
    result = read_agent_task_file(
        run_cmd, project=project, task_id=task_id, filename=AGENT_FAILURE_STATUS_FILENAME
    )
    text = str(result.get("stdout", ""))
    if text == "(not found)":
        return {"exists": False}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {"exists": True, "valid": False, "error": "failure status is not valid JSON"}
    if not isinstance(data, dict):
        return {"exists": True, "valid": False, "error": "failure status is not a JSON object"}

    reason = data.get("reason")
    phase = data.get("phase")
    upstream_ref = data.get("upstream_ref")
    if reason not in _AGENT_FAILURE_REASONS:
        return {"exists": True, "valid": False, "error": "failure status reason is not recognized"}
    if phase not in _AGENT_FAILURE_PHASES:
        return {"exists": True, "valid": False, "error": "failure status phase is not recognized"}
    if reason == "opencode_server_error" and (
        not isinstance(upstream_ref, str) or _AGENT_UPSTREAM_REF_RE.fullmatch(upstream_ref) is None
    ):
        return {"exists": True, "valid": False, "error": "failure status upstream ref is invalid"}

    summary: dict[str, Any] = {
        "exists": True,
        "valid": True,
        "reason": reason,
        "phase": phase,
    }
    if isinstance(upstream_ref, str) and _AGENT_UPSTREAM_REF_RE.fullmatch(upstream_ref):
        summary["upstream_ref"] = upstream_ref
    observed_at = data.get("observed_at")
    if isinstance(observed_at, str) and observed_at:
        summary["observed_at"] = _sanitize_agent_diagnostic_text(observed_at, max_chars=80)
    correlation_hint = data.get("correlation_hint")
    if isinstance(correlation_hint, str) and correlation_hint:
        summary["correlation_hint"] = _sanitize_agent_diagnostic_text(
            correlation_hint, max_chars=240
        )
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


def _job_status_error_code(message: str) -> str | None:
    """Return a stable gateway error code from a bounded job-status error."""
    upper = message.upper()
    if "JOB_NOT_FOUND" in upper:
        return "JOB_NOT_FOUND"
    return None


def _safe_job_summary(job_status, job_id: str | None) -> dict[str, Any] | None:
    if not job_id or job_status is None:
        return None
    try:
        snapshot = job_status(job_id)
    except Exception as exc:
        message = str(exc)[:500]
        summary: dict[str, Any] = {"job_id": job_id, "known": False, "error": message}
        error_code = _job_status_error_code(message)
        if error_code is not None:
            summary["error_code"] = error_code
        if error_code == "JOB_NOT_FOUND":
            # A vanished Gateway job record after a restart proves only control-plane
            # absence. It must not be inflated into worker/process termination proof.
            summary["gateway_job_absent"] = True
            summary["worker_termination_proven"] = False
        return summary
    if not isinstance(snapshot, dict):
        return {"job_id": job_id, "known": False, "error": "job_status returned non-object"}
    summary = {"job_id": job_id, "known": True}
    for key in ("status", "exit_code", "created_at", "started_at", "finished_at"):
        value = snapshot.get(key)
        if isinstance(value, (str, int)) or value is None:
            summary[key] = value
    return summary


def _job_status_token(job: dict[str, Any] | None) -> str | None:
    value = (job or {}).get("status")
    return value.lower() if isinstance(value, str) and value else None


def _gateway_job_absent(job: dict[str, Any] | None) -> bool:
    return bool(
        job
        and job.get("known") is False
        and job.get("error_code") == "JOB_NOT_FOUND"
        and job.get("gateway_job_absent") is True
    )


def _agent_reconciliation_diagnostics(
    *,
    attempt: dict[str, Any] | None,
    job: dict[str, Any] | None,
    status: str | None,
    files: dict[str, dict[str, Any]],
    active: bool,
    terminal: bool,
    last_activity: dict[str, Any],
    semantic_activity: dict[str, Any],
    runner_heartbeat_fresh: bool,
) -> dict[str, Any]:
    """Summarize restart/orphan evidence without inventing worker finality."""
    attempt_job_id = (attempt or {}).get("job_id")
    attempt_bound_job = isinstance(attempt_job_id, str) and bool(attempt_job_id)
    gateway_job_absent = _gateway_job_absent(job)
    report_size = (files.get("report") or {}).get("size_bytes")
    diff_size = (files.get("diff") or {}).get("size_bytes")
    report_has_content = isinstance(report_size, int) and report_size > 0
    diff_has_content = isinstance(diff_size, int) and diff_size > 0
    artifact_incomplete = not (report_has_content or diff_has_content)
    state = None
    if gateway_job_absent and attempt_bound_job and not terminal:
        if active and not runner_heartbeat_fresh:
            state = "lost_after_restart"
        elif active:
            state = "orphaned_attempt"
        elif artifact_incomplete:
            state = "artifact_incomplete"
    return {
        "state": state,
        "gateway_job_absent": gateway_job_absent,
        "attempt_bound_job": attempt_bound_job,
        "status": status,
        "status_active": active,
        "runner_heartbeat_fresh": runner_heartbeat_fresh,
        "artifact_incomplete": artifact_incomplete,
        "worker_termination_proven": False if gateway_job_absent else None,
        "last_activity": last_activity,
        "last_useful_activity": semantic_activity,
    }


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
    now_epoch: int,
    proxy_status: dict[str, Any],
    failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify OpenCode startup/proxy dead time separately from useful work."""
    combined = f"{status_text}\n{log_stdout}"
    stalled_matches = list(_STARTUP_STALLED_RE.finditer(combined))
    server_retry_matches = list(_PRE_USEFUL_SERVER_RETRY_RE.finditer(combined))
    rotation_matches = [*stalled_matches, *server_retry_matches]
    attempts = [int(match.group(1)) for match in rotation_matches]
    max_attempts = [int(match.group(2)) for match in rotation_matches]
    proxy_attempt = proxy_status.get("attempt")
    proxy_max_attempts = proxy_status.get("max_attempts")
    proxy_sidecar_observed = bool(
        proxy_status.get("exists") and proxy_status.get("valid")
    )
    proxy_outcome = proxy_status.get("final_outcome")
    # Merely acquiring a proxy is normal startup, not evidence of a stall.
    # Treat only explicit runner-owned rotation/exhaustion evidence as stalled;
    # live runtime activity is reported separately via final_outcome=running.
    opencode_startup_stalled = bool(
        rotation_matches
        or "OpenCode startup stalled" in combined
        or proxy_status.get("last_error_class") == "startup_stalled"
        or proxy_outcome in {"rotating", "startup_exhausted", "upstream_error_exhausted"}
    )
    startup_timeout = status == "startup-timeout" or "opencode-startup-timeout" in combined
    report_size = (files.get("report") or {}).get("size_bytes")
    diff_size = (files.get("diff") or {}).get("size_bytes")
    useful_agent_activity_seen = bool(
        (isinstance(report_size, int) and report_size > 0)
        or (isinstance(diff_size, int) and diff_size > 0)
        or proxy_outcome == "running"
        or _OPENCODE_TOOL_ACTIVITY_RE.search(combined) is not None
        or any(marker in combined for marker in _USEFUL_AGENT_ACTIVITY_MARKERS)
    )
    # A typed pre-useful-work failure is authoritative. The runner itself
    # emits final report/diff artifacts on every terminal path, so their mere
    # existence cannot retroactively prove that the model or a tool did work.
    if (
        failure
        and failure.get("valid") is True
        and failure.get("phase") == "pre_useful_work"
    ):
        useful_agent_activity_seen = False
    dead_time_kind = None
    if active and opencode_startup_stalled and not useful_agent_activity_seen:
        dead_time_kind = "opencode_startup"
    phase = "startup" if startup_timeout or dead_time_kind == "opencode_startup" else None
    elapsed_seconds = _elapsed_since_earliest_artifact(
        files,
        now_epoch,
        ("status", "log", "heartbeat", "attempt_state", "proxy_status"),
    )
    last_message = _last_startup_message(combined)
    if last_message is None:
        last_error = proxy_status.get("last_error_class")
        if isinstance(last_error, str) and last_error:
            last_message = f"proxy error class: {last_error}"
    return {
        "phase": phase,
        "elapsed_seconds": elapsed_seconds,
        "last_startup_message": last_message,
        "startup_timeout": startup_timeout,
        "opencode_startup_stalled": opencode_startup_stalled,
        "proxy_rotation": {
            "observed": bool(rotation_matches or proxy_sidecar_observed),
            "attempt": proxy_attempt if isinstance(proxy_attempt, int) else (max(attempts) if attempts else None),
            "max_attempts": (
                proxy_max_attempts
                if isinstance(proxy_max_attempts, int)
                else (max(max_attempts) if max_attempts else None)
            ),
            "count": len(rotation_matches),
            "sidecar": proxy_sidecar_observed,
        },
        "useful_agent_activity_seen": useful_agent_activity_seen,
        "dead_time_kind": dead_time_kind,
    }


_REASONING_LOOP_FILLER_WORDS = _FUNCTION_WORDS | frozenset(
    {
        "again",
        "all",
        "batch",
        "complete",
        "every",
        "everything",
        "full",
        "once",
        "same",
        "single",
        "together",
        "now",
        "сейчас",
        "давай",
        "все",
        "всё",
        "снова",
        "опять",
    }
)
_REASONING_LOOP_CATEGORY_TOKENS = {
    "act": {
        "add",
        "apply",
        "check",
        "continue",
        "do",
        "execute",
        "fix",
        "implement",
        "inspect",
        "make",
        "run",
        "start",
        "test",
        "update",
        "verify",
        "добавить",
        "делать",
        "запустить",
        "исправить",
        "проверить",
        "продолжать",
    },
    "verify": {
        "check",
        "checks",
        "compileall",
        "diff",
        "gate",
        "gates",
        "lint",
        "mypy",
        "ruff",
        "static",
        "test",
        "tests",
        "verify",
        "проверка",
        "проверки",
        "тест",
        "тесты",
    },
    "plan": {
        "decide",
        "plan",
        "review",
        "think",
        "validate",
        "план",
        "решить",
        "смотреть",
    },
}
_REASONING_LOOP_SUFFIXES = ("ing", "ed", "es", "s")


def _reasoning_loop_stem(token: str) -> str:
    for suffix in _REASONING_LOOP_SUFFIXES:
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _reasoning_loop_signature(line: str) -> frozenset[str]:
    """Return a compact semantic-ish signature for one agent thought line.

    The detector intentionally does not key on one English sentence such as
    "Let me run ...". It strips filler words, stems small variants and maps
    broad action/verification/planning vocabulary into coarse buckets so loops
    are recognized by repeated intent without progress, not by a literal phrase.
    """
    clean = _ANSI_ESCAPE_RE.sub("", line).strip().lower()
    if not clean or re.match(r"^>\s*build\s*[·:-]", clean, re.I):
        return frozenset()
    if any(marker.lower() in clean for marker in _USEFUL_AGENT_ACTIVITY_MARKERS):
        return frozenset()
    tokens = [_reasoning_loop_stem(t) for t in re.findall(r"[a-zа-яё0-9_+-]{2,}", clean)]
    important = [t for t in tokens if t not in _REASONING_LOOP_FILLER_WORDS]
    if not important:
        return frozenset()
    buckets = {
        bucket
        for bucket, words in _REASONING_LOOP_CATEGORY_TOKENS.items()
        if any(token in words for token in important)
    }
    # Keep a few concrete tokens so unrelated repetitive logs do not collapse
    # into a single generic "act" bucket.
    concrete = [t for t in important if all(t not in words for words in _REASONING_LOOP_CATEGORY_TOKENS.values())]
    return frozenset((*sorted(buckets), *sorted(concrete)[:4]))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _progress_artifact_activity(files: dict[str, dict[str, Any]], now_epoch: int) -> dict[str, Any]:
    # Log and heartbeat activity alone can hide a thinking loop. attempt-state
    # is job bookkeeping, not agent progress. Runner-created evidence files can
    # also exist as zero-byte placeholders before the worker does useful work,
    # so their mtime alone must not reset semantic-stall timers.
    ignored = {"log", "heartbeat", "proxy_status", "failure_status", "attempt_state"}
    content_required = {"report", "diff", "worker_status", "required_checks"}
    useful: dict[str, dict[str, Any]] = {}
    for name, meta in files.items():
        if name in ignored:
            continue
        if name in content_required:
            size = meta.get("size_bytes")
            if not isinstance(size, int) or size <= 0:
                continue
        useful[name] = meta
    return _latest_activity(useful, now_epoch)


def _detect_agent_reasoning_loop(
    *,
    log_stdout: str,
    files: dict[str, dict[str, Any]],
    active: bool,
    terminal: bool,
    now_epoch: int,
    reasoning_loop_after_seconds: int,
) -> dict[str, Any]:
    progress = _progress_artifact_activity(files, now_epoch)
    progress_age = progress.get("age_seconds")
    raw_lines = [line.strip() for line in log_stdout.splitlines() if line.strip()]
    signatures = [_reasoning_loop_signature(line) for line in raw_lines[-80:]]
    signatures = [sig for sig in signatures if sig]
    recent = signatures[-32:]
    if len(recent) >= 2:
        adjacent = [_jaccard(left, right) for left, right in zip(recent, recent[1:], strict=False)]
        average_adjacent_similarity = round(sum(adjacent) / len(adjacent), 3)
    else:
        average_adjacent_similarity = 0.0
    coverage = 0.0
    if recent:
        token_counts: dict[str, int] = {}
        for sig in recent:
            for token in sig:
                token_counts[token] = token_counts.get(token, 0) + 1
        coverage = round(max(token_counts.values()) / len(recent), 3) if token_counts else 0.0
    no_recent_progress = isinstance(progress_age, int) and progress_age >= reasoning_loop_after_seconds
    detected = bool(
        active
        and not terminal
        and no_recent_progress
        and len(recent) >= AGENT_REASONING_LOOP_MIN_LINES
        and (coverage >= 0.72 or average_adjacent_similarity >= 0.34)
    )
    return {
        "detected": detected,
        "progress": progress,
        "progress_age_seconds": progress_age,
        "window_lines": len(recent),
        "dominant_token_coverage": coverage,
        "average_adjacent_similarity": average_adjacent_similarity,
        "continuation_prompt": AGENT_REASONING_LOOP_CONTINUATION_PROMPT,
    }


def _last_line_with_trailing_colon(log_stdout: str) -> str | None:
    """Return the last non-empty log line that ends in a colon, if any.

    ``:`` is a common tail when an agent announces the next step it is about to
    take (a heading, a file list, a working-dir notice) and then never renders
    the promised content. We only look at the trailing colon, never a specific
    literal phrase, so any such stall is caught regardless of wording.
    """
    for line in reversed(log_stdout.splitlines()):
        line = _ANSI_ESCAPE_RE.sub("", line).strip()
        if not line:
            continue
        if line.endswith(":"):
            return line
    return None


def _detect_agent_trailing_colon_stall(
    *,
    log_stdout: str,
    files: dict[str, dict[str, Any]],
    active: bool,
    terminal: bool,
    now_epoch: int,
    trailing_colon_after_seconds: int,
) -> dict[str, Any]:
    """Detect an agent stalled at a trailing-colon without progress.

    An active, non-terminal agent whose most recent meaningful log line ends
    with ``:`` and whose progress artifacts (status/consensus/report/diff) have
    not advanced for a threshold is considered to have announced a step it never
    took. Contrary to a reasoning loop (repeated similar thought lines) the
    signal here is open-ended silence after a narrative colon.
    """
    progress = _progress_artifact_activity(files, now_epoch)
    progress_age = progress.get("age_seconds")
    trailing_colon_line = _last_line_with_trailing_colon(log_stdout)
    no_recent_progress = isinstance(progress_age, int) and progress_age >= trailing_colon_after_seconds
    detected = bool(
        active
        and not terminal
        and trailing_colon_line is not None
        and no_recent_progress
    )
    return {
        "detected": detected,
        "last_meaningful_line": trailing_colon_line,
        "progress": progress,
        "progress_age_seconds": progress_age,
        "continuation_prompt": AGENT_REASONING_LOOP_CONTINUATION_PROMPT,
    }


_INVOKE_OPEN_RE = re.compile(r"<\s*invoke\b", re.IGNORECASE)
_INVOKE_TAIL_TOKEN_RE = re.compile(r"^<\s*/?\s*(invoke|parameter)(\s|>)", re.IGNORECASE)


def _log_ends_with_emitted_invoke(log_stdout: str) -> str | None:
    """Return the opening ``<invoke ...>`` line if the log tail stalls at an
    emitted XML-like tool-call block.

    An agent that prints a tool invocation verbatim (``<invoke name="bash">
    <parameter name="command">...`` and typically ``</invoke>``) as plain text
    and then never makes progress leaves that block as its final meaningful
    output. We look for the structural shape — an ``<invoke`` opener somewhere
    in the tail and a final line that is invoke/parameter syntax — not any
    specific command text or path, so any such emitted call is caught whether
    the ``</invoke>`` closing tag is present or not.

    Returns the opening ``<invoke ...>`` line as a diagnostic excerpt, or
    ``None`` when the log does not stall at an emitted block.
    """
    lines = [_ANSI_ESCAPE_RE.sub("", line).strip() for line in log_stdout.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None
    invoke_line = None
    for line in lines:
        if _INVOKE_OPEN_RE.search(line):
            invoke_line = line
    if invoke_line is None:
        return None
    if not _INVOKE_TAIL_TOKEN_RE.match(lines[-1]):
        return None
    return invoke_line


def _detect_agent_emitted_invoke_stall(
    *,
    log_stdout: str,
    files: dict[str, dict[str, Any]],
    active: bool,
    terminal: bool,
    now_epoch: int,
    emitted_invoke_after_seconds: int,
) -> dict[str, Any]:
    """Detect an agent stalled after emitting a tool-call block as plain text.

    An active, non-terminal agent whose log tail ends at an emitted XML-like
    ``<invoke>`` block and whose progress artifacts have not advanced for a
    threshold has likely printed a tool call instead of executing it. The
    ``<invoke`` block itself must not count as progress; only fresh semantic
    artifacts (status/consensus/report/diff/required-checks) may silence it.
    """
    progress = _progress_artifact_activity(files, now_epoch)
    progress_age = progress.get("age_seconds")
    invoke_line = _log_ends_with_emitted_invoke(log_stdout)
    no_recent_progress = isinstance(progress_age, int) and progress_age >= emitted_invoke_after_seconds
    detected = bool(
        active
        and not terminal
        and invoke_line is not None
        and no_recent_progress
    )
    return {
        "detected": detected,
        "last_invoke_line": invoke_line,
        "progress": progress,
        "progress_age_seconds": progress_age,
        "continuation_prompt": AGENT_REASONING_LOOP_CONTINUATION_PROMPT,
    }


def agent_task_status(
    run_cmd,
    *,
    project: str,
    task_id: str,
    stale_after_seconds: int = AGENT_STALE_AFTER_SECONDS,
    job_status=None,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    """Return a lightweight task snapshot for cheap operator polling.

    Unlike inspect_agent_task(), this does not read the live log tail and does
    not run reasoning-loop detectors that need log text. It is intended as the
    first polling surface while an agent is normally running; operators can
    escalate to inspect_agent_task only when the compact verdict needs deeper
    log-backed diagnosis.
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
            "next": {
                "write_agent_task": {"project": project, "task_id": task_id},
            },
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
        "heartbeat": _task_file_stat(
            run_cmd, project=project, task_id=task_id, filename=AGENT_HEARTBEAT_FILENAME
        ),
        "proxy_status": _task_file_stat(
            run_cmd, project=project, task_id=task_id, filename=AGENT_PROXY_STATUS_FILENAME
        ),
        "failure_status": _task_file_stat(
            run_cmd, project=project, task_id=task_id, filename=AGENT_FAILURE_STATUS_FILENAME
        ),
        "report": _task_file_stat(run_cmd, project=project, task_id=task_id, filename="agent-report.md"),
        "diff": _task_file_stat(
            run_cmd, project=project, task_id=task_id, filename="implementation-diff.patch"
        ),
        "consensus": _task_file_stat(run_cmd, project=project, task_id=task_id, filename="consensus.md"),
        "worker_status": _task_file_stat(
            run_cmd, project=project, task_id=task_id, filename="worker-status.md"
        ),
        "required_checks": _task_file_stat(
            run_cmd, project=project, task_id=task_id, filename="required-checks.log"
        ),
        "attempt_state": _task_file_stat(
            run_cmd, project=project, task_id=task_id, filename=ATTEMPT_STATE_FILENAME
        ),
    }
    activity = _latest_activity(
        {name: meta for name, meta in files.items() if name not in {"heartbeat", "proxy_status"}}, now
    )
    semantic_activity = _progress_artifact_activity(files, now)
    semantic_age = semantic_activity.get("age_seconds")
    heartbeat = _read_agent_heartbeat(run_cmd, project=project, task_id=task_id, now_epoch=now)
    heartbeat_age = heartbeat.get("age_seconds")
    runner_heartbeat_fresh = bool(
        heartbeat.get("state") == "running"
        and isinstance(heartbeat_age, int)
        and heartbeat_age < stale_after_seconds
    )
    proxy_status = _read_agent_proxy_status(
        run_cmd, project=project, task_id=task_id, now_epoch=now
    )
    failure = _read_agent_failure_status(run_cmd, project=project, task_id=task_id)
    if (
        failure.get("valid") is True
        and failure.get("phase") == "pre_useful_work"
    ):
        semantic_activity = {
            "source": None,
            "mtime_epoch": None,
            "age_seconds": None,
        }
        semantic_age = None

    terminal = bool(status_token in _AGENT_TERMINAL_STATUSES or job_token in _AGENT_TERMINAL_STATUSES)
    active = bool(status_token in _AGENT_ACTIVE_STATUSES or job_token in _AGENT_ACTIVE_STATUSES)
    gate_ledger = _read_agent_gate_ledger(
        run_cmd, project=project, task_id=task_id
    )
    gate_ledger["runner_lifecycle_completed"] = bool(
        gate_ledger.get("valid") is True
        and job is not None
        and job.get("known") is True
        and job_token in _AGENT_TERMINAL_STATUSES
    )
    gate_ledger["authoritative"] = False
    reconciliation = _agent_reconciliation_diagnostics(
        attempt=attempt,
        job=job,
        status=status_token,
        files=files,
        active=active,
        terminal=terminal,
        last_activity=activity,
        semantic_activity=semantic_activity,
        runner_heartbeat_fresh=runner_heartbeat_fresh,
    )
    likely_hung = bool(active and not terminal and isinstance(semantic_age, int) and semantic_age >= stale_after_seconds)
    if reconciliation.get("state") is not None:
        likely_hung = True

    failure_reason = failure.get("reason") if failure.get("valid") is True else None
    if terminal and isinstance(failure_reason, str):
        verdict = failure_reason
    elif terminal:
        verdict = "finished"
    elif reconciliation.get("state") is not None:
        verdict = str(reconciliation["state"])
    elif likely_hung:
        verdict = "likely_hung"
    elif active:
        verdict = "running"
    elif status_token is None and job is None and attempt_error is None:
        verdict = "unknown"
    else:
        verdict = "needs_attention"

    next_actions: dict[str, Any] = {
        "agent_status": {
            "project": project,
            "task_id": task_id,
            "purpose": "cheap polling without log tail",
        },
    }
    if verdict in {"lost_after_restart", "orphaned_attempt", "artifact_incomplete", "likely_hung", "needs_attention", "unknown"}:
        next_actions["inspect_agent_task"] = {
            "project": project,
            "task_id": task_id,
            "purpose": "deep diagnostics with bounded log tail and stall detectors",
        }
    if terminal:
        next_actions["read_agent_report"] = {"project": project, "task_id": task_id}
        next_actions["read_agent_diff"] = {"project": project, "task_id": task_id}
    elif job_id:
        next_actions["job_status"] = {"job_id": job_id}

    return {
        "project": project,
        "task_id": task_id,
        "exists": True,
        "status": status_token,
        "job": job,
        "attempt": attempt,
        "attempt_state_error": attempt_error,
        "files": files,
        "last_activity": activity,
        "last_useful_activity": semantic_activity,
        "reconciliation": reconciliation,
        "runner_heartbeat": heartbeat,
        "runner_heartbeat_fresh": runner_heartbeat_fresh,
        "proxy_status": proxy_status,
        "failure": failure,
        "acceptance": gate_ledger,
        "stale_after_seconds": stale_after_seconds,
        "terminal": terminal,
        "likely_hung": likely_hung,
        "verdict": verdict,
        "log_included": False,
        "next": next_actions,
    }



def _supervisor_recreate_hint(
    project: str,
    task_id: str,
    *,
    continuation_prompt: str | None = None,
) -> dict[str, Any]:
    """Describe a safe replacement task without treating workspace files as authority."""
    hint: dict[str, Any] = {
        "project": project,
        "source_task_id": task_id,
        "new_task_id": "<new-task-id>",
        "requires_supervisor_owned_contract": True,
        "do_not_copy_source_task_json": True,
    }
    if continuation_prompt is not None:
        hint["continuation_prompt"] = continuation_prompt
    return hint


def inspect_agent_task(
    run_cmd,
    *,
    project: str,
    task_id: str,
    tail_lines: int = 120,
    stale_after_seconds: int = AGENT_STALE_AFTER_SECONDS,
    reasoning_loop_after_seconds: int = AGENT_REASONING_LOOP_AFTER_SECONDS,
    trailing_colon_after_seconds: int = AGENT_TRAILING_COLON_AFTER_SECONDS,
    emitted_invoke_after_seconds: int = AGENT_EMITTED_INVOKE_AFTER_SECONDS,
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
    if isinstance(reasoning_loop_after_seconds, bool) or not isinstance(reasoning_loop_after_seconds, int):
        raise TypeError("reasoning_loop_after_seconds must be an integer")
    if not 30 <= reasoning_loop_after_seconds <= 86_400:
        raise ValueError("reasoning_loop_after_seconds must be between 30 and 86400")
    if isinstance(trailing_colon_after_seconds, bool) or not isinstance(trailing_colon_after_seconds, int):
        raise TypeError("trailing_colon_after_seconds must be an integer")
    if not 30 <= trailing_colon_after_seconds <= 86_400:
        raise ValueError("trailing_colon_after_seconds must be between 30 and 86400")
    if isinstance(emitted_invoke_after_seconds, bool) or not isinstance(emitted_invoke_after_seconds, int):
        raise TypeError("emitted_invoke_after_seconds must be an integer")
    if not 30 <= emitted_invoke_after_seconds <= 86_400:
        raise ValueError("emitted_invoke_after_seconds must be between 30 and 86400")
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
        "proxy_status": _task_file_stat(run_cmd, project=project, task_id=task_id, filename=AGENT_PROXY_STATUS_FILENAME),
        "failure_status": _task_file_stat(run_cmd, project=project, task_id=task_id, filename=AGENT_FAILURE_STATUS_FILENAME),
        "report": _task_file_stat(run_cmd, project=project, task_id=task_id, filename="agent-report.md"),
        "diff": _task_file_stat(run_cmd, project=project, task_id=task_id, filename="implementation-diff.patch"),
        "consensus": _task_file_stat(run_cmd, project=project, task_id=task_id, filename="consensus.md"),
        "worker_status": _task_file_stat(run_cmd, project=project, task_id=task_id, filename="worker-status.md"),
        "required_checks": _task_file_stat(run_cmd, project=project, task_id=task_id, filename="required-checks.log"),
        "attempt_state": _task_file_stat(run_cmd, project=project, task_id=task_id, filename=ATTEMPT_STATE_FILENAME),
    }
    # Heartbeat/proxy sidecars prove wrapper/provider liveness, but they are
    # deliberately excluded from semantic activity so keepalives do not hide a
    # stuck/silent agent.
    activity = _latest_activity(
        {name: meta for name, meta in files.items() if name not in {"heartbeat", "proxy_status"}}, now
    )
    semantic_activity = _progress_artifact_activity(files, now)
    semantic_age = semantic_activity.get("age_seconds")
    heartbeat = _read_agent_heartbeat(
        run_cmd, project=project, task_id=task_id, now_epoch=now
    )
    heartbeat_age = heartbeat.get("age_seconds")
    runner_heartbeat_fresh = bool(
        heartbeat.get("state") == "running"
        and isinstance(heartbeat_age, int)
        and heartbeat_age < stale_after_seconds
    )
    proxy_status = _read_agent_proxy_status(
        run_cmd, project=project, task_id=task_id, now_epoch=now
    )
    failure = _read_agent_failure_status(run_cmd, project=project, task_id=task_id)
    if (
        failure.get("valid") is True
        and failure.get("phase") == "pre_useful_work"
    ):
        semantic_activity = {
            "source": None,
            "mtime_epoch": None,
            "age_seconds": None,
        }
        semantic_age = None

    terminal = bool(status_token in _AGENT_TERMINAL_STATUSES or job_token in _AGENT_TERMINAL_STATUSES)
    active = bool(status_token in _AGENT_ACTIVE_STATUSES or job_token in _AGENT_ACTIVE_STATUSES)
    gate_ledger = _read_agent_gate_ledger(
        run_cmd, project=project, task_id=task_id
    )
    gate_ledger["runner_lifecycle_completed"] = bool(
        gate_ledger.get("valid") is True
        and job is not None
        and job.get("known") is True
        and job_token in _AGENT_TERMINAL_STATUSES
    )
    gate_ledger["authoritative"] = False
    reconciliation = _agent_reconciliation_diagnostics(
        attempt=attempt,
        job=job,
        status=status_token,
        files=files,
        active=active,
        terminal=terminal,
        last_activity=activity,
        semantic_activity=semantic_activity,
        runner_heartbeat_fresh=runner_heartbeat_fresh,
    )
    likely_hung = bool(active and not terminal and isinstance(semantic_age, int) and semantic_age >= stale_after_seconds)
    if reconciliation.get("state") is not None:
        likely_hung = True

    log = read_agent_log_tail(run_cmd, project=project, task_id=task_id, tail_lines=tail_lines)
    startup = _agent_startup_diagnostics(
        status=status_token,
        status_text=status_text if status_text != "(not found)" else "",
        log_stdout=str(log.get("stdout", "")),
        files=files,
        active=active,
        now_epoch=now,
        proxy_status=proxy_status,
        failure=failure,
    )
    reasoning_loop = _detect_agent_reasoning_loop(
        log_stdout=str(log.get("stdout", "")),
        files=files,
        active=active,
        terminal=terminal,
        now_epoch=now,
        reasoning_loop_after_seconds=reasoning_loop_after_seconds,
    )
    trailing_colon_stall = _detect_agent_trailing_colon_stall(
        log_stdout=str(log.get("stdout", "")),
        files=files,
        active=active,
        terminal=terminal,
        now_epoch=now,
        trailing_colon_after_seconds=trailing_colon_after_seconds,
    )
    emitted_invoke_stall = _detect_agent_emitted_invoke_stall(
        log_stdout=str(log.get("stdout", "")),
        files=files,
        active=active,
        terminal=terminal,
        now_epoch=now,
        emitted_invoke_after_seconds=emitted_invoke_after_seconds,
    )

    failure_reason = failure.get("reason") if failure.get("valid") is True else None
    if startup.get("startup_timeout"):
        verdict = "startup_timeout"
    elif terminal and isinstance(failure_reason, str):
        verdict = failure_reason
    elif terminal:
        verdict = "finished"
    elif reconciliation.get("state") is not None:
        verdict = str(reconciliation["state"])
        likely_hung = True
    elif startup.get("dead_time_kind") == "opencode_startup":
        verdict = "startup_stalled"
    elif reasoning_loop.get("detected"):
        verdict = "reasoning_loop"
        likely_hung = True
    elif trailing_colon_stall.get("detected"):
        verdict = "trailing_colon_stall"
        likely_hung = True
    elif emitted_invoke_stall.get("detected"):
        verdict = "emitted_invoke_stall"
        likely_hung = True
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
        "last_useful_activity": semantic_activity,
        "reconciliation": reconciliation,
        "runner_heartbeat": heartbeat,
        "runner_heartbeat_fresh": runner_heartbeat_fresh,
        "proxy_status": proxy_status,
        "failure": failure,
        "acceptance": gate_ledger,
        "startup": startup,
        "reasoning_loop": reasoning_loop,
        "trailing_colon_stall": trailing_colon_stall,
        "emitted_invoke_stall": emitted_invoke_stall,
        "stale_after_seconds": stale_after_seconds,
        "reasoning_loop_after_seconds": reasoning_loop_after_seconds,
        "trailing_colon_after_seconds": trailing_colon_after_seconds,
        "emitted_invoke_after_seconds": emitted_invoke_after_seconds,
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
    if reconciliation.get("state") is not None:
        result["recovery"] = {
            "action": "inspect_artifacts_preserve_execution_identity",
            "worker_termination_proven": False,
            "replacement_allowed_now": False,
            "do_not_assume_worker_terminated": True,
            "do_not_create_new_task_id": True,
            "inspect_agent_task": {"project": project, "task_id": task_id},
            "supervisor_recreate_after_termination": _supervisor_recreate_hint(
                project,
                task_id,
            ),
        }
    elif (
        terminal
        and failure_reason == "opencode_server_error"
        and failure.get("phase") == "pre_useful_work"
    ):
        result["recovery"] = {
            "action": "recreate_from_supervisor_contract",
            "worker_termination_proven": True,
            "replacement_allowed_now": True,
            "supervisor_recreate": _supervisor_recreate_hint(project, task_id),
            "run_agent_after_recreate": {
                "project": project,
                "task_id": "<new-task-id>",
            },
        }
    elif reasoning_loop.get("detected"):
        result["recovery"] = {
            "action": "cancel_then_recreate_from_supervisor_contract",
            "worker_termination_proven": False,
            "replacement_allowed_now": False,
            "cancel_agent_task": {"project": project, "task_id": task_id},
            "supervisor_recreate_after_termination": _supervisor_recreate_hint(
                project,
                task_id,
                continuation_prompt=AGENT_REASONING_LOOP_CONTINUATION_PROMPT,
            ),
        }
    elif trailing_colon_stall.get("detected"):
        result["recovery"] = {
            "action": "cancel_then_recreate_from_supervisor_contract",
            "worker_termination_proven": False,
            "replacement_allowed_now": False,
            "cancel_agent_task": {"project": project, "task_id": task_id},
            "supervisor_recreate_after_termination": _supervisor_recreate_hint(
                project,
                task_id,
                continuation_prompt=trailing_colon_stall["continuation_prompt"],
            ),
        }
    elif emitted_invoke_stall.get("detected"):
        result["recovery"] = {
            "action": "cancel_then_recreate_from_supervisor_contract",
            "worker_termination_proven": False,
            "replacement_allowed_now": False,
            "cancel_agent_task": {"project": project, "task_id": task_id},
            "supervisor_recreate_after_termination": _supervisor_recreate_hint(
                project,
                task_id,
                continuation_prompt=emitted_invoke_stall["continuation_prompt"],
            ),
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
    source_mode: str | None = None,
    source_ref: str | None = None,
    source_tree_sha: str | None = None,
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
        acceptance_criteria=acceptance_criteria,
        worktree_path=worktree_path,
        base_ref=base_ref,
        allowed_backends=allowed_backends,
        managed_source_sha256=managed_source_sha256,
        source_mode=source_mode,
        source_ref=source_ref,
        source_tree_sha=source_tree_sha,
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
    task_data = json.loads(task_json)
    gates_markdown = build_gates_markdown(
        task_id,
        task_data["gates"],
        required_checks=required_checks,
    )

    tasks_dir = task_tasks_dir(project)
    targets = [
        f"{td}/task.json",
        f"{td}/current-plan.md",
        f"{td}/consensus.md",
        f"{td}/agent-status.md",
        f"{td}/{GATES_MD_FILENAME}",
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
            _encoded_write(f"{td}/{GATES_MD_FILENAME}", gates_markdown),
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


def _validate_continuation_prompt(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise TypeError("continuation_prompt must be a string or None")
    prompt = value.strip()
    if not prompt:
        return None
    if len(prompt) > 400:
        raise ValueError("continuation_prompt must be at most 400 characters")
    return prompt


def _retry_plan_text(
    plan: str,
    *,
    source_task_id: str,
    retry_task_id: str,
    continuation_prompt: str | None = None,
) -> str:
    prompt = _validate_continuation_prompt(continuation_prompt)
    prefix = (
        f"# Retry of {source_task_id}\n\n"
        f"- Source task ID: {source_task_id}\n"
        f"- Retry task ID: {retry_task_id}\n"
        f"- Prepared: {datetime.now(UTC).isoformat()}\n\n"
    )
    if prompt:
        prefix += (
            "## Continuation prompt\n\n"
            f"{prompt}\n\n"
            "The previous attempt may have been stopped for a reasoning loop; continue from durable task files, "
            "do not repeat intent-only planning, and make the next observable progress step.\n\n"
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
    continuation_prompt: str | None = None,
    trusted_never_submitted: bool = False,
) -> dict[str, Any]:
    """Prepare a fresh task only from a control-plane-proven never-submitted source.

    A missing job_id is not proof that dispatch never happened: the durable
    submit path may have crossed the Gateway boundary before losing the job
    receipt. Callers must therefore provide an explicit trusted
    trusted_never_submitted decision from outside worker-writable task
    artifacts. Production currently has no such retry proof and fails closed.
    """
    validate_task_id(source_task_id)
    validate_task_id(retry_task_id)
    continuation_prompt = _validate_continuation_prompt(continuation_prompt)
    if not isinstance(trusted_never_submitted, bool):
        raise TypeError("trusted_never_submitted must be a boolean")
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
    attempt = (
        inspection.get("attempt")
        if isinstance(inspection.get("attempt"), dict)
        else None
    )
    unbound_attempt = bool(attempt and attempt.get("job_id") is None)
    if not inspection.get("terminal") and not unbound_attempt:
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

    acceptance = inspection.get("acceptance")
    if (
        isinstance(acceptance, dict)
        and acceptance.get("exists") is True
        and acceptance.get("valid") is not True
    ):
        return {
            "stdout": "",
            "stderr": "source task gate evidence does not match its immutable task contract",
            "exit_code": 1,
            "code": "AGENT_GATE_EVIDENCE_INVALID",
            "source": {
                "task_id": source_task_id,
                "status": inspection.get("status"),
                "verdict": inspection.get("verdict"),
                "acceptance": acceptance,
            },
        }

    if (
        inspection.get("attempt_state_error") is not None
        or not trusted_never_submitted
        or (attempt is not None and not unbound_attempt)
    ):
        return {
            "stdout": "",
            "stderr": (
                "source task lacks trusted never-submitted proof; worker-writable "
                "task.json cannot seed a new task_id. Retry the same durable execution "
                "or recreate the task from the supervisor-owned contract"
            ),
            "exit_code": 1,
            "code": "AGENT_RETRY_SOURCE_UNTRUSTED",
            "source": {
                "task_id": source_task_id,
                "status": inspection.get("status"),
                "verdict": inspection.get("verdict"),
                "job": inspection.get("job"),
                "acceptance": acceptance,
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
    retry_gates = resolve_gate_specs(retry_contract)
    retry_contract["gates"] = retry_gates
    validate_scope_contract(
        retry_contract.get("allowed_files") or [],
        retry_contract.get("forbidden_files") or [],
    )
    validate_base_ref(retry_contract.get("base_ref") or None)
    resolve_task_source_contract(retry_contract)

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
            continuation_prompt=continuation_prompt,
        ),
        f"{td}/consensus.md": consensus,
        f"{td}/agent-status.md": status,
        f"{td}/{GATES_MD_FILENAME}": build_gates_markdown(
            retry_task_id,
            retry_gates,
            required_checks=retry_contract.get("required_checks") or [],
        ),
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
            "continuation_prompt": continuation_prompt,
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
