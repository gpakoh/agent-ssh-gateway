"""Real-Postgres regressions for fleet lease recovery (corrective r2).

These tests exercise the durable submission-state model (``legacy_unknown`` /
``never_attempted`` / ``attempted``) and the migration safety against a live
Postgres. They are skipped unless ``FLEET_TEST_PG_DSN`` is set, matching the
existing ``test_fleet_state.py`` convention.
"""

from __future__ import annotations

import os
import uuid

import pytest

import examples.mcp_server.fleet_state as fleet_state_module
from examples.mcp_server.fleet_state import FleetState, LeaseNotFoundError

_OLD_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS fleet_worker_pool (
    pool TEXT PRIMARY KEY,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fleet_worker_lease (
    task_id TEXT PRIMARY KEY,
    pool TEXT NOT NULL REFERENCES fleet_worker_pool(pool) ON DELETE RESTRICT,
    lease_token UUID NOT NULL UNIQUE,
    coordinator_id TEXT NOT NULL,
    job_id TEXT UNIQUE,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fleet_task_outcome (
    task_id TEXT PRIMARY KEY,
    pool TEXT NOT NULL,
    job_id TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    result_json JSONB,
    reported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _pg_dsn() -> str:
    dsn = os.environ.get("FLEET_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("FLEET_TEST_PG_DSN not configured")
    return dsn


@pytest.fixture
async def schema_ready():
    dsn = _pg_dsn()
    state = FleetState(dsn)
    await state.ensure_schema()
    yield state
    if state._pool is not None:
        async with state._pool.acquire() as conn:
            await conn.execute("DELETE FROM fleet_worker_lease")
            await conn.execute("DELETE FROM fleet_task_outcome")
            await conn.execute("DELETE FROM fleet_worker_pool")
    await state.close()


class TestMigrationSemantics:
    """A pre-existing in-flight unbound lease must never be reclaimed."""

    async def _seed_old_schema_and_legacy_inflight(self, conn, pool_name, task_id, token):
        # Apply the pre-column schema, then insert a legacy *unbound* row that
        # represents a coordinator crash after a gateway dispatch but before
        # bind: this job really is in flight on the gateway.
        await conn.execute(
            "DROP TABLE IF EXISTS fleet_worker_lease CASCADE"
        )
        await conn.execute(
            "DROP TABLE IF EXISTS fleet_task_outcome CASCADE"
        )
        await conn.execute("DROP TABLE IF EXISTS fleet_worker_pool CASCADE")
        await conn.execute(_OLD_SCHEMA)
        await conn.execute(
            "INSERT INTO fleet_worker_pool(pool, capacity) VALUES($1, 2)",
            pool_name,
        )
        await conn.execute(
            """
            INSERT INTO fleet_worker_lease(
                task_id, pool, lease_token, coordinator_id
            ) VALUES($1, $2, $3::uuid, 'legacy-coord')
            """,
            task_id,
            pool_name,
            token,
        )

    async def test_legacy_inflight_unbound_row_is_not_reclaimed(
        self, schema_ready
    ):
        pool_name = f"test/r2/mig/{uuid.uuid4().hex[:8]}"
        task_id = f"legacy-inflight-{uuid.uuid4().hex[:8]}"
        token = str(uuid.uuid4())
        async with schema_ready._pool.acquire() as conn:
            await self._seed_old_schema_and_legacy_inflight(
                conn, pool_name, task_id, token
            )
            # production migration
            await conn.execute(fleet_state_module.SCHEMA_SQL)
            row = await conn.fetchrow(
                "SELECT submit_state, submit_attempted_at FROM fleet_worker_lease "
                "WHERE task_id = $1",
                task_id,
            )
            assert row["submit_state"] == "legacy_unknown", (
                "pre-existing row must be legacy_unknown, never never_attempted"
            )
            assert row["submit_attempted_at"] is None

        # A NEW acquire after migration must be explicitly never_attempted.
        fresh = await schema_ready.acquire_slot(
            pool_name=pool_name,
            task_id=f"fresh-{uuid.uuid4().hex[:8]}",
            coordinator_id="r2-coord",
            capacity=2,
        )
        assert fresh.acquired is True
        assert fresh.lease is not None
        assert fresh.lease.submit_state == "never_attempted"

        # Unbound reconcile must NOT reclaim the legacy_unknown in-flight row.
        unbound = await schema_ready.list_unbound_leases(pool_name=pool_name)
        legacy_tasks = {
            lease.task_id
            for lease in unbound
            if lease.submit_state == "legacy_unknown"
        }
        assert task_id in legacy_tasks
        # The legacy_unknown row must be refused by the release guard.
        released = await schema_ready.release_never_dispatched(
            task_id=task_id,
            lease_token=token,
        )
        assert released is False, (
            "legacy_unknown inflight row must never be released automatically"
        )
        # A fresh never_attempted row IS reclaimable (the actual leak fix).
        fresh_rows = [
            lease
            for lease in unbound
            if lease.submit_state == "never_attempted"
        ]
        assert fresh_rows, "expected a fresh never_attempted unbound row"
        for lease in fresh_rows:
            gone = await schema_ready.release_never_dispatched(
                task_id=lease.task_id,
                lease_token=lease.lease_token,
            )
            assert gone is True
        after = await schema_ready.list_unbound_leases(pool_name=pool_name)
        assert task_id in {lease.task_id for lease in after}, (
            "legacy_unknown in-flight row must survive reconcile"
        )
        assert not {
            lease.task_id
            for lease in after
            if lease.submit_state == "never_attempted"
        }, "never_attempted rows must be reclaimable"

    async def test_marked_attempted_unbound_row_is_not_released(
        self, schema_ready
    ):
        pool_name = f"test/r2/attempt/{uuid.uuid4().hex[:8]}"
        task_id = f"attempted-{uuid.uuid4().hex[:8]}"
        token = str(uuid.uuid4())
        async with schema_ready._pool.acquire() as conn:
            await self._seed_old_schema_and_legacy_inflight(
                conn, pool_name, task_id, token
            )
            await conn.execute(fleet_state_module.SCHEMA_SQL)
            # Simulate a fresh acquire + transition to attempted.
            await conn.execute(
                """
                UPDATE fleet_worker_lease
                SET submit_state = 'never_attempted'
                WHERE task_id = $1
                """,
                task_id,
            )
            marked = await conn.fetchrow(
                fleet_state_module._MARK_SUBMIT_ATTEMPTED_SQL,
                task_id,
                token,
            )
            assert marked is not None
            assert marked["submit_state"] == "attempted"
            assert marked["submit_attempted_at"] is not None
        # The attempted unbound row must never be released by the guard.
        released = await schema_ready.release_never_dispatched(
            task_id=task_id, lease_token=token
        )
        assert released is False, "attempted unbound row must be retained"
        row = await schema_ready.get_lease(task_id)
        assert row is not None and row.submit_state == "attempted"


class TestRaceFailClosed:
    """acquire(never_attempted) -> reconcile deletes -> mark 0 rows -> no submit."""

    async def test_mark_after_concurrent_release_gets_zero_rows(
        self, schema_ready
    ):
        dsn = _pg_dsn()
        pool_name = f"test/r2/race/{uuid.uuid4().hex[:8]}"
        admission = await schema_ready.acquire_slot(
            pool_name=pool_name,
            task_id=f"race-{uuid.uuid4().hex[:8]}",
            coordinator_id="r2-coord",
            capacity=2,
        )
        assert admission.acquired and admission.lease is not None
        token = admission.lease.lease_token
        task_id = admission.lease.task_id
        assert admission.lease.submit_state == "never_attempted"

        state = FleetState(dsn)
        await state.ensure_schema()
        try:
            # Concurrent reconcile reclaims it (never_attempted, in pool scope).
            reclaimed = await state.release_never_dispatched(
                task_id=task_id, lease_token=token
            )
            assert reclaimed is True
            # The marker transition must now match 0 rows -> fail closed.
            with pytest.raises(
                LeaseNotFoundError, match="lease not found or token mismatch"
            ):
                await state.mark_submit_attempted(
                    task_id=task_id, lease_token=token
                )
        finally:
            await state.close()
