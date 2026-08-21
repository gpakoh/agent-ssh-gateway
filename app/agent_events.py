"""Agent lifecycle events — in-memory observation layer."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

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
    "list_agent_events",
    "record_agent_event",
]
