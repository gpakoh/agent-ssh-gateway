"""OpenCode runner MCP tool — execute handoff tasks via OpenCode CLI.

Thin wrapper around agent_tools.py's opencode script-building logic (the
single, dedicated entrypoint -- not routed through the agent backend
router's cooldown/fallback selection like run_agent). Runs with
--dangerously-skip-permissions -- opencode's own internal safety
confirmations are disabled for unattended execution. Gated by write-mode
(assert_handoff_write_allowed, checked by gateway_run_opencode before
calling into this module) and by tool-mode registration (excluded from
mcp_client/mcp_client_write's tool sets -- see tool_modes.py).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from examples.mcp_server.agent_paths import (
    managed_source_bundle_path,
    managed_workspace_path,
    task_dir,
)
from examples.mcp_server.agent_tasks import (
    AttemptConflictError,
    resolve_task_source_contract,
    validate_task_id,
)
from examples.mcp_server.agent_tools import (
    _agent_attempt_key,
    _build_opencode_script,
    _durable_missing,
    _error_text,
    _execution_fingerprint,
    _isolated_worktree_error,
    _now_iso,
    _opencode_terminal_status,
    _read_current_plan,
    _read_task_json,
    _resolve_attempt,
    _resolve_project_root,
    _submit_same_key_retry,
    _task_string_list,
    _wait_same_job,
    _workflow_execution_scope,
)


def project_run_opencode(
    run_cmd: Callable[[str, str], dict[str, Any]],
    *,
    project: str,
    task_id: str,
    model: str | None = None,
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
    before_gateway_dispatch: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Execute an existing handoff task via OpenCode CLI on the SSH target.

    Args:
        run_cmd: callable(project, command) that executes a single shell
            command -- used only to read current-plan.md, never for the
            multi-line opencode script itself (that needs run_script:
            run_cmd's underlying execute-argv path shlex.splits its
            command string, which mangles a multi-line script's own
            syntax -- if/then/fi, heredocs -- into broken argv).
        project: project name under MCP_GATEWAY_PROJECT_ROOT
        task_id: validated .ai-bridge task ID (must exist in tasks/)
        model: optional model override (e.g., "gpt-4o")
        run_script: callable(project, script) for multi-line bash scripts,
            required for the synchronous (async_submit=False) path
        run_script_async: callable(project, script, submission_key) -> {"job_id": ...},
            submits without waiting -- required when async_submit=True
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
        async_submit: submit and return a job_id immediately instead of
            waiting for the full run (fleet mode: launch several agents
            without blocking, poll each job_id independently)

    Returns:
        dict with keys: task_id, status, exit_code, stdout, stderr,
        started_at, finished_at (async: status="running", job_id set,
        exit_code/stdout/stderr/finished_at are None/empty until polled)
    """
    validate_task_id(task_id)

    started_at = _now_iso()
    td = task_dir(project, task_id)

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
    task_json = _read_task_json(run_cmd, project, task_id)
    managed_path = managed_workspace_path(project, task_id)
    managed_clone = managed_path is not None
    user_worktree = ((task_json or {}).get("worktree_path") or "").strip() or None
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
        source_contract = resolve_task_source_contract(task_json or {})
        source_mode = str(source_contract["source_mode"])
        source_ref = source_contract["source_ref"]
        managed_source_sha256 = source_contract["managed_source_sha256"]
        if source_mode == "dirty_worktree_snapshot" and not managed_clone:
            raise ValueError(
                "dirty_worktree_snapshot requires managed OpenCode execution"
            )
        managed_source_path = None
        if managed_clone:
            if not source_ref:
                if source_mode == "committed_head":
                    raise ValueError("managed OpenCode execution requires an exact base_ref")
                raise ValueError("managed OpenCode execution requires an exact source_ref")
            managed_source_path = managed_source_bundle_path(project, source_ref)
            if not managed_source_path:
                raise ValueError("MCP_AGENT_SOURCE_ROOT is required for managed OpenCode execution")
        _, allowed_files = _workflow_execution_scope(task_json or {})
        forbidden_files = _task_string_list(task_json or {}, "forbidden_files")
        required_checks = _task_string_list(task_json or {}, "required_checks")
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
        base_ref=source_ref,
        managed_source_path=managed_source_path,
        managed_source_sha256=managed_source_sha256,
    )

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
                run_script_async,
                project,
                cmd,
                submission_key,
                before_gateway_dispatch=before_gateway_dispatch,
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
            "status": _opencode_terminal_status(exit_code),
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
            run_script_async,
            project,
            cmd,
            submission_key,
            before_gateway_dispatch=before_gateway_dispatch,
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
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": None,
        }

    exit_code = waiter.get("exit_code")

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
        "status": _opencode_terminal_status(exit_code),
        "attempt_id": attempt_id,
        "job_id": job_id,
        "reconciled_via": waiter.get("reconciled_via"),
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
