"""Pure Docker Compose operation recovery contracts.

This module intentionally does not know about FastMCP, Docker clients,
subprocesses, asyncpg, environment variables, filesystem roots or deployment
configuration. Adapters are responsible for durable storage and for executing
Docker; the domain layer only defines the state machine and replay/recovery
semantics for tracked Compose mutations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

TRACKED_COMPOSE_TOOLS = frozenset(
    {
        "docker_compose_up",
        "docker_compose_down",
        "docker_compose_restart",
    }
)


class DockerOperationStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class DockerOperationPhase(StrEnum):
    DISPATCHING = "dispatching"
    APPLYING = "applying"
    RECONCILING = "reconciling"
    FINAL = "final"


class DockerOperationError(RuntimeError):
    """Base class for operation-state contract violations."""


class DockerOperationConflict(DockerOperationError):
    """An action id was reused for different immutable request data."""


class DockerJournalUnavailable(DockerOperationError):
    """The durable journal could not accept an operation before side effects."""


class ComposeServiceSetConflict(DockerOperationError):
    """A tracked Compose action could not be bound to an exact service set."""


@dataclass(frozen=True)
class ComposeOperationRequest:
    """Immutable, bounded request accepted before a Docker side effect starts."""

    action_id: str
    owner_fingerprint: str
    tool: str
    project_identity: str
    compose_config_digest: str
    services: tuple[str, ...]
    detach: bool | None = None
    build: bool | None = None
    remove_orphans: bool | None = None
    volumes: bool | None = None
    stop_grace_seconds: int | None = None
    execution_deadline_seconds: int | None = None
    transport_wait_deadline_seconds: int | None = None
    request_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.tool not in TRACKED_COMPOSE_TOOLS:
            raise ValueError(f"untracked compose tool: {self.tool}")
        if not self.action_id:
            raise ValueError("action_id is required")
        if not self.owner_fingerprint:
            raise ValueError("owner_fingerprint is required")
        if not self.project_identity:
            raise ValueError("project_identity is required")
        if not self.compose_config_digest:
            raise ValueError("compose_config_digest is required")
        if not self.services:
            raise ValueError("an exact service set is required")
        normalized_services = tuple(sorted(dict.fromkeys(self.services)))
        object.__setattr__(self, "services", normalized_services)
        object.__setattr__(self, "request_digest", _stable_digest(self.public_request()))

    def public_request(self) -> dict[str, Any]:
        """Return request metadata safe for operator-visible status responses."""

        return {
            "action_id": self.action_id,
            "tool": self.tool,
            "project_identity": self.project_identity,
            "compose_config_digest": self.compose_config_digest,
            "services": list(self.services),
            "detach": self.detach,
            "build": self.build,
            "remove_orphans": self.remove_orphans,
            "volumes": self.volumes,
            "stop_grace_seconds": self.stop_grace_seconds,
            "execution_deadline_seconds": self.execution_deadline_seconds,
            "transport_wait_deadline_seconds": self.transport_wait_deadline_seconds,
        }


@dataclass(frozen=True)
class DockerExecutionResult:
    """Typed result produced by the Docker adapter boundary."""

    ok: bool
    timed_out: bool = False
    exit_code: int | None = 0
    stdout_tail: str = ""
    stderr_tail: str = ""
    cause: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class DockerOperationRecord:
    request: ComposeOperationRequest
    status: DockerOperationStatus
    phase: DockerOperationPhase
    dispatch_count: int = 0
    created_at: float = field(default_factory=time.monotonic)
    attempted_at: float | None = None
    finished_at: float | None = None
    result: DockerExecutionResult | None = None
    cause: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def receipt(self) -> dict[str, Any]:
        """Return bounded status/replay data without owner secrets or host paths."""

        payload: dict[str, Any] = {
            "action_id": self.request.action_id,
            "tool": self.request.tool,
            "status": self.status.value,
            "phase": self.phase.value,
            "dispatch_count": self.dispatch_count,
            "request_digest": self.request.request_digest,
            "request": self.request.public_request(),
            "cause": self.cause,
            "evidence": deepcopy(self.evidence),
        }
        if self.result is not None:
            payload["result"] = {
                "ok": self.result.ok,
                "timed_out": self.result.timed_out,
                "exit_code": self.result.exit_code,
                "stdout_tail": self.result.stdout_tail,
                "stderr_tail": self.result.stderr_tail,
                "cause": self.result.cause,
                "evidence": deepcopy(self.result.evidence),
            }
        return payload


class DockerOperationJournal(Protocol):
    async def accept(self, request: ComposeOperationRequest) -> DockerOperationRecord: ...

    async def claim_for_dispatch(self, action_id: str) -> DockerOperationRecord | None: ...

    async def complete(
        self,
        action_id: str,
        result: DockerExecutionResult,
        *,
        status: DockerOperationStatus,
        cause: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> DockerOperationRecord: ...

    async def get(self, action_id: str, owner_fingerprint: str) -> DockerOperationRecord | None: ...

    async def mark_reconciling_unfinished(self) -> list[DockerOperationRecord]: ...


class InMemoryDockerOperationJournal:
    """In-memory journal with the same atomic semantics required from storage."""

    def __init__(self, *, fail_accept: bool = False) -> None:
        self._records: dict[str, DockerOperationRecord] = {}
        self._lock = asyncio.Lock()
        self._fail_accept = fail_accept

    async def accept(self, request: ComposeOperationRequest) -> DockerOperationRecord:
        if self._fail_accept:
            raise DockerJournalUnavailable("docker operation journal is unavailable")
        async with self._lock:
            existing = self._records.get(request.action_id)
            if existing is not None:
                if (
                    existing.request.owner_fingerprint != request.owner_fingerprint
                    or existing.request.tool != request.tool
                    or existing.request.request_digest != request.request_digest
                ):
                    raise DockerOperationConflict("action_id already accepted with different data")
                return existing

            record = DockerOperationRecord(
                request=request,
                status=DockerOperationStatus.ACCEPTED,
                phase=DockerOperationPhase.DISPATCHING,
            )
            self._records[request.action_id] = record
            return record

    async def claim_for_dispatch(self, action_id: str) -> DockerOperationRecord | None:
        async with self._lock:
            record = self._records.get(action_id)
            if record is None or record.status != DockerOperationStatus.ACCEPTED:
                return None
            record.status = DockerOperationStatus.RUNNING
            record.phase = DockerOperationPhase.APPLYING
            record.dispatch_count += 1
            record.attempted_at = time.monotonic()
            return record

    async def complete(
        self,
        action_id: str,
        result: DockerExecutionResult,
        *,
        status: DockerOperationStatus,
        cause: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> DockerOperationRecord:
        if status not in {
            DockerOperationStatus.SUCCEEDED,
            DockerOperationStatus.FAILED,
            DockerOperationStatus.AMBIGUOUS,
        }:
            raise ValueError("complete() requires a terminal status")
        async with self._lock:
            record = self._records[action_id]
            record.status = status
            record.phase = DockerOperationPhase.FINAL
            record.finished_at = time.monotonic()
            record.result = result
            record.cause = cause or result.cause
            record.evidence = deepcopy(evidence or result.evidence)
            return record

    async def get(self, action_id: str, owner_fingerprint: str) -> DockerOperationRecord | None:
        async with self._lock:
            record = self._records.get(action_id)
            if record is None or record.request.owner_fingerprint != owner_fingerprint:
                return None
            return record

    async def mark_reconciling_unfinished(self) -> list[DockerOperationRecord]:
        async with self._lock:
            reconciled: list[DockerOperationRecord] = []
            for record in self._records.values():
                if record.status != DockerOperationStatus.RUNNING:
                    continue
                record.status = DockerOperationStatus.AMBIGUOUS
                record.phase = DockerOperationPhase.RECONCILING
                record.cause = "process_restarted_before_terminal_receipt"
                record.evidence = {
                    **record.evidence,
                    "redispatch_allowed": False,
                }
                reconciled.append(record)
            return reconciled


DockerExecutor = Callable[[ComposeOperationRequest], Awaitable[DockerExecutionResult]]


class DockerOperationCoordinator:
    """Accept, dispatch and replay tracked Docker Compose operations."""

    def __init__(self, journal: DockerOperationJournal, executor: DockerExecutor) -> None:
        self._journal = journal
        self._executor = executor

    async def submit(
        self,
        request: ComposeOperationRequest,
        *,
        async_submit: bool = False,
    ) -> dict[str, Any]:
        """Accept before dispatch and dispatch each action id at most once."""

        await self._journal.accept(request)
        claimed = await self._journal.claim_for_dispatch(request.action_id)
        if claimed is None:
            current = await self._journal.get(request.action_id, request.owner_fingerprint)
            if current is None:
                raise DockerOperationConflict("operation exists but owner fence rejected lookup")
            return current.receipt()

        if async_submit:
            return claimed.receipt()

        try:
            result = await self._executor(request)
        except TimeoutError as exc:
            result = DockerExecutionResult(
                ok=False,
                timed_out=True,
                exit_code=None,
                cause="execution_timeout",
                stderr_tail=str(exc),
            )
        except Exception as exc:
            result = DockerExecutionResult(
                ok=False,
                timed_out=False,
                exit_code=None,
                cause=type(exc).__name__,
                stderr_tail=str(exc),
            )

        terminal = terminal_status(result)
        completed = await self._journal.complete(
            request.action_id,
            result,
            status=terminal,
            cause=result.cause,
            evidence=result.evidence,
        )
        return completed.receipt()

    async def status(self, action_id: str, owner_fingerprint: str) -> dict[str, Any]:
        record = await self._journal.get(action_id, owner_fingerprint)
        if record is None:
            return {"action_id": action_id, "status": "operation_not_tracked"}
        return record.receipt()

    async def reconcile_after_restart(self) -> list[dict[str, Any]]:
        records = await self._journal.mark_reconciling_unfinished()
        return [record.receipt() for record in records]


def build_compose_request(
    *,
    action_id: str,
    owner_fingerprint: str,
    tool: str,
    project_identity: str,
    compose_config_digest: str,
    kwargs: dict[str, Any],
    resolved_services: tuple[str, ...] | list[str] | None = None,
    transport_wait_deadline_seconds: int | None = None,
) -> ComposeOperationRequest:
    """Build immutable request metadata from a confirmed Compose operation."""

    if tool not in TRACKED_COMPOSE_TOOLS:
        raise ValueError(f"untracked compose tool: {tool}")

    raw_services = kwargs.get("services")
    if raw_services is None:
        if not resolved_services:
            raise ComposeServiceSetConflict("services=None must be resolved before acceptance")
        services = tuple(resolved_services)
    else:
        services = tuple(raw_services)
        if resolved_services is not None and tuple(sorted(services)) != tuple(sorted(resolved_services)):
            raise ComposeServiceSetConflict("resolved services differ from confirmed kwargs")

    timeout = kwargs.get("timeout")
    execution_deadline = int(timeout) if timeout is not None else None
    stop_grace = execution_deadline if tool == "docker_compose_down" else None

    return ComposeOperationRequest(
        action_id=action_id,
        owner_fingerprint=owner_fingerprint,
        tool=tool,
        project_identity=project_identity,
        compose_config_digest=compose_config_digest,
        services=services,
        detach=kwargs.get("detach"),
        build=kwargs.get("build"),
        remove_orphans=kwargs.get("remove_orphans"),
        volumes=kwargs.get("volumes"),
        stop_grace_seconds=stop_grace,
        execution_deadline_seconds=execution_deadline,
        transport_wait_deadline_seconds=transport_wait_deadline_seconds,
    )


def terminal_status(result: DockerExecutionResult) -> DockerOperationStatus:
    if result.ok and not result.timed_out:
        return DockerOperationStatus.SUCCEEDED
    if result.timed_out or result.exit_code is None:
        return DockerOperationStatus.AMBIGUOUS
    return DockerOperationStatus.FAILED


def _stable_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()
