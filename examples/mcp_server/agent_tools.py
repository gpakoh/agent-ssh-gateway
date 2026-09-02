"""Agent Backend Router MCP tool — routes task to OpenCode via router selection.

The router selects the backend based on availability and cooldown state.
When disabled, falls back to the task.json ``agent`` field.

Runs opencode with --dangerously-skip-permissions -- opencode's own
internal safety confirmations are disabled for unattended execution.
Gated by write-mode (assert_handoff_write_allowed, checked by the
gateway_run_agent/gateway_run_opencode MCP tool wrappers before calling
into this module) and by tool-mode registration (run_agent/run_opencode
are excluded from mcp_client/mcp_client_write's tool sets -- see
tool_modes.py) -- there is no confirmation flow or override *within*
this module itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from command_policy import CommandPolicyError  # noqa: F401 -- re-exported for tests

from examples.mcp_server.agent_paths import (
    managed_source_bundle_path,
    managed_workspace_path,
    project_state_key,
    task_dir,
)
from examples.mcp_server.agent_sources import (
    ManagedSourceDigestError,
    validate_bundle_digest,
)
from examples.mcp_server.agent_tasks import AttemptConflictError, AttemptStateError

TASKS_REL_DIR = ".ai-bridge/tasks"

# Same markers as quart-platform/opencode-adapter's LIMIT_MARKERS (minus
# the bare "pay", which would false-positive on ordinary opencode output).
# Used to detect a rate-limited proxy in the captured run log and report
# it back to the provider (POST {provider}/proxy/report) for cooldown.
PROXY_LIMIT_MARKERS = (
    "rate limit",
    "too many requests",
    "quota",
    "credits",
    "payment required",
    "free tier",
    "usage limit",
    "please upgrade",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _agent_submission_key(project: str, task_id: str) -> str:
    """Stable gateway idempotency key shared by run_agent/run_opencode."""
    return f"task:{project_state_key(project)}:{task_id}"


def _command_fingerprint(cmd: str) -> str:
    """Hash of the generated shell command ONLY.

    The shell embeds every execution-relevant launch input (model, scope,
    worktree, base_ref, managed source), but it REFERENCES the plan by path
    (``$td/current-plan.md``) -- the plan's BYTES are never part of cmd. Use
    ``_execution_fingerprint`` when the whole execution contract (plan
    content, canonical task.json, model, cmd) must be bound.
    """
    return hashlib.sha256(cmd.encode("utf-8")).hexdigest()


def _execution_fingerprint(
    cmd: str,
    plan: str | None,
    task_json: dict[str, Any] | None,
    model: str | None,
) -> str:
    """Cryptographically bind the exact execution contract to the attempt.

    The generated shell references the plan by path, not by bytes, so cmd
    alone cannot detect a plan rewrite. The fingerprint therefore covers:
      - current-plan.md CONTENT (exact bytes);
      - the canonical task.json blob -- while task_id is immutable, EVERY
        task.json change is treated as changed content, no field guessing;
      - the explicit model override;
      - the generated cmd.

    Deterministic JSON serialization only (sort_keys=True, stable
    separators) -- never repr(dict), whose representations are unstable.
    A change in any bound input changes the fingerprint, so the identity
    resolver surfaces a typed immutable-task-conflict instead of replaying
    an old execution against new content.
    """
    contract = {
        "cmd": cmd,
        "plan": plan,
        "task_json": task_json,
        "model": model,
    }
    payload = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _new_attempt_id() -> str:
    """One durable execution-attempt identity, created before submission."""
    return uuid.uuid4().hex


def _agent_attempt_key(project: str, task_id: str, attempt_id: str | None) -> str:
    """Gateway idempotency key scoped to one execution attempt.

    task_id alone is NOT an execution identity: the durable attempt_id ties
    every retry and reconnect of the SAME (immutable) execution to one job,
    and different content on the same task_id is rejected as a typed
    immutable-task conflict instead of being allowed a second key/job.
    """
    base = _agent_submission_key(project, task_id)
    if not attempt_id:
        return base
    return f"{base}:attempt:{attempt_id}"


def _job_status_terminal(snapshot: dict[str, Any] | None) -> bool:
    """Terminally decided gateway job status, or False when unknown."""
    return snapshot is not None and snapshot.get("status") in {"completed", "failed", "cancelled"}


def _job_status_active(snapshot: dict[str, Any] | None) -> bool:
    """Nonterminal status the backend proved is still in flight."""
    return snapshot is not None and snapshot.get("status") in {
        "pending", "processing", "running", "cancelling",
    }


_SUBMIT_RETRY_ATTEMPTS = 3
_WAIT_RETRY_ATTEMPTS = 2


def _error_text(exc: Exception | None) -> str:
    return str(exc) if exc is not None else "unknown error"


def _agent_diagnostics_hint(project: str, task_id: str, job_id: str | None = None) -> dict[str, Any]:
    """Return MCP-native follow-up calls for inspecting a submitted agent task."""
    hint: dict[str, Any] = {
        "inspect_agent_task": {
            "project": project,
            "task_id": task_id,
            "purpose": "status, job state, artifact mtimes, stale/hung verdict, and log tail",
        },
        "read_agent_log": {"project": project, "task_id": task_id, "purpose": "raw bounded log tail"},
        "read_agent_status": {"project": project, "task_id": task_id, "purpose": "agent-status.md"},
    }
    if job_id:
        hint["job_status"] = {"job_id": job_id, "purpose": "gateway job state"}
    return hint


def _submit_same_key_retry(
    run_script_async: Callable[[str, str, str], dict[str, Any]],
    project: str,
    cmd: str,
    submission_key: str,
) -> tuple[str | None, Exception | None]:
    """Submit under one stable idempotency key with bounded retries.

    The backend reserves the key atomically together with the job envelope
    (reserve_submission_with_job), so a response lost AFTER acceptance is
    recovered by re-submitting the SAME key: the backend returns the
    original job_id without enqueuing a second execution. Returns
    (job_id, last_error); job_id is None only when nothing could be proven
    accepted -- never a duplicate, never an ambiguous timeout.
    """
    last_error: Exception | None = None
    for _attempt in range(_SUBMIT_RETRY_ATTEMPTS):
        try:
            submitted = run_script_async(project, cmd, submission_key)
            job_id = submitted.get("job_id") if isinstance(submitted, dict) else None
            if job_id:
                return job_id, None
            last_error = RuntimeError("async submit returned no job_id")
        except Exception as exc:  # transport / gateway outage; key is idempotent
            last_error = exc
    return None, last_error


def _resolve_attempt(
    read_attempt_state: Callable[[str, str], dict[str, Any] | None],
    claim_attempt_state: Callable[[str, str, dict[str, Any]], bool],
    project: str,
    task_id: str,
    fingerprint: str,
) -> tuple[str, str | None]:
    """Resolve attempt_id + job_id for a run (ONE task_id = ONE execution).

    The task_id is immutable once any attempt record exists:
      - existing record + fingerprint MATCH -> REUSE the recorded attempt
        and its bound job_id UNCONDITIONALLY, terminal or not. A later
        same-content re-invocation (retry, reconnect, process restart,
        terminal replay) returns the SAME execution instead of launching a
        second one; the decision is stateless -- job_status is never
        consulted (a terminal bound job is just as reusable as an active
        one).
      - existing record + fingerprint MISMATCH -> raise AttemptConflictError
        (the recorded attempt belongs to different content; the caller must
        create a NEW task_id). The backend is never submitted for it.
      - NO record -> the FIRST attempt: a fresh attempt_id is claimed with
        the create-if-absent CAS primitive. A concurrent first-attempt race
        (two callers that BOTH observed absence) converges on the single
        winner; the loser re-reads and reuses that record. A lost claim
        with no winner is an inconsistency -> AttemptStateError.

    The durable decision is written (claimed) BEFORE any submission, so a
    crash, restart, or transport retry converges on the same attempt
    instead of a second execution.
    """
    existing = read_attempt_state(project, task_id)
    if existing is not None:
        if existing.get("fingerprint") == fingerprint:
            return existing["attempt_id"], existing.get("job_id")
        raise AttemptConflictError(
            project=project,
            task_id=task_id,
            attempt_id=existing["attempt_id"],
            job_id=existing.get("job_id"),
            recorded_fingerprint=existing["fingerprint"],
            requested_fingerprint=fingerprint,
        )

    attempt_id = _new_attempt_id()
    if claim_attempt_state(
        project,
        task_id,
        {"attempt_id": attempt_id, "fingerprint": fingerprint, "job_id": None},
    ):
        return attempt_id, None

    winner = read_attempt_state(project, task_id)
    if winner is None:
        raise AttemptStateError(
            f"attempt claim for task {task_id} was lost but no winner record exists"
        )
    if winner.get("fingerprint") != fingerprint:
        raise AttemptConflictError(
            project=project,
            task_id=task_id,
            attempt_id=winner["attempt_id"],
            job_id=winner.get("job_id"),
            recorded_fingerprint=winner["fingerprint"],
            requested_fingerprint=fingerprint,
        )
    return winner["attempt_id"], winner.get("job_id")


def _wait_same_job(
    run_script_wait: Callable[[str], dict[str, Any]],
    job_status: Callable[[str], dict[str, Any]],
    job_id: str,
) -> dict[str, Any]:
    """Wait on a known job_id with bounded transport-tolerant reconciliation.

    The underlying wait call is retried a bounded number of times (waiting
    is a read; a duplicate is impossible). If the transport never answers,
    the gateway job_status is consulted: a terminal snapshot is returned as
    a terminal receipt, a proven-nonterminal snapshot as a running receipt,
    and an unreachable/ambiguous backend as a typed "unknown" receipt. This
    never fabricates "running" without proof and never returns an opaque
    timeout with no way forward -- every receipt carries job_id.
    """
    for _attempt in range(_WAIT_RETRY_ATTEMPTS):
        try:
            return run_script_wait(job_id)
        except Exception:
            continue  # wait is a read; a retry can never duplicate execution

    try:
        snapshot = job_status(job_id)
    except Exception as exc:
        return {
            "status": "unknown",
            "job_id": job_id,
            "error": _error_text(exc),
            "wait_timed_out": False,
            "reconciled_via": "job_status",
        }

    if _job_status_terminal(snapshot):
        return {
            "status": snapshot.get("status", "completed"),
            "job_id": job_id,
            "exit_code": snapshot.get("exit_code"),
            "stdout": str(snapshot.get("stdout", "")),
            "stderr": str(snapshot.get("stderr", "")),
            "reconciled_via": "job_status",
        }
    if _job_status_active(snapshot):
        return {
            "status": "running",
            "job_id": job_id,
            "wait_timed_out": True,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "reconciled_via": "job_status",
        }
    return {
        "status": "unknown",
        "job_id": job_id,
        "error": f"job status {snapshot.get('status')!r}; transport unreachable",
        "wait_timed_out": False,
        "reconciled_via": "job_status",
    }


def _durable_missing(
    *,
    run_script_async: Callable[[str, str, str], dict[str, Any]] | None,
    run_script_wait: Callable[[str], dict[str, Any]] | None,
    read_attempt_state: Callable[[str, str], dict[str, Any] | None] | None,
    claim_attempt_state: Callable[[str, str, dict[str, Any]], bool] | None,
    write_attempt_state: Callable[[str, str, dict[str, Any]], None] | None,
    job_status: Callable[[str], dict[str, Any]] | None,
) -> list[str]:
    """Names of the durable-submission callables that are missing, in order.

    The durable sync contract is all-or-nothing: a caller that asks for the
    idempotent submit+wait path but omits even one primitive is prevented
    from silently degrading to a single blocking execution.
    """
    missing: list[str] = []
    for name, present in (
        ("run_script_async", run_script_async),
        ("run_script_wait", run_script_wait),
        ("read_attempt_state", read_attempt_state),
        ("claim_attempt_state", claim_attempt_state),
        ("write_attempt_state", write_attempt_state),
        ("job_status", job_status),
    ):
        if present is None:
            missing.append(name)
    return missing


def _shell_escape(text: str) -> str:
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"


def _resolve_project_root(project: str) -> str | None:
    """Resolve a project name to its absolute host root, or None if it
    can't be resolved (unknown project, registry unavailable, etc.) --
    callers must treat a None result as "skip this, don't fail the whole
    call over it" (used for both the script's own `cd` and for redacting
    the root out of returned stdout/stderr)."""
    try:
        from app.workspace.registry import get_registry

        return str(get_registry().project_info(project)["root"])
    except Exception:
        return None


def _read_task_json(
    run_cmd: Callable[[str, str], dict[str, Any]],
    project: str,
    task_id: str,
) -> dict[str, Any]:
    from examples.mcp_server.agent_tasks import read_agent_task_file

    result = read_agent_task_file(
        run_cmd,
        project=project,
        task_id=task_id,
        filename="task.json",
    )
    raw = result.get("stdout", "")
    if raw == "(not found)" or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        # Corrupt/unrelated stdout must not crash the caller. run_agent
        # still fails closed (empty dict -> "task.json not found or empty");
        # run_opencode treats task.json as optional metadata (worktree_path)
        # and proceeds with the plan as its only contract.
        return {}


def _task_string_list(task_json: dict[str, Any], key: str) -> list[str]:
    """Return a validated list[str] from the supervisor task contract."""
    value = task_json.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"task.json field {key!r} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def _read_current_plan(
    run_cmd: Callable[[str, str], dict[str, Any]],
    project: str,
    task_id: str,
) -> str | None:
    """Read current-plan.md content WITHOUT any normalization.

    Returns the EXACT bytes read from the file, untouched by ``.strip()``:
    the content feeds the contract fingerprint, so a trailing-newline or
    leading-space mutation must change the fingerprint (the worker executes
    the real on-disk file, which differs). Whitespace-only output is still
    treated as ABSENT (None): the presence check strips, the returned bytes
    do not. ``_execution_fingerprint`` is the only downstream consumer of
    the plan content -- the shell built later references the file by path.
    """
    from examples.mcp_server.agent_tasks import read_agent_task_file

    result = read_agent_task_file(
        run_cmd,
        project=project,
        task_id=task_id,
        filename="current-plan.md",
    )
    raw = result.get("stdout", "")
    if raw == "(not found)" or not raw.strip():
        return None
    return raw


def _proxy_fetch_script_lines(
    provider_url: str,
    timeout: str,
    *,
    required: bool,
    startup_reserve_bytes: int,
    startup_reserve_seconds: int,
    admission_wait_seconds: int,
    admission_poll_seconds: int,
) -> list[str]:
    """Fetch one *exclusive* live proxy on the SSH target before OpenCode.

    All OpenCode workers in the shared sshd executor coordinate through a
    private ``/tmp/opencode-proxy-leases`` directory.  The provider currently
    returns a pool but has no server-side lease primitive, so the executor
    performs a local lease under ``flock``/``fcntl`` semantics: a proxy URL is
    held by at most one live runner shell at a time.  Dead owners are reclaimed
    by PID + process-start-time validation, so PID reuse cannot keep a stale
    lease alive.

    The same serialized admission step reserves a short-lived amount of cgroup
    headroom for processes that are starting concurrently.  Docker's memory
    limit remains only a ceiling; workers consume memory dynamically up to that
    ceiling rather than receiving fixed per-process allocations.

    This is an executor-local coordination mechanism, not a cross-host lease.
    If the fleet ever grows to multiple sshd executors, the provider must gain
    an atomic server-side lease API so exclusivity spans hosts as well.
    """
    required_literal = "1" if required else "0"
    return [
        "",
        "# Exclusive proxy + dynamic cgroup-headroom admission for OpenCode",
        "PROXY_BLOCKED=0",
        "PROXY_FETCH_RESULT=",
        "OPENCODE_PROXY_URL=",
        "OPENCODE_PROXY_LEASE_FILE=",
        "OPENCODE_PROXY_DIGEST=",
        'OPENCODE_PROXY_CANDIDATES_B64=${OPENCODE_PROXY_CANDIDATES_B64:-}',
        "PROXY_LEASE_ROOT=/tmp/opencode-proxy-leases",
        "PROXY_OWNER_PID=$$",
        'PROXY_OWNER_START=$(awk \'{print $22}\' "/proc/$PROXY_OWNER_PID/stat" 2>/dev/null || true)',
        f"PROXY_REQUIRED={required_literal}",
        f"OPENCODE_STARTUP_RESERVE_BYTES={startup_reserve_bytes}",
        f"OPENCODE_STARTUP_RESERVE_SECONDS={startup_reserve_seconds}",
        f"OPENCODE_ADMISSION_WAIT_SECONDS={admission_wait_seconds}",
        f"OPENCODE_ADMISSION_POLL_SECONDS={admission_poll_seconds}",
        "PROXY_ADMISSION_STARTED=$(date +%s)",
        "while :; do",
        f"PROXY_FETCH_RESULT=$(python3 - {_shell_escape(provider_url)} {_shell_escape(timeout)} \"$PROXY_OWNER_PID\" \"$PROXY_OWNER_START\" \"$PROXY_LEASE_ROOT\" \"$OPENCODE_STARTUP_RESERVE_BYTES\" \"$OPENCODE_STARTUP_RESERVE_SECONDS\" \"${{OPENCODE_REJECTED_PROXY_DIGESTS:-}}\" \"${{OPENCODE_PROXY_CANDIDATES_B64:-}}\" <<'PROXYFETCH_EOF'",
        "import base64, fcntl, hashlib, json, os, sys, time, urllib.request",
        "from pathlib import Path",
        "from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit",
        "",
        "provider_url, timeout_raw, owner_pid_raw, owner_start, lease_root_raw, reserve_raw, reserve_seconds_raw, rejected_raw, cached_candidates_raw = sys.argv[1:]",
        "timeout = float(timeout_raw)",
        "owner_pid = int(owner_pid_raw)",
        "reserve_bytes = int(reserve_raw)",
        "reserve_seconds = int(reserve_seconds_raw)",
        "rejected = {item for item in rejected_raw.split(',') if item}",
        "lease_root = Path(lease_root_raw)",
        "lease_root.mkdir(mode=0o700, parents=True, exist_ok=True)",
        "try:",
        "    lease_root.chmod(0o700)",
        "except OSError:",
        "    pass",
        "",
        "def _usable(value):",
        "    if not isinstance(value, str):",
        "        return None",
        "    value = value.strip()",
        "    if not value:",
        "        return None",
        "    try:",
        "        parts = urlsplit(value)",
        "        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.port is None:",
        "            return None",
        "    except ValueError:",
        "        return None",
        "    return value",
        "",
        "def _parse_candidates(raw):",
        "    raw = raw.strip()",
        "    if not raw:",
        "        return []",
        "    try:",
        "        data = json.loads(raw)",
        "    except ValueError:",
        "        candidate = _usable(raw)",
        "        return [candidate] if candidate else []",
        "    values = []",
        "    if isinstance(data, dict):",
        "        proxies = data.get('proxies')",
        "        if isinstance(proxies, list):",
        "            values.extend(proxies)",
        "        for key in ('proxy', 'https_proxy', 'url'):",
        "            if key in data:",
        "                values.append(data[key])",
        "    elif isinstance(data, list):",
        "        values.extend(data)",
        "    result = []",
        "    for value in values:",
        "        candidate = _usable(value)",
        "        if candidate and candidate not in result:",
        "            result.append(candidate)",
        "    return result",
        "",
        "def _fetch(url):",
        "    with urllib.request.urlopen(url, timeout=timeout) as response:",
        "        return response.read().decode('utf-8', 'replace')",
        "",
        "if cached_candidates_raw:",
        "    provider_url = 'data:application/json;base64,' + cached_candidates_raw",
        "parts = urlsplit(provider_url)",
        "query = dict(parse_qsl(parts.query, keep_blank_values=True))",
        "query['format'] = 'provider'",
        "query['limit'] = '100'",
        "pool_url = urlunsplit((parts.scheme, parts.netloc, '/proxies', urlencode(query), ''))",
        "candidates = []",
        "errors = []",
        "for candidate_url in (pool_url, provider_url):",
        "    try:",
        "        candidates = _parse_candidates(_fetch(candidate_url))",
        "    except Exception as exc:",
        "        errors.append(type(exc).__name__)",
        "        candidates = []",
        "    if candidates:",
        "        break",
        "if not candidates:",
        "    print('proxy provider returned no usable proxy (' + ','.join(errors) + ')', file=sys.stderr)",
        "    sys.exit(7 if rejected else 3)",
        "",
        "def _proc_start(pid):",
        "    try:",
        "        fields = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8').split()",
        "        return fields[21] if len(fields) > 21 else ''",
        "    except OSError:",
        "        return ''",
        "",
        "def _read_int(path):",
        "    try:",
        "        raw = Path(path).read_text(encoding='ascii').strip()",
        "        return None if raw == 'max' else int(raw)",
        "    except (OSError, ValueError):",
        "        return None",
        "",
        "lock_path = lease_root / '.lock'",
        "lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)",
        "try:",
        "    fcntl.flock(lock_fd, fcntl.LOCK_EX)",
        "    leased = set()",
        "    startup_reserved = 0",
        "    now = time.time()",
        "    for lease_path in lease_root.glob('*.json'):",
        "        try:",
        "            data = json.loads(lease_path.read_text(encoding='utf-8'))",
        "            pid = int(data.get('pid', -1))",
        "            start = str(data.get('start', ''))",
        "            proxy = str(data.get('proxy', ''))",
        "            if pid <= 0 or not start or _proc_start(pid) != start:",
        "                lease_path.unlink(missing_ok=True)",
        "                continue",
        "            if proxy:",
        "                leased.add(proxy)",
        "            reserve_until = float(data.get('startup_reserve_until', 0) or 0)",
        "            if reserve_until > now:",
        "                startup_reserved += int(data.get('startup_reserve_bytes', 0) or 0)",
        "        except Exception:",
        "            try:",
        "                lease_path.unlink()",
        "            except OSError:",
        "                pass",
        "",
        "    memory_current = _read_int('/sys/fs/cgroup/memory.current')",
        "    memory_max = _read_int('/sys/fs/cgroup/memory.max')",
        "    if memory_current is not None and memory_max is not None:",
        "        projected = memory_current + startup_reserved + reserve_bytes",
        "        if projected > memory_max:",
        "            print(f'cgroup headroom unavailable: projected={projected} max={memory_max}', file=sys.stderr)",
        "            sys.exit(6)",
        "",
        "    eligible = [proxy for proxy in candidates if hashlib.sha256(proxy.encode('utf-8')).hexdigest() not in rejected]",
        "    if not eligible:",
        "        print('no alternative live proxy remains after startup rejection', file=sys.stderr)",
        "        sys.exit(7)",
        "    selected = next((proxy for proxy in eligible if proxy not in leased), None)",
        "    if selected is None:",
        "        print('all live proxies are already leased by active OpenCode workers', file=sys.stderr)",
        "        sys.exit(5)",
        "    digest = hashlib.sha256(selected.encode('utf-8')).hexdigest()",
        "    lease_path = lease_root / f'{digest}.json'",
        "    payload = {",
        "        'pid': owner_pid,",
        "        'start': owner_start,",
        "        'proxy': selected,",
        "        'startup_reserve_bytes': reserve_bytes,",
        "        'startup_reserve_until': now + reserve_seconds,",
        "    }",
        "    tmp_path = lease_root / f'.{digest}.{owner_pid}.tmp'",
        "    fd = os.open(tmp_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)",
        "    with os.fdopen(fd, 'w', encoding='utf-8') as fh:",
        "        json.dump(payload, fh, separators=(',', ':'))",
        "    os.replace(tmp_path, lease_path)",
        "    print(selected)",
        "    print(str(lease_path))",
        "    print(digest)",
        "    print(base64.b64encode(json.dumps(candidates, separators=(',', ':')).encode()).decode())",
        "finally:",
        "    try:",
        "        fcntl.flock(lock_fd, fcntl.LOCK_UN)",
        "    finally:",
        "        os.close(lock_fd)",
        "PROXYFETCH_EOF",
        ")",
        "PROXY_FETCH_RC=$?",
        'if [ "$PROXY_FETCH_RC" -eq 0 ] || [ "$PROXY_FETCH_RC" -eq 7 ]; then break; fi',
        'PROXY_ADMISSION_NOW=$(date +%s)',
        'if [ $((PROXY_ADMISSION_NOW - PROXY_ADMISSION_STARTED)) -ge "$OPENCODE_ADMISSION_WAIT_SECONDS" ]; then break; fi',
        'sleep "$OPENCODE_ADMISSION_POLL_SECONDS"',
        "done",
        'OPENCODE_PROXY_URL=$(printf "%s\\n" "$PROXY_FETCH_RESULT" | sed -n \'1p\')',
        'OPENCODE_PROXY_LEASE_FILE=$(printf "%s\\n" "$PROXY_FETCH_RESULT" | sed -n \'2p\')',
        'OPENCODE_PROXY_DIGEST=$(printf "%s\\n" "$PROXY_FETCH_RESULT" | sed -n \'3p\')',
        'OPENCODE_PROXY_CANDIDATES_B64=$(printf "%s\\n" "$PROXY_FETCH_RESULT" | sed -n \'4p\')',
        'if [ "$PROXY_FETCH_RC" -eq 0 ] && [ -n "$OPENCODE_PROXY_URL" ] && [ -n "$OPENCODE_PROXY_LEASE_FILE" ]; then',
        '  export HTTP_PROXY="$OPENCODE_PROXY_URL" HTTPS_PROXY="$OPENCODE_PROXY_URL" ALL_PROXY="$OPENCODE_PROXY_URL"',
        '  export http_proxy="$OPENCODE_PROXY_URL" https_proxy="$OPENCODE_PROXY_URL" all_proxy="$OPENCODE_PROXY_URL"',
        '  export NO_PROXY="localhost,127.0.0.1" no_proxy="localhost,127.0.0.1"',
        '  echo "Using exclusive live proxy from configured provider" >> "$td/agent-status.md"',
        "else",
        '  printf "Proxy/memory admission failed (rc=%s)\\n" "$PROXY_FETCH_RC" > "$td/proxy-status.log"',
        '  if [ "$PROXY_REQUIRED" -eq 1 ]; then',
        "    PROXY_BLOCKED=1",
        '    echo "Exclusive proxy required; OpenCode launch blocked" >> "$td/agent-status.md"',
        "  else",
        '    echo "Proxy unavailable; direct fallback explicitly allowed" >> "$td/agent-status.md"',
        "  fi",
        "fi",
        "",
    ]


def _proxy_local_release_script_lines() -> list[str]:
    """Release the executor-local proxy lease; stale leases self-heal too."""
    return [
        "",
        "# Release executor-local proxy lease after OpenCode finishes/reporting",
        'if [ -n "${OPENCODE_PROXY_LEASE_FILE:-}" ]; then',
        '  python3 - "$OPENCODE_PROXY_LEASE_FILE" "$PROXY_OWNER_PID" "$PROXY_OWNER_START" <<\'PROXYLOCALRELEASE_EOF\'',
        "import json, os, sys",
        "from pathlib import Path",
        "",
        "lease_path = Path(sys.argv[1])",
        "owner_pid = int(sys.argv[2])",
        "owner_start = sys.argv[3]",
        "root = Path('/tmp/opencode-proxy-leases')",
        "try:",
        "    if lease_path.parent.resolve() != root.resolve():",
        "        raise ValueError('unexpected lease path')",
        "    data = json.loads(lease_path.read_text(encoding='utf-8'))",
        "    if int(data.get('pid', -1)) == owner_pid and str(data.get('start', '')) == owner_start:",
        "        lease_path.unlink(missing_ok=True)",
        "except (OSError, ValueError, json.JSONDecodeError):",
        "    pass",
        "PROXYLOCALRELEASE_EOF",
        "  OPENCODE_PROXY_LEASE_FILE=",
        "fi",
        "",
    ]

def _proxy_report_script_lines(provider_url: str, timeout: str) -> list[str]:
    """Bash lines that give a rate-limited proxy back to the provider.

    Mirrors quart-platform/opencode-adapter's ProxySource.report_limit:
    when the captured opencode log (written by _build_opencode_script to
    ``$td/opencode-output.log``) matches a rate-limit marker, POST
    ``{provider_base}/proxy/report`` with the leased proxy and a
    best-effort retry_after_seconds parsed from the log text. The provider
    puts the proxy on cooldown (it is NOT deleted from the pool), exactly
    like the adapter's report loop: lease a live proxy before the run,
    return it when the IP hit limits.

    The whole block is best-effort: a failed report must never fail the
    agent run (opencode already finished at this point). Sets RATE_LIMITED=1
    so _build_opencode_script can mark the task status accordingly.
    """
    base = urlsplit(provider_url)
    report_url = f"{base.scheme}://{base.netloc}/proxy/report"
    marker_pattern = "|".join(PROXY_LIMIT_MARKERS)
    return [
        "",
        "# Give a rate-limited proxy back to the provider (POST /proxy/report)",
        'if [ -n "$OPENCODE_PROXY_URL" ] && [ -f "$td/opencode-output.log" ]; then',
        f"  if grep -Eqi {_shell_escape(marker_pattern)} \"$td/opencode-output.log\"; then",
        "    RATE_LIMITED=1",
        '    RETRY_AFTER=$(python3 - "$td/opencode-output.log" <<\'PROXYRETRY_EOF\'',
        "import re, sys",
        "",
        "text = open(sys.argv[1], encoding='utf-8', errors='replace').read()",
        "m = re.search(r'(?:retry(?:ing)?|try\\s+again)\\s+in\\s+(\\d+)\\s*(h(?:our)?s?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)(?:\\s+(\\d+)\\s*(h(?:our)?s?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?))?', text, re.I)",
        "if m:",
        "    total = 0",
        "    for i in range(1, len(m.groups()), 2):",
        "        number, unit = m.group(i), m.group(i + 1)",
        "        if not number or not unit:",
        "            continue",
        "        unit = unit.lower()",
        "        if unit.startswith('h'):",
        "            total += int(number) * 3600",
        "        elif unit.startswith('m'):",
        "            total += int(number) * 60",
        "        else:",
        "            total += int(number)",
        "    if total:",
        "        print(total)",
        "        sys.exit(0)",
        "m2 = re.search(r'\\bretry\\s+after\\s+(\\d+)\\b', text, re.I)",
        "if m2:",
        "    print(int(m2.group(1)))",
        "PROXYRETRY_EOF",
        ")",
        '    [ -n "$RETRY_AFTER" ] || RETRY_AFTER=300',
        '    python3 - "$OPENCODE_PROXY_URL" "$RETRY_AFTER" <<\'PROXYREPORT_EOF\'',
        "import json, sys, urllib.request",
        "",
        "proxy_url, retry_after = sys.argv[1], sys.argv[2]",
        f"report_url = {report_url!r}",
        "try:",
        "    req = urllib.request.Request(",
        "        report_url,",
        "        data=json.dumps({'proxy': proxy_url, 'retry_after_seconds': int(retry_after)}).encode(),",
        "        headers={'Content-Type': 'application/json'},",
        "    )",
        f"    urllib.request.urlopen(req, timeout={timeout})",
        "except Exception:",
        "    pass",
        "PROXYREPORT_EOF",
        '    echo "Rate limited via leased proxy — reported to provider (retry after ${RETRY_AFTER}s)" >> "$td/agent-status.md"',
        "  fi",
        "fi",
        "",
    ]


def _proxy_startup_cooldown_script_lines(provider_url: str, timeout: str) -> list[str]:
    """Best-effort cooldown for a proxy that never produced model output."""
    base = urlsplit(provider_url)
    report_url = f"{base.scheme}://{base.netloc}/proxy/report"
    return [
        'if [ -n "${OPENCODE_PROXY_URL:-}" ]; then',
        '  python3 - "$OPENCODE_PROXY_URL" <<\'PROXYSTARTUPREPORT_EOF\'',
        "import json, sys, urllib.request",
        "proxy_url = sys.argv[1]",
        f"report_url = {report_url!r}",
        "try:",
        "    req = urllib.request.Request(",
        "        report_url,",
        "        data=json.dumps({'proxy': proxy_url, 'retry_after_seconds': 300}).encode(),",
        "        headers={'Content-Type': 'application/json'},",
        "    )",
        f"    urllib.request.urlopen(req, timeout={timeout})",
        "except Exception:",
        "    pass",
        "PROXYSTARTUPREPORT_EOF",
        "fi",
    ]


def _opencode_startup_watchdog_script_lines(
    opencode_flags: str,
    startup_timeout_seconds: int,
    kill_grace_seconds: int,
    runtime_timeout_seconds: int,
) -> list[str]:
    prompt = (
        "Read the plan at $td/current-plan.md and execute it fully. "
        "Save the implementation diff to $td/implementation-diff.patch. "
        "Update $td/agent-status.md as you complete each step. "
        "Do not commit, do not push, do not create branches."
    )
    return [
        f"OPENCODE_STARTUP_RESPONSE_TIMEOUT_SECONDS={startup_timeout_seconds}",
        f"OPENCODE_STARTUP_KILL_GRACE_SECONDS={kill_grace_seconds}",
        f"OPENCODE_RUNTIME_TIMEOUT_SECONDS={runtime_timeout_seconds}",
        "OPENCODE_STARTUP_STALLED=0",
        "_kill_opencode_process() {",
        '  if [ "$OPENCODE_PROCESS_GROUP" -eq 1 ]; then kill "$1" "-$OPENCODE_PID" 2>/dev/null || true; else kill "$1" "$OPENCODE_PID" 2>/dev/null || true; fi',
        "}",
        "run_opencode_attempt() {",
        '  : > "$td/opencode-output.log"',
        "  OPENCODE_STARTUP_STALLED=0",
        "  FAILURE_REASON=",
        '  if command -v setsid >/dev/null 2>&1; then',
        f'    setsid "$OPCODE_BIN" run {opencode_flags} < /dev/null "{prompt}" > "$td/opencode-output.log" 2>&1 &',
        "    OPENCODE_PID=$!",
        "    OPENCODE_PROCESS_GROUP=1",
        "  else",
        f'    "$OPCODE_BIN" run {opencode_flags} < /dev/null "{prompt}" > "$td/opencode-output.log" 2>&1 &',
        "    OPENCODE_PID=$!",
        "    OPENCODE_PROCESS_GROUP=0",
        "  fi",
        "  OPENCODE_STARTUP_STARTED=$(date +%s)",
        '  while kill -0 "$OPENCODE_PID" 2>/dev/null; do',
        '    if python3 - "$td/opencode-output.log" <<\'OPENCODEPROGRESS_EOF\'',
        "import re, sys",
        "text = open(sys.argv[1], encoding='utf-8', errors='replace').read()",
        "text = re.sub(r'\\x1b\\[[0-?]*[ -/]*[@-~]', '', text)",
        "for raw in text.splitlines():",
        "    line = raw.strip()",
        "    if not line:",
        "        continue",
        "    if re.match(r'^>\\s*build\\s*[·:-]', line, re.I):",
        "        continue",
        "    if 'certificate has expired' in line.lower():",
        "        continue",
        "    raise SystemExit(0)",
        "raise SystemExit(1)",
        "OPENCODEPROGRESS_EOF",
        "    then",
        '      OPENCODE_RUNTIME_STARTED=$(date +%s)',
        '      while kill -0 "$OPENCODE_PID" 2>/dev/null; do',
        "        OPENCODE_RUNTIME_NOW=$(date +%s)",
        '        if [ $((OPENCODE_RUNTIME_NOW - OPENCODE_RUNTIME_STARTED)) -ge "$OPENCODE_RUNTIME_TIMEOUT_SECONDS" ]; then',
        '          _kill_opencode_process -TERM',
        "          OPENCODE_RUNTIME_KILL_STARTED=$(date +%s)",
        '          while kill -0 "$OPENCODE_PID" 2>/dev/null; do',
        "            OPENCODE_RUNTIME_KILL_NOW=$(date +%s)",
        '            if [ $((OPENCODE_RUNTIME_KILL_NOW - OPENCODE_RUNTIME_KILL_STARTED)) -ge "$OPENCODE_STARTUP_KILL_GRACE_SECONDS" ]; then',
        '              _kill_opencode_process -KILL',
        "              break",
        "            fi",
        "            sleep 1",
        "          done",
        '          wait "$OPENCODE_PID" 2>/dev/null || true',
        "          RC=79",
        '          FAILURE_REASON="opencode-run-timeout"',
        '          cat "$td/opencode-output.log"',
        "          return",
        "        fi",
        "        sleep 1",
        "      done",
        '      wait "$OPENCODE_PID"; RC=$?',
        '      cat "$td/opencode-output.log"',
        "      return",
        "    fi",
        "    OPENCODE_STARTUP_NOW=$(date +%s)",
        '    if [ $((OPENCODE_STARTUP_NOW - OPENCODE_STARTUP_STARTED)) -ge "$OPENCODE_STARTUP_RESPONSE_TIMEOUT_SECONDS" ]; then',
        "      OPENCODE_STARTUP_STALLED=1",
        '      _kill_opencode_process -TERM',
        "      OPENCODE_KILL_STARTED=$(date +%s)",
        '      while kill -0 "$OPENCODE_PID" 2>/dev/null; do',
        "        OPENCODE_KILL_NOW=$(date +%s)",
        '        if [ $((OPENCODE_KILL_NOW - OPENCODE_KILL_STARTED)) -ge "$OPENCODE_STARTUP_KILL_GRACE_SECONDS" ]; then',
        '          _kill_opencode_process -KILL',
        "          break",
        "        fi",
        "        sleep 1",
        "      done",
        '      wait "$OPENCODE_PID" 2>/dev/null || true',
        "      RC=78",
        '      FAILURE_REASON="opencode-startup-timeout"',
        '      cat "$td/opencode-output.log"',
        "      return",
        "    fi",
        "    sleep 1",
        "  done",
        '  wait "$OPENCODE_PID"; RC=$?',
        '  if [ "$RC" -ne 0 ] && python3 - "$td/opencode-output.log" <<\'OPENCODEPROXYFAIL_EOF\'',
        "import re, sys",
        "text = open(sys.argv[1], encoding='utf-8', errors='replace').read()",
        "text = re.sub(r'\\x1b\\[[0-?]*[ -/]*[@-~]', '', text)",
        "saw_expired_certificate = False",
        "for raw in text.splitlines():",
        "    line = raw.strip()",
        "    if not line or re.match(r'^>\\s*build\\s*[·:-]', line, re.I):",
        "        continue",
        "    if 'certificate has expired' in line.lower():",
        "        saw_expired_certificate = True",
        "        continue",
        "    raise SystemExit(1)",
        "raise SystemExit(0 if saw_expired_certificate else 1)",
        "OPENCODEPROXYFAIL_EOF",
        "  then",
        "    OPENCODE_STARTUP_STALLED=1",
        '    FAILURE_REASON="opencode-proxy-certificate-expired"',
        "  fi",
        '  cat "$td/opencode-output.log"',
        "}",
    ]


def _parent_prerun_snapshot_script_lines(project_root: str) -> list[str]:
    """Capture parent state without writing to the source repository.

    Both the synthetic index and any blobs/trees created while hashing the
    working tree are redirected into a random temporary object database.
    Source ``.git/index`` is only hashed and the source object database is
    mounted as an alternate read-only input.  This keeps the supervisor guard
    itself from mutating the checkout it is supposed to protect.
    """
    return [
        f"PARENT_ROOT={_shell_escape(project_root)}",
        'PARENT_ROOT_REAL=$(cd "$PARENT_ROOT" 2>/dev/null && pwd -P) || { echo "Parent root canonicalization FAILED" >> "$td/agent-status.md"; exit 75; }',
        'PARENT_HEAD_BEFORE=$(git -C "$PARENT_ROOT" rev-parse HEAD 2>/dev/null) || { echo "Parent HEAD snapshot FAILED" >> "$td/agent-status.md"; exit 75; }',
        'PARENT_INDEX_PATH=$(git -C "$PARENT_ROOT" rev-parse --path-format=absolute --git-path index 2>/dev/null) || { echo "Parent index path snapshot FAILED" >> "$td/agent-status.md"; exit 75; }',
        'PARENT_SOURCE_OBJECTS=$(git -C "$PARENT_ROOT" rev-parse --path-format=absolute --git-path objects 2>/dev/null) || { echo "Parent object path snapshot FAILED" >> "$td/agent-status.md"; exit 75; }',
        'PARENT_INDEX_TREE_BEFORE=$(sha256sum "$PARENT_INDEX_PATH" 2>/dev/null | awk \'{print $1}\')',
        'if [ -z "$PARENT_INDEX_TREE_BEFORE" ] || [ ! -d "$PARENT_SOURCE_OBJECTS" ]; then echo "Parent index/object snapshot FAILED" >> "$td/agent-status.md"; exit 75; fi',
        'printf "%s\\n" "$PARENT_HEAD_BEFORE" > "$td/parent-head-before.txt"',
        'printf "%s\\n" "$PARENT_INDEX_TREE_BEFORE" > "$td/parent-index-tree-before.txt"',
        'PARENT_TMP_BEFORE=$(mktemp -d /tmp/mcp-parent-before.XXXXXX) || { echo "Parent snapshot tempdir FAILED" >> "$td/agent-status.md"; exit 75; }',
        'PARENT_INDEX="$PARENT_TMP_BEFORE/index"',
        'PARENT_OBJECTS="$PARENT_TMP_BEFORE/objects"',
        'mkdir -p "$PARENT_OBJECTS"',
        'if GIT_INDEX_FILE="$PARENT_INDEX" GIT_OBJECT_DIRECTORY="$PARENT_OBJECTS" GIT_ALTERNATE_OBJECT_DIRECTORIES="$PARENT_SOURCE_OBJECTS" git -C "$PARENT_ROOT" read-tree HEAD >/dev/null 2>&1 && \\',
        '   GIT_INDEX_FILE="$PARENT_INDEX" GIT_OBJECT_DIRECTORY="$PARENT_OBJECTS" GIT_ALTERNATE_OBJECT_DIRECTORIES="$PARENT_SOURCE_OBJECTS" git -C "$PARENT_ROOT" add -A -- . >/dev/null 2>&1; then',
        '  PARENT_TREE_BEFORE=$(GIT_INDEX_FILE="$PARENT_INDEX" GIT_OBJECT_DIRECTORY="$PARENT_OBJECTS" GIT_ALTERNATE_OBJECT_DIRECTORIES="$PARENT_SOURCE_OBJECTS" git -C "$PARENT_ROOT" write-tree 2>/dev/null)',
        '  [ -n "$PARENT_TREE_BEFORE" ] || { echo "Parent snapshot capture FAILED" >> "$td/agent-status.md"; rm -rf "$PARENT_TMP_BEFORE"; exit 75; }',
        '  printf "%s\\n" "$PARENT_TREE_BEFORE" > "$td/parent-tree-before.txt"',
        "else",
        '  echo "Parent snapshot capture FAILED" >> "$td/agent-status.md"',
        '  rm -rf "$PARENT_TMP_BEFORE"',
        "  exit 75",
        "fi",
        'rm -rf "$PARENT_TMP_BEFORE"',
    ]


def _isolated_worktree_error(project_root: str | None, worktree_path: str | None) -> str | None:
    """Reject missing workspaces and any workspace located inside source.

    A nested ``<source>/.ai-bridge/worktrees/...`` directory is not physical
    isolation: the worker still writes inside the authoritative checkout and a
    Git worktree created there also mutates the source repository's common Git
    metadata.  Production workspaces therefore have to live outside the whole
    source tree, not merely differ from its root path.
    """
    if not project_root:
        return None
    if not worktree_path or not worktree_path.strip():
        return "isolated worktree_path is required for agent execution"
    raw = worktree_path.strip()
    resolved = raw if os.path.isabs(raw) else os.path.join(project_root, raw)
    parent = os.path.realpath(project_root)
    candidate = os.path.realpath(resolved)
    try:
        inside_parent = os.path.commonpath([parent, candidate]) == parent
    except ValueError:
        inside_parent = False
    if inside_parent:
        return "agent workspace must be outside the authoritative source checkout"
    return None


def _supervisor_postrun_script_lines(
    allowed_files: list[str],
    forbidden_files: list[str],
    required_checks: list[str],
    parent_root: str | None = None,
) -> list[str]:
    """Build fail-closed post-run evidence, scope and check enforcement.

    The contract values are captured by the MCP process before OpenCode starts
    and embedded into the runner script.  The worker therefore cannot relax
    its own allowed/forbidden scope by rewriting task.json while it runs.

    Evidence is generated with a temporary Git index rooted at BASE_HEAD. This
    includes tracked edits, deletions, staged changes and previously-untracked
    files without mutating the worktree's real index.  BASE_HEAD is captured
    before the worker starts, so an illicit worker commit cannot make its
    changes disappear from the supervisor diff.
    """
    allowed_json = json.dumps(allowed_files, separators=(",", ":"))
    forbidden_json = json.dumps(forbidden_files, separators=(",", ":"))
    parent_post_lines: list[str] = []
    if parent_root:
        parent_post_lines = [
            f"PARENT_ROOT={_shell_escape(parent_root)}",
            'PARENT_HEAD_AFTER=$(git -C "$PARENT_ROOT" rev-parse HEAD 2>/dev/null || true)',
            'PARENT_INDEX_PATH_AFTER=$(git -C "$PARENT_ROOT" rev-parse --path-format=absolute --git-path index 2>/dev/null || true)',
            'PARENT_SOURCE_OBJECTS_AFTER=$(git -C "$PARENT_ROOT" rev-parse --path-format=absolute --git-path objects 2>/dev/null || true)',
            'PARENT_INDEX_TREE_AFTER=$(sha256sum "$PARENT_INDEX_PATH_AFTER" 2>/dev/null | awk \'{print $1}\')',
            'printf "%s\\n" "$PARENT_HEAD_AFTER" > "$td/parent-head-after.txt"',
            'printf "%s\\n" "$PARENT_INDEX_TREE_AFTER" > "$td/parent-index-tree-after.txt"',
            'if [ -z "$PARENT_HEAD_AFTER" ] || [ "$PARENT_HEAD_AFTER" != "$PARENT_HEAD_BEFORE" ] || [ -z "$PARENT_INDEX_TREE_AFTER" ] || [ "$PARENT_INDEX_TREE_AFTER" != "$PARENT_INDEX_TREE_BEFORE" ] || [ "$PARENT_INDEX_PATH_AFTER" != "$PARENT_INDEX_PATH" ] || [ "$PARENT_SOURCE_OBJECTS_AFTER" != "$PARENT_SOURCE_OBJECTS" ]; then',
            "  PARENT_RC=1",
            "fi",
            'PARENT_TMP_AFTER=$(mktemp -d /tmp/mcp-parent-after.XXXXXX || true)',
            'PARENT_INDEX="$PARENT_TMP_AFTER/index"',
            'PARENT_OBJECTS="$PARENT_TMP_AFTER/objects"',
            'if [ -n "$PARENT_TMP_AFTER" ]; then mkdir -p "$PARENT_OBJECTS"; fi',
            'if [ -n "$PARENT_TMP_AFTER" ] && GIT_INDEX_FILE="$PARENT_INDEX" GIT_OBJECT_DIRECTORY="$PARENT_OBJECTS" GIT_ALTERNATE_OBJECT_DIRECTORIES="$PARENT_SOURCE_OBJECTS_AFTER" git -C "$PARENT_ROOT" read-tree HEAD >/dev/null 2>&1 && \\',
            '   GIT_INDEX_FILE="$PARENT_INDEX" GIT_OBJECT_DIRECTORY="$PARENT_OBJECTS" GIT_ALTERNATE_OBJECT_DIRECTORIES="$PARENT_SOURCE_OBJECTS_AFTER" git -C "$PARENT_ROOT" add -A -- . >/dev/null 2>&1; then',
            '  PARENT_TREE_AFTER=$(GIT_INDEX_FILE="$PARENT_INDEX" GIT_OBJECT_DIRECTORY="$PARENT_OBJECTS" GIT_ALTERNATE_OBJECT_DIRECTORIES="$PARENT_SOURCE_OBJECTS_AFTER" git -C "$PARENT_ROOT" write-tree 2>/dev/null)',
            "else",
            "  PARENT_TREE_AFTER=",
            "  PARENT_RC=1",
            "fi",
            'if [ -n "$PARENT_TMP_AFTER" ]; then rm -rf "$PARENT_TMP_AFTER"; fi',
            'if [ -z "$PARENT_TREE_AFTER" ] || [ "$PARENT_TREE_AFTER" != "$PARENT_TREE_BEFORE" ]; then',
            "  PARENT_RC=1",
            '  echo "Supervisor parent-checkout guard FAILED" >> "$td/agent-status.md"',
            "else",
            '  echo "Supervisor parent-checkout guard passed" >> "$td/agent-status.md"',
            "fi",
            'printf "%s\\n" "$PARENT_TREE_AFTER" > "$td/parent-tree-after.txt"',
        ]

    lines = [
        "",
        "# Supervisor-owned evidence collection and contract enforcement",
        "PARENT_RC=0",
        "EVIDENCE_RC=0",
        "SCOPE_RC=0",
        "CHECKS_RC=0",
        "SCOPE_RAN=0",
        "CHECKS_RAN=0",
        'SUPERVISOR_INDEX="$td/.supervisor-index"',
        'rm -f "$SUPERVISOR_INDEX" "$td/changed-files.z" "$td/scope-violations.json"',
        'POST_HEAD=$(git rev-parse HEAD 2>/dev/null || true)',
        'if GIT_INDEX_FILE="$SUPERVISOR_INDEX" git read-tree "$BASE_HEAD" >/dev/null 2>&1 && \\',
        '   GIT_INDEX_FILE="$SUPERVISOR_INDEX" git add -A -- . >/dev/null 2>&1 && \\',
        '   GIT_INDEX_FILE="$SUPERVISOR_INDEX" git diff --cached --binary --no-color "$BASE_HEAD" -- > "$td/implementation-diff.patch" 2>/dev/null && \\',
        '   GIT_INDEX_FILE="$SUPERVISOR_INDEX" git diff --cached --no-renames --name-only -z "$BASE_HEAD" -- > "$td/changed-files.z" 2>/dev/null; then',
        '  echo "Supervisor evidence collected" >> "$td/agent-status.md"',
        "else",
        "  EVIDENCE_RC=1",
        '  : > "$td/implementation-diff.patch"',
        '  echo "Supervisor evidence collection FAILED" >> "$td/agent-status.md"',
        "fi",
        'rm -f "$SUPERVISOR_INDEX"',
        'if [ "$EVIDENCE_RC" -eq 0 ]; then',
        "  SCOPE_RAN=1",
        f"  python3 - \"$td/changed-files.z\" {_shell_escape(allowed_json)} {_shell_escape(forbidden_json)} \"$BASE_HEAD\" \"$POST_HEAD\" \"$td/scope-violations.json\" <<'SCOPE_EOF'",
        "import json, re, sys",
        "from pathlib import PurePosixPath",
        "",
        "changed_path, allowed_raw, forbidden_raw, base_head, post_head, report_path = sys.argv[1:]",
        "allowed = json.loads(allowed_raw)",
        "forbidden = json.loads(forbidden_raw)",
        "raw = open(changed_path, 'rb').read()",
        "changed = [p.decode('utf-8', 'surrogateescape') for p in raw.split(b'\\0') if p]",
        "",
        "def compile_glob(pattern):",
        "    pattern = pattern.replace('\\\\', '/').strip()",
        "    while pattern.startswith('./'):",
        "        pattern = pattern[2:]",
        "    parts = PurePosixPath(pattern).parts if pattern else ()",
        "    if not pattern or pattern.startswith('/') or '..' in parts:",
        "        raise ValueError(f'invalid scope pattern: {pattern!r}')",
        "    out = ['^']",
        "    i = 0",
        "    while i < len(pattern):",
        "        if pattern[i:i+3] == '**/':",
        "            out.append('(?:.*/)?')",
        "            i += 3",
        "        elif pattern[i:i+2] == '**':",
        "            out.append('.*')",
        "            i += 2",
        "        elif pattern[i] == '*':",
        "            out.append('[^/]*')",
        "            i += 1",
        "        elif pattern[i] == '?':",
        "            out.append('[^/]')",
        "            i += 1",
        "        else:",
        "            out.append(re.escape(pattern[i]))",
        "            i += 1",
        "    out.append('$')",
        "    return re.compile(''.join(out))",
        "",
        "violations = []",
        "try:",
        "    allowed_rx = [(p, compile_glob(p)) for p in allowed]",
        "    forbidden_rx = [(p, compile_glob(p)) for p in forbidden]",
        "except (TypeError, ValueError) as exc:",
        "    violations.append({'type': 'invalid-contract', 'detail': str(exc)})",
        "    allowed_rx = []",
        "    forbidden_rx = []",
        "",
        "for path in changed:",
        "    if not any(rx.fullmatch(path) for _, rx in allowed_rx):",
        "        violations.append({'type': 'outside-allowed-files', 'path': path})",
        "    for pattern, rx in forbidden_rx:",
        "        if rx.fullmatch(path):",
        "            violations.append({'type': 'forbidden-file', 'path': path, 'pattern': pattern})",
        "            break",
        "if not post_head or post_head != base_head:",
        "    violations.append({'type': 'head-changed', 'base_head': base_head, 'post_head': post_head})",
        "",
        "report = {",
        "    'base_head': base_head,",
        "    'post_head': post_head,",
        "    'changed_files': changed,",
        "    'allowed_files': allowed,",
        "    'forbidden_files': forbidden,",
        "    'violations': violations,",
        "}",
        "with open(report_path, 'w', encoding='utf-8') as fh:",
        "    json.dump(report, fh, ensure_ascii=False, indent=2)",
        "    fh.write('\\n')",
        "if violations:",
        "    for item in violations:",
        "        print('scope violation:', item, file=sys.stderr)",
        "    sys.exit(3)",
        "SCOPE_EOF",
        "  SCOPE_RC=$?",
        '  if [ "$SCOPE_RC" -eq 0 ]; then',
        '    echo "Supervisor scope check passed" >> "$td/agent-status.md"',
        "  else",
        '    echo "Supervisor scope check FAILED" >> "$td/agent-status.md"',
        "  fi",
        "else",
        '  echo "Supervisor scope check skipped: evidence unavailable" >> "$td/agent-status.md"',
        "fi",
        *parent_post_lines,
        'if [ "$RC" -eq 0 ] && [ "$PARENT_RC" -eq 0 ] && [ "$EVIDENCE_RC" -eq 0 ] && [ "$SCOPE_RC" -eq 0 ]; then',
        "  CHECKS_RAN=1",
        '  : > "$td/required-checks.log"',
        '  CHECK_ROOT=$(mktemp -d /tmp/mcp-required-checks.XXXXXX) || { echo "Supervisor required-check workspace create FAILED" >> "$td/agent-status.md"; CHECKS_RC=1; }',
        '  if [ "$CHECKS_RC" -eq 0 ]; then',
        '    echo "Supervisor required-check workspace: $CHECK_ROOT" >> "$td/required-checks.log"',
        '    if git clone --no-hardlinks --no-checkout "$PWD" "$CHECK_ROOT" >> "$td/required-checks.log" 2>&1; then',
        '      if git -C "$CHECK_ROOT" checkout --detach "$BASE_HEAD" >> "$td/required-checks.log" 2>&1; then',
        '        if [ -s "$td/implementation-diff.patch" ]; then',
        '          if git -C "$CHECK_ROOT" apply --binary "$td/implementation-diff.patch" >> "$td/required-checks.log" 2>&1; then',
        '            echo "Supervisor required-check workspace ready (BASE_HEAD + implementation diff)" >> "$td/required-checks.log"',
        "          else",
        "            CHECKS_RC=1",
        '            echo "Supervisor required-check patch apply FAILED" >> "$td/agent-status.md"',
        "          fi",
        "        else",
        '          echo "Supervisor required-check workspace ready (BASE_HEAD, no changes)" >> "$td/required-checks.log"',
        "        fi",
        "      else",
        "        CHECKS_RC=1",
        '        echo "Supervisor required-check clone checkout FAILED" >> "$td/agent-status.md"',
        "      fi",
        "    else",
        "      CHECKS_RC=1",
        '      echo "Supervisor required-check clone FAILED" >> "$td/agent-status.md"',
        "    fi",
        "  fi",
        '  if [ "$CHECKS_RC" -eq 0 ] && [ -f "$CHECK_ROOT/uv.lock" ] && [ -f "$CHECK_ROOT/pyproject.toml" ]; then',
        "    DEV_EXTRA=$(cd \"$CHECK_ROOT\" && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV python3 -c 'import tomllib; data=tomllib.load(open(\"pyproject.toml\", \"rb\")); print(\"1\" if \"dev\" in data.get(\"project\", {}).get(\"optional-dependencies\", {}) else \"0\")' 2>>\"$td/required-checks.log\")",
        '    DEV_EXTRA_RC=$?',
        '    if [ "$DEV_EXTRA_RC" -ne 0 ]; then',
        "      CHECKS_RC=1",
        '      echo "Supervisor required-check dev-extra detection FAILED" >> "$td/agent-status.md"',
        '    elif [ "$DEV_EXTRA" = "1" ]; then',
        '      echo "Supervisor required-check: bootstrapping declared dev extra" >> "$td/required-checks.log"',
        '      if ( cd "$CHECK_ROOT" && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV uv sync --frozen --extra dev ) >> "$td/required-checks.log" 2>&1; then',
        '        echo "Supervisor required-check: dev extra bootstrap succeeded" >> "$td/required-checks.log"',
        "      else",
        "        CHECKS_RC=1",
        '        echo "Supervisor required-check dev extra bootstrap FAILED" >> "$td/agent-status.md"',
        "      fi",
        "    fi",
        "  fi",
    ]

    for check in required_checks:
        lines.extend(
            [
                '  if [ "$CHECKS_RC" -eq 0 ]; then',
                f'    echo {_shell_escape("$ " + check)} >> "$td/required-checks.log"',
                f'    if ( cd "$CHECK_ROOT" && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV sh -c {_shell_escape(check)} ) >> "$td/required-checks.log" 2>&1; then',
                '      echo "PASS" >> "$td/required-checks.log"',
                "    else",
                "      CHECKS_RC=$?",
                '      echo "FAIL exit=$CHECKS_RC" >> "$td/required-checks.log"',
                "    fi",
                "  fi",
            ]
        )

    lines.extend(
        [
            '  if [ -n "${CHECK_ROOT:-}" ]; then rm -rf "$CHECK_ROOT"; fi',
            "fi",
            'if [ "$CHECKS_RAN" -eq 0 ]; then',
            '  echo "Supervisor required checks skipped" >> "$td/agent-status.md"',
            'elif [ "$CHECKS_RC" -eq 0 ]; then',
            '  echo "Supervisor required checks passed" >> "$td/agent-status.md"',
            "else",
            '  echo "Supervisor required checks FAILED" >> "$td/agent-status.md"',
            "fi",
            # Required checks are supervisor-triggered shell commands and can
            # still reach the parent checkout via absolute paths. Re-run the
            # authoritative parent snapshot after every required check so a
            # persistent parent mutation cannot happen after the first guard
            # and escape detection. PARENT_RC is intentionally not reset: an
            # earlier worker-side contamination remains sticky.
            *parent_post_lines,
            "FINAL_RC=$RC",
            'if [ "$EVIDENCE_RC" -ne 0 ]; then FINAL_RC=70; fi',
            'if [ "$SCOPE_RC" -ne 0 ]; then FINAL_RC=71; fi',
            'if [ "$CHECKS_RC" -gt 0 ] && [ "$CHECKS_RC" -ne 127 ]; then FINAL_RC=72; fi',
            'if [ "$PARENT_RC" -ne 0 ]; then FINAL_RC=74; fi',
            'if [ "$CHECKS_RC" -eq 127 ]; then CHECKS_WARNING=1; else CHECKS_WARNING=0; fi',
            'python3 - "$td/supervisor-verdict.json" "${BASE_HEAD:-}" "${POST_HEAD:-}" "$EVIDENCE_RC" "$SCOPE_RC" "$CHECKS_RC" "$PARENT_RC" "$FINAL_RC" "${SCOPE_RAN:-0}" "${CHECKS_RAN:-0}" <<\'VERDICT_EOF\'',
            "import json, os, sys, tempfile",
            "",
            "path, base_head, post_head, evidence_rc, scope_rc, checks_rc, parent_rc, final_rc, scope_ran, checks_ran = sys.argv[1:]",
            "payload = {",
            "    'version': 1,",
            "    'base_head': base_head,",
            "    'post_head': post_head,",
            "    'evidence_rc': int(evidence_rc),",
            "    'scope_rc': int(scope_rc),",
            "    'checks_rc': int(checks_rc),",
            "    'parent_rc': int(parent_rc),",
            "    'final_rc': int(final_rc),",
            "    'scope_ran': int(scope_ran),",
            "    'checks_ran': int(checks_ran),",
            "}",
            "directory = os.path.dirname(path) or '.'",
            "fd, tmp = tempfile.mkstemp(prefix='.supervisor-verdict.', dir=directory)",
            "try:",
            "    with os.fdopen(fd, 'w', encoding='utf-8') as fh:",
            "        json.dump(payload, fh, sort_keys=True, separators=(',', ':'))",
            "        fh.flush()",
            "        os.fsync(fh.fileno())",
            "    os.replace(tmp, path)",
            "finally:",
            "    if os.path.exists(tmp): os.unlink(tmp)",
            "VERDICT_EOF",
            'if [ "$?" -ne 0 ]; then FINAL_RC=75; fi',
        ]
    )
    return lines


_WORKER_SECURE_COPY_PY = """\
import hashlib
import os
import stat
import sys
import tempfile

src, expected = sys.argv[1], sys.argv[2].strip().lower()


def die(message):
    print(message)
    sys.exit(3)


if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
    die("managed source digest metadata invalid")

flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
try:
    fd = os.open(src, flags)
except OSError:
    die("immutable managed source bundle is unavailable")

digest = hashlib.sha256()
try:
    out_dir = tempfile.mkdtemp(prefix="managed-source-")
    os.chmod(out_dir, 0o700)
    dst = os.path.join(out_dir, "source.bundle")
    with os.fdopen(fd, "rb", closefd=True) as fin, open(dst, "wb") as fout:
        if not stat.S_ISREG(os.fstat(fin.fileno()).st_mode):
            die("managed source bundle is not a regular file")
        while True:
            chunk = fin.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
except OSError as exc:
    die("managed source private copy failed: %s" % exc)

actual = digest.hexdigest()
if actual != expected:
    try:
        os.unlink(dst)
        os.rmdir(out_dir)
    except OSError:
        pass
    die("managed source digest mismatch")

os.chmod(dst, 0o400)
print(dst)
print(out_dir)
"""


def _managed_secure_copy_lines() -> list[str]:
    """Worker-side digest binding for the managed source bundle.

    The worker never trusts the published path: it makes one private copy
    (opened without following symlinks) while hashing the exact bytes
    written, and refuses to continue unless that hash equals the digest the
    supervisor embedded from trusted task metadata. All subsequent git
    operations consume only ``$MANAGED_SOURCE_COPY``.
    """
    return [
        "MANAGED_COPY_PLAN=$(python3 - \"$MANAGED_SOURCE_BUNDLE\" \"$MANAGED_SOURCE_EXPECTED_SHA256\" <<'MANAGED_COPY_PY'",
        _WORKER_SECURE_COPY_PY.rstrip("\n"),
        "MANAGED_COPY_PY",
        ") || { echo \"Managed source binding failed: $MANAGED_COPY_PLAN\" >> \"$td/agent-status.md\"; exit 73; }",
        'MANAGED_SOURCE_COPY=$(printf "%s\\n" "$MANAGED_COPY_PLAN" | sed -n "1p")',
        'MANAGED_PRIVATE_DIR=$(printf "%s\\n" "$MANAGED_COPY_PLAN" | sed -n "2p")',
        "trap 'rm -rf \"$MANAGED_PRIVATE_DIR\" 2>/dev/null' EXIT",
    ]


def _build_opencode_script(
    td: str,
    task_id: str,
    model: str | None,
    project_root: str | None = None,
    worktree_path: str | None = None,
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    required_checks: list[str] | None = None,
    managed_clone: bool = False,
    base_ref: str | None = None,
    managed_source_path: str | None = None,
    managed_source_sha256: str | None = None,
) -> str:
    opencode_flags = "--dangerously-skip-permissions"
    if model:
        opencode_flags += f" --model {_shell_escape(model)}"

    proxy_provider_url = os.environ.get("OPENCODE_PROXY_PROVIDER_URL", "").strip()
    proxy_timeout = os.environ.get("OPENCODE_PROXY_PROVIDER_TIMEOUT", "5").strip() or "5"
    proxy_required = os.environ.get("OPENCODE_PROXY_REQUIRED", "true").strip().lower() not in {
        "0", "false", "no", "off"
    }
    startup_reserve_bytes = int(
        os.environ.get("OPENCODE_STARTUP_RESERVE_BYTES", str(768 * 1024 * 1024))
    )
    startup_reserve_seconds = int(os.environ.get("OPENCODE_STARTUP_RESERVE_SECONDS", "60"))
    admission_wait_seconds = int(os.environ.get("OPENCODE_ADMISSION_WAIT_SECONDS", "300"))
    admission_poll_seconds = int(os.environ.get("OPENCODE_ADMISSION_POLL_SECONDS", "2"))
    startup_response_timeout_seconds = int(os.environ.get("OPENCODE_STARTUP_RESPONSE_TIMEOUT_SECONDS", "60"))
    startup_kill_grace_seconds = int(os.environ.get("OPENCODE_STARTUP_KILL_GRACE_SECONDS", "5"))
    startup_max_proxy_attempts = int(os.environ.get("OPENCODE_STARTUP_MAX_PROXY_ATTEMPTS", "4"))
    runtime_timeout_seconds = int(os.environ.get("OPENCODE_RUN_TIMEOUT_SECONDS", "1800"))
    if (
        startup_reserve_bytes < 0
        or startup_reserve_seconds < 0
        or admission_wait_seconds < 0
        or admission_poll_seconds <= 0
        or startup_response_timeout_seconds <= 0
        or startup_kill_grace_seconds < 0
        or startup_max_proxy_attempts <= 0
        or runtime_timeout_seconds <= 0
    ):
        raise ValueError("OpenCode admission timing/reserve values are invalid")
    allowed_files = list(allowed_files or [])
    forbidden_files = list(forbidden_files or [])
    required_checks = list(required_checks or [])
    if managed_clone and not worktree_path:
        raise ValueError("managed_clone requires worktree_path")
    if managed_clone and not os.path.isabs(td):
        raise ValueError("managed_clone requires an absolute task state path")
    if managed_clone and not base_ref:
        raise ValueError("managed_clone requires an exact base_ref")
    if managed_clone and not managed_source_path:
        raise ValueError("managed_clone requires an immutable managed source")

    # Digest binding is MANDATORY in managed mode: the worker may only
    # clone bytes whose SHA-256 was computed by the trusted control plane
    # over the snapshot it fully proved, and embedded here from task
    # metadata. Missing or malformed metadata fails closed before any
    # script is emitted or worker launched -- there is no recapture
    # fallback.
    expected_digest = ""
    if managed_clone:
        if (
            not isinstance(managed_source_sha256, str)
            or not managed_source_sha256.strip()
        ):
            raise ManagedSourceDigestError(
                "managed OpenCode execution requires publisher digest metadata"
            )
        expected_digest = validate_bundle_digest(managed_source_sha256.strip())

    # Managed workers trust the immutable bundle, never the authoritative
    # checkout. Drop the parent path even if a caller supplied it.
    if managed_clone:
        project_root = None

    parts = []
    if project_root:
        # `td` (below) is relative to the project root -- execute_argv's
        # sync dispatch path sets cwd server-side, so this was previously
        # a no-op there, but execute_raw's async dispatch path (used by
        # run_script_async / async_submit=True) has no cwd concept at all:
        # the script would otherwise run from the SSH session's own
        # default directory (its home dir, not the project), and every
        # relative $td reference below would silently resolve to the
        # wrong place -- confirmed live via a real MCP run_agent call,
        # which failed with "current-plan.md not found" despite the file
        # genuinely existing at the right path. Make the script
        # self-sufficient regardless of which dispatch path invoked it.
        parts.append(f"cd {_shell_escape(project_root)} || exit 1")
        # Absolute task dir: a git worktree below is a fresh checkout from
        # HEAD and never contains .ai-bridge (it is gitignored), so a
        # relative $td would resolve inside the worktree to nothing. The
        # plan/status/diff must stay in the main checkout's task dir.
        parts.append(f"td={_shell_escape(os.path.join(project_root, td))}")
    else:
        parts.append(f"td='{td}'")
    parts.extend([
        'mkdir -p "$td"',
        'echo "Status: running" > "$td/agent-status.md"',
        "OPCODE_BIN=$(command -v opencode 2>/dev/null || echo '/root/.opencode/bin/opencode')",
    ])
    if project_root and worktree_path and not managed_clone:
        parts.extend(_parent_prerun_snapshot_script_lines(project_root))
    if worktree_path:
        if base_ref:
            parts.append(f"TASK_BASE_REF={_shell_escape(base_ref)}")
            if managed_clone:
                parts.append('TASK_BASE_COMMIT="$TASK_BASE_REF"')
            else:
                parts.append(
                    'TASK_BASE_COMMIT=$(git rev-parse --verify "$TASK_BASE_REF^{commit}" 2>/dev/null) || { echo "Requested base_ref is unavailable locally" >> "$td/agent-status.md"; exit 73; }'
                )
        elif project_root:
            parts.append('TASK_BASE_COMMIT="$PARENT_HEAD_BEFORE"')
        else:
            parts.append('TASK_BASE_COMMIT=$(git rev-parse HEAD 2>/dev/null) || { echo "Workspace baseline resolution FAILED" >> "$td/agent-status.md"; exit 73; }')
        wt = worktree_path
        if project_root and not os.path.isabs(wt):
            wt = os.path.normpath(os.path.join(project_root, wt))
        managed_source_lines: list[str] = []
        if managed_clone:
            managed_source_lines = [
                f"MANAGED_SOURCE_BUNDLE={_shell_escape(managed_source_path or '')}",
                f"MANAGED_SOURCE_EXPECTED_SHA256={_shell_escape(expected_digest)}",
                *_managed_secure_copy_lines(),
                'MANAGED_SOURCE_HEADS=$(git bundle list-heads "$MANAGED_SOURCE_COPY" 2>/dev/null) || MANAGED_SOURCE_HEADS=""',
                'MANAGED_SOURCE_HEAD=$(printf "%s\\n" "$MANAGED_SOURCE_HEADS" | awk \'NF { print $1; exit }\')',
                'MANAGED_SOURCE_EXTRA_HEAD=$(printf "%s\\n" "$MANAGED_SOURCE_HEADS" | awk \'NF { count++; if (count == 2) { print $1; exit } }\')',
                'if [ -z "$MANAGED_SOURCE_HEAD" ] || [ -n "$MANAGED_SOURCE_EXTRA_HEAD" ] || [ "$MANAGED_SOURCE_HEAD" != "$TASK_BASE_COMMIT" ]; then echo "Immutable managed source bundle does not match base_ref" >> "$td/agent-status.md"; exit 73; fi',
                'MANAGED_VERIFY_DIR=$(mktemp -d)',
                'git init -q --bare "$MANAGED_VERIFY_DIR/v.git" || { echo "managed bundle verification scratch failed" >> "$td/agent-status.md"; rm -rf "$MANAGED_VERIFY_DIR"; exit 73; }',
                'if ! git -C "$MANAGED_VERIFY_DIR/v.git" bundle verify "$MANAGED_SOURCE_COPY" >/dev/null 2>>"$td/agent-status.md"; then echo "Immutable managed source bundle failed integrity verification" >> "$td/agent-status.md"; rm -rf "$MANAGED_VERIFY_DIR"; exit 73; fi',
                'rm -rf "$MANAGED_VERIFY_DIR"',
            ]
            create_workspace_lines = [
                '  git clone --no-hardlinks --no-checkout "$MANAGED_SOURCE_COPY" "$wt" 2>>"$td/agent-status.md" || { echo "managed clone failed: $wt" >> "$td/agent-status.md"; exit 1; }',
                '  git -C "$wt" checkout --detach "$TASK_BASE_COMMIT" 2>>"$td/agent-status.md" || { echo "managed clone checkout failed: $wt" >> "$td/agent-status.md"; exit 1; }',
                '  git -C "$wt" remote remove origin 2>>"$td/agent-status.md" || { echo "managed clone source remote removal failed: $wt" >> "$td/agent-status.md"; exit 1; }',
            ]
        else:
            create_workspace_lines = [
                '  git worktree add --detach "$wt" "$TASK_BASE_COMMIT" 2>>"$td/agent-status.md" || { echo "git worktree add failed: $wt" >> "$td/agent-status.md"; exit 1; }'
            ]
        baseline_reuse_lines = [
            '  wt_head=$(git -C "$wt" rev-parse HEAD 2>/dev/null || true)',
            '  if [ -z "$wt_head" ] || [ "$wt_head" != "$TASK_BASE_COMMIT" ]; then',
            '    echo "Refusing workspace with baseline drift: $wt" >> "$td/agent-status.md"',
            "    exit 1",
            "  fi",
        ]
        parts.extend([
            f"wt={_shell_escape(wt)}",
            'wt_parent=$(dirname "$wt")',
            'mkdir -p "$wt_parent"',
            'wt_parent_real=$(cd "$wt_parent" 2>/dev/null && pwd -P) || { echo "Workspace parent canonicalization failed: $wt_parent" >> "$td/agent-status.md"; exit 1; }',
            *(['case "$wt_parent_real/" in "$PARENT_ROOT_REAL/"*) echo "Refusing workspace parent inside source checkout: $wt_parent_real" >> "$td/agent-status.md"; exit 1;; esac'] if project_root else []),
            *managed_source_lines,
            'if [ -L "$wt" ]; then echo "Refusing symlink workspace: $wt" >> "$td/agent-status.md"; exit 1; fi',
            'if [ -e "$wt" ]; then',
            '  wt_real=$(cd "$wt" 2>/dev/null && pwd -P || true)',
            *(['  case "$wt_real/" in "$PARENT_ROOT_REAL/"*) echo "Refusing workspace inside source checkout: $wt_real" >> "$td/agent-status.md"; exit 1;; esac'] if project_root else []),
            '  wt_top=$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null || true)',
            '  wt_top_real=$(cd "$wt_top" 2>/dev/null && pwd -P || true)',
            '  if [ -z "$wt_real" ] || [ -z "$wt_top_real" ] || [ "$wt_real" != "$wt_top_real" ]; then',
            '    echo "Refusing non-worktree-root path: $wt" >> "$td/agent-status.md"',
            "    exit 1",
            "  fi",
            '  if [ -n "$(git -C "$wt" status --porcelain=v1 --untracked-files=all)" ]; then',
            '    echo "Refusing dirty existing workspace: $wt" >> "$td/agent-status.md"',
            "    exit 1",
            "  fi",
            *baseline_reuse_lines,
            *([
                '  if [ -n "$(git -C "$wt" remote)" ]; then',
                '    echo "Refusing managed clone with source remote metadata: $wt" >> "$td/agent-status.md"',
                "    exit 1",
                "  fi",
            ] if managed_clone else []),
            '  echo "Workspace already exists, reusing clean root: $wt" >> "$td/agent-status.md"',
            "else",
            *create_workspace_lines,
            "fi",
            'cd "$wt" || exit 1',
            # Isolate opencode's storage per run: parallel fleet agents
            # share one HOME (~/.local/share/opencode/opencode.db) and two
            # concurrent processes race SQLite schema migration / WAL
            # locking ("Failed query: CREATE TABLE ..." -- seen live in the
            # E2E smoke). Point XDG data+cache at the task dir (gitignored,
            # OUTSIDE the worktree); config (~/.config/opencode) is
            # read-only and safe to share.
            # Storing under $wt is fatal: opencode's background snapshot
            # `git add --all --sparse` uses the worktree as --work-tree and
            # would then add .opencode-data -- including the snapshot repo's
            # own object store -- re-scanning its own growth forever; the
            # opencode process waits for the snapshot and never exits, so
            # the runner script hangs (job timeout 600s, seen live).
            f'export XDG_DATA_HOME="{td}/.opencode-data"',
            f'export XDG_CACHE_HOME="{td}/.opencode-cache"',
            'mkdir -p "$XDG_DATA_HOME" "$XDG_CACHE_HOME"',
        ])
    parts.extend([
        'BASE_HEAD=$(git rev-parse HEAD 2>/dev/null) || { echo "Supervisor baseline capture FAILED" >> "$td/agent-status.md"; exit 73; }',
        'printf "%s\\n" "$BASE_HEAD" > "$td/base-head.txt"',
    ])
    parts.extend([
        "PROXY_BLOCKED=0",
        "RESOURCE_EXHAUSTED=0",
        "FAILURE_REASON=",
        "OOM_KILL_BEFORE=$(awk '$1 == \"oom_kill\" {print $2}' /sys/fs/cgroup/memory.events 2>/dev/null || true)",
    ])
    parts.extend(
        _opencode_startup_watchdog_script_lines(
            opencode_flags,
            startup_response_timeout_seconds,
            startup_kill_grace_seconds,
            runtime_timeout_seconds,
        )
    )
    if proxy_provider_url:
        parts.append("acquire_opencode_proxy() {")
        parts.extend(
            _proxy_fetch_script_lines(
                proxy_provider_url,
                proxy_timeout,
                required=proxy_required,
                startup_reserve_bytes=startup_reserve_bytes,
                startup_reserve_seconds=startup_reserve_seconds,
                admission_wait_seconds=admission_wait_seconds,
                admission_poll_seconds=admission_poll_seconds,
            )
        )
        parts.append("}")
        parts.append("release_opencode_proxy() {")
        parts.extend(_proxy_local_release_script_lines())
        parts.append("}")
        parts.append("cooldown_opencode_proxy() {")
        parts.extend(_proxy_startup_cooldown_script_lines(proxy_provider_url, proxy_timeout))
        parts.append("}")
        parts.append("report_rate_limited_proxy() {")
        parts.extend(_proxy_report_script_lines(proxy_provider_url, proxy_timeout))
        parts.append("}")
    elif proxy_required:
        parts.extend(
            [
                "PROXY_BLOCKED=1",
                'printf "Proxy provider is not configured\n" > "$td/proxy-status.log"',
                'echo "Proxy required; OpenCode launch blocked" >> "$td/agent-status.md"',
            ]
        )
    if proxy_provider_url:
        parts.append("acquire_opencode_proxy")
    startup_retry_lines: list[str] = []
    if proxy_provider_url:
        startup_retry_lines = [
            f"OPENCODE_STARTUP_MAX_PROXY_ATTEMPTS={startup_max_proxy_attempts}",
            "OPENCODE_PROXY_ATTEMPT=1",
            'while [ "${OPENCODE_STARTUP_STALLED:-0}" -eq 1 ] && [ "$PROXY_BLOCKED" -eq 0 ] && [ "$OPENCODE_PROXY_ATTEMPT" -lt "$OPENCODE_STARTUP_MAX_PROXY_ATTEMPTS" ]; do',
            '  echo "OpenCode startup stalled; rotating proxy (attempt $OPENCODE_PROXY_ATTEMPT/$OPENCODE_STARTUP_MAX_PROXY_ATTEMPTS)" >> "$td/agent-status.md"',
            "  cooldown_opencode_proxy",
            '  if [ -n "${OPENCODE_PROXY_DIGEST:-}" ]; then',
            '    OPENCODE_REJECTED_PROXY_DIGESTS="${OPENCODE_REJECTED_PROXY_DIGESTS:+$OPENCODE_REJECTED_PROXY_DIGESTS,}$OPENCODE_PROXY_DIGEST"',
            "  fi",
            "  release_opencode_proxy",
            "  OPENCODE_PROXY_ATTEMPT=$((OPENCODE_PROXY_ATTEMPT + 1))",
            "  acquire_opencode_proxy",
            '  if [ "$PROXY_BLOCKED" -eq 0 ]; then',
            "    run_opencode_attempt",
            "  else",
            "    RC=76",
            "    break",
            "  fi",
            "done",
            'if [ "${OPENCODE_STARTUP_STALLED:-0}" -eq 1 ] && [ "$PROXY_BLOCKED" -eq 0 ] && [ "$OPENCODE_PROXY_ATTEMPT" -ge "$OPENCODE_STARTUP_MAX_PROXY_ATTEMPTS" ]; then',
            '  echo "OpenCode startup attempts exhausted ($OPENCODE_PROXY_ATTEMPT/$OPENCODE_STARTUP_MAX_PROXY_ATTEMPTS)" >> "$td/agent-status.md"',
            "fi",
        ]
    parts.extend([
        'if [ "$PROXY_BLOCKED" -eq 1 ]; then',
        "  RC=76",
        'elif [ -f "$td/current-plan.md" ]; then',
        # stdin must be /dev/null for opencode: the script itself is piped
        # to `sh` via stdin (execute_project_script*), and a long-running
        # child that reads stdin steals the script tail the shell has not
        # consumed yet -- sh then hits EOF with an unclosed construct
        # ("sh: syntax error: unexpected end of file (expecting \"fi\")",
        # seen live in the E2E smoke) and aborts before the post-run
        # status/report block. Children inherit /dev/null, so this covers
        # the whole opencode subtree.
        "  run_opencode_attempt",
        "else",
        '  echo "Error: current-plan.md not found in $td"',
        "  RC=1",
        "fi",
        *startup_retry_lines,
        "OOM_KILL_AFTER=$(awk '$1 == \"oom_kill\" {print $2}' /sys/fs/cgroup/memory.events 2>/dev/null || true)",
        'if [ "$RC" -eq 137 ]; then',
        "  RESOURCE_EXHAUSTED=1",
        '  FAILURE_REASON="sigkill-exit-137"',
        '  case "$OOM_KILL_BEFORE:$OOM_KILL_AFTER" in',
        '    *[!0-9:]*|:*) ;;',
        '    *) if [ "$OOM_KILL_AFTER" -gt "$OOM_KILL_BEFORE" ]; then FAILURE_REASON="cgroup-oom-kill"; fi ;;',
        "  esac",
        '  printf "OpenCode resource exhaustion: %s (oom_kill %s -> %s)\n" "$FAILURE_REASON" "${OOM_KILL_BEFORE:-unknown}" "${OOM_KILL_AFTER:-unknown}" >> "$td/agent-status.md"',
        "fi",
        # Preserve the detailed worker-authored status before supervisor/proxy
        # post-processing replaces agent-status.md with its canonical final
        # one-line state. This survives MCP restarts and keeps the worker's
        # step log/deliverables available for later review.
        'if [ -f "$td/agent-status.md" ]; then cp "$td/agent-status.md" "$td/worker-status.md"; fi',
    ])
    if proxy_provider_url:
        parts.extend(
            [
                'if [ "${OPENCODE_STARTUP_STALLED:-0}" -eq 1 ]; then cooldown_opencode_proxy; fi',
                "report_rate_limited_proxy",
                "release_opencode_proxy",
            ]
        )
    parts.extend(
        _supervisor_postrun_script_lines(
            allowed_files,
            forbidden_files,
            required_checks,
            parent_root=project_root if worktree_path and not managed_clone else None,
        )
    )
    parts.extend(
        [
            'if [ $FINAL_RC -eq 0 ] && [ "${CHECKS_WARNING:-0}" -eq 1 ]; then',
            '  echo "Status: needs-review-warning" > "$td/agent-status.md"',
            'elif [ $FINAL_RC -eq 0 ]; then',
            '  echo "Status: needs-review" > "$td/agent-status.md"',
            'elif [ "$PROXY_BLOCKED" -eq 1 ] && [ "$PARENT_RC" -eq 0 ] && [ "$EVIDENCE_RC" -eq 0 ] && [ "$SCOPE_RC" -eq 0 ] && [ "$CHECKS_RC" -eq 0 ]; then',
            '  echo "Status: blocked" > "$td/agent-status.md"',
            'elif [ "${RATE_LIMITED:-0}" = "1" ] && [ "$PARENT_RC" -eq 0 ] && [ "$EVIDENCE_RC" -eq 0 ] && [ "$SCOPE_RC" -eq 0 ] && [ "$CHECKS_RC" -eq 0 ]; then',
            '  echo "Status: rate-limited" > "$td/agent-status.md"',
            'elif [ "$RESOURCE_EXHAUSTED" -eq 1 ] && [ "$PARENT_RC" -eq 0 ] && [ "$EVIDENCE_RC" -eq 0 ] && [ "$SCOPE_RC" -eq 0 ] && [ "$CHECKS_RC" -eq 0 ]; then',
            '  echo "Status: resource-exhausted" > "$td/agent-status.md"',
            "else",
            '  echo "Status: failed" > "$td/agent-status.md"',
            "fi",
        ]
    )
    parts.append(
        f'cat > "$td/agent-report.md" << REOF\n'
        f"# Agent Runner Result — {task_id}\n\n"
        f"- Agent: opencode\n"
        f"- Status: $(head -1 \"$td/agent-status.md\" | cut -d' ' -f2)\n"
        f"- Worker exit code: $RC\n"
        f"- Final exit code: $FINAL_RC\n"
        f"- Parent-guard exit code: $PARENT_RC\n"
        f"- Evidence exit code: $EVIDENCE_RC\n"
        f"- Scope exit code: $SCOPE_RC (ran=$SCOPE_RAN)\n"
        f"- Required-checks exit code: $CHECKS_RC (ran=$CHECKS_RAN)\n"
        f"- Failure reason: ${{FAILURE_REASON:-none}}\n"
        f"- cgroup oom_kill: ${{OOM_KILL_BEFORE:-unknown}} -> ${{OOM_KILL_AFTER:-unknown}}\n"
        f"- Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        f"REOF"
    )
    parts.extend([
        'if [ -s "$td/worker-status.md" ]; then',
        '  printf "\\n## Worker status snapshot\\n\\n" >> "$td/agent-report.md"',
        '  cat "$td/worker-status.md" >> "$td/agent-report.md"',
        '  printf "\\n" >> "$td/agent-report.md"',
        "fi",
    ])
    parts.append("exit $FINAL_RC")
    return "\n".join(parts)


def project_run_agent(
    run_cmd: Callable[[str, str], dict[str, Any]],
    *,
    project: str,
    task_id: str,
    model: str | None = None,
    router: Any | None = None,
    run_script: Callable[[str, str], dict[str, Any]] | None = None,
    run_script_async: Callable[[str, str, str], dict[str, Any]] | None = None,
    run_script_wait: Callable[[str], dict[str, Any]] | None = None,
    read_attempt_state: Callable[[str, str], dict[str, Any] | None] | None = None,
    claim_attempt_state: Callable[[str, str, dict[str, Any]], bool] | None = None,
    write_attempt_state: Callable[[str, str, dict[str, Any]], None] | None = None,
    job_status: Callable[[str], dict[str, Any]] | None = None,
    resolve_trusted_attempt: Callable[[str, str, str], tuple[str, str | None]] | None = None,
    record_trusted_attempt: Callable[[str, str, str, str, str], None] | None = None,
    async_submit: bool = False,
) -> dict[str, Any]:
    """Execute a handoff task via the agent backend router.

    Reads ``task.json`` from the SSH target, validates the task contract
    (``agent``, ``allowed_backends``), selects the backend via the router
    (when enabled), and delegates to the appropriate execution path.

    Args:
        run_cmd: callable(project, command) that executes a shell command
        project: project name under ``MCP_GATEWAY_PROJECT_ROOT``
        task_id: validated ``.ai-bridge`` task ID
        model: optional model override
        router: optional ``AgentBackendRouter`` instance
        run_script: callable(project, script) for multi-line bash scripts
        run_script_async: callable(project, script, submission_key) -> {"job_id": ...},
            submits without waiting -- required when async_submit=True.
            Fleet mode: call run_agent repeatedly with async_submit=True to
            launch several agents without blocking on each one, then poll
            each job_id independently via job_status/job_result/job_wait.
            The router's cooldown tracking (record_result) is NOT fed by
            async-submitted jobs -- there is no completion callback into
            this process once a job is handed off, so an async run's
            eventual failure/rate-limit never reaches the router. Only the
            synchronous path (async_submit=False) updates cooldown state.
        run_script_wait: callable(job_id) -> terminal job result dict, or a
            durable receipt {"job_id", "status": "running",
            "wait_timed_out": True} when the bounded wait expires while the
            job is still running. When both run_script_async and
            run_script_wait are set, the synchronous (async_submit=False)
            path is durable: it submits under the stable idempotency key
            and waits on the gateway job instead of a single blocking
            execute-argv request, so a timeout after acceptance returns a
            job_id-bearing receipt (status="running", wait_timed_out=True)
            to poll -- never an opaque timeout with unknown state.
        read_attempt_state: callable(project, task_id) -> dict with
            {"attempt_id", "fingerprint", "job_id"} or None. Reads the
            durable execution-attempt record for this task.
        claim_attempt_state: callable(project, task_id, record) -> bool.
            Create-if-absent atomic claim of the FIRST attempt record
            (claims the new attempt_id only when no record exists yet, so
            concurrent first attempts converge on one winner). Required for
            the durable contract -- without it a first-attempt race could
            launch two executions.
        write_attempt_state: callable(project, task_id, record) -> None.
            Binds the submitted job_id back into the attempt record on the
            target. Required for the durable contract: without it a process
            restart could not distinguish "retry the same attempt" from
            "new intentional run" and duplication/reuse errors would return.
        job_status: callable(job_id) -> {"status": ...} snapshot used to
            reconcile a wait whose transport failed while the job_id is
            already known.
        async_submit: submit and return a job_id immediately instead of
            waiting for the full run.

    Returns:
        dict with keys: task_id, status, exit_code, stdout, stderr,
        started_at, finished_at, attempt_id, job_id. Durable receipts use
        the documented reconciliation statuses: "not-accepted" (submit
        could not be proven accepted; retryable=True, same attempt),
        "running"/"wait_timed_out": True (job_id known), "unknown" (job_id
        known but no backend proof of terminal or running), or a terminal
        status when the job is decided.
    """
    from examples.mcp_server.agent_tasks import validate_base_ref, validate_task_id

    validate_task_id(task_id)

    started_at = _now_iso()
    td = task_dir(project, task_id)

    task_json = _read_task_json(run_cmd, project, task_id)
    if not task_json:
        return {
            "task_id": task_id,
            "status": "error",
            "error": "task.json not found or empty",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    agent = task_json.get("agent", "auto")
    allowed = task_json.get("allowed_backends", [])
    if agent != "auto":
        allowed = allowed or [agent]
    if not allowed:
        return {
            "task_id": task_id,
            "status": "error",
            "error": "task.json missing allowed_backends",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    if router is not None and getattr(router, "enabled", False):
        preferred = agent if agent == "opencode" else None
        selected = router.select_backend(task_agent=preferred)
    else:
        # Router disabled: use task.json agent field if valid, else first allowed
        selected = agent if agent in allowed else allowed[0]

    if not selected:
        cooldowns = router.get_cooldowns() if router else []
        cooldown_info = "; ".join(f"{c.backend}: blocked until {c.until}" for c in cooldowns)
        return {
            "task_id": task_id,
            "status": "blocked",
            "error": f"all backends unavailable ({cooldown_info})"
            if cooldown_info
            else "no backend available",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    if selected not in allowed:
        return {
            "task_id": task_id,
            "status": "error",
            "error": f"selected backend '{selected}' not in allowed_backends {allowed}",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    if selected == "opencode":
        plan = _read_current_plan(run_cmd, project, task_id)
        if not plan:
            return {
                "task_id": task_id,
                "status": "error",
                "error": "current-plan.md not found — write task plan first",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": _now_iso(),
            }
        project_root = _resolve_project_root(project)
        managed_path = managed_workspace_path(project, task_id)
        managed_clone = managed_path is not None
        user_worktree = (task_json.get("worktree_path") or "").strip() or None
        if managed_clone and user_worktree:
            return {
                "task_id": task_id,
                "status": "error",
                "error": "managed execution is active but task.json supplies an explicit worktree_path; "
                "remove worktree_path from the task or disable MCP_AGENT_WORKSPACE_ROOT",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": _now_iso(),
            }
        worktree_path = managed_path or user_worktree
        isolation_error = _isolated_worktree_error(project_root, worktree_path)
        if isolation_error:
            return {
                "task_id": task_id,
                "status": "error",
                "error": isolation_error,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": _now_iso(),
            }
        try:
            raw_base_ref = task_json.get("base_ref")
            validate_base_ref(raw_base_ref)
            base_ref = raw_base_ref if isinstance(raw_base_ref, str) and raw_base_ref else None
            managed_source_path = None
            managed_source_sha256 = None
            if managed_clone:
                if not base_ref:
                    raise ValueError("managed OpenCode execution requires an exact base_ref")
                managed_source_path = managed_source_bundle_path(project, base_ref)
                if not managed_source_path:
                    raise ValueError("MCP_AGENT_SOURCE_ROOT is required for managed OpenCode execution")
                raw_sha256 = task_json.get("managed_source_sha256")
                if isinstance(raw_sha256, str) and raw_sha256.strip():
                    managed_source_sha256 = raw_sha256.strip()
            allowed_files = _task_string_list(task_json, "allowed_files")
            forbidden_files = _task_string_list(task_json, "forbidden_files")
            required_checks = _task_string_list(task_json, "required_checks")
        except (TypeError, ValueError) as exc:
            return {
                "task_id": task_id,
                "status": "error",
                "error": str(exc),
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": _now_iso(),
            }
        cmd = _build_opencode_script(
            td,
            task_id,
            model,
            project_root=None if managed_clone else project_root,
            worktree_path=worktree_path,
            allowed_files=allowed_files,
            forbidden_files=forbidden_files,
            required_checks=required_checks,
            managed_clone=managed_clone,
            base_ref=base_ref,
            managed_source_path=managed_source_path,
            managed_source_sha256=managed_source_sha256,
        )
    else:
        return {
            "task_id": task_id,
            "status": "error",
            "error": f"unsupported backend: {selected}",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    fingerprint = _execution_fingerprint(cmd, plan, task_json, model)
    durable_store = (
        read_attempt_state is not None
        and claim_attempt_state is not None
        and write_attempt_state is not None
        and job_status is not None
    )

    if async_submit:
        if run_script_async is None:
            return {
                "task_id": task_id,
                "status": "error",
                "error": "async_submit=True requires an async submit callable",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": None,
            }
        attempt_id = None
        job_id = None
        if durable_store:
            assert read_attempt_state is not None
            assert claim_attempt_state is not None
            assert write_attempt_state is not None
            assert job_status is not None
            try:
                if resolve_trusted_attempt is not None:
                    attempt_id, job_id = resolve_trusted_attempt(project, task_id, fingerprint)
                else:
                    attempt_id, job_id = _resolve_attempt(
                        read_attempt_state, claim_attempt_state,
                        project, task_id, fingerprint,
                    )
            except AttemptConflictError as exc:
                return {
                    "task_id": task_id,
                    "status": "error",
                    "kind": "immutable-task-conflict",
                    "error": _error_text(exc),
                    "attempt_id": exc.attempt_id,
                    "job_id": exc.job_id,
                    "fingerprint": fingerprint,
                    "recorded_fingerprint": exc.recorded_fingerprint,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "",
                    "started_at": started_at,
                    "finished_at": _now_iso(),
                }
            except Exception as exc:
                return {
                    "task_id": task_id,
                    "status": "error",
                    "kind": "durable-state-error",
                    "error": _error_text(exc),
                    "attempt_id": None,
                    "job_id": None,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "",
                    "started_at": started_at,
                    "finished_at": _now_iso(),
                }
        submission_key = _agent_attempt_key(project, task_id, attempt_id)
        if job_id is None:
            job_id, submit_error = _submit_same_key_retry(
                run_script_async, project, cmd, submission_key
            )
        else:
            submit_error = None
        if job_id is None:
            return {
                "task_id": task_id,
                "status": "not-accepted",
                "retryable": True,
                "error": _error_text(submit_error),
                "attempt_id": attempt_id,
                "job_id": None,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": _now_iso(),
            }
        if attempt_id:
            if record_trusted_attempt is not None:
                try:
                    record_trusted_attempt(project, task_id, attempt_id, fingerprint, job_id)
                except Exception as exc:
                    return {
                        "task_id": task_id,
                        "status": "error",
                        "kind": "trusted-delivery-state-error",
                        "error": _error_text(exc),
                        "attempt_id": attempt_id,
                        "job_id": job_id,
                        "exit_code": None,
                        "stdout": "",
                        "stderr": "",
                        "started_at": started_at,
                        "finished_at": _now_iso(),
                    }
            assert write_attempt_state is not None
            try:
                write_attempt_state(
                    project, task_id,
                    {"attempt_id": attempt_id, "fingerprint": fingerprint, "job_id": job_id},
                )
            except Exception as exc:
                return {
                    "task_id": task_id,
                    "status": "error",
                    "kind": "durable-state-error",
                    "error": _error_text(exc),
                    "attempt_id": attempt_id,
                    "job_id": job_id,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "",
                    "started_at": started_at,
                    "finished_at": _now_iso(),
                }
        return {
            "task_id": task_id,
            "status": "running",
            "job_id": job_id,
            "attempt_id": attempt_id,
            "diagnostics": _agent_diagnostics_hint(project, task_id, job_id),
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": None,
        }

    # Durable sync path (default). Fail closed: a caller that wants the
    # idempotent submit+wait contract must supply the complete durable
    # primitive set; anything less is a typed error, never a silent drop to
    # a single blocking execution. Attempt identity is resolved and
    # persisted BEFORE the first submission, submit retries reuse the same
    # key (lost-response recovery), and a transport-lost wait reconciles via
    # job_status instead of claiming unproven state.
    if run_script_async is None:
        # Pure legacy blocking path, reachable only from callers that pass
        # no durable callables at all (unit harness). Production wiring in
        # the adapter layer always provides the full durable set.
        result = (run_script or run_cmd)(project, cmd)
        exit_code = result.get("exit_code")

        if router is not None and selected:
            router.record_result(
                selected,
                exit_code=exit_code if exit_code is not None else -1,
                stdout=result.get("stdout", ""),
                stderr=result.get("stderr", ""),
            )

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        if project_root:
            try:
                from examples.mcp_server.mcp_client_tools import _redact_project_root

                stdout = _redact_project_root(stdout, project_root)
                stderr = _redact_project_root(stderr, project_root)
            except Exception:
                pass  # redaction failure must not hide a real result

        return {
            "task_id": task_id,
            "status": "needs-review"
            if exit_code == 0
            else "blocked"
            if exit_code == 76
            else "resource-exhausted"
            if exit_code == 137
            else "failed"
            if exit_code is not None
            else "error",
            "attempt_id": None,
            "job_id": None,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    missing = _durable_missing(
        run_script_async=run_script_async,
        run_script_wait=run_script_wait,
        read_attempt_state=read_attempt_state,
        claim_attempt_state=claim_attempt_state,
        write_attempt_state=write_attempt_state,
        job_status=job_status,
    )
    if missing:
        return {
            "task_id": task_id,
            "status": "error",
            "kind": "durable-requirements-not-met",
            "error": "durable sync execution requires: " + ", ".join(missing),
            "attempt_id": None,
            "job_id": None,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    assert run_script_wait is not None
    assert read_attempt_state is not None
    assert claim_attempt_state is not None
    assert write_attempt_state is not None
    assert job_status is not None

    try:
        if resolve_trusted_attempt is not None:
            attempt_id, job_id = resolve_trusted_attempt(project, task_id, fingerprint)
        else:
            attempt_id, job_id = _resolve_attempt(
                read_attempt_state, claim_attempt_state,
                project, task_id, fingerprint,
            )
    except AttemptConflictError as exc:
        return {
            "task_id": task_id,
            "status": "error",
            "kind": "immutable-task-conflict",
            "error": _error_text(exc),
            "attempt_id": exc.attempt_id,
            "job_id": exc.job_id,
            "fingerprint": fingerprint,
            "recorded_fingerprint": exc.recorded_fingerprint,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }
    except Exception as exc:
        return {
            "task_id": task_id,
            "status": "error",
            "kind": "durable-state-error",
            "error": _error_text(exc),
            "attempt_id": None,
            "job_id": None,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    submission_key = _agent_attempt_key(project, task_id, attempt_id)
    submitted_now = job_id is None
    if submitted_now:
        job_id, submit_error = _submit_same_key_retry(
            run_script_async, project, cmd, submission_key
        )
        if job_id is None:
            return {
                "task_id": task_id,
                "status": "not-accepted",
                "retryable": True,
                "error": _error_text(submit_error),
                "attempt_id": attempt_id,
                "job_id": None,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": _now_iso(),
            }

    if job_id is None:
        return {
            "task_id": task_id,
            "status": "error",
            "kind": "durable-state-error",
            "error": "accepted job identity is missing",
            "attempt_id": attempt_id,
            "job_id": None,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    if record_trusted_attempt is not None:
        try:
            record_trusted_attempt(project, task_id, attempt_id, fingerprint, job_id)
        except Exception as exc:
            return {
                "task_id": task_id,
                "status": "error",
                "kind": "trusted-delivery-state-error",
                "error": _error_text(exc),
                "attempt_id": attempt_id,
                "job_id": job_id,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": _now_iso(),
            }

    if submitted_now:
        try:
            write_attempt_state(
                project, task_id,
                {"attempt_id": attempt_id, "fingerprint": fingerprint, "job_id": job_id},
            )
        except Exception as exc:
            return {
                "task_id": task_id,
                "status": "error",
                "kind": "durable-state-error",
                "error": _error_text(exc),
                "attempt_id": attempt_id,
                "job_id": job_id,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": _now_iso(),
            }

    waiter = _wait_same_job(run_script_wait, job_status, job_id)

    if waiter.get("wait_timed_out") or waiter.get("status") == "running":
        return {
            "task_id": task_id,
            "status": "running",
            "job_id": job_id,
            "attempt_id": attempt_id,
            "wait_timed_out": True,
            "reconciled_via": waiter.get("reconciled_via"),
            "diagnostics": _agent_diagnostics_hint(project, task_id, job_id),
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": None,
        }

    if waiter.get("status") == "unknown":
        return {
            "task_id": task_id,
            "status": "unknown",
            "job_id": job_id,
            "attempt_id": attempt_id,
            "error": waiter.get("error", "job state unresolved after transport loss"),
            "reconciled_via": waiter.get("reconciled_via"),
            "diagnostics": _agent_diagnostics_hint(project, task_id, job_id),
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": None,
        }

    exit_code = waiter.get("exit_code")

    if router is not None and selected:
        router.record_result(
            selected,
            exit_code=exit_code if exit_code is not None else -1,
            stdout=waiter.get("stdout", ""),
            stderr=waiter.get("stderr", ""),
        )

    stdout = waiter.get("stdout", "")
    stderr = waiter.get("stderr", "")
    if project_root:
        try:
            from examples.mcp_server.mcp_client_tools import _redact_project_root

            stdout = _redact_project_root(stdout, project_root)
            stderr = _redact_project_root(stderr, project_root)
        except Exception:
            pass  # redaction failure must not hide a real result

    return {
        "task_id": task_id,
        "status": "needs-review"
        if exit_code == 0
        else "blocked"
        if exit_code == 76
        else "resource-exhausted"
        if exit_code == 137
        else "failed"
        if exit_code is not None
        else "error",
        "attempt_id": attempt_id,
        "job_id": job_id,
        "reconciled_via": waiter.get("reconciled_via"),
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
