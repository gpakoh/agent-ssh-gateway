"""Agent lifecycle events — in-memory observation layer."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.exceptions import ObservabilityDegradedError

if TYPE_CHECKING:
    from app.agent_event_store import AgentEventStore as PGAgentEventStore
    from app.session_store import AgentEventRecord

logger = logging.getLogger(__name__)

MAX_EVENTS_PER_JOB = 100


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """A single agent lifecycle event."""

    id: str
    job_id: str
    agent_id: str
    event_type: str
    created_at: float
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "agent_id": self.agent_id,
            "type": self.event_type,
            "timestamp": self.created_at,
            "payload": self.payload,
        }


class AgentEventStore:
    """In-memory ring buffer of agent events, bounded per job."""

    def __init__(self, max_events_per_job: int = MAX_EVENTS_PER_JOB) -> None:
        self._max = max_events_per_job
        self._events: dict[str, deque[AgentEvent]] = defaultdict(lambda: deque(maxlen=self._max))

    def emit(
        self,
        job_id: str,
        agent_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        """Record an event and return it."""
        event = AgentEvent(
            id=uuid.uuid4().hex,
            job_id=job_id,
            agent_id=agent_id,
            event_type=event_type,
            created_at=time.time(),
            payload=payload or {},
        )
        self._events[job_id].append(event)
        return event

    def get_events(self, job_id: str) -> list[AgentEvent]:
        """Return all events for a job (oldest first)."""
        return list(self._events.get(job_id, ()))

    def get_latest(self, job_id: str) -> AgentEvent | None:
        """Return the most recent event for a job, or None."""
        q = self._events.get(job_id)
        return q[-1] if q else None


class AgentEventEmitter:
    """Thin emitter wrapping the store."""

    def __init__(self, store: AgentEventStore | None = None) -> None:
        self._store = store or AgentEventStore()

    @property
    def store(self) -> AgentEventStore:
        return self._store

    def emit(
        self,
        job_id: str,
        agent_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        return self._store.emit(job_id, agent_id, event_type, payload)


agent_events = AgentEventEmitter()

dual_write_emitter: DualWriteAgentEventEmitter | None = None


@dataclass
class EventSubscription:
    """Live subscription to committed events for one job."""

    queue: asyncio.Queue
    watermark: int  # last PG-committed sequence at subscription time


@dataclass(slots=True)
class ObservabilityState:
    """Degraded-state machine for the observability pipeline.

    HEALTHY --(persistence failure)--> DEGRADED
    DEGRADED --(successful persistence)--> HEALTHY

    Observability degradation NEVER affects job execution outcomes.
    """

    is_degraded: bool = False
    degraded_since: datetime | None = None
    degraded_reason: str | None = None

    def mark_degraded(self, reason: str) -> None:
        if not self.is_degraded:
            self.is_degraded = True
            self.degraded_since = datetime.now(UTC)
        self.degraded_reason = reason

    def mark_healthy(self) -> None:
        self.is_degraded = False
        self.degraded_since = None
        self.degraded_reason = None


class DualWriteAgentEventEmitter:
    """PG-first emitter with live fan-out. Backward-compatible wrapper.

    Persistence order is strict: PG insert commits first, only then are
    live subscribers fanned out and the in-memory cache updated. A PG
    failure flips the emitter's :class:`ObservabilityState` to degraded
    and raises :class:`ObservabilityDegradedError`; callers must catch it
    and continue job execution. The next successful persist clears the
    degraded state automatically.
    """

    def __init__(
        self,
        memory_emitter: AgentEventEmitter,
        pg_store: PGAgentEventStore | None = None,
    ) -> None:
        self._memory = memory_emitter
        self._pg = pg_store
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._watermarks: dict[str, int] = {}  # last committed PG sequence per job
        self._observability_state = ObservabilityState()

    @property
    def observability_state(self) -> ObservabilityState:
        """Current degraded-state machine (for /health exposure)."""
        return self._observability_state

    @property
    def store(self) -> AgentEventStore:
        """Backward compat: expose in-memory store."""
        return self._memory.store

    def emit(
        self,
        job_id: str,
        agent_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        """Sync emit — writes to memory only. Backward compatible."""
        return self._memory.emit(job_id, agent_id, event_type, payload)

    async def pg_emit(
        self,
        job_id: str,
        attempt_id: str,
        owner_id: str,
        agent_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentEventRecord | None:
        """Persist-first: write to PG, then fan out to subscribers.

        Returns committed record or None if PG is not configured.
        Raises ObservabilityDegradedError if the PG write fails — after
        marking the emitter degraded. Post-commit fan-out failures never
        flip state or propagate: the event IS committed once PG returned.
        """
        if not self._pg:
            return None
        try:
            record = await self._pg.insert(
                job_id=job_id,
                attempt_id=attempt_id,
                owner_id=owner_id,
                agent_id=agent_id,
                event_type=event_type,
                payload=payload,
            )
        except Exception as exc:
            reason = f"postgres unavailable: {exc}"
            self._observability_state.mark_degraded(reason)
            logger.warning(
                "PG emit failed for job %s — observability degraded", job_id, exc_info=exc
            )
            raise ObservabilityDegradedError(reason) from exc
        # Committed from here on: recovery + fan-out must not re-raise.
        self._observability_state.mark_healthy()
        self._watermarks[job_id] = record.sequence
        event_data = {
            "sequence": record.sequence,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "agent_id": agent_id,
            "type": event_type,
            "payload": payload or {},
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }
        for queue in self._subscribers.get(job_id, []):
            try:
                queue.put_nowait(event_data)
            except asyncio.QueueFull:
                pass  # overflow handled by subscriber control state (Task 7)
        self._memory.emit(job_id, agent_id, event_type, payload)
        return record

    async def subscribe(self, job_id: str) -> EventSubscription:
        """Create a live subscription with committed watermark."""
        watermark = self._watermarks.get(job_id)
        if watermark is None and self._pg:
            watermark = await self._pg.get_latest_sequence(job_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers[job_id].append(queue)
        return EventSubscription(queue=queue, watermark=watermark or 0)

    def remove_subscriber(self, job_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        subs = self._subscribers.get(job_id, [])
        self._subscribers[job_id] = [q for q in subs if q is not queue]

    def get_committed_sequence(self, job_id: str) -> int | None:
        """Last sequence known committed to PG for this job."""
        return self._watermarks.get(job_id)


def record_agent_event(
    job_id: str,
    agent_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> AgentEvent:
    """Emit one lifecycle event through the process-wide store."""
    return agent_events.emit(job_id, agent_id, event_type, payload)


def list_agent_events(job_id: str) -> list[AgentEvent]:
    """Read the event timeline of one job from the process-wide store."""
    return agent_events.store.get_events(job_id)


__all__ = [
    "AgentEvent",
    "AgentEventEmitter",
    "AgentEventStore",
    "DualWriteAgentEventEmitter",
    "EventSubscription",
    "ObservabilityState",
    "list_agent_events",
    "record_agent_event",
]
