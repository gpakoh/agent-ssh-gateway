"""Corrective round: execution-attempt identity + reconciliation contract.

Architect review (durable agent submission, round 2) requires:
  B1. ONE task_id = ONE immutable logical execution. A task_id is bound to
      a single attempt_id for its lifetime. Same-content re-invocations
      (retry / reconnect / process restart / terminal replay) resolve to the
      SAME attempt_id and SAME job_id and never enqueue a second execution;
      changed content (different model/plan/scope -> different execution
      fingerprint) is a typed immutable-task-conflict -- the caller must
      create a NEW task_id. The fingerprint cryptographically binds the
      exact execution contract: current-plan.md BYTES (the generated shell
      only references the plan path, never its contents), the canonical
      task.json blob, the explicit model override, and the generated cmd.
      There is never a "second attempt" for a given task_id.
  B2. lost response AFTER backend acceptance: retry the same logical
      attempt with the same key converges on the same job_id; execution
      and enqueue happen at most once.
  B3. wait_job transport failure with a known job_id: bounded
      reconciliation via job_status; receipts never claim "running" unless
      nonterminal is proven; no opaque failure; no second execution.
  B4. durable sync path never routes through fleet.submit; after a
      wait_timed_out receipt for Job A, re-invoking the SAME attempt must
      not spawn a parallel submit (capacity=1 safe).
  B5. a running/unknown receipt must never be recorded as a terminal task
      result; terminal state is written exactly once.
  B6. production fail-closed: sync durable execution requires
      run_script_async + run_script_wait + read/write/claim_attempt_state +
      job_status; missing one is a typed error, never a silent blocking
      fallback.
  B7. reconciliation contract: the same logical attempt resolves to the
      same job_id across re-invocation; receipts expose attempt_id +
      job_id and are state-documented (not-accepted / running / unknown /
      terminal).

All tests deterministic. They are RED against the pre-corrective code: it
has no attempt identity at all (task-scoped key only).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from examples.mcp_server.agent_backend_router import AgentBackendRouter
from examples.mcp_server.agent_tools import (
    _agent_attempt_key,
    _command_fingerprint,
    project_run_agent,
)
from examples.mcp_server.opencode_tools import project_run_opencode

TASK_ID = "attempt-att-001"
OPENC_TASK_ID = "attempt-openc-001"


def _task_json(**extra: object) -> str:
    data: dict[str, object] = {
        "agent": "auto",
        "allowed_backends": ["opencode"],
        "worktree_path": "../agent-worktrees/attempt-att-001",
    }
    data.update(extra)
    return json.dumps(data)


def _run_cmd(task_json: str = "{}", current_plan: str = "# Plan\n\n1. Do the thing") -> MagicMock:
    def fn(project: str, command: str) -> dict:
        if command.startswith("ls -ld -- "):
            return {"exit_code": 0, "stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": ""}
        if command.startswith("cat ") and "task.json" in command:
            return {"exit_code": 0, "stdout": task_json, "stderr": ""}
        if command.startswith("cat ") and "current-plan.md" in command:
            return {"exit_code": 0, "stdout": current_plan, "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    return MagicMock(side_effect=fn)


def _attempt_store(
    initial: dict[str, Any] | None = None,
) -> tuple[dict, MagicMock, MagicMock, MagicMock]:
    """In-memory durable attempt-state store: {project: {task: record}}.

    ``claim`` emulates the remote create-if-absent (CAS) primitive: it
    reports True exactly once for an absent record and never overwrites an
    existing one, mirroring the hardlink claim of claim_agent_attempt_state.
    """
    data: dict[str, dict[str, dict[str, Any]]] = {}
    if initial is not None:
        data = {"test": {TASK_ID: dict(initial)}}

    def read(project: str, task_id: str) -> dict[str, Any] | None:
        rec = data.get(project, {}).get(task_id)
        return dict(rec) if rec is not None else None

    def claim(project: str, task_id: str, record: dict[str, Any]) -> bool:
        if data.get(project, {}).get(task_id) is not None:
            return False
        data.setdefault(project, {})[task_id] = dict(record)
        return True

    def write(project: str, task_id: str, record: dict[str, Any]) -> None:
        data.setdefault(project, {})[task_id] = dict(record)

    return (
        data,
        MagicMock(side_effect=read),
        MagicMock(side_effect=claim),
        MagicMock(side_effect=write),
    )


def _submit(keys: list[str], job_id: str = "job-a") -> MagicMock:
    def fn(project: str, script: str, submission_key: str) -> dict:
        keys.append(submission_key)
        return {"job_id": job_id}

    return MagicMock(side_effect=fn)


def _completed(exit_code: int = 0, stdout: str = "ok", stderr: str = "") -> dict:
    return {"status": "completed", "exit_code": exit_code, "stdout": stdout, "stderr": stderr}


class TestBlocker1AttemptIdentity:
    """ONE task_id = ONE immutable logical execution.

    A task_id is bound to exactly one attempt for its lifetime: same-content
    re-invocations replay the SAME attempt/job (never a duplicate); changed
    content (different fingerprint) is a typed conflict, never a second
    execution.
    """

    def test_retry_of_same_attempt_reuses_same_durable_job(self):
        """A. SAME content + same attempt must keep one job: the attempt
        record binds attempt_id -> job_id across calls."""
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        wait = MagicMock(return_value=_completed(stdout="same"))
        rc = _run_cmd(task_json=_task_json())

        r1 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-a"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-a"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["attempt_id"] == r2["attempt_id"]
        assert r1["job_id"] == r2["job_id"] == "job-a"
        assert len(keys) == 1, "second call must re-route to the existing job, not resubmit"
        assert keys[0] == _agent_attempt_key("test", TASK_ID, r1["attempt_id"])

    def test_recovery_after_process_restart_finds_same_attempt(self):
        """B. attempt record survives a process restart: a fresh process
        finds the same attempt_id + job_id and waits on the SAME job."""
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        wait = MagicMock(return_value=_completed(stdout="recovered", exit_code=0))
        rc = _run_cmd(task_json=_task_json())

        first = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-b"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        # "New process": a freshly built read/write pair over the same store.
        _, read2, claim, write2 = _attempt_store(initial=store["test"][TASK_ID])
        recovered = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-b"), run_script_wait=wait,
            read_attempt_state=read2, claim_attempt_state=claim, write_attempt_state=write2,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert first["attempt_id"] == recovered["attempt_id"]
        assert recovered["job_id"] == "job-b"
        assert recovered["status"] == "needs-review"
        assert recovered["stdout"] == "recovered"
        assert len(keys) == 1, "recovery must not resubmit a known nonterminal job"

    def test_terminal_task_rerun_is_idempotent_replay_not_new_execution(self):
        """C. ONE task_id = ONE immutable execution: after the recorded job
        is TERMINAL, a same-content request is an idempotent replay -- the
        same attempt_id, the same terminal job_id, the same result, and NO
        new submission. The reuse decision is stateless (it never consults
        job_status: a terminal bound job is reusable, exactly like an active
        one)."""
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        rc = _run_cmd(task_json=_task_json())
        wait = MagicMock(return_value=_completed(stdout="immutable-result", exit_code=0))
        never_status = MagicMock(
            side_effect=AssertionError("reuse decision must not consult job_status")
        )

        r1 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-done"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=never_status,
        )
        r2 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-done"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=never_status,
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "needs-review"
        assert r2["attempt_id"] == r1["attempt_id"]
        assert r2["job_id"] == r1["job_id"] == "job-done"
        assert r2["stdout"] == "immutable-result"
        assert len(keys) == 1, "terminal replay must not launch a second execution"
        assert store["test"][TASK_ID]["attempt_id"] == r1["attempt_id"]

    def test_fingerprint_change_on_terminal_task_is_typed_conflict(self):
        """D. a different model/content on the same task_id changes the
        execution fingerprint: the task is immutable, so this is a typed
        immutable-task-conflict (the caller must create a NEW task_id) and
        the backend is NEVER submitted again."""
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        rc = _run_cmd(task_json=_task_json())
        wait = MagicMock(return_value=_completed(stdout="v1", exit_code=0))

        r1 = project_run_agent(
            rc, project="test", task_id=TASK_ID, model="opencode-sonnet",
            run_script_async=_submit(keys, "job-old"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "completed", "exit_code": 0}),
        )
        r2 = project_run_agent(
            rc, project="test", task_id=TASK_ID, model="opencode-mini",
            run_script_async=_submit(keys, "job-new"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "completed", "exit_code": 0}),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "error"
        assert r2["kind"] == "immutable-task-conflict"
        assert r2["attempt_id"] == r1["attempt_id"]
        assert r2["job_id"] == r1["job_id"] == "job-old"
        assert "NEW task_id" in r2["error"]
        assert len(keys) == 1, "a conflict must never reach submit"
        assert store["test"][TASK_ID]["attempt_id"] == r1["attempt_id"]

    def test_different_model_on_active_task_is_conflict_not_second_execution(self):
        """E. immutability holds while the first execution is still ACTIVE:
        a different model on the same task_id is a typed conflict, never a
        second execution parked beside the first."""
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        rc = _run_cmd(task_json=_task_json())

        r1 = project_run_agent(
            rc, project="test", task_id=TASK_ID, model="opencode-sonnet",
            run_script_async=_submit(keys, "job-m1"),
            run_script_wait=MagicMock(
                return_value={"job_id": "job-m1", "status": "running", "wait_timed_out": True}
            ),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_agent(
            rc, project="test", task_id=TASK_ID, model="opencode-mini",
            run_script_async=_submit(keys, "job-m2"),
            run_script_wait=MagicMock(
                return_value={"job_id": "job-m2", "status": "running", "wait_timed_out": True}
            ),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "running"
        assert r1["job_id"] == "job-m1"
        assert r2["status"] == "error"
        assert r2["kind"] == "immutable-task-conflict"
        assert r2["attempt_id"] == r1["attempt_id"]
        assert r2["job_id"] == "job-m1"
        assert len(keys) == 1
        assert _command_fingerprint("")  # helper exists, deterministic

    def test_plan_content_change_is_typed_conflict(self):
        """D2. the fingerprint binds the exact current-plan CONTENT: the
        generated shell only references $td/current-plan.md, never its
        bytes, so rewriting the plan on the same task_id is changed content
        -> immutable-task-conflict and the backend is never re-submitted."""
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        rc_v1 = _run_cmd(task_json=_task_json(), current_plan="# Plan v1")
        rc_v2 = _run_cmd(task_json=_task_json(), current_plan="# Plan v2")
        wait = MagicMock(return_value=_completed(stdout="plan-v1", exit_code=0))

        r1 = project_run_agent(
            rc_v1, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-plan1"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_agent(
            rc_v2, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-plan2"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "error"
        assert r2["kind"] == "immutable-task-conflict"
        assert r2["attempt_id"] == r1["attempt_id"]
        assert r2["job_id"] == r1["job_id"] == "job-plan1"
        assert "NEW task_id" in r2["error"]
        assert len(keys) == 1, "a plan-content conflict must never reach submit"

    def test_task_json_field_change_is_typed_conflict(self):
        """D3. ANY task.json change on the same task_id is changed content
        -> immutable-task-conflict: task_id is immutable, so there is no
        guessing which fields are execution-relevant; even a non-execution
        field (priority) must not be silently replayed."""
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        rc_v1 = _run_cmd(task_json=_task_json(), current_plan="# Plan")
        rc_v2 = _run_cmd(task_json=_task_json(priority="high"), current_plan="# Plan")
        wait = MagicMock(return_value=_completed(stdout="task-v1", exit_code=0))

        r1 = project_run_agent(
            rc_v1, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-task1"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_agent(
            rc_v2, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-task2"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "error"
        assert r2["kind"] == "immutable-task-conflict"
        assert r2["attempt_id"] == r1["attempt_id"]
        assert r2["job_id"] == r1["job_id"] == "job-task1"
        assert len(keys) == 1, "a task.json-content conflict must never reach submit"

    def test_new_task_id_allows_changed_plan_independent_execution(self):
        """D4. immutability is scoped PER task_id: the same (changed) plan
        on a NEW task_id is an independent execution, never a conflict."""
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        rc_v1 = _run_cmd(task_json=_task_json(), current_plan="# Plan v1")
        rc_v2 = _run_cmd(task_json=_task_json(), current_plan="# Plan v2")
        wait = MagicMock(return_value=_completed(stdout="ok", exit_code=0))
        second_task = "attempt-att-002"

        r1 = project_run_agent(
            rc_v1, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-f1"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_agent(
            rc_v2, project="test", task_id=second_task,
            run_script_async=_submit(keys, "job-f2"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "needs-review"
        assert r1["attempt_id"] != r2["attempt_id"]
        assert r1["job_id"] == "job-f1"
        assert r2["job_id"] == "job-f2"
        assert len(keys) == 2, "a new task_id is allowed its own independent execution"
        assert keys[0] != keys[1]

    def test_plan_whitespace_mutation_is_typed_conflict(self):
        """D5. the fingerprint binds the EXACT current-plan bytes: a
        trailing-newline / leading-space mutation (strip()-invariant) is
        changed content -> immutable-task-conflict, because the worker
        executes the real on-disk file which differs. submit calls == 0."""
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        rc_v1 = _run_cmd(task_json=_task_json(), current_plan="Do X\n")
        rc_v2 = _run_cmd(task_json=_task_json(), current_plan="Do X\n\n")
        wait = MagicMock(return_value=_completed(stdout="ws-v1", exit_code=0))

        r1 = project_run_agent(
            rc_v1, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-ws1"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_agent(
            rc_v2, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-ws2"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "error"
        assert r2["kind"] == "immutable-task-conflict"
        assert r2["attempt_id"] == r1["attempt_id"]
        assert r2["job_id"] == r1["job_id"] == "job-ws1"
        assert "NEW task_id" in r2["error"]
        assert len(keys) == 1, "a whitespace-mutated plan must never reach submit"

    def test_byte_identical_plan_replays_same_attempt(self):
        """D6. byte-identical plan content across re-invocations is a replay
        of the SAME attempt/job -- no second execution."""
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        rc = _run_cmd(task_json=_task_json(), current_plan="Do X\n")
        wait = MagicMock(return_value=_completed(stdout="plan-bytes", exit_code=0))

        r1 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-bytes"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-bytes"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "needs-review"
        assert r2["attempt_id"] == r1["attempt_id"]
        assert r2["job_id"] == r1["job_id"] == "job-bytes"
        assert len(keys) == 1

    def test_whitespace_only_plan_is_missing_not_executable(self):
        """D7. a whitespace-only plan stays 'plan missing': nothing is
        claimed and nothing is submitted for it."""
        rc = _run_cmd(task_json=_task_json(), current_plan="   \n")
        store, read, claim, write = _attempt_store()
        submit = MagicMock(side_effect=AssertionError("must not submit without a plan"))

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=submit,
            run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert result["status"] == "error"
        assert "current-plan.md not found" in result["error"]
        submit.assert_not_called()


class TestBlocker2LostResponseAfterAcceptance:
    """A submit whose HTTP response is lost must converge, not duplicate."""

    def test_transport_error_on_first_submit_retries_same_key_and_returns_job(self):
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()
        seen: list[str] = []
        attempts = {"n": 0}

        def flaky(project: str, script: str, submission_key: str) -> dict:
            seen.append(submission_key)
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("connection reset after gateway accepted")
            return {"job_id": "job-b2"}

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=flaky, run_script_wait=MagicMock(return_value=_completed(stdout="b2-ok")),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert result["status"] == "needs-review"
        assert result["job_id"] == "job-b2"
        assert len(seen) == 2
        assert seen[0] == seen[1], "the retry must reuse the identical idempotency key"
        assert len(set(seen)) == 1

    def test_persistent_submit_failure_returns_not_accepted_receipt(self):
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()
        keys: list[str] = []

        def dead(project: str, script: str, submission_key: str) -> dict:
            keys.append(submission_key)
            raise RuntimeError("gateway unreachable")

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=dead, run_script_wait=MagicMock(),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert result["status"] == "not-accepted"
        assert result["retryable"] is True
        assert result["job_id"] is None
        assert "attempt_id" in result
        assert len(keys) == 3, "submit retry is bounded"
        assert len(set(keys)) == 1


class TestBlocker3WaitTransportFailureReconciliation:
    """Wait_job transport loss with a known job_id must reconcile, not hang
    and never claim an unproven state."""

    def _wait_raises(self) -> MagicMock:
        def boom(job_id: str) -> dict:
            raise RuntimeError(f"wait transport reset for {job_id}")

        return MagicMock(side_effect=boom)

    def test_terminal_snapshot_via_job_status(self):
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit([], "job-b3t"), run_script_wait=self._wait_raises(),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "failed", "exit_code": 2, "stdout": "", "stderr": "die"}),
        )

        assert result["status"] == "failed"
        assert result["exit_code"] == 2
        assert result["job_id"] == "job-b3t"
        assert result["reconciled_via"] == "job_status"

    def test_nonterminal_proven_via_job_status_is_running_not_error(self):
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit([], "job-b3r"), run_script_wait=self._wait_raises(),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert result["status"] == "running"
        assert result["wait_timed_out"] is True
        assert result["job_id"] == "job-b3r"
        assert result["exit_code"] is None
        assert result["reconciled_via"] == "job_status"

    def test_both_unreachable_returns_typed_unknown_never_opaque(self):
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit([], "job-b3u"), run_script_wait=self._wait_raises(),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(side_effect=RuntimeError("job status unreachable")),
        )

        assert result["status"] == "unknown"
        assert result["job_id"] == "job-b3u"
        assert "error" in result
        assert result["finished_at"] is None


class TestBlocker4NoParallelSpawnAfterTimeout:
    """After a wait_timed_out receipt, re-invoking the SAME attempt never
    spawns a parallel submit; a second attempt only ever sees one job."""

    def test_reinvoke_after_wait_timeout_reuses_job_without_resubmit(self):
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()
        keys: list[str] = []

        def submit(project: str, script: str, submission_key: str) -> dict:
            keys.append(submission_key)
            return {"job_id": "job-b4"}

        wait = MagicMock(
            return_value={"job_id": "job-b4", "status": "running", "wait_timed_out": True}
        )

        r1 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=submit, run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        # Caller polls again: same content, same attempt (job still running).
        r2 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=submit, run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "running"
        assert r2["status"] == "running"
        assert r1["job_id"] == r2["job_id"] == "job-b4"
        assert len(keys) == 1, "capacity=1: exactly one submission ever exists"


class TestBlocker5RunningReceiptNeverTerminal:
    def test_wait_timeout_does_not_feed_router_cooldown(self):
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()
        router = AgentBackendRouter(fallback_order=["opencode"], enabled=True)

        def submit(project: str, script: str, submission_key: str) -> dict:
            return {"job_id": "job-b5"}

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID, router=router,
            run_script_async=submit,
            run_script_wait=MagicMock(
                return_value={"job_id": "job-b5", "status": "running", "wait_timed_out": True}
            ),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert result["status"] == "running"
        assert router._backends["opencode"].status.value == "available"

    def test_unknown_receipt_does_not_feed_router_cooldown(self):
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()
        router = AgentBackendRouter(fallback_order=["opencode"], enabled=True)

        def dead(job_id: str) -> dict:
            raise RuntimeError("wait dead")

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID, router=router,
            run_script_async=_submit([], "job-b5u"), run_script_wait=MagicMock(side_effect=dead),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(side_effect=RuntimeError("status dead")),
        )

        assert result["status"] == "unknown"
        assert router._backends["opencode"].status.value == "available"

    def test_terminal_result_feeds_router_exactly_once(self):
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()
        router = AgentBackendRouter(fallback_order=["opencode"], enabled=True)

        def submit(project: str, script: str, submission_key: str) -> dict:
            return {"job_id": "job-b5t"}

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID, router=router,
            run_script_async=submit,
            run_script_wait=MagicMock(return_value={"status": "failed", "exit_code": 1, "stdout": "", "stderr": "x"}),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert result["status"] == "failed"
        assert router._backends["opencode"].status.value == "failed"


class TestBlocker6FailClosedNoSilentFallback:
    def test_sync_async_without_wait_is_typed_error(self):
        rc = _run_cmd(task_json=_task_json())
        blocking = MagicMock(side_effect=AssertionError("must never be called"))

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script=blocking,
            run_script_async=MagicMock(return_value={"job_id": "job-b6"}),
        )

        assert result["status"] == "error"
        assert result["kind"] == "durable-requirements-not-met"
        assert "run_script_wait" in result["error"]
        blocking.assert_not_called()

    def test_sync_durable_without_attempt_store_is_typed_error(self):
        rc = _run_cmd(task_json=_task_json())
        blocking = MagicMock(side_effect=AssertionError("must never be called"))

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script=blocking,
            run_script_async=MagicMock(return_value={"job_id": "job-b6"}), run_script_wait=MagicMock(),
        )

        assert result["status"] == "error"
        assert result["kind"] == "durable-requirements-not-met"
        assert "read_attempt_state" in result["error"]
        assert "job_status" in result["error"]
        blocking.assert_not_called()


class TestBlocker7ReconciliationContract:
    def test_same_attempt_resubmit_returns_same_job_id(self):
        """A same logical attempt re-invoked reconciles to the SAME job_id.
        With a durable store, the second invocation reuses the bound job and
        submits nothing; every submission is attempt-scoped."""
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        wait = MagicMock(return_value=_completed())

        r1 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-b7"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-b7"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["attempt_id"] == r2["attempt_id"]
        assert r1["job_id"] == r2["job_id"] == "job-b7"
        assert len(keys) == 1, "re-invocation reconciles via the bound job, no resubmit"
        assert keys[0] == _agent_attempt_key("test", TASK_ID, r1["attempt_id"])

    def test_receipts_expose_attempt_id_and_job_id(self):
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()

        result = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit([], "job-b7b"), run_script_wait=MagicMock(
                return_value={"job_id": "job-b7b", "status": "running", "wait_timed_out": True}
            ),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert result["attempt_id"] == store["test"][TASK_ID]["attempt_id"]
        assert result["job_id"] == "job-b7b"


class TestBlocker1OpencodeParity:
    """run_opencode shares the same one-task-one-execution identity contract."""

    def _openc_store(self) -> tuple[dict, object, object, object]:
        store: dict = {}

        def read(p: str, t: str):
            return dict(store.get((p, t))) if (p, t) in store else None

        def claim(p: str, t: str, rec: dict) -> bool:
            if (p, t) in store:
                return False
            store[(p, t)] = dict(rec)
            return True

        def write(p: str, t: str, rec: dict) -> None:
            store[(p, t)] = dict(rec)

        return store, read, claim, write

    def test_opencode_same_attempt_reuses_same_job(self):
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = self._openc_store()

        keys: list[str] = []

        def submit(p: str, s: str, k: str) -> dict:
            keys.append(k)
            return {"job_id": "job-op-a"}

        r1 = project_run_opencode(
            rc, project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_opencode(
            rc, project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["attempt_id"] == r2["attempt_id"]
        assert len(keys) == 1
        assert keys[0] == _agent_attempt_key("test", OPENC_TASK_ID, r1["attempt_id"])

    def test_opencode_terminal_task_rerun_is_idempotent_replay(self):
        """BLOCKER 3 parity: run_opencode shares the ONE task_id = ONE
        execution contract -- a terminal task replayed by the same task_id
        returns the same terminal job/result without re-submitting."""
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = self._openc_store()
        keys: list[str] = []

        def submit(p: str, s: str, k: str) -> dict:
            keys.append(k)
            return {"job_id": "job-op-replay"}

        call_args = dict(
            project="test", task_id=OPENC_TASK_ID, run_script_async=submit,
            run_script_wait=MagicMock(return_value=_completed(stdout="openc-replay", exit_code=0)),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "completed", "exit_code": 0}),
        )

        r1 = project_run_opencode(rc, **call_args)
        r2 = project_run_opencode(rc, **call_args)

        assert r1["status"] == "needs-review"
        assert r2["status"] == "needs-review"
        assert r2["attempt_id"] == r1["attempt_id"]
        assert r2["job_id"] == r1["job_id"] == "job-op-replay"
        assert r2["stdout"] == "openc-replay"
        assert len(keys) == 1

    def test_opencode_fingerprint_change_is_typed_conflict(self):
        """BLOCKER 3 parity: a different model on the same task_id is a typed
        immutable-task-conflict; run_opencode never submits for it."""
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = self._openc_store()
        keys: list[str] = []

        def submit(p: str, s: str, k: str) -> dict:
            keys.append(k)
            return {"job_id": "job-op-m1"}

        r1 = project_run_opencode(
            rc, project="test", task_id=OPENC_TASK_ID, model="opencode-sonnet",
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_opencode(
            rc, project="test", task_id=OPENC_TASK_ID, model="opencode-mini",
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "error"
        assert r2["kind"] == "immutable-task-conflict"
        assert r2["job_id"] == "job-op-m1"
        assert len(keys) == 1

    def test_opencode_plan_content_change_is_typed_conflict(self):
        """PLAN BOUNDS parity: run_opencode's fingerprint binds the exact
        current-plan bytes; a plan rewrite on the same task_id conflicts and
        never reaches submit."""
        store, read, claim, write = self._openc_store()
        keys: list[str] = []
        rc_v1 = _run_cmd(task_json=_task_json(), current_plan="# Plan v1")
        rc_v2 = _run_cmd(task_json=_task_json(), current_plan="# Plan v2")

        def submit(p: str, s: str, k: str) -> dict:
            keys.append(k)
            return {"job_id": "job-op-plan1"}

        r1 = project_run_opencode(
            rc_v1, project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_opencode(
            rc_v2, project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "error"
        assert r2["kind"] == "immutable-task-conflict"
        assert r2["job_id"] == "job-op-plan1"
        assert len(keys) == 1

    def test_opencode_task_json_field_change_is_typed_conflict(self):
        """PLAN BOUNDS parity: ANY task.json change on the same task_id is a
        typed conflict for run_opencode too."""
        store, read, claim, write = self._openc_store()
        keys: list[str] = []
        rc_v1 = _run_cmd(task_json=_task_json(), current_plan="# Plan")
        rc_v2 = _run_cmd(task_json=_task_json(priority="high"), current_plan="# Plan")

        def submit(p: str, s: str, k: str) -> dict:
            keys.append(k)
            return {"job_id": "job-op-task1"}

        r1 = project_run_opencode(
            rc_v1, project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_opencode(
            rc_v2, project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "error"
        assert r2["kind"] == "immutable-task-conflict"
        assert r2["job_id"] == "job-op-task1"
        assert len(keys) == 1

    def test_opencode_new_task_id_allows_changed_plan_independent_execution(self):
        """PLAN BOUNDS parity: immutability is per task_id; a changed plan on
        a NEW task_id is an independent run for run_opencode as well."""
        store, read, claim, write = self._openc_store()
        keys: list[str] = []
        rc_v1 = _run_cmd(task_json=_task_json(), current_plan="# Plan v1")
        rc_v2 = _run_cmd(task_json=_task_json(), current_plan="# Plan v2")
        second_task = "attempt-openc-002"

        def submit(p: str, s: str, k: str) -> dict:
            keys.append(k)
            return {"job_id": f"job-op-f{len(keys)}"}

        r1 = project_run_opencode(
            rc_v1, project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_opencode(
            rc_v2, project="test", task_id=second_task,
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "needs-review"
        assert r1["attempt_id"] != r2["attempt_id"]
        assert r1["job_id"] == "job-op-f1"
        assert r2["job_id"] == "job-op-f2"
        assert len(keys) == 2

    def test_opencode_plan_whitespace_mutation_is_typed_conflict(self):
        """PLAN BOUNDS parity: run_opencode's fingerprint binds the exact
        plan bytes, so a strip()-invariant whitespace mutation conflicts and
        never reaches submit."""
        store, read, claim, write = self._openc_store()
        keys: list[str] = []
        rc_v1 = _run_cmd(task_json=_task_json(), current_plan="Do X\n")
        rc_v2 = _run_cmd(task_json=_task_json(), current_plan="Do X\n\n")

        def submit(p: str, s: str, k: str) -> dict:
            keys.append(k)
            return {"job_id": "job-op-ws1"}

        r1 = project_run_opencode(
            rc_v1, project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r2 = project_run_opencode(
            rc_v2, project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "error"
        assert r2["kind"] == "immutable-task-conflict"
        assert r2["job_id"] == "job-op-ws1"
        assert len(keys) == 1

    def test_opencode_byte_identical_plan_replays_same_attempt(self):
        """PLAN BOUNDS parity: byte-identical plan replays ONE attempt/job."""
        store, read, claim, write = self._openc_store()
        keys: list[str] = []
        rc = _run_cmd(task_json=_task_json(), current_plan="Do X\n")

        def submit(p: str, s: str, k: str) -> dict:
            keys.append(k)
            return {"job_id": "job-op-bytes"}

        call_args = dict(
            project="test", task_id=OPENC_TASK_ID, run_script_async=submit,
            run_script_wait=MagicMock(return_value=_completed(stdout="op-bytes", exit_code=0)),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )
        r1 = project_run_opencode(rc, **call_args)
        r2 = project_run_opencode(rc, **call_args)

        assert r1["status"] == "needs-review"
        assert r2["status"] == "needs-review"
        assert r2["attempt_id"] == r1["attempt_id"]
        assert r2["job_id"] == r1["job_id"] == "job-op-bytes"
        assert len(keys) == 1

    def test_opencode_whitespace_only_plan_is_missing_not_executable(self):
        """PLAN BOUNDS parity: whitespace-only plan is 'missing' -- no
        claim, no submit."""
        rc = _run_cmd(task_json=_task_json(), current_plan="   \n")
        store, read, claim, write = self._openc_store()
        submit = MagicMock(side_effect=AssertionError("must not submit without a plan"))

        result = project_run_opencode(
            rc, project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=MagicMock(),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=MagicMock(return_value={"status": "running"}),
        )

        assert result["status"] == "error"
        assert "current-plan.md not found" in result["error"]
        submit.assert_not_called()