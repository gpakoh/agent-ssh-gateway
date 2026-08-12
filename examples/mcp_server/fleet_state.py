"""Durable multi-coordinator fleet admission state backed by Postgres.

This module is intentionally independent from the read-only Postgres MCP
adapter. Fleet coordination is control-plane state and needs transactional
writes; user-facing ``postgres_*`` tools must remain read-only.

Core invariants
---------------
* Lease rows are the source of truth for active capacity. There is no mutable
  ``active`` counter to drift or double-decrement.
* Admission is serialized per pool with ``SELECT ... FOR UPDATE`` and counts
  committed lease rows inside the same transaction.
* ``task_id`` is globally unique. Re-acquiring the same task is idempotent and
  never consumes a second slot.
* A random ``lease_token`` guards bind/heartbeat/complete operations against a
  stale coordinator releasing or mutating a newer lease.
* This layer has deliberately NO age-only stale reaper. A heartbeat timestamp
  is diagnostic evidence, not proof a gateway-owned worker is dead. Recovery
  must first prove job/process liveness elsewhere and then call a terminal
  completion/reconciliation operation.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import asyncpg

DEFAULT_POOL_CAPACITY: Final = 2
MAX_NAME_LENGTH: Final = 200
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"needs-review", "completed", "failed", "cancelled", "rate-limited", "resource-exhausted", "blocked", "error"}
)

SCHEMA_SQL: Final = """
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

CREATE INDEX IF NOT EXISTS idx_fleet_worker_lease_pool
    ON fleet_worker_lease(pool);

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

_POOL_INSERT_SQL: Final = """
INSERT INTO fleet_worker_pool(pool, capacity, updated_at)
VALUES($1, $2, now())
ON CONFLICT (pool) DO NOTHING
"""

_POOL_UPDATE_CAPACITY_SQL: Final = """
UPDATE fleet_worker_pool
SET capacity = $2, updated_at = now()
WHERE pool = $1
"""

_POOL_LOCK_SQL: Final = """
SELECT capacity
FROM fleet_worker_pool
WHERE pool = $1
FOR UPDATE
"""

_GET_LEASE_SQL: Final = """
SELECT task_id, pool, lease_token::text AS lease_token, coordinator_id,
       job_id, claimed_at, heartbeat_at
FROM fleet_worker_lease
WHERE task_id = $1
"""

_GET_LEASE_FOR_UPDATE_SQL: Final = _GET_LEASE_SQL + " FOR UPDATE"

_COUNT_LEASES_SQL: Final = "SELECT count(*) FROM fleet_worker_lease WHERE pool = $1"

_INSERT_LEASE_SQL: Final = """
INSERT INTO fleet_worker_lease(task_id, pool, lease_token, coordinator_id)
VALUES($1, $2, $3::uuid, $4)
ON CONFLICT (task_id) DO NOTHING
RETURNING task_id, pool, lease_token::text AS lease_token, coordinator_id,
          job_id, claimed_at, heartbeat_at
"""

_BIND_JOB_SQL: Final = """
UPDATE fleet_worker_lease
SET job_id = $3, heartbeat_at = now()
WHERE task_id = $1 AND lease_token = $2::uuid AND job_id IS NULL
RETURNING task_id, pool, lease_token::text AS lease_token, coordinator_id,
          job_id, claimed_at, heartbeat_at
"""

_HEARTBEAT_SQL: Final = """
UPDATE fleet_worker_lease
SET heartbeat_at = now(), coordinator_id = $3
WHERE task_id = $1 AND lease_token = $2::uuid
RETURNING heartbeat_at
"""

_UPSERT_OUTCOME_SQL: Final = """
INSERT INTO fleet_task_outcome(
    task_id, pool, job_id, status, exit_code, result_json, reported_at
)
VALUES($1, $2, $3, $4, $5, $6::jsonb, now())
ON CONFLICT (task_id) DO UPDATE
SET pool = EXCLUDED.pool,
    job_id = EXCLUDED.job_id,
    status = EXCLUDED.status,
    exit_code = EXCLUDED.exit_code,
    result_json = EXCLUDED.result_json,
    reported_at = now()
"""

_DELETE_LEASE_SQL: Final = """
DELETE FROM fleet_worker_lease
WHERE task_id = $1 AND lease_token = $2::uuid
"""

_GET_OUTCOME_SQL: Final = """
SELECT task_id, pool, job_id, status, exit_code, result_json, reported_at
FROM fleet_task_outcome
WHERE task_id = $1
"""

_GET_LEASE_BY_JOB_SQL: Final = """
SELECT task_id, pool, lease_token::text AS lease_token, coordinator_id,
       job_id, claimed_at, heartbeat_at
FROM fleet_worker_lease
WHERE job_id = $1
"""


class FleetStateError(RuntimeError):
    """Base error for durable fleet state operations."""


class LeaseConflictError(FleetStateError):
    """The task/job is already associated with a conflicting lease."""


class LeaseNotFoundError(FleetStateError):
    """No lease exists for the requested task/token pair."""


class PoolCapacityMismatchError(FleetStateError):
    """Coordinators disagree about the configured capacity for one pool."""


class TaskAlreadyTerminalError(FleetStateError):
    """The task_id already has a durable terminal outcome and cannot be re-admitted."""


@dataclass(frozen=True)
class WorkerLease:
    task_id: str
    pool: str
    lease_token: str
    coordinator_id: str
    job_id: str | None
    claimed_at: datetime | None
    heartbeat_at: datetime | None


@dataclass(frozen=True)
class AdmissionResult:
    acquired: bool
    existing: bool
    capacity: int
    active: int
    lease: WorkerLease | None


@dataclass(frozen=True)
class PoolSnapshot:
    pool: str
    capacity: int
    active: int


@dataclass(frozen=True)
class TaskOutcome:
    task_id: str
    pool: str
    job_id: str | None
    status: str
    exit_code: int | None
    result: Any
    reported_at: datetime | None


def _require_name(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    value = value.strip()
    if not value or len(value) > MAX_NAME_LENGTH or "\x00" in value:
        raise ValueError(f"{label} must be 1..{MAX_NAME_LENGTH} non-NUL characters")
    return value


def _require_capacity(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("capacity must be a positive integer")
    return value


def _lease_from_row(row: Any) -> WorkerLease:
    return WorkerLease(
        task_id=row["task_id"],
        pool=row["pool"],
        lease_token=str(row["lease_token"]),
        coordinator_id=row["coordinator_id"],
        job_id=row["job_id"],
        claimed_at=row.get("claimed_at"),
        heartbeat_at=row.get("heartbeat_at"),
    )


class FleetState:
    """Transactional Postgres store for shared worker admission and outcomes."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        min_size: int = 1,
        max_size: int = 5,
        command_timeout: float = 10.0,
    ) -> None:
        if dsn is not None and not dsn.strip():
            raise ValueError("dsn must be non-empty when provided")
        # asyncpg deliberately accepts dsn=None and then resolves the standard
        # PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD environment variables.
        # FleetRuntime validates that the required PG* identity fields exist
        # before choosing this path, so None here is explicit configuration,
        # not an accidental localhost fallback.
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._command_timeout = command_timeout
        self._pool: asyncpg.Pool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
                command_timeout=self._command_timeout,
                server_settings={"statement_timeout": str(int(self._command_timeout * 1000))},
            )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ensure_schema(self) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)

    async def acquire_slot(
        self,
        *,
        pool_name: str,
        task_id: str,
        coordinator_id: str,
        capacity: int = DEFAULT_POOL_CAPACITY,
    ) -> AdmissionResult:
        """Atomically admit one task or return a full-pool result.

        Repeated admission of the same global task_id returns the existing
        lease and never consumes another slot. No stale rows are reaped here.
        """
        pool_name = _require_name(pool_name, "pool_name")
        task_id = _require_name(task_id, "task_id")
        coordinator_id = _require_name(coordinator_id, "coordinator_id")
        capacity = _require_capacity(capacity)

        pg_pool = await self._ensure_pool()
        async with pg_pool.acquire() as conn, conn.transaction():
            await conn.execute(_POOL_INSERT_SQL, pool_name, capacity)
            pool_row = await conn.fetchrow(_POOL_LOCK_SQL, pool_name)
            if pool_row is None:  # pragma: no cover - impossible after insert
                raise FleetStateError(f"pool row disappeared: {pool_name}")
            effective_capacity = int(pool_row["capacity"])
            if effective_capacity != capacity:
                raise PoolCapacityMismatchError(
                    f"pool {pool_name!r} capacity is {effective_capacity}, "
                    f"coordinator requested {capacity}"
                )

            existing = await conn.fetchrow(_GET_LEASE_SQL, task_id)
            if existing is not None:
                lease = _lease_from_row(existing)
                if lease.pool != pool_name:
                    raise LeaseConflictError(
                        f"task {task_id!r} already leased in pool {lease.pool!r}"
                    )
                active = int(await conn.fetchval(_COUNT_LEASES_SQL, pool_name))
                return AdmissionResult(True, True, effective_capacity, active, lease)

            terminal = await conn.fetchrow(_GET_OUTCOME_SQL, task_id)
            if terminal is not None:
                raise TaskAlreadyTerminalError(
                    f"task {task_id!r} already has terminal status {terminal['status']!r}"
                )

            active = int(await conn.fetchval(_COUNT_LEASES_SQL, pool_name))
            if active >= effective_capacity:
                return AdmissionResult(False, False, effective_capacity, active, None)

            token = str(uuid.uuid4())
            inserted = await conn.fetchrow(
                _INSERT_LEASE_SQL,
                task_id,
                pool_name,
                token,
                coordinator_id,
            )
            if inserted is None:
                # A task_id can race across different pool rows. The global PK
                # is the final authority; fetch the winner and return/raise
                # without consuming capacity in this pool.
                winner = await conn.fetchrow(_GET_LEASE_SQL, task_id)
                if winner is None:  # pragma: no cover - would indicate DB corruption
                    raise FleetStateError("task lease insert lost without a visible winner")
                lease = _lease_from_row(winner)
                if lease.pool != pool_name:
                    raise LeaseConflictError(
                        f"task {task_id!r} concurrently leased in pool {lease.pool!r}"
                    )
                active = int(await conn.fetchval(_COUNT_LEASES_SQL, pool_name))
                return AdmissionResult(True, True, effective_capacity, active, lease)

            return AdmissionResult(
                True,
                False,
                effective_capacity,
                active + 1,
                _lease_from_row(inserted),
            )

    async def bind_job(self, *, task_id: str, lease_token: str, job_id: str) -> WorkerLease:
        """Bind a gateway job exactly once to an admitted task lease."""
        task_id = _require_name(task_id, "task_id")
        lease_token = _require_name(lease_token, "lease_token")
        job_id = _require_name(job_id, "job_id")

        pg_pool = await self._ensure_pool()
        async with pg_pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow(_GET_LEASE_FOR_UPDATE_SQL, task_id)
            if current is None:
                raise LeaseNotFoundError(f"no lease for task {task_id!r}")
            lease = _lease_from_row(current)
            if lease.lease_token != lease_token:
                raise LeaseConflictError("lease token mismatch")
            if lease.job_id is not None:
                if lease.job_id == job_id:
                    return lease
                raise LeaseConflictError(
                    f"task {task_id!r} already bound to job {lease.job_id!r}"
                )
            try:
                updated = await conn.fetchrow(_BIND_JOB_SQL, task_id, lease_token, job_id)
            except asyncpg.UniqueViolationError as exc:
                raise LeaseConflictError(f"job_id {job_id!r} is already bound") from exc
            if updated is None:
                raise LeaseConflictError("lease changed while binding job")
            return _lease_from_row(updated)

    async def heartbeat(
        self,
        *,
        task_id: str,
        lease_token: str,
        coordinator_id: str,
    ) -> datetime | None:
        task_id = _require_name(task_id, "task_id")
        lease_token = _require_name(lease_token, "lease_token")
        coordinator_id = _require_name(coordinator_id, "coordinator_id")
        pg_pool = await self._ensure_pool()
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                _HEARTBEAT_SQL,
                task_id,
                lease_token,
                coordinator_id,
            )
        if row is None:
            raise LeaseNotFoundError("lease not found or token mismatch")
        return row.get("heartbeat_at")

    async def complete_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        status: str,
        exit_code: int | None = None,
        result: Any = None,
        expected_job_id: str | None = None,
    ) -> TaskOutcome:
        """Persist terminal outcome and release the slot atomically.

        Callers must first establish terminal worker/job state. This method
        intentionally accepts only terminal statuses and has no timeout-based
        force-release path.
        """
        task_id = _require_name(task_id, "task_id")
        lease_token = _require_name(lease_token, "lease_token")
        status = _require_name(status, "status")
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"status {status!r} is not terminal")
        if expected_job_id is not None:
            expected_job_id = _require_name(expected_job_id, "expected_job_id")
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
            raise ValueError("exit_code must be an integer or None")
        try:
            result_json = json.dumps(result, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("result must be JSON-serializable") from exc

        pg_pool = await self._ensure_pool()
        async with pg_pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(_GET_LEASE_FOR_UPDATE_SQL, task_id)
            if row is None:
                existing = await conn.fetchrow(_GET_OUTCOME_SQL, task_id)
                if existing is not None and existing["status"] == status:
                    return TaskOutcome(
                        task_id=existing["task_id"],
                        pool=existing["pool"],
                        job_id=existing["job_id"],
                        status=existing["status"],
                        exit_code=existing["exit_code"],
                        result=existing["result_json"],
                        reported_at=existing.get("reported_at"),
                    )
                raise LeaseNotFoundError(f"no active lease for task {task_id!r}")
            lease = _lease_from_row(row)
            if lease.lease_token != lease_token:
                raise LeaseConflictError("lease token mismatch")
            if expected_job_id is not None and lease.job_id != expected_job_id:
                raise LeaseConflictError(
                    f"job mismatch: expected {expected_job_id!r}, lease has {lease.job_id!r}"
                )

            await conn.execute(
                _UPSERT_OUTCOME_SQL,
                task_id,
                lease.pool,
                lease.job_id,
                status,
                exit_code,
                result_json,
            )
            deleted = await conn.execute(_DELETE_LEASE_SQL, task_id, lease_token)
            if deleted != "DELETE 1":
                raise LeaseConflictError("lease changed before terminal release")
            outcome_row = await conn.fetchrow(_GET_OUTCOME_SQL, task_id)
            if outcome_row is None:  # pragma: no cover - impossible after upsert
                raise FleetStateError("terminal outcome disappeared after upsert")
            return TaskOutcome(
                task_id=outcome_row["task_id"],
                pool=outcome_row["pool"],
                job_id=outcome_row["job_id"],
                status=outcome_row["status"],
                exit_code=outcome_row["exit_code"],
                result=outcome_row["result_json"],
                reported_at=outcome_row.get("reported_at"),
            )

    async def get_lease(self, task_id: str) -> WorkerLease | None:
        task_id = _require_name(task_id, "task_id")
        pg_pool = await self._ensure_pool()
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(_GET_LEASE_SQL, task_id)
        return _lease_from_row(row) if row is not None else None

    async def get_lease_by_job(self, job_id: str) -> WorkerLease | None:
        """Return the active lease bound to a gateway job, if any."""
        job_id = _require_name(job_id, "job_id")
        pg_pool = await self._ensure_pool()
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(_GET_LEASE_BY_JOB_SQL, job_id)
        return _lease_from_row(row) if row is not None else None

    async def get_outcome(self, task_id: str) -> TaskOutcome | None:
        """Return a durable terminal outcome without mutating fleet state."""
        task_id = _require_name(task_id, "task_id")
        pg_pool = await self._ensure_pool()
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(_GET_OUTCOME_SQL, task_id)
        if row is None:
            return None
        return TaskOutcome(
            task_id=row["task_id"],
            pool=row["pool"],
            job_id=row["job_id"],
            status=row["status"],
            exit_code=row["exit_code"],
            result=row["result_json"],
            reported_at=row.get("reported_at"),
        )

    async def configure_pool(
        self,
        pool_name: str,
        *,
        capacity: int,
        expected_capacity: int | None = None,
    ) -> PoolSnapshot:
        """Create a pool or CAS-change its capacity.

        Blind capacity rewrites are forbidden once the row exists: callers
        changing an established pool must provide its expected current value.
        This prevents one stale coordinator configuration from silently
        overriding the shared admission limit.
        """
        pool_name = _require_name(pool_name, "pool_name")
        capacity = _require_capacity(capacity)
        if expected_capacity is not None:
            expected_capacity = _require_capacity(expected_capacity)

        pg_pool = await self._ensure_pool()
        async with pg_pool.acquire() as conn, conn.transaction():
            inserted = await conn.execute(_POOL_INSERT_SQL, pool_name, capacity)
            row = await conn.fetchrow(_POOL_LOCK_SQL, pool_name)
            if row is None:  # pragma: no cover
                raise FleetStateError(f"pool row disappeared: {pool_name}")
            current = int(row["capacity"])
            created = inserted == "INSERT 0 1"
            if current != capacity:
                if expected_capacity is None or current != expected_capacity:
                    raise PoolCapacityMismatchError(
                        f"pool {pool_name!r} capacity is {current}, expected "
                        f"{expected_capacity!r} before changing to {capacity}"
                    )
                await conn.execute(_POOL_UPDATE_CAPACITY_SQL, pool_name, capacity)
                current = capacity
            elif not created and expected_capacity is not None and current != expected_capacity:
                raise PoolCapacityMismatchError(
                    f"pool {pool_name!r} capacity is {current}, expected {expected_capacity}"
                )
            active = int(await conn.fetchval(_COUNT_LEASES_SQL, pool_name))
            return PoolSnapshot(pool_name, current, active)

    async def get_pool_snapshot(
        self,
        pool_name: str,
        *,
        capacity: int = DEFAULT_POOL_CAPACITY,
    ) -> PoolSnapshot:
        pool_name = _require_name(pool_name, "pool_name")
        capacity = _require_capacity(capacity)
        pg_pool = await self._ensure_pool()
        async with pg_pool.acquire() as conn, conn.transaction():
            await conn.execute(_POOL_INSERT_SQL, pool_name, capacity)
            row = await conn.fetchrow(_POOL_LOCK_SQL, pool_name)
            if row is None:  # pragma: no cover
                raise FleetStateError(f"pool row disappeared: {pool_name}")
            current = int(row["capacity"])
            if current != capacity:
                raise PoolCapacityMismatchError(
                    f"pool {pool_name!r} capacity is {current}, coordinator requested {capacity}"
                )
            active = int(await conn.fetchval(_COUNT_LEASES_SQL, pool_name))
            return PoolSnapshot(pool_name, current, active)


__all__ = [
    "AdmissionResult",
    "DEFAULT_POOL_CAPACITY",
    "FleetState",
    "FleetStateError",
    "LeaseConflictError",
    "LeaseNotFoundError",
    "PoolCapacityMismatchError",
    "PoolSnapshot",
    "TaskAlreadyTerminalError",
    "SCHEMA_SQL",
    "TERMINAL_STATUSES",
    "TaskOutcome",
    "WorkerLease",
]
