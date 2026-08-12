"""Tests for durable multi-coordinator fleet admission state."""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

import pytest

import examples.mcp_server.fleet_state as fleet_state_module
from examples.mcp_server.fleet_state import (
    SCHEMA_SQL,
    FleetState,
    LeaseConflictError,
    LeaseNotFoundError,
    PoolCapacityMismatchError,
)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.closed = False

    def acquire(self):
        return _Acquire(self.conn)

    async def close(self):
        self.closed = True


class _FakeConn:
    def __init__(
        self,
        *,
        fetchrows: list[Any] | None = None,
        fetchvals: list[Any] | None = None,
        execute_results: list[str] | None = None,
    ):
        self.fetchrows = deque(fetchrows or [])
        self.fetchvals = deque(fetchvals or [])
        self.execute_results = deque(execute_results or [])
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []

    def transaction(self):
        self.calls.append(("transaction", "", ()))
        return _Tx()

    async def execute(self, sql: str, *args: Any):
        self.calls.append(("execute", sql, args))
        return self.execute_results.popleft() if self.execute_results else "OK"

    async def fetchrow(self, sql: str, *args: Any):
        self.calls.append(("fetchrow", sql, args))
        if not self.fetchrows:
            raise AssertionError(f"unexpected fetchrow: {sql}")
        return self.fetchrows.popleft()

    async def fetchval(self, sql: str, *args: Any):
        self.calls.append(("fetchval", sql, args))
        if not self.fetchvals:
            raise AssertionError(f"unexpected fetchval: {sql}")
        return self.fetchvals.popleft()


NOW = datetime(2026, 8, 12, tzinfo=UTC)


def _lease_row(
    *,
    task_id: str = "task-1",
    pool: str = "ssh-gateway/sshd",
    token: str = "11111111-1111-1111-1111-111111111111",
    coordinator: str = "gpt-a",
    job_id: str | None = None,
):
    return {
        "task_id": task_id,
        "pool": pool,
        "lease_token": token,
        "coordinator_id": coordinator,
        "job_id": job_id,
        "claimed_at": NOW,
        "heartbeat_at": NOW,
    }


def _state(conn: _FakeConn) -> FleetState:
    state = FleetState("postgresql://unused-in-unit-tests")
    state._pool = _FakePool(conn)  # type: ignore[assignment]
    return state


class TestSchemaInvariants:
    def test_lease_rows_not_mutable_active_counter_are_capacity_truth(self):
        schema = SCHEMA_SQL.lower()
        assert " active " not in schema
        assert "task_id text primary key" in schema
        assert "job_id text unique" in schema
        assert "lease_token uuid not null unique" in schema

    def test_admission_sql_serializes_pool_row_and_counts_leases(self):
        assert "for update" in fleet_state_module._POOL_LOCK_SQL.lower()
        assert "count(*)" in fleet_state_module._COUNT_LEASES_SQL.lower()
        assert "fleet_worker_lease" in fleet_state_module._COUNT_LEASES_SQL

    def test_no_age_only_reaper_api_exists(self):
        public = {
            name
            for name, member in inspect.getmembers(FleetState, predicate=inspect.iscoroutinefunction)
            if not name.startswith("_")
        }
        assert not any("reap" in name or "stale" in name or "expire" in name for name in public)
        source = inspect.getsource(FleetState)
        assert "heartbeat_at <" not in source
        assert "DELETE FROM fleet_worker_lease WHERE heartbeat" not in source


@pytest.mark.asyncio
async def test_pool_uses_read_write_connection_settings(monkeypatch):
    captured = {}
    fake = _FakePool(_FakeConn())

    async def _create_pool(**kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(fleet_state_module.asyncpg, "create_pool", _create_pool)
    state = FleetState("postgresql://fleet", command_timeout=7)
    assert await state._ensure_pool() is fake
    assert captured["dsn"] == "postgresql://fleet"
    assert captured["server_settings"]["statement_timeout"] == "7000"
    assert "default_transaction_read_only" not in captured["server_settings"]


@pytest.mark.asyncio
async def test_acquire_new_slot_counts_rows_and_returns_lease():
    inserted = _lease_row()
    conn = _FakeConn(
        fetchrows=[{"capacity": 2}, None, inserted],
        fetchvals=[0],
        execute_results=["INSERT 0 1"],
    )
    result = await _state(conn).acquire_slot(
        pool_name="ssh-gateway/sshd",
        task_id="task-1",
        coordinator_id="gpt-a",
        capacity=2,
    )

    assert result.acquired is True
    assert result.existing is False
    assert result.capacity == 2
    assert result.active == 1
    assert result.lease is not None
    assert result.lease.task_id == "task-1"
    sql_order = [sql for kind, sql, _ in conn.calls if kind in {"execute", "fetchrow", "fetchval"}]
    assert sql_order[0] == fleet_state_module._POOL_INSERT_SQL
    assert sql_order[1] == fleet_state_module._POOL_LOCK_SQL
    assert fleet_state_module._COUNT_LEASES_SQL in sql_order


@pytest.mark.asyncio
async def test_acquire_same_task_is_idempotent_without_second_insert():
    existing = _lease_row(job_id="job-1")
    conn = _FakeConn(
        fetchrows=[{"capacity": 2}, existing],
        fetchvals=[1],
        execute_results=["INSERT 0 0"],
    )
    result = await _state(conn).acquire_slot(
        pool_name="ssh-gateway/sshd",
        task_id="task-1",
        coordinator_id="gpt-b",
        capacity=2,
    )

    assert result.acquired is True
    assert result.existing is True
    assert result.active == 1
    assert result.lease is not None and result.lease.job_id == "job-1"
    assert all(sql != fleet_state_module._INSERT_LEASE_SQL for _, sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_acquire_full_pool_blocks_without_inserting():
    conn = _FakeConn(
        fetchrows=[{"capacity": 2}, None],
        fetchvals=[2],
        execute_results=["INSERT 0 0"],
    )
    result = await _state(conn).acquire_slot(
        pool_name="ssh-gateway/sshd",
        task_id="task-3",
        coordinator_id="gpt-c",
        capacity=2,
    )

    assert result.acquired is False
    assert result.active == 2
    assert result.lease is None
    assert all(sql != fleet_state_module._INSERT_LEASE_SQL for _, sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_acquire_rejects_coordinator_capacity_disagreement():
    conn = _FakeConn(fetchrows=[{"capacity": 6}], execute_results=["INSERT 0 0"])
    with pytest.raises(PoolCapacityMismatchError, match="capacity is 6"):
        await _state(conn).acquire_slot(
            pool_name="ssh-gateway/sshd",
            task_id="task-1",
            coordinator_id="gpt-a",
            capacity=8,
        )


@pytest.mark.asyncio
async def test_global_task_id_conflict_across_pools_fails_closed():
    conn = _FakeConn(
        fetchrows=[{"capacity": 2}, _lease_row(pool="other-pool")],
        execute_results=["INSERT 0 0"],
    )
    with pytest.raises(LeaseConflictError, match="other-pool"):
        await _state(conn).acquire_slot(
            pool_name="ssh-gateway/sshd",
            task_id="task-1",
            coordinator_id="gpt-a",
            capacity=2,
        )


@pytest.mark.asyncio
async def test_configure_pool_requires_expected_capacity_for_change():
    conn = _FakeConn(fetchrows=[{"capacity": 2}], execute_results=["INSERT 0 0"])
    with pytest.raises(PoolCapacityMismatchError):
        await _state(conn).configure_pool("ssh-gateway/sshd", capacity=6)


@pytest.mark.asyncio
async def test_configure_pool_cas_change():
    conn = _FakeConn(
        fetchrows=[{"capacity": 2}],
        fetchvals=[1],
        execute_results=["INSERT 0 0", "UPDATE 1"],
    )
    snapshot = await _state(conn).configure_pool(
        "ssh-gateway/sshd", capacity=6, expected_capacity=2
    )
    assert snapshot.capacity == 6
    assert snapshot.active == 1
    assert any(sql == fleet_state_module._POOL_UPDATE_CAPACITY_SQL for _, sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_bind_job_is_idempotent_for_same_job():
    current = _lease_row(job_id="job-1")
    conn = _FakeConn(fetchrows=[current])
    lease = await _state(conn).bind_job(
        task_id="task-1",
        lease_token=current["lease_token"],
        job_id="job-1",
    )
    assert lease.job_id == "job-1"
    assert all(sql != fleet_state_module._BIND_JOB_SQL for _, sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_bind_job_rejects_stale_lease_token():
    current = _lease_row()
    conn = _FakeConn(fetchrows=[current])
    with pytest.raises(LeaseConflictError, match="token mismatch"):
        await _state(conn).bind_job(
            task_id="task-1",
            lease_token="22222222-2222-2222-2222-222222222222",
            job_id="job-1",
        )


@pytest.mark.asyncio
async def test_complete_requires_terminal_status_before_touching_db():
    conn = _FakeConn()
    with pytest.raises(ValueError, match="not terminal"):
        await _state(conn).complete_task(
            task_id="task-1",
            lease_token="11111111-1111-1111-1111-111111111111",
            status="running",
        )
    assert conn.calls == []


@pytest.mark.asyncio
async def test_complete_persists_outcome_then_deletes_lease():
    lease = _lease_row(job_id="job-1")
    outcome = {
        "task_id": "task-1",
        "pool": "ssh-gateway/sshd",
        "job_id": "job-1",
        "status": "needs-review",
        "exit_code": 0,
        "result_json": {"artifact": "ok"},
        "reported_at": NOW,
    }
    conn = _FakeConn(
        fetchrows=[lease, outcome],
        execute_results=["INSERT 0 1", "DELETE 1"],
    )
    result = await _state(conn).complete_task(
        task_id="task-1",
        lease_token=lease["lease_token"],
        status="needs-review",
        exit_code=0,
        result={"artifact": "ok"},
        expected_job_id="job-1",
    )

    assert result.status == "needs-review"
    assert result.result == {"artifact": "ok"}
    writes = [(sql, args) for kind, sql, args in conn.calls if kind == "execute"]
    assert writes[0][0] == fleet_state_module._UPSERT_OUTCOME_SQL
    assert writes[1][0] == fleet_state_module._DELETE_LEASE_SQL


@pytest.mark.asyncio
async def test_complete_is_idempotent_after_terminal_outcome_recorded():
    outcome = {
        "task_id": "task-1",
        "pool": "ssh-gateway/sshd",
        "job_id": "job-1",
        "status": "failed",
        "exit_code": 17,
        "result_json": {"reason": "worker"},
        "reported_at": NOW,
    }
    conn = _FakeConn(fetchrows=[None, outcome])
    result = await _state(conn).complete_task(
        task_id="task-1",
        lease_token="11111111-1111-1111-1111-111111111111",
        status="failed",
        exit_code=17,
    )
    assert result.status == "failed"
    assert result.job_id == "job-1"


@pytest.mark.asyncio
async def test_heartbeat_wrong_token_never_creates_or_releases_slot():
    conn = _FakeConn(fetchrows=[None])
    with pytest.raises(LeaseNotFoundError):
        await _state(conn).heartbeat(
            task_id="task-1",
            lease_token="22222222-2222-2222-2222-222222222222",
            coordinator_id="gpt-b",
        )
    assert all("DELETE" not in sql.upper() for _, sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_live_two_coordinators_never_over_admit_same_pool():
    """Optional real-Postgres concurrency check.

    Unit tests prove the SQL shape; this verifies the row lock actually
    serializes two independent coordinator connections. It is opt-in because
    the normal MCP postgres connector is deliberately read-only.
    """
    dsn = os.environ.get("FLEET_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("FLEET_TEST_PG_DSN not configured")

    suffix = uuid.uuid4().hex[:12]
    pool_name = f"test/fleet/{suffix}"
    task_a = f"task-a-{suffix}"
    task_b = f"task-b-{suffix}"
    state_a = FleetState(dsn)
    state_b = FleetState(dsn)
    try:
        await state_a.ensure_schema()
        await state_a.configure_pool(pool_name, capacity=1)
        result_a, result_b = await asyncio.gather(
            state_a.acquire_slot(
                pool_name=pool_name,
                task_id=task_a,
                coordinator_id="gpt-a",
                capacity=1,
            ),
            state_b.acquire_slot(
                pool_name=pool_name,
                task_id=task_b,
                coordinator_id="gpt-b",
                capacity=1,
            ),
        )
        assert sum(result.acquired for result in (result_a, result_b)) == 1
        snapshot = await state_a.get_pool_snapshot(pool_name, capacity=1)
        assert snapshot.active == 1
    finally:
        # Test-only cleanup is intentionally explicit SQL on the internal
        # write pool. Production FleetState exposes no age-based/reaper API.
        for state in (state_a, state_b):
            if state._pool is not None:
                async with state._pool.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM fleet_worker_lease WHERE pool = $1",
                        pool_name,
                    )
                    await conn.execute(
                        "DELETE FROM fleet_worker_pool WHERE pool = $1",
                        pool_name,
                    )
        await state_a.close()
        await state_b.close()
