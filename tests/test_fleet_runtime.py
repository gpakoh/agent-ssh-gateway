"""Crash-safety tests for MCP FleetRuntime wiring."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

import examples.mcp_server.fleet_runtime as runtime_module
from examples.mcp_server.fleet_runtime import FleetRuntime, FleetRuntimeError, fleet_task_id
from examples.mcp_server.fleet_state import (
    AdmissionResult,
    LeaseNotFoundError,
    TaskAlreadyTerminalError,
    TaskOutcome,
    WorkerLease,
)


def test_default_pool_targets_dedicated_agent_executor():
    assert runtime_module._DEFAULT_POOL == "ssh-gateway/agent-sshd"


def _lease(
    *,
    job_id: str | None = None,
    submit_state: str | None = "never_attempted",
    submit_attempted_at=None,
) -> WorkerLease:
    return WorkerLease(
        task_id=fleet_task_id("demo", "task-1"),
        pool="ssh-gateway/sshd",
        lease_token="11111111-1111-1111-1111-111111111111",
        coordinator_id="gpt-a",
        job_id=job_id,
        claimed_at=None,
        heartbeat_at=None,
        submit_state=submit_state,
        submit_attempted_at=submit_attempted_at,
    )


def _runtime(state) -> FleetRuntime:
    runtime = FleetRuntime(
        state,
        pool_name="ssh-gateway/sshd",
        capacity=2,
        coordinator_id="gpt-a",
    )
    runtime._schema_ready = True
    return runtime


def _mk_state():
    """A MagicMock state whose marker transition succeeds by default.

    The r2 submission gate calls ``mark_submit_attempted`` before any gateway
    dispatch, so dispatch-oriented tests need it preconfigured.
    """
    state = MagicMock()
    state.mark_submit_attempted = AsyncMock(return_value=_lease(submit_state="attempted"))
    state.release_never_dispatched = AsyncMock(return_value=True)
    return state


@pytest.mark.asyncio
async def test_full_pool_never_calls_submit():
    state = _mk_state()
    state.acquire_slot = AsyncMock(
        return_value=AdmissionResult(
            acquired=False,
            existing=False,
            capacity=2,
            active=2,
            lease=None,
        )
    )
    submit = MagicMock()

    result = await _runtime(state).submit(
        project="demo", task_id="task-1", submit_sync=submit
    )

    assert result["status"] == "blocked"
    assert result["error_code"] == "FLEET_CAPACITY_EXHAUSTED"
    assert result["retryable"] is True
    assert result["retry_after_seconds"] == 60
    assert result["queued"] is False
    assert result["fallback"]["safe_when"] == "a terminal supervised implementation diff already exists"
    assert result["fleet"] == {
        "pool": "ssh-gateway/sshd",
        "capacity": 2,
        "active": 2,
        "available": 0,
        "retry_after_seconds": 60,
        "queued": False,
    }
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_capacity_retry_after_is_operator_configurable(monkeypatch):
    monkeypatch.setenv("MCP_AGENT_FLEET_CAPACITY_RETRY_AFTER_SECONDS", "120")
    state = _mk_state()
    state.acquire_slot = AsyncMock(
        return_value=AdmissionResult(
            acquired=False,
            existing=False,
            capacity=4,
            active=3,
            lease=None,
        )
    )

    result = await _runtime(state).submit(
        project="demo", task_id="task-1", submit_sync=MagicMock()
    )

    assert result["error_code"] == "FLEET_CAPACITY_EXHAUSTED"
    assert result["retry_after_seconds"] == 120
    assert result["fleet"]["available"] == 1


@pytest.mark.parametrize("value", ["0", "-1", "soon"])
def test_capacity_retry_after_must_be_positive_integer(monkeypatch, value):
    monkeypatch.setenv("MCP_AGENT_FLEET_CAPACITY_RETRY_AFTER_SECONDS", value)

    with pytest.raises(FleetRuntimeError, match="MCP_AGENT_FLEET_CAPACITY_RETRY_AFTER_SECONDS"):
        runtime_module._configured_capacity_retry_after_seconds()


@pytest.mark.asyncio
async def test_bound_existing_lease_returns_job_without_resubmit():
    state = _mk_state()
    state.acquire_slot = AsyncMock(
        return_value=AdmissionResult(
            acquired=True,
            existing=True,
            capacity=2,
            active=1,
            lease=_lease(job_id="job-existing"),
        )
    )
    submit = MagicMock()

    result = await _runtime(state).submit(
        project="demo", task_id="task-1", submit_sync=submit
    )

    assert result["job_id"] == "job-existing"
    assert result["fleet"]["existing_lease"] is True
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_new_lease_binds_returned_gateway_job():
    lease = _lease()
    bound = _lease(job_id="job-42")
    state = _mk_state()
    state.acquire_slot = AsyncMock(
        return_value=AdmissionResult(True, False, 2, 1, lease)
    )
    state.bind_job = AsyncMock(return_value=bound)
    state.complete_task = AsyncMock()

    result = await _runtime(state).submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
    )

    assert result["job_id"] == "job-42"
    state.bind_job.assert_awaited_once_with(
        task_id=lease.task_id,
        lease_token=lease.lease_token,
        job_id="job-42",
    )
    state.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_submit_exception_never_releases_lease():
    lease = _lease()
    state = _mk_state()
    state.acquire_slot = AsyncMock(
        return_value=AdmissionResult(True, False, 2, 1, lease)
    )
    state.bind_job = AsyncMock()
    state.complete_task = AsyncMock()

    def ambiguous_failure():
        raise RuntimeError("connection dropped after request may have reached gateway")

    with pytest.raises(RuntimeError, match="connection dropped"):
        await _runtime(state).submit(
            project="demo", task_id="task-1", submit_sync=ambiguous_failure
        )

    state.bind_job.assert_not_awaited()
    state.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_definite_presubmit_terminal_result_releases_lease():
    lease = _lease()
    state = _mk_state()
    state.acquire_slot = AsyncMock(
        return_value=AdmissionResult(True, False, 2, 1, lease)
    )
    state.complete_task = AsyncMock()

    result = await _runtime(state).submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "blocked", "error": "router cooldown"},
    )

    assert result["fleet"]["released"] is True
    state.complete_task.assert_awaited_once()
    assert state.complete_task.await_args.kwargs["status"] == "blocked"


@pytest.mark.asyncio
async def test_unknown_submit_shape_keeps_lease_fail_closed():
    lease = _lease()
    state = _mk_state()
    state.acquire_slot = AsyncMock(
        return_value=AdmissionResult(True, False, 2, 1, lease)
    )
    state.complete_task = AsyncMock()

    with pytest.raises(FleetRuntimeError, match="neither a job_id"):
        await _runtime(state).submit(
            project="demo",
            task_id="task-1",
            submit_sync=lambda: {"status": "mystery"},
        )
    state.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_task_id_is_not_submitted_again():
    state = _mk_state()
    state.acquire_slot = AsyncMock(side_effect=TaskAlreadyTerminalError("already terminal"))
    state.get_outcome = AsyncMock(
        return_value=TaskOutcome(
            task_id=fleet_task_id("demo", "task-1"),
            pool="ssh-gateway/sshd",
            job_id="job-old",
            status="failed",
            exit_code=1,
            result=None,
            reported_at=None,
        )
    )
    submit = MagicMock()

    result = await _runtime(state).submit(
        project="demo", task_id="task-1", submit_sync=submit
    )

    assert result["status"] == "blocked"
    assert result["fleet"]["terminal"] is True
    assert result["fleet"]["job_id"] == "job-old"
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_running_gateway_result_never_releases_slot():
    state = _mk_state()
    state.get_lease_by_job = AsyncMock()
    runtime = _runtime(state)

    await runtime.reconcile_gateway_result(
        job_id="job-1", result={"status": "running"}
    )

    state.get_lease_by_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_terminal_gateway_result_does_not_consume_agent_lease():
    lease = _lease(job_id="job-1")
    state = _mk_state()
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    runtime = _runtime(state)

    reconciled = await runtime.reconcile_gateway_result(
        job_id="job-1",
        result={"job_id": "job-1", "status": "completed", "exit_code": 0},
    )

    assert reconciled is False
    state.get_lease_by_job.assert_not_awaited()
    state.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_gateway_result_releases_exact_bound_lease():
    lease = _lease(job_id="job-1")
    state = _mk_state()
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    runtime = _runtime(state)

    await runtime.reconcile_gateway_result(
        job_id="job-1",
        result={"status": "completed", "exit_code": 0, "stdout": "large-output-not-persisted"},
        owned_reconciliation=True,
    )

    state.complete_task.assert_awaited_once_with(
        task_id=lease.task_id,
        lease_token=lease.lease_token,
        status="completed",
        exit_code=0,
        result={"status": "completed", "exit_code": 0},
        expected_job_id="job-1",
    )


@pytest.mark.asyncio
async def test_terminal_gateway_result_with_foreign_job_id_is_rejected():
    lease = _lease(job_id="job-1")
    state = _mk_state()
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    runtime = _runtime(state)

    reconciled = await runtime.reconcile_gateway_result(
        job_id="job-1",
        result={"job_id": "job-FOREIGN", "status": "completed", "exit_code": 0},
        owned_reconciliation=True,
    )

    assert reconciled is False
    state.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_gateway_result_releases_exact_bound_lease():
    lease = _lease(job_id="job-1")
    state = _mk_state()
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    runtime = _runtime(state)

    await runtime.reconcile_gateway_result(
        job_id="job-1",
        result={
            "job_id": "job-1",
            "status": "ambiguous",
            "exit_code": -1,
            "progress": {
                "locally_interrupted": True,
                "cancellation_outcome": "ambiguous",
            },
        },
        owned_reconciliation=True,
    )

    state.complete_task.assert_awaited_once_with(
        task_id=lease.task_id,
        lease_token=lease.lease_token,
        status="ambiguous",
        exit_code=-1,
        result={
            "status": "ambiguous",
            "exit_code": -1,
            "job_id": "job-1",
            "locally_interrupted": True,
            "cancellation_outcome": "ambiguous",
        },
        expected_job_id="job-1",
    )


@pytest.mark.asyncio
async def test_terminal_gateway_without_bound_lease_is_noop():
    state = _mk_state()
    state.get_lease_by_job = AsyncMock(return_value=None)
    state.complete_task = AsyncMock()

    await _runtime(state).reconcile_gateway_result(
        job_id="job-1",
        result={"status": "failed", "exit_code": 7},
        owned_reconciliation=True,
    )

    state.complete_task.assert_not_awaited()


def test_fleet_disabled_does_not_require_database(monkeypatch):
    monkeypatch.setenv("MCP_AGENT_FLEET_ENABLED", "0")
    assert runtime_module.fleet_enabled() is False


def test_asyncpg_dsn_normalizes_sqlalchemy_driver_prefix():
    assert runtime_module._normalize_asyncpg_dsn(
        "postgresql+asyncpg://user:pass@db/gateway"
    ) == "postgresql://user:pass@db/gateway"


def test_configured_dsn_reuses_standard_pg_environment(monkeypatch):
    monkeypatch.delenv("MCP_FLEET_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "mcp-postgres")
    monkeypatch.setenv("PGDATABASE", "gateway")
    monkeypatch.setenv("PGUSER", "postgres")
    monkeypatch.setenv("PGPASSWORD", "secret-that-must-not-be-copied-into-a-dsn")

    assert runtime_module._configured_dsn() is None


def test_configured_dsn_requires_explicit_target_or_complete_pg_environment(monkeypatch):
    for name in (
        "MCP_FLEET_DATABASE_URL",
        "DATABASE_URL",
        "PGHOST",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(FleetRuntimeError, match="missing: PGHOST, PGDATABASE, PGUSER"):
        runtime_module._configured_dsn()


def test_resource_exhausted_is_pre_submit_terminal():
    from examples.mcp_server.fleet_runtime import _PRE_SUBMIT_TERMINAL

    assert "resource-exhausted" in _PRE_SUBMIT_TERMINAL


def _watch_runtime(state, **kwargs) -> FleetRuntime:
    runtime = FleetRuntime(
        state,
        pool_name="ssh-gateway/sshd",
        capacity=2,
        coordinator_id="gpt-a",
        watch_poll_interval=0.01,
        **kwargs,
    )
    runtime._schema_ready = True
    return runtime


async def _wait_until(predicate, *, timeout_s: float = 5.0):
    waited = 0.0
    while waited < timeout_s:
        if predicate():
            return True
        await asyncio.sleep(0.01)
        waited += 0.01
    return predicate()


@pytest.mark.asyncio
async def test_gateway_io_concurrency_is_bounded():
    state = _mk_state()
    state.close = AsyncMock()
    runtime = _watch_runtime(state, gateway_io_concurrency=2)
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    max_active = 0

    def status_fn(job_id: str) -> dict:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        release.wait(timeout=2)
        with lock:
            active -= 1
        return {"job_id": job_id, "status": "running"}

    polls = [
        asyncio.create_task(runtime._run_gateway_io(status_fn, f"job-{index}"))
        for index in range(8)
    ]
    assert await _wait_until(lambda: max_active == 2)
    await asyncio.sleep(0.05)
    assert max_active == 2

    release.set()
    await asyncio.gather(*polls)
    await runtime.close()


@pytest.mark.asyncio
async def test_cancelled_gateway_io_keeps_capacity_until_worker_finishes():
    state = _mk_state()
    state.close = AsyncMock()
    runtime = _watch_runtime(state, gateway_io_concurrency=2)
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    max_active = 0

    def status_fn(job_id: str) -> dict:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        release.wait(timeout=2)
        with lock:
            active -= 1
        return {"job_id": job_id, "status": "running"}

    first = [
        asyncio.create_task(runtime._run_gateway_io(status_fn, f"first-{index}"))
        for index in range(2)
    ]
    assert await _wait_until(lambda: max_active == 2)
    for task in first:
        task.cancel()
    await asyncio.sleep(0.05)
    assert not any(task.done() for task in first)

    second = [
        asyncio.create_task(runtime._run_gateway_io(status_fn, f"second-{index}"))
        for index in range(2)
    ]
    await asyncio.sleep(0.05)
    assert max_active == 2

    release.set()
    first_results = await asyncio.gather(*first, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in first_results)
    await asyncio.gather(*second)
    assert max_active == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_parallel_submissions_share_gateway_io_bound():
    state = _mk_state()
    state.acquire_slot = AsyncMock(
        return_value=AdmissionResult(True, False, 64, 8, _lease())
    )
    state.complete_task = AsyncMock()
    state.close = AsyncMock()
    runtime = _watch_runtime(state, gateway_io_concurrency=2)
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    max_active = 0

    def submit_sync() -> dict:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        release.wait(timeout=2)
        with lock:
            active -= 1
        return {"status": "blocked", "exit_code": None}

    submissions = [
        asyncio.create_task(
            runtime.submit(
                project="demo",
                task_id=f"task-{index}",
                submit_sync=submit_sync,
                sweep_before_submit=False,
            )
        )
        for index in range(8)
    ]
    assert await _wait_until(lambda: max_active == 2)
    await asyncio.sleep(0.05)
    assert max_active == 2
    assert state.acquire_slot.await_count == 2

    release.set()
    results = await asyncio.gather(*submissions)
    assert all(result["status"] == "blocked" for result in results)
    await runtime.close()


def test_gateway_io_concurrency_must_be_positive():
    with pytest.raises(FleetRuntimeError, match="gateway_io_concurrency"):
        FleetRuntime(
            MagicMock(),
            pool_name="ssh-gateway/sshd",
            capacity=2,
            coordinator_id="gpt-a",
            gateway_io_concurrency=0,
        )


@pytest.mark.asyncio
async def test_bound_job_watcher_releases_lease_when_gateway_turns_terminal():
    lease = _lease()
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, lease))
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-42"))
    state.complete_task = AsyncMock()
    state.get_lease_by_job = AsyncMock(return_value=_lease(job_id="job-42"))
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    statuses = iter(
        [
            {"job_id": "job-42", "status": "running"},
            {"job_id": "job-42", "status": "completed", "exit_code": 0},
        ]
    )

    def status_fn(job_id: str) -> dict:
        try:
            return next(statuses)
        except StopIteration:
            return {"job_id": job_id, "status": "completed", "exit_code": 0}

    result = await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=status_fn,
    )
    assert result["job_id"] == "job-42"

    assert await _wait_until(lambda: state.complete_task.await_count >= 1)
    assert state.complete_task.await_args.kwargs["expected_job_id"] == "job-42"
    assert state.complete_task.await_args.kwargs["status"] == "completed"
    await runtime.close()


@pytest.mark.asyncio
async def test_watcher_keeps_lease_while_gateway_job_running():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-42"))
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=lambda jid: {"job_id": jid, "status": "running"},
    )
    await asyncio.sleep(0.05)

    assert state.complete_task.await_count == 0
    assert "job-42" in runtime._watchers_by_job
    await runtime.close()


@pytest.mark.asyncio
async def test_sync_running_receipt_observes_eventual_terminal_result():
    lease = _lease(job_id="job-42")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=lease)
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)
    observer = MagicMock()
    detailed = {
        "job_id": "job-42",
        "status": "failed",
        "exit_code": 77,
        "stdout": "rate limit",
        "stderr": "",
    }

    result = await runtime.submit(
        project="demo",
        task_id="task-sync-timeout",
        submit_sync=lambda: {
            "task_id": "task-sync-timeout",
            "status": "running",
            "wait_timed_out": True,
            "job_id": "job-42",
        },
        job_status_fn=lambda jid: {"job_id": jid, "status": "failed"},
        job_result_fn=lambda _jid: detailed,
        terminal_observer=observer,
        observe_submitted_job=False,
    )

    assert result["status"] == "running"
    assert await _wait_until(lambda: state.complete_task.await_count == 1)
    observer.assert_called_once_with("job-42", detailed)
    await runtime.close()


@pytest.mark.asyncio
async def test_sync_post_acceptance_error_receipt_observes_eventual_terminal_result():
    lease = _lease(job_id="job-error")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=lease)
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)
    observer = MagicMock()
    detailed = {
        "job_id": "job-error",
        "status": "failed",
        "exit_code": 1,
        "stdout": "",
        "stderr": "worker failed",
    }

    result = await runtime.submit(
        project="demo",
        task_id="task-state-error",
        submit_sync=lambda: {
            "task_id": "task-state-error",
            "status": "error",
            "kind": "durable-state-error",
            "job_id": "job-error",
        },
        job_status_fn=lambda jid: {"job_id": jid, "status": "failed"},
        job_result_fn=lambda _jid: detailed,
        terminal_observer=observer,
        observe_submitted_job=False,
    )

    assert result["status"] == "error"
    assert await _wait_until(lambda: state.complete_task.await_count == 1)
    observer.assert_called_once_with("job-error", detailed)
    await runtime.close()


@pytest.mark.asyncio
async def test_sync_terminal_receipt_reconciles_without_double_observer_accounting():
    lease = _lease(job_id="job-42")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=lease)
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)
    observer = MagicMock()

    result = await runtime.submit(
        project="demo",
        task_id="task-sync-terminal",
        submit_sync=lambda: {
            "task_id": "task-sync-terminal",
            "status": "needs-review",
            "job_id": "job-42",
            "exit_code": 0,
        },
        job_status_fn=lambda jid: {"job_id": jid, "status": "completed"},
        job_result_fn=lambda jid: {
            "job_id": jid,
            "status": "completed",
            "exit_code": 0,
            "stdout": "done",
            "stderr": "",
        },
        terminal_observer=observer,
        observe_submitted_job=False,
    )

    assert result["status"] == "needs-review"
    assert await _wait_until(lambda: state.complete_task.await_count == 1)
    observer.assert_not_called()
    await runtime.close()


@pytest.mark.asyncio
async def test_existing_bound_lease_is_observed_even_for_sync_request():
    lease = _lease(job_id="job-existing")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, True, 2, 1, lease))
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)
    observer = MagicMock()
    submit_sync = MagicMock()
    detailed = {
        "job_id": "job-existing",
        "status": "failed",
        "exit_code": 1,
        "stdout": "",
        "stderr": "worker failed",
    }

    result = await runtime.submit(
        project="demo",
        task_id="task-existing",
        submit_sync=submit_sync,
        job_status_fn=lambda jid: {"job_id": jid, "status": "failed"},
        job_result_fn=lambda _jid: detailed,
        terminal_observer=observer,
        observe_submitted_job=False,
    )

    assert result["status"] == "running"
    submit_sync.assert_not_called()
    assert await _wait_until(lambda: state.complete_task.await_count == 1)
    observer.assert_called_once_with("job-existing", detailed)
    await runtime.close()


@pytest.mark.asyncio
async def test_watcher_releases_on_authoritative_job_not_found():
    lease = _lease(job_id="job-42")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=lease)
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    def status_fn(job_id: str) -> dict:
        raise RuntimeError(f"JOB_NOT_FOUND for {job_id}")

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=status_fn,
    )

    assert await _wait_until(lambda: state.complete_task.await_count >= 1)
    state.complete_task.assert_awaited_once_with(
        task_id=lease.task_id,
        lease_token=lease.lease_token,
        status="ambiguous",
        exit_code=None,
        result={
            "status": "ambiguous",
            "job_id": "job-42",
            "error": "Gateway job status is no longer available",
            "error_code": "JOB_NOT_FOUND",
            "liveness_reconciled": True,
        },
        expected_job_id="job-42",
    )
    assert "job-42" not in runtime._watchers_by_job
    await runtime.close()


@pytest.mark.asyncio
async def test_watcher_recovers_after_transient_errors():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-42"))
    state.complete_task = AsyncMock()
    state.get_lease_by_job = AsyncMock(return_value=_lease(job_id="job-42"))
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    calls = {"count": 0}

    def status_fn(job_id: str) -> dict:
        calls["count"] += 1
        if calls["count"] <= 3:
            raise RuntimeError("upstream timeout")
        return {"job_id": job_id, "status": "completed", "exit_code": 0}

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=status_fn,
    )

    assert await _wait_until(lambda: state.complete_task.await_count >= 1)
    assert calls["count"] >= 4
    await runtime.close()


@pytest.mark.asyncio
async def test_watcher_retries_terminal_reconciliation_without_double_observer_accounting():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-42"))
    state.get_lease_by_job = AsyncMock(return_value=_lease(job_id="job-42"))
    state.complete_task = AsyncMock(side_effect=[RuntimeError("postgres unavailable"), None])
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)
    observer = MagicMock()

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=lambda jid: {"job_id": jid, "status": "completed", "exit_code": 0},
        terminal_observer=observer,
    )

    assert await _wait_until(lambda: state.complete_task.await_count >= 2)
    assert state.complete_task.await_count == 2
    observer.assert_called_once_with(
        "job-42", {"job_id": "job-42", "status": "completed", "exit_code": 0}
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_watcher_does_not_release_lease_until_terminal_observer_succeeds():
    state = _mk_state()
    lease = _lease(job_id="job-42")
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=lease)
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)
    runtime._watch_poll_interval = 0.2
    observer = MagicMock(side_effect=[RuntimeError("router temporarily unavailable"), None])

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=lambda jid: {"job_id": jid, "status": "failed", "exit_code": 1},
        terminal_observer=observer,
    )

    assert await _wait_until(lambda: observer.call_count == 1)
    state.complete_task.assert_not_awaited()
    assert "job-42" in runtime._watchers_by_job

    assert await _wait_until(lambda: state.complete_task.await_count == 1)
    assert observer.call_count == 2
    state.complete_task.assert_awaited_once_with(
        task_id=lease.task_id,
        lease_token=lease.lease_token,
        status="failed",
        exit_code=1,
        result={"status": "failed", "exit_code": 1, "job_id": "job-42"},
        expected_job_id="job-42",
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_sweep_releases_terminal_bound_lease_before_admission():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.list_bound_leases = AsyncMock(return_value=[_lease(job_id="job-stale")])
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-new"))
    state.complete_task = AsyncMock()
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    statuses = {
        "job-stale": {"job_id": "job-stale", "status": "completed", "exit_code": 0},
        "job-new": {"job_id": "job-new", "status": "running"},
    }

    result = await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-new"},
        job_status_fn=lambda jid: statuses[jid],
    )

    assert result["job_id"] == "job-new"
    state.complete_task.assert_awaited_once()
    assert state.complete_task.await_args.kwargs["expected_job_id"] == "job-stale"
    assert state.complete_task.await_args.kwargs["status"] == "completed"
    await runtime.close()


@pytest.mark.asyncio
async def test_sweep_releases_authoritative_missing_job_as_ambiguous():
    lease = _lease(job_id="job-stale")
    state = _mk_state()
    state.list_bound_leases = AsyncMock(return_value=[lease])
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    runtime = _watch_runtime(state)

    def status_fn(job_id: str) -> dict:
        raise RuntimeError(f"JOB_NOT_FOUND for {job_id}")

    released = await runtime.sweep_bound_leases(status_fn)

    assert released == 1
    state.complete_task.assert_awaited_once_with(
        task_id=lease.task_id,
        lease_token=lease.lease_token,
        status="ambiguous",
        exit_code=None,
        result={
            "status": "ambiguous",
            "job_id": "job-stale",
            "error": "Gateway job status is no longer available",
            "error_code": "JOB_NOT_FOUND",
            "liveness_reconciled": True,
        },
        expected_job_id="job-stale",
    )
    assert "job-stale" not in runtime._watchers_by_job


@pytest.mark.asyncio
async def test_sweep_retains_running_bound_lease():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.list_bound_leases = AsyncMock(return_value=[_lease(job_id="job-running")])
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-new"))
    state.complete_task = AsyncMock()
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    statuses = {
        "job-running": {"job_id": "job-running", "status": "running"},
        "job-new": {"job_id": "job-new", "status": "running"},
    }

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-new"},
        job_status_fn=lambda jid: statuses[jid],
    )

    assert state.complete_task.await_count == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_sweep_is_scoped_to_pool():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.list_bound_leases = AsyncMock(return_value=[])
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-new"))
    state.complete_task = AsyncMock()
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-new"},
        job_status_fn=lambda jid: {"job_id": jid, "status": "running"},
    )

    state.list_bound_leases.assert_awaited_once_with(pool_name="ssh-gateway/sshd")
    await runtime.close()


@pytest.mark.asyncio
async def test_sweep_discovered_running_lease_restores_watcher():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.list_bound_leases = AsyncMock(return_value=[_lease(job_id="job-running")])
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-new"))
    state.complete_task = AsyncMock()
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    statuses = {
        "job-running": {"job_id": "job-running", "status": "running"},
        "job-new": {"job_id": "job-new", "status": "running"},
    }

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-new"},
        job_status_fn=lambda jid: statuses[jid],
    )

    assert "job-running" in runtime._watchers_by_job
    assert state.complete_task.await_count == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_sweep_restores_watcher_for_unreachable_bound_lease():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.list_bound_leases = AsyncMock(return_value=[_lease(job_id="job-unreachable")])
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-new"))
    state.complete_task = AsyncMock()
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    def status_fn(job_id: str) -> dict:
        if job_id == "job-unreachable":
            raise RuntimeError("upstream timeout")
        return {"job_id": job_id, "status": "running"}

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-new"},
        job_status_fn=status_fn,
    )

    assert "job-unreachable" in runtime._watchers_by_job
    assert "job-new" in runtime._watchers_by_job
    assert state.complete_task.await_count == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_close_finishes_when_watcher_status_fn_keeps_raising():
    """close() must not hang when a watcher polls an unreachable job.

    Regression: #128 moved ``_closed = True`` after the watcher gather. A
    watcher whose gateway status call raises (upstream timeout) has its
    CancelledError converted into that RuntimeError by the gateway-io shield,
    so it keeps polling while ``_closed`` is still False and close() never
    leaves the gather.
    """
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.list_bound_leases = AsyncMock(return_value=[_lease(job_id="job-unreachable")])
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-new"))
    state.complete_task = AsyncMock()
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    def status_fn(job_id: str) -> dict:
        if job_id == "job-unreachable":
            raise RuntimeError("upstream timeout")
        return {"job_id": job_id, "status": "running"}

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-new"},
        job_status_fn=status_fn,
    )

    assert "job-unreachable" in runtime._watchers_by_job
    await asyncio.wait_for(runtime.close(), timeout=5.0)
    assert runtime._closed


@pytest.mark.asyncio
async def test_gateway_io_cancel_preserved_when_worker_already_failed():
    """Cancelling a gateway-io await must raise CancelledError, not the
    worker's own exception.

    Regression: the shield re-await in the CancelledError handler re-raised
    the already-completed worker failure (RuntimeError), swallowing the
    cancellation. Callers rely on CancelledError to stop their loops.
    """
    state = _mk_state()
    state.close = AsyncMock()
    runtime = _watch_runtime(state, gateway_io_concurrency=2)
    started = threading.Event()
    release = threading.Event()

    def boom(job_id: str) -> dict:
        started.set()
        release.wait(timeout=2)
        raise RuntimeError("JOB_NOT_FOUND")

    task = asyncio.create_task(runtime._run_gateway_io(boom, "job-x"))
    assert await _wait_until(started.is_set)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await runtime.close()


@pytest.mark.asyncio
async def test_existing_bound_lease_restores_watcher_on_submit():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, True, 2, 1, _lease(job_id="job-existing")))
    state.list_bound_leases = AsyncMock(return_value=[])
    state.complete_task = AsyncMock()
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    submit = MagicMock()
    result = await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=submit,
        job_status_fn=lambda jid: {"job_id": jid, "status": "running"},
    )

    assert result["job_id"] == "job-existing"
    submit.assert_not_called()
    assert "job-existing" in runtime._watchers_by_job
    assert state.complete_task.await_count == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_exactly_one_watcher_per_job_id():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, True, 2, 1, _lease(job_id="job-1")))
    state.list_bound_leases = AsyncMock(return_value=[])
    state.complete_task = AsyncMock()
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    for _ in range(2):
        await runtime.submit(
            project="demo",
            task_id="task-1",
            submit_sync=MagicMock(),
            job_status_fn=lambda jid: {"job_id": jid, "status": "running"},
        )

    assert list(runtime._watchers_by_job.keys()) == ["job-1"]
    await runtime.close()


@pytest.mark.asyncio
async def test_submit_can_skip_redundant_pre_sweep_but_still_tracks_new_job():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.list_bound_leases = AsyncMock(return_value=[])
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-batch"))
    state.complete_task = AsyncMock()
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-batch"},
        job_status_fn=lambda jid: {"job_id": jid, "status": "running"},
        sweep_before_submit=False,
    )

    state.list_bound_leases.assert_not_awaited()
    assert "job-batch" in runtime._watchers_by_job
    await runtime.close()


@pytest.mark.asyncio
async def test_close_cancels_watchers_then_closes_state():
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-42"))
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=lambda jid: {"job_id": jid, "status": "running"},
    )
    assert runtime._watchers_by_job

    await runtime.close()

    assert not runtime._watchers_by_job
    state.close.assert_awaited_once()


def _lease_state(*, submit_state: str | None = "never_attempted", job_id: str | None = None) -> WorkerLease:
    return WorkerLease(
        task_id=fleet_task_id("demo", "lease-state"),
        pool="ssh-gateway/sshd",
        lease_token="22222222-2222-2222-2222-222222222222",
        coordinator_id="gpt-a",
        job_id=job_id,
        claimed_at=None,
        heartbeat_at=None,
        submit_state=submit_state,
        submit_attempted_at=None,
    )


@pytest.mark.asyncio
async def test_mark_failure_after_concurrent_reclaim_submits_zero_gateway_io():
    """RESU red: reconcile deletes the lease, so the marker transition gets
    0 rows; the gateway submit MUST NOT run."""
    lease = _lease_state(submit_state="never_attempted")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, lease))
    state.mark_submit_attempted = AsyncMock(
        side_effect=LeaseNotFoundError("lease not found or token mismatch")
    )
    state.complete_task = AsyncMock()
    gateway_calls = {"n": 0}

    def submit_sync():
        gateway_calls["n"] += 1
        return {"task_id": "task-1", "status": "running", "job_id": "job-42"}

    with pytest.raises(LeaseNotFoundError):
        await _runtime(state).submit(
            project="demo", task_id="task-1", submit_sync=submit_sync
        )

    assert gateway_calls["n"] == 0, "gateway submit must be 0 when marker loses race"
    state.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_attempted_lease_is_never_redispatched():
    """Crash after dispatch-before-bind leaves an unbound 'attempted' lease;
    we must NOT re-dispatch (could double-execute)."""
    lease = _lease_state(submit_state="attempted")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, lease))
    state.complete_task = AsyncMock()
    gateway_calls = {"n": 0}

    def submit_sync():
        gateway_calls["n"] += 1
        return {"task_id": "task-1", "status": "running", "job_id": "job-42"}

    result = await _runtime(state).submit(
        project="demo", task_id="task-1", submit_sync=submit_sync
    )

    assert gateway_calls["n"] == 0
    assert result["status"] == "blocked"
    assert result["fleet"]["submit_state"] == "attempted"
    state.mark_submit_attempted.assert_not_awaited()


@pytest.mark.asyncio
async def test_opted_in_unbound_attempted_lease_redispatches_same_execution():
    lease = _lease_state(submit_state="attempted")
    bound = _lease_state(submit_state="attempted", job_id="job-42")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, True, 2, 1, lease))
    state.bind_job = AsyncMock(return_value=bound)
    state.complete_task = AsyncMock()
    submit_sync = MagicMock(
        return_value={"task_id": "task-1", "status": "running", "job_id": "job-42"}
    )

    result = await _runtime(state).submit(
        project="demo",
        task_id="task-1",
        submit_sync=submit_sync,
        retry_attempted_unbound=True,
    )

    submit_sync.assert_called_once_with()
    state.mark_submit_attempted.assert_not_awaited()
    state.bind_job.assert_awaited_once_with(
        task_id=fleet_task_id("demo", "task-1"),
        lease_token=lease.lease_token,
        job_id="job-42",
    )
    assert result["job_id"] == "job-42"


@pytest.mark.asyncio
async def test_not_accepted_receipt_can_retry_same_attempted_lease_and_bind_job():
    never = _lease_state(submit_state="never_attempted")
    attempted = _lease_state(submit_state="attempted")
    bound = _lease_state(submit_state="attempted", job_id="job-42")
    state = _mk_state()
    state.acquire_slot = AsyncMock(
        side_effect=[
            AdmissionResult(True, False, 2, 1, never),
            AdmissionResult(True, True, 2, 1, attempted),
        ]
    )
    state.bind_job = AsyncMock(return_value=bound)
    state.complete_task = AsyncMock()
    submit_sync = MagicMock(
        side_effect=[
            {
                "task_id": "task-1",
                "status": "not-accepted",
                "retryable": True,
                "job_id": None,
            },
            {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        ]
    )
    runtime = _runtime(state)

    first = await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=submit_sync,
        retry_attempted_unbound=True,
    )
    second = await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=submit_sync,
        retry_attempted_unbound=True,
    )

    assert first["status"] == "not-accepted"
    assert first["retryable"] is True
    assert first["fleet"]["released"] is False
    assert first["fleet"]["retry_same_execution"] is True
    assert second["job_id"] == "job-42"
    assert submit_sync.call_count == 2
    state.mark_submit_attempted.assert_awaited_once_with(
        task_id=fleet_task_id("demo", "task-1"),
        lease_token=never.lease_token,
    )
    state.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_exception_can_retry_same_attempted_lease_when_opted_in():
    never = _lease_state(submit_state="never_attempted")
    attempted = _lease_state(submit_state="attempted")
    bound = _lease_state(submit_state="attempted", job_id="job-42")
    state = _mk_state()
    state.acquire_slot = AsyncMock(
        side_effect=[
            AdmissionResult(True, False, 2, 1, never),
            AdmissionResult(True, True, 2, 1, attempted),
        ]
    )
    state.bind_job = AsyncMock(return_value=bound)
    state.complete_task = AsyncMock()
    submit_sync = MagicMock(
        side_effect=[
            RuntimeError("transport dropped after idempotent submit"),
            {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        ]
    )
    runtime = _runtime(state)

    with pytest.raises(RuntimeError, match="transport dropped"):
        await runtime.submit(
            project="demo",
            task_id="task-1",
            submit_sync=submit_sync,
            retry_attempted_unbound=True,
        )
    result = await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=submit_sync,
        retry_attempted_unbound=True,
    )

    assert result["job_id"] == "job-42"
    assert submit_sync.call_count == 2
    state.mark_submit_attempted.assert_awaited_once()
    state.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_legacy_unknown_lease_is_never_redispatched():
    lease = _lease_state(submit_state="legacy_unknown")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, lease))
    state.complete_task = AsyncMock()
    gateway_calls = {"n": 0}

    def submit_sync():
        gateway_calls["n"] += 1
        return {"status": "running", "job_id": "job-42"}

    result = await _runtime(state).submit(
        project="demo", task_id="task-1", submit_sync=submit_sync
    )

    assert gateway_calls["n"] == 0
    assert result["fleet"]["submit_state"] == "legacy_unknown"
    state.mark_submit_attempted.assert_not_awaited()


@pytest.mark.asyncio
async def test_marker_failure_releases_gate_so_next_submit_proceeds():
    lease1 = _lease_state(submit_state="never_attempted")
    lease2 = _lease_state(submit_state="never_attempted")
    calls = {"n": 0}
    state = _mk_state()
    state.acquire_slot = AsyncMock(
        return_value=AdmissionResult(True, False, 2, 1, lease1)
    )

    async def mark_that_fails_once(task_id=None, lease_token=None):
        calls["n"] += 1
        raise LeaseNotFoundError("lease not found or token mismatch")

    state.mark_submit_attempted = AsyncMock(side_effect=mark_that_fails_once)
    state.complete_task = AsyncMock()

    with pytest.raises(LeaseNotFoundError):
        await _runtime(state).submit(
            project="demo", task_id="task-1", submit_sync=lambda: {"status": "running", "job_id": "j1"}
        )
    # Gate must be released after the marker failure - a second submit proceeds.
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, lease2))
    state.mark_submit_attempted = AsyncMock(return_value=_lease_state(submit_state="attempted"))
    state.bind_job = AsyncMock(return_value=_lease_state(submit_state="attempted", job_id="j2"))
    result = await _runtime(state).submit(
        project="demo", task_id="task-1", submit_sync=lambda: {"status": "running", "job_id": "j2"}
    )
    assert result["job_id"] == "j2"


@pytest.mark.asyncio
async def test_sweep_unbound_releases_only_never_attempted():
    never = _lease_state(submit_state="never_attempted")
    attempted = _lease_state(submit_state="attempted")
    legacy = _lease_state(submit_state="legacy_unknown")
    state = _mk_state()
    state.list_unbound_leases = AsyncMock(return_value=[never, attempted, legacy])
    state.release_never_dispatched = AsyncMock(return_value=True)
    runtime = _runtime(state)

    released = await runtime.sweep_unbound_leases()

    assert released == 1
    state.release_never_dispatched.assert_awaited_once_with(
        task_id=never.task_id, lease_token=never.lease_token
    )


@pytest.mark.asyncio
async def test_sweep_unbound_binds_attempted_from_trusted_identity_without_release():
    attempted = _lease_state(submit_state="attempted")
    state = _mk_state()
    state.list_unbound_leases = AsyncMock(return_value=[attempted])
    state.bind_job = AsyncMock(
        return_value=_lease_state(submit_state="attempted", job_id="job-recovered")
    )
    state.complete_task = AsyncMock()
    resolver = MagicMock(return_value="job-recovered")
    runtime = _runtime(state)

    released = await runtime.sweep_unbound_leases(resolver)

    assert released == 0
    resolver.assert_called_once_with(attempted.task_id)
    state.bind_job.assert_awaited_once_with(
        task_id=attempted.task_id,
        lease_token=attempted.lease_token,
        job_id="job-recovered",
    )
    state.release_never_dispatched.assert_not_awaited()
    state.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_unbound_binds_legacy_unknown_from_trusted_identity_without_release():
    legacy = _lease_state(submit_state="legacy_unknown")
    state = _mk_state()
    state.list_unbound_leases = AsyncMock(return_value=[legacy])
    state.bind_job = AsyncMock(
        return_value=_lease_state(submit_state="legacy_unknown", job_id="job-legacy")
    )
    resolver = MagicMock(return_value="job-legacy")
    runtime = _runtime(state)

    released = await runtime.sweep_unbound_leases(resolver)

    assert released == 0
    resolver.assert_called_once_with(legacy.task_id)
    state.bind_job.assert_awaited_once_with(
        task_id=legacy.task_id,
        lease_token=legacy.lease_token,
        job_id="job-legacy",
    )
    state.release_never_dispatched.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_unbound_keeps_indeterminate_rows_when_trusted_identity_unresolved():
    attempted = _lease_state(submit_state="attempted")
    legacy = _lease_state(submit_state="legacy_unknown")
    state = _mk_state()
    state.list_unbound_leases = AsyncMock(return_value=[attempted, legacy])
    state.bind_job = AsyncMock()
    state.complete_task = AsyncMock()
    resolver = MagicMock(side_effect=[None, RuntimeError("malformed trusted binding")])
    runtime = _runtime(state)

    released = await runtime.sweep_unbound_leases(resolver)

    assert released == 0
    assert resolver.call_count == 2
    state.bind_job.assert_not_awaited()
    state.release_never_dispatched.assert_not_awaited()
    state.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_unbound_unknown_submit_state_is_fail_closed_before_resolver():
    corrupt = _lease_state(submit_state="future_or_corrupt_state")
    state = _mk_state()
    state.list_unbound_leases = AsyncMock(return_value=[corrupt])
    state.bind_job = AsyncMock()
    state.complete_task = AsyncMock()
    resolver = MagicMock(return_value="job-must-not-bind")
    runtime = _runtime(state)

    released = await runtime.sweep_unbound_leases(resolver)

    assert released == 0
    resolver.assert_not_called()
    state.bind_job.assert_not_awaited()
    state.release_never_dispatched.assert_not_awaited()
    state.complete_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_binds_trusted_unbound_then_releases_only_from_gateway_terminal():
    attempted = _lease_state(submit_state="attempted")
    bound = _lease_state(submit_state="attempted", job_id="job-recovered")
    state = _mk_state()
    state.list_unbound_leases = AsyncMock(return_value=[attempted])
    state.bind_job = AsyncMock(return_value=bound)
    state.list_bound_leases = AsyncMock(return_value=[bound])
    state.complete_task = AsyncMock()
    resolver = MagicMock(return_value="job-recovered")
    runtime = _runtime(state)

    released = await runtime.reconcile(
        lambda jid: {"job_id": jid, "status": "completed", "exit_code": 0},
        trusted_job_resolver=resolver,
    )

    assert released == 1
    state.bind_job.assert_awaited_once_with(
        task_id=attempted.task_id,
        lease_token=attempted.lease_token,
        job_id="job-recovered",
    )
    state.complete_task.assert_awaited_once_with(
        task_id=bound.task_id,
        lease_token=bound.lease_token,
        status="completed",
        exit_code=0,
        result={"status": "completed", "exit_code": 0, "job_id": "job-recovered"},
        expected_job_id="job-recovered",
    )
    state.release_never_dispatched.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_unbound_skips_rows_without_never_attempted():
    state = _mk_state()
    state.list_unbound_leases = AsyncMock(return_value=[])
    runtime = _runtime(state)

    assert await runtime.sweep_unbound_leases() == 0
    state.release_never_dispatched.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_sweeps_unbound_always_and_bound_with_status_fn():
    state = _mk_state()
    state.list_unbound_leases = AsyncMock(return_value=[])
    state.list_bound_leases = AsyncMock(return_value=[])
    runtime = _runtime(state)

    await runtime.reconcile(job_status_fn=lambda jid: {"job_id": jid, "status": "running"})

    state.list_unbound_leases.assert_awaited_once()
    state.list_bound_leases.assert_awaited_once()


@pytest.mark.asyncio
async def test_watcher_uses_terminal_job_result_for_observer_exactly_once():
    lease = _lease(job_id="job-42")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=lease)
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)
    observer = MagicMock()
    status_calls = {"count": 0}

    def status_fn(job_id: str) -> dict:
        status_calls["count"] += 1
        if status_calls["count"] == 1:
            return {"job_id": job_id, "status": "running"}
        return {"job_id": job_id, "status": "failed", "exit_code": None}

    detailed = {
        "job_id": "job-42",
        "status": "failed",
        "exit_code": 77,
        "stdout": "rate limit; retrying in 7 hours",
        "stderr": "",
    }
    result_fn = MagicMock(return_value=detailed)

    result = await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=status_fn,
        job_result_fn=result_fn,
        terminal_observer=observer,
    )
    assert result["job_id"] == "job-42"

    assert await _wait_until(lambda: state.complete_task.await_count == 1)
    state.complete_task.assert_awaited_once_with(
        task_id=lease.task_id,
        lease_token=lease.lease_token,
        status="failed",
        exit_code=77,
        result={"status": "failed", "exit_code": 77, "job_id": "job-42"},
        expected_job_id="job-42",
    )
    observer.assert_called_once_with("job-42", detailed)
    assert "stdout" not in state.complete_task.await_args.kwargs["result"]
    runtime._notify_terminal_observer(
        job_id="job-42",
        result=detailed,
        terminal_observer=observer,
    )
    await asyncio.sleep(0.03)
    assert observer.call_count == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_watcher_retries_terminal_detail_before_reconcile_and_observer():
    lease = _lease(job_id="job-42")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=lease)
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)
    observer = MagicMock()
    first_detail_attempt = threading.Event()
    allow_detail = threading.Event()
    detail_calls = {"count": 0}

    def result_fn(job_id: str) -> dict:
        detail_calls["count"] += 1
        if detail_calls["count"] == 1:
            first_detail_attempt.set()
            raise RuntimeError("transient job-result transport failure")
        allow_detail.wait(timeout=2)
        return {
            "job_id": job_id,
            "status": "failed",
            "exit_code": 77,
            "stdout": "rate limit",
            "stderr": "",
        }

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=lambda jid: {"job_id": jid, "status": "failed"},
        job_result_fn=result_fn,
        terminal_observer=observer,
    )

    assert await _wait_until(first_detail_attempt.is_set)
    assert state.complete_task.await_count == 0
    observer.assert_not_called()

    allow_detail.set()
    assert await _wait_until(lambda: state.complete_task.await_count == 1)
    assert detail_calls["count"] >= 2
    observer.assert_called_once()
    await runtime.close()


@pytest.mark.asyncio
async def test_watcher_rejects_mismatched_terminal_result_job_identity():
    lease = _lease(job_id="job-42")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=lease)
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)
    observer = MagicMock()

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=lambda jid: {"job_id": jid, "status": "failed"},
        job_result_fn=lambda _jid: {
            "job_id": "different-job",
            "status": "failed",
            "exit_code": 77,
            "stdout": "rate limit",
            "stderr": "",
        },
        terminal_observer=observer,
    )

    await asyncio.sleep(0.05)
    state.complete_task.assert_not_awaited()
    observer.assert_not_called()
    assert "job-42" in runtime._watchers_by_job
    await runtime.close()


@pytest.mark.asyncio
async def test_watcher_rejects_mismatched_terminal_status_job_identity_without_detail_fn():
    lease = _lease(job_id="job-42")
    state = _mk_state()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=lease)
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=lambda _jid: {
            "job_id": "different-job",
            "status": "failed",
            "exit_code": 1,
        },
    )

    await asyncio.sleep(0.05)
    state.complete_task.assert_not_awaited()
    assert "job-42" in runtime._watchers_by_job
    await runtime.close()
