"""Runtime wiring between MCP agent tools and durable FleetState admission.

The feature is deliberately opt-in.  The source tree can ship this module
before the gateway's durable submission backend is deployed; production only
sets ``MCP_AGENT_FLEET_ENABLED=1`` once both sides of the idempotency contract
are live.

Safety properties
-----------------
* Postgres admission happens before a worker submit.
* Repeated calls for one task reuse the same lease and the gateway's stable
  submission key; a bound lease returns its existing job without resubmitting.
* An ambiguous exception during HTTP submission NEVER releases the lease.  A
  later retry reuses the lease and the same gateway idempotency key instead of
  launching a second worker.
* Slots are released only after an authoritative terminal gateway result or a
  definite pre-submit terminal result.  There is no heartbeat-age reaper.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Callable
from typing import Any, Final

from examples.mcp_server.agent_paths import project_state_key
from examples.mcp_server.fleet_state import (
    DEFAULT_POOL_CAPACITY,
    FleetState,
    TaskAlreadyTerminalError,
)

_ENABLED_ENV: Final = "MCP_AGENT_FLEET_ENABLED"
_DSN_ENV: Final = "MCP_FLEET_DATABASE_URL"
_POOL_ENV: Final = "MCP_AGENT_FLEET_POOL"
_CAPACITY_ENV: Final = "MCP_AGENT_FLEET_CAPACITY"
_COORDINATOR_ENV: Final = "MCP_AGENT_COORDINATOR_ID"
_DEFAULT_POOL: Final = "ssh-gateway/sshd"
_GATEWAY_TERMINAL: Final[frozenset[str]] = frozenset({"completed", "failed", "cancelled"})
_PRE_SUBMIT_TERMINAL: Final[frozenset[str]] = frozenset(
    {"needs-review", "completed", "failed", "cancelled", "rate-limited", "blocked", "error"}
)


class FleetRuntimeError(RuntimeError):
    """Fleet runtime configuration or coordination failure."""


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise FleetRuntimeError(f"{name} must be a boolean flag")


def _normalize_asyncpg_dsn(value: str) -> str:
    value = value.strip()
    if value.startswith("postgresql+asyncpg://"):
        return "postgresql://" + value[len("postgresql+asyncpg://") :]
    return value


def _configured_dsn() -> str:
    raw = os.environ.get(_DSN_ENV, "").strip() or os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        raise FleetRuntimeError(
            f"{_DSN_ENV} or DATABASE_URL is required when {_ENABLED_ENV}=1"
        )
    return _normalize_asyncpg_dsn(raw)


def _configured_capacity() -> int:
    raw = os.environ.get(_CAPACITY_ENV, str(DEFAULT_POOL_CAPACITY)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise FleetRuntimeError(f"{_CAPACITY_ENV} must be a positive integer") from exc
    if value <= 0:
        raise FleetRuntimeError(f"{_CAPACITY_ENV} must be a positive integer")
    return value


def _configured_coordinator_id() -> str:
    explicit = os.environ.get(_COORDINATOR_ENV, "").strip()
    if explicit:
        return explicit
    return f"{socket.gethostname()}:{os.getpid()}"


def fleet_task_id(project: str, task_id: str) -> str:
    """Build one globally stable task key without exposing host paths."""
    value = f"{project_state_key(project)}:{task_id}"
    if len(value) > 200:
        raise FleetRuntimeError("fleet task identity exceeds 200 characters")
    return value


def _small_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep durable outcomes useful without copying large worker output."""
    summary: dict[str, Any] = {}
    for key in ("status", "exit_code", "job_id", "error", "finished_at"):
        if key in result:
            value = result[key]
            if isinstance(value, str) and len(value) > 500:
                value = value[:500]
            summary[key] = value
    return summary


class FleetRuntime:
    """One event-loop-owned coordinator around a durable :class:`FleetState`."""

    def __init__(
        self,
        state: FleetState,
        *,
        pool_name: str,
        capacity: int,
        coordinator_id: str,
    ) -> None:
        self.state = state
        self.pool_name = pool_name
        self.capacity = capacity
        self.coordinator_id = coordinator_id
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            await self.state.ensure_schema()
            self._schema_ready = True

    async def submit(
        self,
        *,
        project: str,
        task_id: str,
        submit_sync: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Admit then perform one idempotent gateway submission.

        ``submit_sync`` is the existing blocking MCP agent implementation.  It
        executes in a worker thread so Postgres stays on the FastMCP event loop.
        Any exception from that blocking submit is intentionally allowed to
        propagate while the lease remains active: the exception is ambiguous
        evidence and therefore cannot authorize a release.
        """
        await self.ensure_ready()
        durable_task_id = fleet_task_id(project, task_id)
        try:
            admission = await self.state.acquire_slot(
                pool_name=self.pool_name,
                task_id=durable_task_id,
                coordinator_id=self.coordinator_id,
                capacity=self.capacity,
            )
        except TaskAlreadyTerminalError as exc:
            outcome = await self.state.get_outcome(durable_task_id)
            return {
                "task_id": task_id,
                "status": "blocked",
                "error": str(exc),
                "fleet": {
                    "pool": self.pool_name,
                    "terminal": True,
                    "job_id": outcome.job_id if outcome else None,
                    "terminal_status": outcome.status if outcome else None,
                },
            }

        if not admission.acquired or admission.lease is None:
            return {
                "task_id": task_id,
                "status": "blocked",
                "error": "Fleet worker pool is at capacity",
                "fleet": {
                    "pool": self.pool_name,
                    "capacity": admission.capacity,
                    "active": admission.active,
                },
            }

        lease = admission.lease
        if lease.job_id:
            return {
                "task_id": task_id,
                "status": "running",
                "job_id": lease.job_id,
                "exit_code": None,
                "finished_at": None,
                "fleet": {
                    "pool": lease.pool,
                    "existing_lease": True,
                    "active": admission.active,
                    "capacity": admission.capacity,
                },
            }

        result = await asyncio.to_thread(submit_sync)
        job_id = result.get("job_id") if isinstance(result, dict) else None
        if isinstance(job_id, str) and job_id:
            bound = await self.state.bind_job(
                task_id=durable_task_id,
                lease_token=lease.lease_token,
                job_id=job_id,
            )
            result = dict(result)
            result["fleet"] = {
                "pool": bound.pool,
                "existing_lease": admission.existing,
                "active": admission.active,
                "capacity": admission.capacity,
            }
            return result

        status = str(result.get("status") or "") if isinstance(result, dict) else ""
        if status in _PRE_SUBMIT_TERMINAL:
            await self.state.complete_task(
                task_id=durable_task_id,
                lease_token=lease.lease_token,
                status=status,
                exit_code=result.get("exit_code") if isinstance(result, dict) else None,
                result=_small_result(result if isinstance(result, dict) else {}),
            )
            result = dict(result)
            result["fleet"] = {
                "pool": lease.pool,
                "released": True,
                "reason": "definite pre-submit terminal result",
            }
            return result

        raise FleetRuntimeError(
            "Agent submit returned neither a job_id nor a terminal pre-submit status"
        )

    async def reconcile_gateway_result(
        self,
        *,
        job_id: str,
        result: dict[str, Any],
    ) -> None:
        """Release a bound lease only when gateway reports a terminal job."""
        status = str(result.get("status") or "")
        if status not in _GATEWAY_TERMINAL:
            return
        await self.ensure_ready()
        lease = await self.state.get_lease_by_job(job_id)
        if lease is None:
            return
        exit_code = result.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            exit_code = None
        await self.state.complete_task(
            task_id=lease.task_id,
            lease_token=lease.lease_token,
            status=status,
            exit_code=exit_code,
            result=_small_result(result),
            expected_job_id=job_id,
        )


_runtime: FleetRuntime | None = None
_runtime_lock: asyncio.Lock | None = None


def fleet_enabled() -> bool:
    return _env_flag(_ENABLED_ENV, default=False)


async def get_fleet_runtime() -> FleetRuntime | None:
    """Return the process singleton when fleet admission is enabled."""
    global _runtime, _runtime_lock
    if not fleet_enabled():
        return None
    if _runtime is not None:
        return _runtime
    if _runtime_lock is None:
        _runtime_lock = asyncio.Lock()
    async with _runtime_lock:
        if _runtime is None:
            pool_name = os.environ.get(_POOL_ENV, _DEFAULT_POOL).strip() or _DEFAULT_POOL
            _runtime = FleetRuntime(
                FleetState(_configured_dsn()),
                pool_name=pool_name,
                capacity=_configured_capacity(),
                coordinator_id=_configured_coordinator_id(),
            )
        return _runtime


async def close_fleet_runtime() -> None:
    """Close the asyncpg pool during MCP shutdown/tests."""
    global _runtime, _runtime_lock
    if _runtime is not None:
        await _runtime.state.close()
    _runtime = None
    _runtime_lock = None


__all__ = [
    "FleetRuntime",
    "FleetRuntimeError",
    "close_fleet_runtime",
    "fleet_enabled",
    "fleet_task_id",
    "get_fleet_runtime",
]
