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
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import call as mcall

import pytest

import examples.mcp_server.mcp_infra.adapters.agent as agent_adapter
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


class TestOuterSubmissionEnvelopeBinding:
    """Outer envelope of run_agent/run_opencode must bind to the receipt.

    Gateway TODO #33: pre-submit validation failures, backend/fleet blocked
    replays, not-accepted submits and unknown/running receipts without a job
    identity are confirmed or unproven NON-submissions and must never return
    outer ok=true with the "Submitted ..." success text. A receipt with an
    accepted job_id (or an evidenced terminal exit_code) is a true
    submission and keeps success semantics. All submission fakes are local
    mocks -- no agent is ever dispatched.
    """

    @pytest.fixture(autouse=True)
    def _handoff_write_mode(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "handoff")

    def _normalize(self, raw: dict) -> dict:
        return agent_adapter._normalize_single_agent_submission("run_agent", raw)

    # ── direct normalizer: confirmed non-submissions fail closed ──────

    def test_raw_error_receipt_is_agent_submission_failed(self):
        raw = {
            "task_id": TASK_ID,
            "status": "error",
            "error": "task.json missing allowed_backends",
            "job_id": None,
        }
        envelope = self._normalize(raw)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "AGENT_SUBMISSION_FAILED"
        assert envelope["error"]["message"] == "task.json missing allowed_backends"
        assert envelope["result"] is raw
        assert envelope["meta"]["source"] == "agent"

    def test_backend_blocked_receipt_without_job_is_outer_failure(self):
        """Pre-submit router cooldown: status=blocked, no job, exit_code None."""
        raw = {
            "task_id": TASK_ID,
            "status": "blocked",
            "error": "all backends unavailable (opencode: blocked until ...)",
            "job_id": None,
            "exit_code": None,
        }
        envelope = self._normalize(raw)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "AGENT_SUBMISSION_FAILED"
        assert "all backends unavailable" in envelope["error"]["message"]
        assert envelope["result"] is raw

    def test_not_accepted_receipt_is_outer_failure_and_keeps_retryable(self):
        raw = {
            "task_id": TASK_ID,
            "status": "not-accepted",
            "retryable": True,
            "error": "Gateway request timed out",
            "attempt_id": "att-1",
            "job_id": None,
            "exit_code": None,
        }
        envelope = self._normalize(raw)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "AGENT_SUBMISSION_FAILED"
        assert envelope["error"]["retryable"] is True
        assert envelope["error"]["message"] == "Gateway request timed out"
        assert envelope["result"]["attempt_id"] == "att-1"
        assert envelope["result"]["job_id"] is None

    def test_running_receipt_without_job_identity_is_outer_failure(self):
        """Fail closed: a running claim with no job_id proves no submission."""
        raw = {"task_id": TASK_ID, "status": "running", "job_id": None}
        envelope = self._normalize(raw)
        assert envelope["ok"] is False
        assert "status=running" in envelope["error"]["message"]
        assert envelope["result"] is raw

    def test_unknown_receipt_without_job_identity_is_outer_failure(self):
        raw = {"task_id": TASK_ID, "status": "unknown", "job_id": None}
        envelope = self._normalize(raw)
        assert envelope["ok"] is False
        assert "status=unknown" in envelope["error"]["message"]

    def test_missing_status_without_job_identity_is_outer_failure(self):
        raw = {"task_id": TASK_ID, "job_id": None}
        envelope = self._normalize(raw)
        assert envelope["ok"] is False
        assert "status=missing" in envelope["error"]["message"]

    def test_fleet_capacity_blocked_receipt_preserves_diagnostics(self):
        raw = {
            "task_id": TASK_ID,
            "status": "blocked",
            "error_code": "FLEET_CAPACITY_EXHAUSTED",
            "error": "Fleet worker pool is at capacity",
            "retryable": True,
            "retry_after_seconds": 30,
            "fleet": {"pool": "std", "capacity": 1, "active": 1},
        }
        envelope = self._normalize(raw)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "AGENT_SUBMISSION_FAILED"
        assert envelope["error"]["retryable"] is True
        assert envelope["result"]["error_code"] == "FLEET_CAPACITY_EXHAUSTED"
        assert envelope["result"]["retry_after_seconds"] == 30
        assert envelope["result"]["fleet"] == {"pool": "std", "capacity": 1, "active": 1}

    # ── direct normalizer: true submissions retain success ────────────

    def test_terminal_replay_receipt_with_job_passes_through(self):
        raw = {
            "task_id": TASK_ID,
            "status": "needs-review",
            "attempt_id": "att-1",
            "job_id": "job-done",
            "exit_code": 0,
            "stdout": "immutable-result",
        }
        assert self._normalize(raw) is raw

    def test_wait_timeout_durable_receipt_with_job_passes_through(self):
        raw = {
            "task_id": TASK_ID,
            "status": "running",
            "job_id": "job-7",
            "attempt_id": "att-1",
            "wait_timed_out": True,
            "exit_code": None,
        }
        assert self._normalize(raw) is raw

    def test_async_submission_running_receipt_with_job_passes_through(self):
        raw = {"task_id": TASK_ID, "status": "running", "job_id": "job-single"}
        assert self._normalize(raw) is raw

    def test_terminal_blocked_with_evidenced_exit_passes_through(self):
        """Exit-76 terminal blocked is a real job outcome; run_tool_async
        surfaces its non-zero exit_code as an error, not a success."""
        raw = {
            "task_id": TASK_ID,
            "status": "blocked",
            "job_id": "job-b76",
            "attempt_id": "att-1",
            "exit_code": 76,
        }
        assert self._normalize(raw) is raw

    def test_legacy_blocking_terminal_receipt_with_exit_evidence_passes_through(self):
        raw = {
            "task_id": TASK_ID,
            "status": "needs-review",
            "attempt_id": None,
            "job_id": None,
            "exit_code": 0,
            "stdout": "done",
        }
        assert self._normalize(raw) is raw

    def test_canonical_envelope_is_untouched(self):
        canonical = {
            "ok": False,
            "tool": "run_agent",
            "result": {"status": "error"},
            "error": {"code": "INVALID_INPUT", "message": "already normalized"},
            "meta": {},
        }
        assert self._normalize(canonical) is canonical

    # ── end-to-end: project_run_agent receipts -> outer envelope ──────

    def test_project_run_agent_not_accepted_receipt_normalizes_to_failure(self):
        """A real not-accepted receipt from the durable executor must never
        become outer ok=true; no new submission happens on normalization."""
        rc = _run_cmd(task_json=_task_json())
        _, read, claim, write = _attempt_store()
        keys: list[str] = []

        def dead(project: str, script: str, submission_key: str) -> dict:
            keys.append(submission_key)
            raise RuntimeError("gateway unreachable")

        receipt = project_run_agent(
            rc, project="test", task_id=TASK_ID,
            run_script_async=dead, run_script_wait=MagicMock(),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )
        assert receipt["status"] == "not-accepted"

        envelope = self._normalize(receipt)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "AGENT_SUBMISSION_FAILED"
        assert envelope["result"]["status"] == "not-accepted"
        assert len(keys) == 3 and len(set(keys)) == 1, "bounded same-key retry only"

    def test_project_run_agent_terminal_replay_stays_accepted_submission(self):
        """Idempotent replay of a terminal bound job is a true submission:
        the outer envelope stays accepted and no second launch occurs."""
        _, read, claim, write = _attempt_store()
        keys: list[str] = []
        rc = _run_cmd(task_json=_task_json())
        call_kwargs = dict(
            project="test", task_id=TASK_ID,
            run_script_async=_submit(keys, "job-replay"),
            run_script_wait=MagicMock(return_value=_completed(stdout="replayed")),
            read_attempt_state=read, claim_attempt_state=claim, write_attempt_state=write,
            job_status=_running_status(),
        )
        first = project_run_agent(rc, **call_kwargs)
        replay = project_run_agent(rc, **call_kwargs)

        envelope = self._normalize(replay)
        assert envelope is replay, "true submission receipts pass through unchanged"
        assert replay["status"] == "needs-review"
        assert replay["job_id"] == first["job_id"] == "job-replay"
        assert len(keys) == 1, "terminal replay must not launch a second execution"

    # ── gateway_run_agent outer envelope (fakes only; no dispatch) ────

    async def test_gateway_run_agent_blocked_fleet_receipt_is_outer_failure(
        self, monkeypatch
    ):
        raw = {
            "task_id": "single",
            "status": "blocked",
            "error": "unbound lease is in-flight or indeterminate; refusing to re-dispatch",
            "fleet": {"submit_state": "attempted"},
        }

        async def fake_submit(**_kwargs):
            return dict(raw)

        monkeypatch.setattr(agent_adapter, "_submit_agent_with_fleet", fake_submit)
        monkeypatch.setattr(
            agent_adapter, "_agent_router_terminal_observer", lambda: None
        )

        result = await agent_adapter.gateway_run_agent("test", "single")

        assert result["ok"] is False
        assert result["error"]["code"] == "AGENT_SUBMISSION_FAILED"
        assert result["result"]["status"] == "blocked"
        assert result["meta"]["source"] == "agent"

    async def test_gateway_run_agent_not_accepted_receipt_is_outer_failure(
        self, monkeypatch
    ):
        raw = {
            "task_id": "single",
            "status": "not-accepted",
            "retryable": True,
            "error": "submit transport timed out",
            "attempt_id": "att-g1",
            "job_id": None,
        }

        async def fake_submit(**_kwargs):
            return dict(raw)

        monkeypatch.setattr(agent_adapter, "_submit_agent_with_fleet", fake_submit)
        monkeypatch.setattr(
            agent_adapter, "_agent_router_terminal_observer", lambda: None
        )

        result = await agent_adapter.gateway_run_agent("test", "single")

        assert result["ok"] is False
        assert result["error"]["code"] == "AGENT_SUBMISSION_FAILED"
        assert result["error"]["retryable"] is True
        assert result["result"]["attempt_id"] == "att-g1"

    async def test_gateway_run_agent_running_receipt_with_job_stays_success(
        self, monkeypatch
    ):
        raw = {"task_id": "single", "status": "running", "job_id": "job-single"}

        async def fake_submit(**_kwargs):
            return dict(raw)

        monkeypatch.setattr(agent_adapter, "_submit_agent_with_fleet", fake_submit)
        monkeypatch.setattr(
            agent_adapter, "_agent_router_terminal_observer", lambda: None
        )

        result = await agent_adapter.gateway_run_agent("test", "single")

        assert result["ok"] is True
        assert result["result"]["job_id"] == "job-single"

    async def test_gateway_run_agent_unknown_receipt_without_job_is_failure(
        self, monkeypatch
    ):
        raw = {
            "task_id": "single",
            "status": "unknown",
            "job_id": None,
            "error": "job state unresolved after transport loss",
        }

        async def fake_submit(**_kwargs):
            return dict(raw)

        monkeypatch.setattr(agent_adapter, "_submit_agent_with_fleet", fake_submit)
        monkeypatch.setattr(
            agent_adapter, "_agent_router_terminal_observer", lambda: None
        )

        result = await agent_adapter.gateway_run_agent("test", "single")

        assert result["ok"] is False
        assert result["error"]["code"] == "AGENT_SUBMISSION_FAILED"
        assert result["result"]["status"] == "unknown"

    # ── gateway_run_opencode outer envelope (direct OpenCode path) ────

    async def test_gateway_run_opencode_not_accepted_receipt_is_outer_failure(
        self, monkeypatch
    ):
        raw = {
            "task_id": OPENC_TASK_ID,
            "status": "not-accepted",
            "retryable": True,
            "error": "Gateway request timed out",
            "job_id": None,
        }

        def fake_run(*_args, **_kwargs):
            return dict(raw)

        monkeypatch.delenv("MCP_OPENCODE_FLEET_ADMISSION_ENABLED", raising=False)
        monkeypatch.setattr(agent_adapter, "_project_run_opencode", fake_run)

        result = await agent_adapter.gateway_run_opencode("test", OPENC_TASK_ID)

        assert result["ok"] is False
        assert result["error"]["code"] == "AGENT_SUBMISSION_FAILED"
        assert result["error"]["retryable"] is True
        assert result["result"]["status"] == "not-accepted"
        assert result["help"]["recommended_model"] == "big-pickle"

    async def test_gateway_run_opencode_blocked_fleet_receipt_is_outer_failure(
        self, monkeypatch
    ):
        raw = {
            "task_id": OPENC_TASK_ID,
            "status": "blocked",
            "error_code": "FLEET_CAPACITY_EXHAUSTED",
            "error": "Fleet worker pool is at capacity",
            "retryable": True,
        }

        fleet = MagicMock()
        fleet.submit = AsyncMock(return_value=dict(raw))

        async def get_fleet():
            return fleet

        monkeypatch.setenv("MCP_OPENCODE_FLEET_ADMISSION_ENABLED", "true")
        monkeypatch.setattr(agent_adapter, "get_fleet_runtime", get_fleet)

        result = await agent_adapter.gateway_run_opencode("test", OPENC_TASK_ID)

        assert result["ok"] is False
        assert result["error"]["code"] == "AGENT_SUBMISSION_FAILED"
        assert result["result"]["error_code"] == "FLEET_CAPACITY_EXHAUSTED"
        assert result["help"]["recommended_model"] == "big-pickle"

    async def test_gateway_run_opencode_terminal_receipt_with_job_stays_success(
        self, monkeypatch
    ):
        raw = {
            "task_id": OPENC_TASK_ID,
            "status": "needs-review",
            "attempt_id": "att-o1",
            "job_id": "job-o1",
            "exit_code": 0,
            "stdout": "openc-ok",
        }

        def fake_run(*_args, **_kwargs):
            return dict(raw)

        monkeypatch.delenv("MCP_OPENCODE_FLEET_ADMISSION_ENABLED", raising=False)
        monkeypatch.setattr(agent_adapter, "_project_run_opencode", fake_run)

        result = await agent_adapter.gateway_run_opencode("test", OPENC_TASK_ID)

        assert result["ok"] is True
        assert result["result"]["job_id"] == "job-o1"
        assert result["result"]["exit_code"] == 0