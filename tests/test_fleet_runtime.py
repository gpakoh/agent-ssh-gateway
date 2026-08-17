"""Crash-safety tests for MCP FleetRuntime wiring."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import examples.mcp_server.fleet_runtime as runtime_module
from examples.mcp_server.fleet_runtime import FleetRuntime, FleetRuntimeError, fleet_task_id
from examples.mcp_server.fleet_state import (
    AdmissionResult,
    TaskAlreadyTerminalError,
    TaskOutcome,
    WorkerLease,
)


def test_default_pool_targets_dedicated_agent_executor():
    assert runtime_module._DEFAULT_POOL == "ssh-gateway/agent-sshd"


def _lease(*, job_id: str | None = None) -> WorkerLease:
    return WorkerLease(
        task_id=fleet_task_id("demo", "task-1"),
        pool="ssh-gateway/sshd",
        lease_token="11111111-1111-1111-1111-111111111111",
        coordinator_id="gpt-a",
        job_id=job_id,
        claimed_at=None,
        heartbeat_at=None,
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


@pytest.mark.asyncio
async def test_full_pool_never_calls_submit():
    state = MagicMock()
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
    assert result["fleet"]["active"] == 2
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_bound_existing_lease_returns_job_without_resubmit():
    state = MagicMock()
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
    state = MagicMock()
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
    state = MagicMock()
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
    state = MagicMock()
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
    state = MagicMock()
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
    state = MagicMock()
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
    state = MagicMock()
    state.get_lease_by_job = AsyncMock()
    runtime = _runtime(state)

    await runtime.reconcile_gateway_result(
        job_id="job-1", result={"status": "running"}
    )

    state.get_lease_by_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_gateway_result_releases_exact_bound_lease():
    lease = _lease(job_id="job-1")
    state = MagicMock()
    state.get_lease_by_job = AsyncMock(return_value=lease)
    state.complete_task = AsyncMock()
    runtime = _runtime(state)

    await runtime.reconcile_gateway_result(
        job_id="job-1",
        result={"status": "completed", "exit_code": 0, "stdout": "large-output-not-persisted"},
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
async def test_terminal_gateway_without_bound_lease_is_noop():
    state = MagicMock()
    state.get_lease_by_job = AsyncMock(return_value=None)
    state.complete_task = AsyncMock()

    await _runtime(state).reconcile_gateway_result(
        job_id="job-1", result={"status": "failed", "exit_code": 7}
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
async def test_bound_job_watcher_releases_lease_when_gateway_turns_terminal():
    lease = _lease()
    state = MagicMock()
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
    state = MagicMock()
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
async def test_watcher_never_releases_on_job_not_found():
    state = MagicMock()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-42"))
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
    await asyncio.sleep(0.05)

    assert state.complete_task.await_count == 0
    assert "job-42" in runtime._watchers_by_job
    await runtime.close()


@pytest.mark.asyncio
async def test_watcher_recovers_after_transient_errors():
    state = MagicMock()
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
async def test_watcher_retries_terminal_reconciliation_after_transient_state_error():
    state = MagicMock()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-42"))
    state.get_lease_by_job = AsyncMock(return_value=_lease(job_id="job-42"))
    state.complete_task = AsyncMock(side_effect=[RuntimeError("postgres unavailable"), None])
    state.list_bound_leases = AsyncMock(return_value=[])
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    await runtime.submit(
        project="demo",
        task_id="task-1",
        submit_sync=lambda: {"task_id": "task-1", "status": "running", "job_id": "job-42"},
        job_status_fn=lambda jid: {"job_id": jid, "status": "completed", "exit_code": 0},
    )

    assert await _wait_until(lambda: state.complete_task.await_count >= 2)
    assert state.complete_task.await_count == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_sweep_releases_terminal_bound_lease_before_admission():
    state = MagicMock()
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
async def test_sweep_retains_running_bound_lease():
    state = MagicMock()
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
    state = MagicMock()
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
    state = MagicMock()
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
    state = MagicMock()
    state.acquire_slot = AsyncMock(return_value=AdmissionResult(True, False, 2, 1, _lease()))
    state.list_bound_leases = AsyncMock(return_value=[_lease(job_id="job-unreachable")])
    state.bind_job = AsyncMock(return_value=_lease(job_id="job-new"))
    state.complete_task = AsyncMock()
    state.close = AsyncMock()
    runtime = _watch_runtime(state)

    def status_fn(job_id: str) -> dict:
        if job_id == "job-unreachable":
            raise RuntimeError("JOB_NOT_FOUND")
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
async def test_existing_bound_lease_restores_watcher_on_submit():
    state = MagicMock()
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
    state = MagicMock()
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
    state = MagicMock()
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
    state = MagicMock()
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
