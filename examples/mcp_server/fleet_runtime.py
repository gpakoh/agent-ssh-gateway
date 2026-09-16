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
* Exactly one persistent reconciliation watcher runs per bound gateway
  job_id; it restores after coordinator restart via the pre-admission sweep
  and ends only on authoritative terminal reconciliation or runtime close.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, TypeVar

from examples.mcp_server.agent_paths import project_state_key
from examples.mcp_server.fleet_state import (
    ATTEMPTED,
    DEFAULT_POOL_CAPACITY,
    LEGACY_UNKNOWN,
    MIN_GENERATION_RECONCILIATION_QUIESCENCE_SECONDS,
    NEVER_ATTEMPTED,
    FleetState,
    TaskAlreadyTerminalError,
)

_T = TypeVar("_T")

_ENABLED_ENV: Final = "MCP_AGENT_FLEET_ENABLED"

_GATEWAY_EXECUTOR_SHUTDOWN_TIMEOUT_ENV: Final = "MCP_FLEET_EXECUTOR_SHUTDOWN_TIMEOUT"
_GATEWAY_EXECUTOR_SHUTDOWN_TIMEOUT_DEFAULT: Final = 10.0

logger = logging.getLogger("mcp_server.fleet_runtime")


def _gateway_executor_shutdown_timeout() -> float:
    """Bounded deadline for joining the gateway executor during close (seconds).

    A sync gateway worker thread blocked on I/O cannot be interrupted by asyncio
    cancellation; bounded join forces close() to give up and keep the process
    shutdown moving. Env-tunable for operators; tests override it to keep the
    slow path fast.
    """
    raw = os.environ.get(_GATEWAY_EXECUTOR_SHUTDOWN_TIMEOUT_ENV, "").strip()
    if not raw:
        return _GATEWAY_EXECUTOR_SHUTDOWN_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"{_GATEWAY_EXECUTOR_SHUTDOWN_TIMEOUT_ENV} must be a number"
        ) from None
    if value <= 0:
        raise ValueError(
            f"{_GATEWAY_EXECUTOR_SHUTDOWN_TIMEOUT_ENV} must be positive"
        )
    return value
_DSN_ENV: Final = "MCP_FLEET_DATABASE_URL"
_POOL_ENV: Final = "MCP_AGENT_FLEET_POOL"
_CAPACITY_ENV: Final = "MCP_AGENT_FLEET_CAPACITY"
_COORDINATOR_ENV: Final = "MCP_AGENT_COORDINATOR_ID"
_CAPACITY_RETRY_AFTER_ENV: Final = "MCP_AGENT_FLEET_CAPACITY_RETRY_AFTER_SECONDS"
_DEFAULT_POOL: Final = "ssh-gateway/agent-sshd"
_DEFAULT_GATEWAY_IO_CONCURRENCY: Final = 4
_DEFAULT_CAPACITY_RETRY_AFTER_SECONDS: Final = 60
_MAX_OBSERVED_TERMINAL_JOBS: Final = 4096
_GATEWAY_TERMINAL: Final[frozenset[str]] = frozenset(
    {"completed", "failed", "cancelled", "ambiguous"}
)
_MISSING_JOB_ERROR_CODE: Final = "JOB_NOT_FOUND"
_PRE_SUBMIT_TERMINAL: Final[frozenset[str]] = frozenset(
    {
        "needs-review",
        "completed",
        "failed",
        "cancelled",
        "ambiguous",
        "rate-limited",
        "startup-timeout",
        "run-timeout",
        "resource-exhausted",
        "blocked",
        "error",
    }
)


class FleetRuntimeError(RuntimeError):
    """Fleet runtime configuration or coordination failure."""


@dataclass(frozen=True)
class ExecutionPlaneRecoveryBoundary:
    """Trusted replacement-generation evidence for historical unbound leases."""

    gateway_started_at: datetime
    executor_started_at: datetime
    mcp_started_at: datetime
    observed_at: datetime
    gateway_generation: str
    executor_generation: str
    mcp_generation: str
    quiescence_seconds: int = MIN_GENERATION_RECONCILIATION_QUIESCENCE_SECONDS


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


def _configured_dsn() -> str | None:
    raw = os.environ.get(_DSN_ENV, "").strip() or os.environ.get("DATABASE_URL", "").strip()
    if raw:
        return _normalize_asyncpg_dsn(raw)
    required = ("PGHOST", "PGDATABASE", "PGUSER")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise FleetRuntimeError(
            f"{_DSN_ENV}, DATABASE_URL, or PGHOST/PGDATABASE/PGUSER is required "
            f"when {_ENABLED_ENV}=1 (missing: {', '.join(missing)})"
        )
    return None


def _configured_capacity() -> int:
    raw = os.environ.get(_CAPACITY_ENV, str(DEFAULT_POOL_CAPACITY)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise FleetRuntimeError(f"{_CAPACITY_ENV} must be a positive integer") from exc
    if value <= 0:
        raise FleetRuntimeError(f"{_CAPACITY_ENV} must be a positive integer")
    return value


def _configured_capacity_retry_after_seconds() -> int:
    raw = os.environ.get(
        _CAPACITY_RETRY_AFTER_ENV,
        str(_DEFAULT_CAPACITY_RETRY_AFTER_SECONDS),
    ).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise FleetRuntimeError(
            f"{_CAPACITY_RETRY_AFTER_ENV} must be a positive integer"
        ) from exc
    if value <= 0:
        raise FleetRuntimeError(f"{_CAPACITY_RETRY_AFTER_ENV} must be a positive integer")
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
    for key in (
        "status",
        "exit_code",
        "job_id",
        "error",
        "error_code",
        "finished_at",
        "liveness_reconciled",
    ):
        if key in result:
            value = result[key]
            if isinstance(value, str) and len(value) > 500:
                value = value[:500]
            summary[key] = value
    progress = result.get("progress")
    if isinstance(progress, dict):
        for key in ("locally_interrupted", "cancellation_outcome"):
            if key in progress:
                summary[key] = progress[key]
    return summary


def _gateway_error_code(exc: Exception) -> str | None:
    """Extract a gateway machine error code without importing the MCP error layer."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            return detail["code"]
        if isinstance(body.get("code"), str):
            return body["code"]
    text = str(exc)
    if _MISSING_JOB_ERROR_CODE in text:
        return _MISSING_JOB_ERROR_CODE
    return None


def _gateway_missing_job_result(job_id: str, exc: Exception) -> dict[str, Any] | None:
    """Convert authoritative missing-job status into a terminal fleet outcome.

    A gateway JOB_NOT_FOUND response means the worker job is no longer a live
    execution candidate.  It does not prove remote command success/cancel/fail,
    so fleet records it as terminal ``ambiguous`` and releases the capacity slot
    through the same expected-job-id guarded path as normal terminal statuses.
    Generic transport/timeouts still return ``None`` and keep the lease.
    """
    if _gateway_error_code(exc) != _MISSING_JOB_ERROR_CODE:
        return None
    return {
        "job_id": job_id,
        "status": "ambiguous",
        "error_code": _MISSING_JOB_ERROR_CODE,
        "error": "Gateway job status is no longer available",
        "liveness_reconciled": True,
    }


class FleetRuntime:
    """One event-loop-owned coordinator around a durable :class:`FleetState`."""

    def __init__(
        self,
        state: FleetState,
        *,
        pool_name: str,
        capacity: int,
        coordinator_id: str,
        watch_poll_interval: float = 30.0,
        gateway_io_concurrency: int = _DEFAULT_GATEWAY_IO_CONCURRENCY,
    ) -> None:
        if gateway_io_concurrency <= 0:
            raise FleetRuntimeError("gateway_io_concurrency must be a positive integer")
        self.state = state
        self.pool_name = pool_name
        self.capacity = capacity
        self.coordinator_id = coordinator_id
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._watch_poll_interval = watch_poll_interval
        self._gateway_io_gate = asyncio.Semaphore(gateway_io_concurrency)
        self._gateway_executor = ThreadPoolExecutor(
            max_workers=gateway_io_concurrency,
            thread_name_prefix="fleet-gateway",
        )
        self._watchers_by_job: dict[str, asyncio.Task] = {}
        # Insertion-ordered bounded dedupe: concurrent sweep/watcher paths can
        # both observe the same idempotently completed job, but old job ids do
        # not accumulate for the whole process lifetime.
        self._observed_terminal_jobs: dict[str, None] = {}
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._closed = False

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
        submit_with_dispatch_guard: Callable[[Callable[[], None]], dict[str, Any]] | None = None,
        job_status_fn: Callable[[str], dict[str, Any]] | None = None,
        job_result_fn: Callable[[str], dict[str, Any]] | None = None,
        terminal_observer: Callable[[str, dict[str, Any]], None] | None = None,
        observe_submitted_job: bool = True,
        retry_attempted_unbound: bool = False,
        trusted_job_resolver: Callable[[str], str | None] | None = None,
        recovery_boundary: ExecutionPlaneRecoveryBoundary | None = None,
        sweep_before_submit: bool = True,
    ) -> dict[str, Any]:
        """Admit then perform one idempotent gateway submission.

        ``sweep_before_submit=False`` is reserved for callers that already
        performed one shared reconciliation sweep for a batch. Watcher setup
        for this submission is unchanged.
        """
        await self.ensure_ready()
        if sweep_before_submit:
            try:
                await self.sweep_unbound_leases(
                    trusted_job_resolver,
                    recovery_boundary=recovery_boundary,
                )
            except Exception:
                pass
        if job_status_fn is not None and sweep_before_submit:
            try:
                await self.sweep_bound_leases(
                    job_status_fn,
                    job_result_fn=job_result_fn,
                    terminal_observer=terminal_observer,
                )
            except Exception:
                pass
        durable_task_id = fleet_task_id(project, task_id)
        await self._gateway_io_gate.acquire()
        try:
            admission = await self.state.acquire_slot(
                pool_name=self.pool_name,
                task_id=durable_task_id,
                coordinator_id=self.coordinator_id,
                capacity=self.capacity,
            )
        except TaskAlreadyTerminalError as exc:
            self._gateway_io_gate.release()
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
        except BaseException:
            self._gateway_io_gate.release()
            raise
        if not admission.acquired or admission.lease is None:
            self._gateway_io_gate.release()
            retry_after_seconds = _configured_capacity_retry_after_seconds()
            return {
                "task_id": task_id,
                "status": "blocked",
                "error_code": "FLEET_CAPACITY_EXHAUSTED",
                "error": "Fleet worker pool is at capacity",
                "retryable": True,
                "retry_after_seconds": retry_after_seconds,
                "queued": False,
                "fallback": {
                    "safe_when": "a terminal supervised implementation diff already exists",
                    "next_step": "inspect the task evidence and use the trusted materialization path instead of launching another worker",
                },
                "fleet": {
                    "pool": self.pool_name,
                    "capacity": admission.capacity,
                    "active": admission.active,
                    "available": max(admission.capacity - admission.active, 0),
                    "retry_after_seconds": retry_after_seconds,
                    "queued": False,
                },
            }
        lease = admission.lease
        if lease.job_id:
            self._gateway_io_gate.release()
            if job_status_fn is not None:
                # This call did not execute submit_sync at all, so no direct
                # synchronous router accounting can happen in the caller.
                # Observe the already-bound job regardless of this request's
                # async/sync mode.
                self._track_watcher(
                    job_id=lease.job_id,
                    job_status_fn=job_status_fn,
                    job_result_fn=job_result_fn,
                    terminal_observer=terminal_observer,
                )
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
        retrying_attempted = (
            retry_attempted_unbound and lease.submit_state == ATTEMPTED
        )
        if lease.submit_state != NEVER_ATTEMPTED and not retrying_attempted:
            # Generic callers stay fail-closed for an unbound lease whose
            # dispatch state is ambiguous. Production agent adapters may opt
            # into retry_attempted_unbound only because their lower layer binds
            # one immutable task attempt to one stable gateway idempotency key;
            # re-dispatch therefore asks for the SAME execution identity rather
            # than creating a second worker.
            self._gateway_io_gate.release()
            return {
                "task_id": task_id,
                "status": "blocked",
                "error": "unbound lease is in-flight or indeterminate; refusing to re-dispatch",
                "fleet": {
                    "pool": lease.pool,
                    "existing_lease": True,
                    "submit_state": lease.submit_state,
                    "active": admission.active,
                    "capacity": admission.capacity,
                },
            }
        dispatch_marked = threading.Event()
        owner_loop = asyncio.get_running_loop()

        if submit_with_dispatch_guard is None:
            # Compatibility path for non-agent callers: without a dispatch-aware
            # submitter the narrowest safe boundary remains immediately before
            # entering the opaque submit callable.
            if not retrying_attempted:
                try:
                    await self.state.mark_submit_attempted(
                        task_id=durable_task_id,
                        lease_token=lease.lease_token,
                    )
                except BaseException:
                    self._gateway_io_gate.release()
                    raise
            submit_call = submit_sync
        else:
            def _before_gateway_dispatch() -> None:
                if retrying_attempted or dispatch_marked.is_set():
                    return
                transition = asyncio.run_coroutine_threadsafe(
                    self.state.mark_submit_attempted(
                        task_id=durable_task_id,
                        lease_token=lease.lease_token,
                    ),
                    owner_loop,
                )
                transition.result()
                dispatch_marked.set()

            def submit_call() -> dict[str, Any]:
                return submit_with_dispatch_guard(_before_gateway_dispatch)

        try:
            result = await self._run_gateway_io(submit_call, permit_held=True)
        except BaseException:
            if (
                submit_with_dispatch_guard is not None
                and not retrying_attempted
                and not dispatch_marked.is_set()
            ):
                # The dispatch-aware submitter failed before its exact Gateway
                # boundary.  Reclaim only if the token-fenced row still proves
                # it was never attempted; an uncertain/succeeded marker cannot
                # be deleted by this statement.
                try:
                    await self.state.release_never_dispatched(
                        task_id=durable_task_id,
                        lease_token=lease.lease_token,
                    )
                except Exception:
                    pass
            raise
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
            if job_status_fn is not None:
                result_status = str(result.get("status") or "")
                should_observe_current_job = observe_submitted_job or result_status in {
                    "running",
                    "unknown",
                    "error",
                }
                self._track_watcher(
                    job_id=job_id,
                    job_status_fn=job_status_fn,
                    job_result_fn=job_result_fn,
                    terminal_observer=terminal_observer if should_observe_current_job else None,
                )
            return result
        status = str(result.get("status") or "") if isinstance(result, dict) else ""
        if status == "not-accepted" and retry_attempted_unbound:
            # The lower durable submitter could not prove acceptance, but it
            # retained the immutable attempt + stable submission key. Keep the
            # capacity lease active and return the retryable receipt unchanged;
            # the next opted-in call safely re-dispatches that SAME key.
            result = dict(result)
            result["fleet"] = {
                "pool": lease.pool,
                "existing_lease": admission.existing,
                "released": False,
                "submit_state": ATTEMPTED,
                "retry_same_execution": True,
            }
            return result
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
        if (
            submit_with_dispatch_guard is not None
            and not retrying_attempted
            and not dispatch_marked.is_set()
        ):
            # A dispatch-aware internal submitter that returns an invalid local
            # result without invoking its hook has proven that no Gateway I/O
            # occurred. Do not turn that local contract bug into permanent
            # capacity debt; the token/state-fenced DELETE still refuses any
            # row that was concurrently marked or rebound.
            try:
                await self.state.release_never_dispatched(
                    task_id=durable_task_id,
                    lease_token=lease.lease_token,
                )
            except Exception:
                pass
        raise FleetRuntimeError(
            "Agent submit returned neither a job_id nor a terminal pre-submit status"
        )

    async def reconcile_gateway_result(
        self,
        *,
        job_id: str,
        result: dict[str, Any],
        owned_reconciliation: bool = False,
    ) -> bool:
        """Release a bound lease only from an observer-owning reconciliation path.

        Generic ``job_status``/``job_result`` polling also sees terminal gateway
        state, but it has no agent-router observer context. Letting that path
        delete the lease can race the watcher and permanently lose eventual
        backend feedback. Watchers opt in explicitly; after process restart the
        next agent admission performs the observer-aware bound-lease sweep.
        """
        status = str(result.get("status") or "")
        if status not in _GATEWAY_TERMINAL or not owned_reconciliation:
            return False
        await self.ensure_ready()
        lease = await self.state.get_lease_by_job(job_id)
        if lease is None:
            return False
        result_job_id = result.get("job_id")
        if result_job_id is not None and result_job_id != job_id:
            # Never persist terminal metadata for a different gateway job under
            # this lease. Watcher/sweep callers will keep the lease and retry
            # authoritative reconciliation instead of corrupting its identity.
            return False
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
        return True

    async def _resolve_terminal_result(
        self,
        *,
        job_id: str,
        status_result: dict[str, Any],
        job_result_fn: Callable[[str], dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """Return authoritative terminal detail for backend classification.

        Cheap status polling is sufficient to prove liveness/terminal state,
        but it may omit exit_code/stdout/stderr.  When a detailed result
        callable is supplied, require a terminal result for the same job before
        feeding any backend observer; transient result-fetch failures are left
        retryable rather than guessed as generic failures.
        """
        status_job_id = status_result.get("job_id")
        if status_job_id is not None and status_job_id != job_id:
            return None
        if job_result_fn is None:
            return status_result
        try:
            detailed = await self._run_gateway_io(job_result_fn, job_id)
        except Exception:
            return None
        if not isinstance(detailed, dict):
            return None
        if str(detailed.get("status") or "") not in _GATEWAY_TERMINAL:
            return None
        detailed_job_id = detailed.get("job_id")
        if detailed_job_id is not None and detailed_job_id != job_id:
            return None
        return detailed

    def _notify_terminal_observer(
        self,
        *,
        job_id: str,
        result: dict[str, Any],
        terminal_observer: Callable[[str, dict[str, Any]], None] | None,
    ) -> bool:
        if terminal_observer is None or job_id in self._observed_terminal_jobs:
            return True
        try:
            terminal_observer(job_id, result)
        except Exception:
            logger.exception("terminal agent observer failed for job %s", job_id)
            return False
        self._observed_terminal_jobs[job_id] = None
        if len(self._observed_terminal_jobs) > _MAX_OBSERVED_TERMINAL_JOBS:
            oldest_job_id = next(iter(self._observed_terminal_jobs))
            self._observed_terminal_jobs.pop(oldest_job_id, None)
        return True

    async def _run_gateway_io(
        self,
        fn: Callable[..., _T],
        *args: Any,
        permit_held: bool = False,
    ) -> _T:
        """Bound blocking control-plane I/O without reducing worker capacity.

        Cancellation must not release capacity while the underlying sync call
        is still running: cancelling an await does not stop a worker thread.
        """
        if not permit_held:
            await self._gateway_io_gate.acquire()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_T] | None = None
        try:
            future = loop.run_in_executor(self._gateway_executor, fn, *args)
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if future is not None:
                try:
                    await asyncio.shield(future)
                except Exception:
                    # The worker already failed (e.g. JOB_NOT_FOUND), so there
                    # is nothing left to join. Cancellation must win: the
                    # caller expects CancelledError, not the worker's error,
                    # otherwise its own cleanup/shutdown logic misbehaves.
                    pass
            raise
        finally:
            self._gateway_io_gate.release()

    async def sweep_unbound_leases(
        self,
        trusted_job_resolver: Callable[[str], str | None] | None = None,
        *,
        recovery_boundary: ExecutionPlaneRecoveryBoundary | None = None,
    ) -> int:
        """Reconcile unbound leases without inferring safety from lease age.

        ``never_attempted`` rows are proven pre-dispatch and can be released.
        ``attempted``/legacy rows stay fail-closed unless a caller can recover
        the exact accepted gateway ``job_id`` from control-plane-owned trusted
        state.  In that case we only bind the existing lease; terminal/running
        classification remains the responsibility of the normal authoritative
        gateway-status reconciliation path.

        The resolver receives the durable fleet task id and must return one
        exact trusted gateway job id or ``None``.  Resolver failures are treated
        as unresolved state and never release capacity.
        """
        await self.ensure_ready()
        leases = await self.state.list_unbound_leases(pool_name=self.pool_name)
        released = 0
        for lease in leases:
            if lease.submit_state == NEVER_ATTEMPTED:
                try:
                    ok = await self.state.release_never_dispatched(
                        task_id=lease.task_id,
                        lease_token=lease.lease_token,
                    )
                except Exception:
                    continue
                if ok:
                    released += 1
                continue
            if lease.submit_state not in {ATTEMPTED, LEGACY_UNKNOWN}:
                # Unknown/corrupt/future submission states remain fail-closed.
                # Trusted job binding is intentionally limited to the two
                # historical states whose dispatch ambiguity is understood.
                continue
            if trusted_job_resolver is None:
                continue
            try:
                # The current control-plane resolver is local, but historical
                # recovery may need one bounded Gateway lookup. Never run that
                # synchronous HTTP path on the FastMCP event loop.
                job_id = await self._run_gateway_io(trusted_job_resolver, lease.task_id)
            except Exception:
                continue
            if isinstance(job_id, str) and job_id.strip():
                try:
                    await self.state.bind_job(
                        task_id=lease.task_id,
                        lease_token=lease.lease_token,
                        job_id=job_id.strip(),
                    )
                except Exception:
                    continue
                continue

            # A clean authoritative resolver miss is necessary but not enough:
            # retained submission metadata can be evicted independently from a
            # remote process. Generation reconciliation is allowed only when a
            # separately attested replacement boundary proves that both the
            # Gateway recovery plane and dedicated executor have been replaced.
            # The resolver itself is authoritative across retained Redis state
            # and current single-worker Gateway memory; optional quiescence is
            # an additional operator barrier rather than the primary proof.
            if recovery_boundary is None:
                continue
            try:
                outcome = await self.state.reconcile_unbound_after_execution_plane_replacement(
                    task_id=lease.task_id,
                    lease_token=lease.lease_token,
                    expected_submit_state=lease.submit_state,
                    gateway_started_at=recovery_boundary.gateway_started_at,
                    executor_started_at=recovery_boundary.executor_started_at,
                    mcp_started_at=recovery_boundary.mcp_started_at,
                    observed_at=recovery_boundary.observed_at,
                    gateway_generation=recovery_boundary.gateway_generation,
                    executor_generation=recovery_boundary.executor_generation,
                    mcp_generation=recovery_boundary.mcp_generation,
                    quiescence_seconds=recovery_boundary.quiescence_seconds,
                )
            except Exception:
                continue
            if outcome is not None:
                released += 1
        return released

    async def reconcile(
        self,
        job_status_fn: Callable[[str], dict[str, Any]] | None = None,
        *,
        job_result_fn: Callable[[str], dict[str, Any]] | None = None,
        terminal_observer: Callable[[str, dict[str, Any]], None] | None = None,
        trusted_job_resolver: Callable[[str], str | None] | None = None,
        recovery_boundary: ExecutionPlaneRecoveryBoundary | None = None,
    ) -> int:
        """Reclaim abandoned never-dispatched leases and terminal bound leases.

        The unbound sweep always runs; the bound sweep runs only when a
        gateway status function is supplied (it cannot run without one).
        """
        released = await self.sweep_unbound_leases(
            trusted_job_resolver,
            recovery_boundary=recovery_boundary,
        )
        if job_status_fn is not None:
            try:
                released += await self.sweep_bound_leases(
                    job_status_fn,
                    job_result_fn=job_result_fn,
                    terminal_observer=terminal_observer,
                )
            except Exception:
                pass
        return released

    async def sweep_bound_leases(
        self,
        job_status_fn: Callable[[str], dict[str, Any]],
        *,
        job_result_fn: Callable[[str], dict[str, Any]] | None = None,
        terminal_observer: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> int:
        """Reconcile this pool and restore watchers for unresolved jobs."""
        await self.ensure_ready()
        leases = await self.state.list_bound_leases(pool_name=self.pool_name)
        released = 0
        for lease in leases:
            job_id = lease.job_id
            if not job_id:
                continue
            try:
                result = await self._run_gateway_io(job_status_fn, job_id)
            except Exception as exc:
                missing_job = _gateway_missing_job_result(job_id, exc)
                if missing_job is not None:
                    if not self._notify_terminal_observer(
                        job_id=job_id,
                        result=missing_job,
                        terminal_observer=terminal_observer,
                    ):
                        self._track_watcher(
                            job_id=job_id,
                            job_status_fn=job_status_fn,
                            job_result_fn=job_result_fn,
                            terminal_observer=terminal_observer,
                        )
                        continue
                    try:
                        if await self.reconcile_gateway_result(
                            job_id=job_id,
                            result=missing_job,
                            owned_reconciliation=True,
                        ):
                            released += 1
                    except Exception:
                        self._track_watcher(
                            job_id=job_id,
                            job_status_fn=job_status_fn,
                            job_result_fn=job_result_fn,
                            terminal_observer=terminal_observer,
                        )
                    continue
                self._track_watcher(
                    job_id=job_id,
                    job_status_fn=job_status_fn,
                    job_result_fn=job_result_fn,
                    terminal_observer=terminal_observer,
                )
                continue
            status = str(result.get("status") or "")
            if status not in _GATEWAY_TERMINAL:
                self._track_watcher(
                    job_id=job_id,
                    job_status_fn=job_status_fn,
                    job_result_fn=job_result_fn,
                    terminal_observer=terminal_observer,
                )
                continue
            terminal_result = await self._resolve_terminal_result(
                job_id=job_id,
                status_result=result,
                job_result_fn=job_result_fn,
            )
            if terminal_result is None:
                self._track_watcher(
                    job_id=job_id,
                    job_status_fn=job_status_fn,
                    job_result_fn=job_result_fn,
                    terminal_observer=terminal_observer,
                )
                continue
            terminal_status = str(terminal_result.get("status") or status)
            exit_code = terminal_result.get("exit_code")
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                exit_code = None
            if not self._notify_terminal_observer(
                job_id=job_id,
                result=terminal_result,
                terminal_observer=terminal_observer,
            ):
                self._track_watcher(
                    job_id=job_id,
                    job_status_fn=job_status_fn,
                    job_result_fn=job_result_fn,
                    terminal_observer=terminal_observer,
                )
                continue
            try:
                await self.state.complete_task(
                    task_id=lease.task_id,
                    lease_token=lease.lease_token,
                    status=terminal_status,
                    exit_code=exit_code,
                    result=_small_result(terminal_result),
                    expected_job_id=job_id,
                )
                released += 1
            except Exception:
                self._track_watcher(
                    job_id=job_id,
                    job_status_fn=job_status_fn,
                    job_result_fn=job_result_fn,
                    terminal_observer=terminal_observer,
                )
        return released

    def _track_watcher(
        self,
        *,
        job_id: str,
        job_status_fn: Callable[[str], dict[str, Any]],
        job_result_fn: Callable[[str], dict[str, Any]] | None = None,
        terminal_observer: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        """Start exactly one persistent reconciliation watcher per job_id."""
        if self._closing or self._closed or job_id in self._watchers_by_job:
            return
        task = asyncio.create_task(
            self._watch_gateway_job(
                job_id=job_id,
                job_status_fn=job_status_fn,
                job_result_fn=job_result_fn,
                terminal_observer=terminal_observer,
            )
        )
        self._watchers_by_job[job_id] = task

        def _forget(done: asyncio.Task, _job_id: str = job_id) -> None:
            if self._watchers_by_job.get(_job_id) is done:
                self._watchers_by_job.pop(_job_id, None)

        task.add_done_callback(_forget)

    async def _watch_gateway_job(
        self,
        *,
        job_id: str,
        job_status_fn: Callable[[str], dict[str, Any]],
        job_result_fn: Callable[[str], dict[str, Any]] | None = None,
        terminal_observer: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        """Poll until terminal reconciliation succeeds or runtime closes."""
        # Exit as soon as shutdown begins, not only after it has completed.
        # A status call that keeps raising can turn the watcher's cancellation
        # into a plain error (see _run_gateway_io); relying on _closed alone
        # would keep close() blocked in its gather forever.
        while not (self._closing or self._closed):
            try:
                result = await self._run_gateway_io(job_status_fn, job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                missing_job = _gateway_missing_job_result(job_id, exc)
                if missing_job is not None:
                    if not self._notify_terminal_observer(
                        job_id=job_id,
                        result=missing_job,
                        terminal_observer=terminal_observer,
                    ):
                        await asyncio.sleep(self._watch_poll_interval)
                        continue
                    try:
                        await self.reconcile_gateway_result(
                            job_id=job_id,
                            result=missing_job,
                            owned_reconciliation=True,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await asyncio.sleep(self._watch_poll_interval)
                        continue
                    return
                await asyncio.sleep(self._watch_poll_interval)
                continue
            status = str(result.get("status") or "")
            if status in _GATEWAY_TERMINAL:
                terminal_result = await self._resolve_terminal_result(
                    job_id=job_id,
                    status_result=result,
                    job_result_fn=job_result_fn,
                )
                if terminal_result is None:
                    await asyncio.sleep(self._watch_poll_interval)
                    continue
                if not self._notify_terminal_observer(
                    job_id=job_id,
                    result=terminal_result,
                    terminal_observer=terminal_observer,
                ):
                    await asyncio.sleep(self._watch_poll_interval)
                    continue
                try:
                    await self.reconcile_gateway_result(
                        job_id=job_id,
                        result=terminal_result,
                        owned_reconciliation=True,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(self._watch_poll_interval)
                    continue
                return
            await asyncio.sleep(self._watch_poll_interval)

    async def close(self) -> None:
        """Join process-owned resources; a failed close remains retryable."""
        if self._closed:
            return
        async with self._close_lock:
            if self._closed:
                return
            # Block creation of new reconciliation watchers as soon as shutdown
            # begins. Keep this sticky after a failed close: a half-closed
            # process runtime must never resume background work, but a later
            # close call may still retry the remaining cleanup.
            self._closing = True
            watchers = list(self._watchers_by_job.values())
            for task in watchers:
                task.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)
            self._watchers_by_job.clear()
            # A running sync gateway call cannot be cancelled by cancelling its
            # asyncio waiter. Process shutdown therefore waits for executor work
            # to finish instead of abandoning fleet-gateway threads. The wait is
            # bounded: a worker thread blocked on an unreachable gateway must
            # not hold the whole process shutdown open forever. On timeout the
            # asyncio waiter is cancelled while the (uninterruptible) executor
            # worker keeps running in its thread; close proceeds to terminal
            # state and logs the abandoned work.
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self._gateway_executor.shutdown,
                        wait=True,
                        cancel_futures=True,
                    ),
                    timeout=_gateway_executor_shutdown_timeout(),
                )
            except TimeoutError:
                logger.warning(
                    "FleetRuntime gateway executor did not join within %.1fs; "
                    "abandoning executor join and continuing process shutdown",
                    _gateway_executor_shutdown_timeout(),
                )
            # Mark terminal only after every owned resource has actually closed.
            # If state.close() raises/cancels, globals retain this runtime so a
            # later process-level close can retry rather than losing ownership.
            await self.state.close()
            self._closed = True


_runtime: FleetRuntime | None = None
_runtime_lock: asyncio.Lock | None = None
_runtime_loop: asyncio.AbstractEventLoop | None = None


def fleet_enabled() -> bool:
    return _env_flag(_ENABLED_ENV, default=False)


async def get_fleet_runtime() -> FleetRuntime | None:
    """Return the process singleton when fleet admission is enabled."""
    global _runtime, _runtime_lock, _runtime_loop
    if not fleet_enabled():
        return None
    current_loop = asyncio.get_running_loop()
    if _runtime is not None:
        if _runtime_loop is not current_loop:
            raise FleetRuntimeError("FleetRuntime accessed from a non-owner event loop")
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
            _runtime_loop = current_loop
        return _runtime


async def _close_fleet_runtime_on_owner_loop() -> None:
    """Close and clear the singleton from its owning event loop."""
    global _runtime, _runtime_lock, _runtime_loop
    runtime = _runtime
    if runtime is not None:
        await runtime.close()
    # Clear ownership only after successful cleanup. Losing the singleton on
    # an exception/cancellation would make an unfinished executor/state close
    # impossible to retry and falsely report that no process runtime remains.
    _runtime = None
    _runtime_lock = None
    _runtime_loop = None


async def close_fleet_runtime() -> None:
    """Close the process singleton on the event loop that created it.

    The public Starlette proxy runs in the process main thread while FastMCP
    owns a separate loop in its internal server thread. Process shutdown may
    therefore originate off-loop; marshal cleanup back to the owner rather
    than awaiting asyncpg/tasks from the wrong loop.
    """
    global _runtime, _runtime_lock, _runtime_loop
    if _runtime is None:
        _runtime_lock = None
        _runtime_loop = None
        return

    owner_loop = _runtime_loop
    current_loop = asyncio.get_running_loop()
    if owner_loop is None or owner_loop is current_loop:
        await _close_fleet_runtime_on_owner_loop()
        return
    if not owner_loop.is_running():
        raise FleetRuntimeError("FleetRuntime owner loop is not running during process shutdown")

    future = asyncio.run_coroutine_threadsafe(
        _close_fleet_runtime_on_owner_loop(),
        owner_loop,
    )
    await asyncio.wrap_future(future)


__all__ = [
    "ExecutionPlaneRecoveryBoundary",
    "FleetRuntime",
    "FleetRuntimeError",
    "close_fleet_runtime",
    "fleet_enabled",
    "fleet_task_id",
    "get_fleet_runtime",
]
