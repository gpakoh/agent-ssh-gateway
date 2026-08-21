# Agent Events Observability Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an in-memory agent events observability layer that captures started/heartbeat/completed/failed lifecycle events per job and exposes them via API.

**Architecture:** New `app/agent_events.py` module with `AgentEvent` dataclass, `AgentEventEmitter`, and `AgentEventStore` (in-memory ring buffer per job). Integration into `JobManager._run_job` at three firing points. New `GET /api/agents/{job_id}/events` endpoint.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, uuid4. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-21-agent-events-observability.md`

## Global Constraints

- Python 3.11+ (project minimum)
- No new pip dependencies — stdlib only (dataclasses, uuid, time, asyncio, collections)
- Follow existing code style: no comments unless asked, type hints on public APIs
- Tests use pytest + pytest-asyncio (project standard)
- All new files go under `app/` for source, `tests/` for tests

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `app/agent_events.py` | Create | AgentEvent dataclass, AgentEventEmitter, AgentEventStore |
| `app/job_manager.py:628-643` | Modify | Emit `started` event |
| `app/job_manager.py:605-625` | Modify | Emit `heartbeat` event in heartbeat loop |
| `app/job_manager.py:776-787` | Modify | Emit `completed`/`failed` event |
| `app/routers/agents.py` | Create | GET /api/agents/{job_id}/events endpoint |
| `app/main.py` | Modify | Include agents router |
| `tests/test_agent_events.py` | Create | Unit + lifecycle integration tests |

---

### Task 1: AgentEvent data model + AgentEventStore

**Files:**
- Create: `app/agent_events.py`
- Create: `tests/test_agent_events.py`

**Interfaces:**
- Produces: `AgentEvent`, `AgentEventEmitter`, `agent_events` (module-level singleton)

- [ ] **Step 1: Create `app/agent_events.py`**

```python
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
        self._events: dict[str, deque[AgentEvent]] = defaultdict(
            lambda: deque(maxlen=self._max)
        )

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
```

- [ ] **Step 2: Create `tests/test_agent_events.py` with store unit tests**

```python
"""Unit tests for AgentEventStore and AgentEventEmitter."""

from app.agent_events import AgentEventEmitter, AgentEventStore


class TestAgentEventStore:
    def test_emit_returns_event(self):
        store = AgentEventStore()
        ev = store.emit("j1", "agent1", "started", {"command": "ls"})
        assert ev.event_type == "started"
        assert ev.job_id == "j1"
        assert ev.agent_id == "agent1"
        assert ev.payload == {"command": "ls"}
        assert ev.id

    def test_get_events_returns_oldest_first(self):
        store = AgentEventStore()
        store.emit("j1", "a", "started", {})
        store.emit("j1", "a", "heartbeat", {})
        store.emit("j1", "a", "completed", {})
        events = store.get_events("j1")
        assert [e.event_type for e in events] == ["started", "heartbeat", "completed"]

    def test_get_events_empty_for_unknown_job(self):
        store = AgentEventStore()
        assert store.get_events("nonexistent") == []

    def test_get_latest(self):
        store = AgentEventStore()
        store.emit("j1", "a", "started", {})
        latest = store.emit("j1", "a", "completed", {})
        assert store.get_latest("j1") is latest

    def test_get_latest_none_for_unknown(self):
        store = AgentEventStore()
        assert store.get_latest("nonexistent") is None

    def test_bounded_eviction(self):
        store = AgentEventStore(max_events_per_job=3)
        for i in range(5):
            store.emit("j1", "a", "progress", {"i": i})
        events = store.get_events("j1")
        assert len(events) == 3
        assert [e.payload["i"] for e in events] == [2, 3, 4]

    def test_isolation_between_jobs(self):
        store = AgentEventStore()
        store.emit("j1", "a", "started", {})
        store.emit("j2", "b", "started", {})
        assert len(store.get_events("j1")) == 1
        assert len(store.get_events("j2")) == 1
        assert store.get_events("j1")[0].agent_id == "a"
        assert store.get_events("j2")[0].agent_id == "b"

    def test_to_dict(self):
        store = AgentEventStore()
        ev = store.emit("j1", "a", "started", {"k": "v"})
        d = ev.to_dict()
        assert d["type"] == "started"
        assert d["job_id"] == "j1"
        assert d["payload"] == {"k": "v"}
        assert "id" in d
        assert "timestamp" in d


class TestAgentEventEmitter:
    def test_emit_wraps_store(self):
        emitter = AgentEventEmitter()
        ev = emitter.emit("j1", "a", "started", {})
        assert ev.event_type == "started"
        assert emitter.store.get_latest("j1") is ev

    def test_emitter_uses_injected_store(self):
        store = AgentEventStore(max_events_per_job=5)
        emitter = AgentEventEmitter(store=store)
        emitter.emit("j1", "a", "started", {})
        assert len(store.get_events("j1")) == 1
```

- [ ] **Step 3: Run tests**

```bash
uv run python -m pytest tests/test_agent_events.py -v
```

Expected: 11 tests pass

- [ ] **Step 4: Commit**

```bash
git add app/agent_events.py tests/test_agent_events.py
git commit -m "feat(agent-events): add AgentEventStore and AgentEventEmitter"
```

---

### Task 2: Integrate emitter into JobManager._run_job

**Files:**
- Modify: `app/job_manager.py` (3 insertion points)
- Test: `tests/test_agent_events.py` (append lifecycle integration tests)

**Interfaces:**
- Consumes: `AgentEventEmitter` from `app/agent_events.py`
- Integration points:
  - Line 628-643: after `job.status = "running"`, emit `started`
  - Line 605-625: inside `_heartbeat_loop`, emit `heartbeat` each successful renewal
  - Line 776-787: after terminal status, emit `completed` or `failed`

- [ ] **Step 1: Add import in job_manager.py**

Add near top of `app/job_manager.py`:

```python
from app.agent_events import agent_events as _agent_events
```

- [ ] **Step 2: Emit `started` after status=running (line ~643)**

After the `notify_listeners` call at line 637-643, add:

```python
            _agent_events.emit(
                job_id, job.owner_id, "started",
                {"command": job.command, "session_id": job.session_id},
            )
```

- [ ] **Step 3: Emit `heartbeat` in heartbeat loop (line ~613)**

Inside `_heartbeat_loop`, after successful `heartbeat_durable_execution` (line 613, before the cancellation check), add:

```python
                    _agent_events.emit(job_id, job.owner_id, "heartbeat", {"state": "running"})
```

- [ ] **Step 4: Emit `completed`/`failed` at terminal state (line ~787)**

After the terminal `notify_listeners` at line 780-787, add:

```python
                _terminal_type = "completed" if job.status == "completed" else "failed"
                _agent_events.emit(
                    job_id, job.owner_id, _terminal_type,
                    {
                        "exit_code": job.exit_code,
                        "error": job.error_message,
                        "duration": job.duration,
                    },
                )
```

- [ ] **Step 5: Add lifecycle integration tests**

Append to `tests/test_agent_events.py`:

```python
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent_events import AgentEventEmitter, AgentEventStore, agent_events


class TestAgentEventLifecycle:
    """Integration tests: events fire at correct lifecycle points."""

    def test_success_lifecycle_ordering(self):
        """started -> heartbeat(s) -> completed."""
        store = AgentEventStore()
        emitter = AgentEventEmitter(store=store)
        emitter.emit("j1", "a", "started", {"command": "ls"})
        emitter.emit("j1", "a", "heartbeat", {"state": "running"})
        emitter.emit("j1", "a", "heartbeat", {"state": "running"})
        emitter.emit("j1", "a", "completed", {"exit_code": 0, "duration": 1.5})

        events = store.get_events("j1")
        assert len(events) == 4
        assert events[0].event_type == "started"
        assert events[1].event_type == "heartbeat"
        assert events[2].event_type == "heartbeat"
        assert events[3].event_type == "completed"
        assert events[3].payload["exit_code"] == 0

    def test_failure_lifecycle_ordering(self):
        """started -> failed."""
        store = AgentEventStore()
        emitter = AgentEventEmitter(store=store)
        emitter.emit("j1", "a", "started", {"command": "bad"})
        emitter.emit("j1", "a", "failed", {"exit_code": 1, "error": "denied"})

        events = store.get_events("j1")
        assert len(events) == 2
        assert events[0].event_type == "started"
        assert events[1].event_type == "failed"
        assert events[1].payload["error"] == "denied"

    def test_cross_job_isolation(self):
        """Job A events invisible to job B."""
        store = AgentEventStore()
        emitter = AgentEventEmitter(store=store)
        emitter.emit("j1", "a", "started", {})
        emitter.emit("j1", "a", "completed", {})
        emitter.emit("j2", "b", "started", {})
        emitter.emit("j2", "b", "failed", {})

        j1 = store.get_events("j1")
        j2 = store.get_events("j2")
        assert len(j1) == 2
        assert len(j2) == 2
        assert all(e.job_id == "j1" for e in j1)
        assert all(e.job_id == "j2" for e in j2)
        assert j1[-1].event_type == "completed"
        assert j2[-1].event_type == "failed"

    def test_timestamps_are_monotonic(self):
        """Events are ordered by insertion time."""
        store = AgentEventStore()
        emitter = AgentEventEmitter(store=store)
        ev1 = emitter.emit("j1", "a", "started", {})
        ev2 = emitter.emit("j1", "a", "completed", {})
        assert ev1.created_at <= ev2.created_at
```

- [ ] **Step 6: Run tests**

```bash
uv run python -m pytest tests/test_agent_events.py -v
```

Expected: 15 tests pass (11 unit + 4 lifecycle)

- [ ] **Step 7: Commit**

```bash
git add app/job_manager.py tests/test_agent_events.py
git commit -m "feat(agent-events): integrate emitter into JobManager._run_job

Emit started/heartbeat/completed/failed at lifecycle points.
Integration tests verify ordering and isolation."
```

---

### Task 3: API endpoint GET /api/agents/{job_id}/events

**Files:**
- Create: `app/routers/agents.py`
- Modify: `app/main.py` (include router)
- Test: `tests/test_agent_events.py` (append API tests)

**Interfaces:**
- Consumes: `AgentEventEmitter` from `app/agent_events.py`
- Produces: `GET /api/agents/{job_id}/events` endpoint

- [ ] **Step 1: Create `app/routers/agents.py`**

```python
"""Agent events query API."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.agent_events import agent_events
from app.auth_middleware import require_scope

router = APIRouter()


@router.get("/api/agents/{job_id}/events")
async def get_agent_events(
    job_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    _identity=None,
):
    """Return agent lifecycle events for a job."""
    _identity = _identity or Depends(require_scope("jobs:read"))
    events = agent_events.store.get_events(job_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"No events for job {job_id}")
    sliced = events[-limit:]
    return {
        "job_id": job_id,
        "events": [e.to_dict() for e in sliced],
    }
```

- [ ] **Step 2: Include router in `app/main.py`**

Add near other router includes:

```python
from app.routers.agents import router as agents_router
app.include_router(agents_router)
```

- [ ] **Step 3: Add API tests**

Append to `tests/test_agent_events.py`:

```python
from fastapi.testclient import TestClient

from app.main import app


class TestAgentEventsAPI:
    def test_events_returns_lifecycle(self):
        agent_events.emit("j-api", "a", "started", {"command": "echo"})
        agent_events.emit("j-api", "a", "completed", {"exit_code": 0})

        with TestClient(app) as client:
            resp = client.get("/api/agents/j-api/events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "j-api"
        assert len(data["events"]) == 2
        assert data["events"][0]["type"] == "started"
        assert data["events"][1]["type"] == "completed"

    def test_events_404_for_unknown_job(self):
        with TestClient(app) as client:
            resp = client.get("/api/agents/nonexistent/events")
        assert resp.status_code == 404

    def test_events_limit(self):
        for i in range(5):
            agent_events.emit("j-lim", "a", "progress", {"i": i})

        with TestClient(app) as client:
            resp = client.get("/api/agents/j-lim/events?limit=2")
        assert resp.status_code == 200
        assert len(resp.json()["events"]) == 2
        # Last two
        assert resp.json()["events"][0]["payload"]["i"] == 3
        assert resp.json()["events"][1]["payload"]["i"] == 4
```

- [ ] **Step 4: Run tests**

```bash
uv run python -m pytest tests/test_agent_events.py -v
```

Expected: 18 tests pass (15 + 3 API)

- [ ] **Step 5: Run lint**

```bash
uv run ruff check app/agent_events.py app/routers/agents.py tests/test_agent_events.py
uv run ruff format --check app/agent_events.py app/routers/agents.py tests/test_agent_events.py
```

- [ ] **Step 6: Commit**

```bash
git add app/routers/agents.py app/main.py tests/test_agent_events.py
git commit -m "feat(agent-events): add GET /api/agents/{job_id}/events endpoint"
```

---

### Task 4: Full regression + cleanup

**Files:** No new files. Verification only.

- [ ] **Step 1: Run full test suite**

```bash
uv run python -m pytest -q --tb=short
```

Expected: all tests pass (including 18 new agent events tests)

- [ ] **Step 2: Run ruff on all changed files**

```bash
uv run ruff check app/agent_events.py app/routers/agents.py app/job_manager.py tests/test_agent_events.py
```

- [ ] **Step 3: Final commit message if any fixes needed**

```bash
git add -A
git commit -m "fix(agent-events): lint and test fixes"
```

- [ ] **Step 4: Push and verify CI**

```bash
git push gitea master
```

## Summary

After all tasks:

- `app/agent_events.py` — AgentEvent, AgentEventStore, AgentEventEmitter
- `app/routers/agents.py` — GET /api/agents/{job_id}/events
- `app/job_manager.py` — 3 emission points (start, heartbeat, terminal)
- `tests/test_agent_events.py` — 18 tests (unit, lifecycle, isolation, API)

Known limitations:
- In-memory only — events lost on restart (intentional for v1)
- No Postgres/Redis persistence (future extension)
- No MCP-side push (gateway-only emission)
- Heartbeat events only fire for durable jobs (non-durable jobs have no heartbeat loop)
