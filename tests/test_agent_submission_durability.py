"""Durable idempotency contract for the run_agent/run_opencode submission path.

Reported defect (Agent 2 audit round): a default (``async_submit=False``)
run_agent / run_opencode ended in a ~121s Gateway timeout with ambiguous
state.  The sync branch executed the whole OpenCode run as one blocking
``/api/ssh/execute-argv`` request WITHOUT a submission_key and WITHOUT a
durable job, so after the MCP client's 120s HTTP timeout the caller could
not tell "not accepted / accepted / running / completed / failed" apart,
and a retry would launch a second agent.

These tests pin the DURABLE SYNC contract: the sync path persists an
execution-attempt identity, submits under the attempt-scoped idempotency key
(``task:<project>:<task_id>:attempt:<attempt_id>``), waits on the durable
gateway job, and on wait expiry returns a durable receipt (job_id + running
+ wait_timed_out) the caller can poll via job_status/job_result instead of
an opaque timeout.  The full corrective contract (attempt identity, lost
response, wait reconciliation, fail-closed) is covered in
test_agent_attempt_identity.py; this file holds the slim durable-sync
regression set.  All tests are deterministic -- no sleeps, no I/O.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock
from unittest.mock import call as mcall

from examples.mcp_server.agent_backend_router import AgentBackendRouter
from examples.mcp_server.agent_tools import (
    _agent_attempt_key,
    project_run_agent,
)
from examples.mcp_server.opencode_tools import project_run_opencode

TASK_ID = "test-agent-001"
OPENC_TASK_ID = "2026-06-25-fix-auth-opencode"


def _task_json(**extra: object) -> str:
    data: dict[str, object] = {
        "agent": "auto",
        "allowed_backends": ["opencode"],
        "worktree_path": "../agent-worktrees/test-agent-001",
    }
    data.update(extra)
    return json.dumps(data)


def _run_cmd(
    task_json: str = "{}",
    current_plan: str = "# Plan\n\n1. Do the thing",
) -> MagicMock:
    """run_cmd fake: answers the plan/task.json/ls reads, fallback catch-all."""

    def fn(project: str, command: str) -> dict:
        if command.startswith("ls -ld -- "):
            return {"exit_code": 0, "stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": ""}
        if command.startswith("cat ") and "task.json" in command:
            return {"exit_code": 0, "stdout": task_json, "stderr": ""}
        if command.startswith("cat ") and "current-plan.md" in command:
            return {"exit_code": 0, "stdout": current_plan, "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    return MagicMock(side_effect=fn)


def _completed(exit_code: int = 0, stdout: str = "ok", stderr: str = "") -> dict:
    return {"status": "completed", "exit_code": exit_code, "stdout": stdout, "stderr": stderr}


def _submit(keys: list[str], job_id: str = "job-1") -> MagicMock:
    """run_script_async fake: record every key, return a stable job_id."""

    def fn(project: str, script: str, submission_key: str) -> dict:
        keys.append(submission_key)
        return {"job_id": job_id}

    return MagicMock(side_effect=fn)


def _attempt_store(
    initial: dict[str, object] | None = None,
) -> tuple[dict, MagicMock, MagicMock, MagicMock]:
    """In-memory durable attempt-state store at the dummy task key.

    ``claim`` emulates the remote create-if-absent (CAS) primitive: True
    exactly once for an absent record, never overwrites an existing one
    (mirrors the hardlink claim of claim_agent_attempt_state).
    """
    data: dict[str, dict[str, dict[str, object]]] = {}
    if initial is not None:
        data = {"test": {TASK_ID: dict(initial)}}

    def read(project: str, task_id: str) -> dict[str, object] | None:
        rec = data.get(project, {}).get(task_id)
        return dict(rec) if rec is not None else None

    def claim(project: str, task_id: str, record: dict[str, object]) -> bool:
        if data.get(project, {}).get(task_id) is not None:
            return False
        data.setdefault(project, {})[task_id] = dict(record)
        return True

    def write(project: str, task_id: str, record: dict[str, object]) -> None:
        data.setdefault(project, {})[task_id] = dict(record)

    return (
        data,
        MagicMock(side_effect=read),
        MagicMock(side_effect=claim),
        MagicMock(side_effect=write),
    )


class _ConcurrentAttemptStore:
    """Thread-safe CAS attempt store with a one-shot barrier inside read().

    The two racing readers each collect their snapshot UNDER the lock
    BEFORE the barrier, then wait for each other at the barrier, then
    return. That guarantees both "first attempts" genuinely observe the
    task as ABSENT before either may claim -- the exact producer of the
    lost-update race the durable CAS claim must resolve.

    The barrier is one-shot: only the first two reads wait on it; any later
    read (e.g. the runner-up's re-read after a lost claim) just returns the
    existing record without touching the barrier, so it can never deadlock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], dict[str, object]] = {}
        self._barrier = threading.Barrier(2)
        self._first_round_reads = 0
        self.reads_saw_absent = 0

    def read(self, project: str, task_id: str) -> dict[str, object] | None:
        with self._lock:
            rec = self._data.get((project, task_id))
            snapshot = dict(rec) if rec is not None else None
            if snapshot is None:
                self.reads_saw_absent += 1
            first_round = self._first_round_reads < 2
            if first_round:
                self._first_round_reads += 1
        if first_round:
            try:
                self._barrier.wait(timeout=15)
            except threading.BrokenBarrierError:
                pass
        return snapshot

    def claim(self, project: str, task_id: str, record: dict[str, object]) -> bool:
        with self._lock:
            if (project, task_id) in self._data:
                return False
            self._data[(project, task_id)] = dict(record)
            return True

    def write(self, project: str, task_id: str, record: dict[str, object]) -> None:
        with self._lock:
            self._data[(project, task_id)] = dict(record)


def _running_status() -> MagicMock:
    return MagicMock(return_value={"status": "running"})


class TestDurableSyncContractRunAgent:
    """run_agent: the default sync path must be durable + idempotent."""

    def test_retry_reuses_same_attempt_and_never_uses_blocking_run_script(self):
        """A. response lost after acceptance -> retry converges on one
        execution identity (attempt-scoped key, one submission); the
        blocking run_script must never be used."""
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        blocking = MagicMock(side_effect=AssertionError("blocking run_script must not be used"))
        wait = MagicMock(return_value=_completed())

        r1 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script=blocking, run_script_async=_submit(keys),
            run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )
        r2 = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script=blocking, run_script_async=_submit(keys),
            run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )

        assert r1["status"] == "needs-review"
        assert r2["status"] == "needs-review"
        assert r1["attempt_id"] == r2["attempt_id"]
        assert r1["job_id"] == r2["job_id"] == "job-1"
        assert len(keys) == 1, "same attempt reuses the bound job; exactly one submission"
        assert keys[0] == _agent_attempt_key("test", TASK_ID, r1["attempt_id"])
        blocking.assert_not_called()
        assert wait.call_args_list == [mcall("job-1"), mcall("job-1")]

    def test_timeout_before_acceptance_surfaces_not_accepted_then_retry_proceeds(self):
        """B. transport timeout before acceptance surfaces as a typed
        'not-accepted' receipt (nothing was accepted -> nothing to
        duplicate); a later retry passes the SAME key."""
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()
        state = {"n": 0, "keys": []}

        def flaky(project: str, script: str, submission_key: str) -> dict:
            state["n"] += 1
            state["keys"].append(submission_key)
            if state["n"] == 1:
                raise RuntimeError("Gateway request timed out")
            return {"job_id": "job-2"}

        not_accepted = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=flaky,
            run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )
        assert not_accepted["status"] in {"not-accepted", "needs-review"}
        if not_accepted["status"] == "not-accepted":
            assert not_accepted["retryable"] is True
            assert not_accepted["job_id"] is None

        # The same attempt retried with a healthy backend converges on one job.
        result = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=flaky,
            run_script_wait=MagicMock(return_value=_completed()),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )
        assert result["status"] == "needs-review"
        assert result["job_id"] == "job-2"
        assert len(state["keys"]) > 0
        assert len(set(state["keys"])) == 1, "every retry reuses the identical key"
        assert len(state["keys"]) >= 2

    def test_concurrent_same_key_submissions_converge_on_one_job(self):
        """C. TRUE concurrent first-attempt race: two threads submit the SAME
        fresh task at once, BOTH observe the task absent on their initial
        read, and the durable CAS claim guarantees exactly ONE attempt is
        born, ONE idempotency key is submitted, and ONE job is created --
        the runner-up re-reads the winner instead of spawning a parallel
        execution."""
        store = _ConcurrentAttemptStore()
        rc = _run_cmd(task_json=_task_json())
        keys: list[str] = []
        jobs: dict[str, str] = {}
        claimed: list[bool] = []
        wait = MagicMock(return_value=_completed(stdout="a"))

        def claim_rec(project: str, task_id: str, record: dict) -> bool:
            ok = store.claim(project, task_id, record)
            claimed.append(ok)
            return ok

        def submit(project: str, script: str, submission_key: str) -> dict:
            keys.append(submission_key)
            jobs.setdefault(submission_key, f"job-c{len(jobs) + 1}")
            return {"job_id": jobs[submission_key]}

        results: list[dict | None] = [None, None]

        def run_attempt() -> int:
            return project_run_agent(
                rc, project="test", task_id=TASK_ID,
                run_script_async=submit, run_script_wait=wait,
                read_attempt_state=store.read, claim_attempt_state=claim_rec,
                write_attempt_state=store.write, job_status=_running_status(),
            )

        # Two threads -> a live race window inside read()/claim().
        t1 = threading.Thread(target=lambda: results.__setitem__(0, run_attempt()))
        t2 = threading.Thread(target=lambda: results.__setitem__(1, run_attempt()))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        r1, r2 = results[0], results[1]
        assert isinstance(r1, dict) and isinstance(r2, dict)
        assert r1["status"] == "needs-review"
        assert r2["status"] == "needs-review"
        assert store.reads_saw_absent == 2, "both first attempts must see the task absent"
        assert r1["attempt_id"] == r2["attempt_id"]
        assert r1["job_id"] == r2["job_id"] == "job-c1"
        assert len(keys) == 1 and len(set(keys)) == 1
        assert len(jobs) == 1
        assert len(claimed) == 2 and sum(claimed) == 1, "exactly one claim wins"
        assert keys[0] == _agent_attempt_key("test", TASK_ID, r1["attempt_id"])

    def test_recovery_after_process_restart_finds_same_receipt(self):
        """D. a fresh submission (new process) for the same task finds the
        persisted attempt record, resolves the original job and the
        completed result is recovered, not relaunched."""
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        wait = MagicMock(return_value=_completed(stdout="recovered", exit_code=0))

        first = project_run_agent(
            _run_cmd(task_json=_task_json()), project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-4"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )
        # A different process image submits the same task again: fresh fakes
        # over the SAME persisted store.
        _, read2, claim, write2 = _attempt_store(initial=store["test"][TASK_ID])
        recovered = project_run_agent(
            _run_cmd(task_json=_task_json()), project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-4"), run_script_wait=wait,
            read_attempt_state=read2, claim_attempt_state=claim, write_attempt_state=write2,
            job_status=_running_status(),
        )

        assert first["attempt_id"] == recovered["attempt_id"]
        assert first["job_id"] == recovered["job_id"] == "job-4"
        assert recovered["status"] == "needs-review"
        assert recovered["stdout"] == "recovered"
        assert len(keys) == 1

    def test_different_tasks_get_different_keys_and_independent_jobs(self):
        """E. distinct task_ids must NOT share an idempotency key or a job."""
        rc = _run_cmd(task_json=_task_json())
        keys: list[str] = []
        wait = MagicMock(return_value=_completed())

        store_e1, read_e1, claim_e1, write_e1 = _attempt_store()
        project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-e1"), run_script_wait=wait,
            read_attempt_state=read_e1, claim_attempt_state=claim_e1, write_attempt_state=write_e1,
            job_status=_running_status(),
        )
        store_e2, read_e2, claim_e2, write_e2 = _attempt_store()
        project_run_agent(
            rc, project="test", task_id="test-agent-002",
            run_script_async=_submit(keys, "job-e2"), run_script_wait=wait,
            read_attempt_state=read_e2, claim_attempt_state=claim_e2, write_attempt_state=write_e2,
            job_status=_running_status(),
        )

        assert len(keys) == 2
        assert keys[0] != keys[1]
        assert keys[1] == _agent_attempt_key("test", "test-agent-002", store_e2["test"]["test-agent-002"]["attempt_id"])

    def test_failure_after_acceptance_reconciles_as_failed_not_unknown(self):
        """F. a terminal gateway failure after acceptance must resolve to
        'failed' (and feed the router), never 'unknown'/'error'."""
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()
        router = AgentBackendRouter(fallback_order=["opencode"], enabled=True)

        def submit(project: str, script: str, submission_key: str) -> dict:
            return {"job_id": "job-6"}

        wait = MagicMock(
            return_value={"status": "failed", "exit_code": 1, "stdout": "", "stderr": "boom"}
        )
        result = project_run_agent(
            rc, project="test", task_id=TASK_ID, router=router,
            run_script_async=submit, run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )

        assert result["status"] == "failed"
        assert result["exit_code"] == 1
        assert result["job_id"] == "job-6"
        assert router._backends["opencode"].status.value == "failed"

    def test_wait_timeout_returns_durable_receipt_not_opaque_timeout(self):
        """G. the reported symptom: a bounded wait expiry after acceptance
        returns a durable receipt (job_id + running + wait_timed_out) the
        caller can poll, instead of an opaque timeout with no job_id."""
        rc = _run_cmd(task_json=_task_json())
        store, read, claim, write = _attempt_store()
        keys: list[str] = []

        def submit(project: str, script: str, submission_key: str) -> dict:
            keys.append(submission_key)
            return {"job_id": "job-7"}

        wait = MagicMock(
            return_value={"job_id": "job-7", "status": "running", "wait_timed_out": True}
        )
        result = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=submit, run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )

        assert result["status"] == "running"
        assert result["job_id"] == "job-7"
        assert result["attempt_id"] is not None
        assert result["wait_timed_out"] is True
        assert result["exit_code"] is None
        assert result["finished_at"] is None
        assert result["stdout"] == ""
        assert result["stderr"] == ""
        assert len(keys) == 1

    def test_blocking_fallback_preserved_without_async_wait_callables(self):
        """H. legacy callers that pass no async/wait callables keep the old
        blocking run_script behavior unchanged."""
        rc = _run_cmd(task_json=_task_json())
        blocking = MagicMock(return_value={"exit_code": 0, "stdout": "done", "stderr": ""})
        result = project_run_agent(rc, project="test", task_id=TASK_ID, run_script=blocking)
        assert result["status"] == "needs-review"
        assert result["stdout"] == "done"
        blocking.assert_called_once()


class TestDurableSyncContractRunOpencode:
    """run_opencode shares the same durable sync contract."""

    def test_completed_run_returns_full_result_with_job_id(self):
        store, read, claim, write = _attempt_store()
        keys: list[str] = []
        wait = MagicMock(return_value=_completed(stdout="openc-ok", exit_code=0))
        result = project_run_opencode(
            _run_cmd(task_json=_task_json()),
            project="test", task_id=OPENC_TASK_ID,
            run_script_async=_submit(keys, "job-o1"), run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )
        assert result["status"] == "needs-review"
        assert result["exit_code"] == 0
        assert result["job_id"] == "job-o1"
        assert result["attempt_id"] is not None
        assert keys == [_agent_attempt_key("test", OPENC_TASK_ID, result["attempt_id"])]
        wait.assert_called_once_with("job-o1")

    def test_wait_timeout_returns_durable_receipt(self):
        store, read, claim, write = _attempt_store()
        keys: list[str] = []

        def submit(project: str, script: str, submission_key: str) -> dict:
            keys.append(submission_key)
            return {"job_id": "job-o2"}

        wait = MagicMock(
            return_value={"job_id": "job-o2", "status": "running", "wait_timed_out": True}
        )
        result = project_run_opencode(
            _run_cmd(task_json=_task_json()),
            project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )
        assert result["status"] == "running"
        assert result["job_id"] == "job-o2"
        assert result["wait_timed_out"] is True
        assert result["finished_at"] is None
        assert len(keys) == 1

    def test_failure_after_acceptance_reconciles_as_failed(self):
        store, read, claim, write = _attempt_store()

        def submit(project: str, script: str, submission_key: str) -> dict:
            return {"job_id": "job-o3"}

        wait = MagicMock(
            return_value={"status": "failed", "exit_code": 1, "stdout": "", "stderr": "boom"}
        )
        result = project_run_opencode(
            _run_cmd(task_json=_task_json()),
            project="test", task_id=OPENC_TASK_ID,
            run_script_async=submit, run_script_wait=wait,
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )
        assert result["status"] == "failed"
        assert result["exit_code"] == 1
        assert result["job_id"] == "job-o3"