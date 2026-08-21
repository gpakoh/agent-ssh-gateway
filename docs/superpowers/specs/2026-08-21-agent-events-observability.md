# Agent Events Observability Layer — Design Spec

## Problem

`JobManager._run_job` drives agent lifecycle but emits lifecycle signals only through `JobRecord.notify_listeners` (live SSE push) and audit/webhook side channels. Neither provides:

- Persistent event history surviving restarts
- Queryable per-job timeline for supervisor diagnostics
- Structured heartbeat visibility
- In-memory ring buffer with bounded memory

Supervisor cannot answer: Is the agent alive? Where is it stuck? What did it actually do? When was the last heartbeat?

## Non-Goals

- No new task manager, queue, or Redis pipeline
- No replacement of `JobManager` or `notify_listeners`
- No Postgres/Redis persistence in v1 (in-memory ring buffer only)
- No MCP-side event push (gateway-only emission)

## Architecture

```
JobManager._run_job
        |
        v
AgentEventEmitter.emit(job_id, event_type, payload)
        |
        v
AgentEventStore  (in-memory ring buffer, per-job bounded)
        |
   +----+----+
   |         |
   v         v
GET /api/agents/{job_id}/events   (history query)
```

## Data Model

```python
@dataclass
class AgentEvent:
    id: str              # uuid4 hex
    job_id: str
    agent_id: str        # owner_id from JobRecord (fingerprint)
    event_type: str      # started | heartbeat | progress | completed | failed
    created_at: float    # time.time()
    payload: dict        # free-form structured data
```

## Event Types

| Type | When | Payload |
|------|------|---------|
| `started` | job.status set to "running" | `{command, session_id}` |
| `heartbeat` | periodic during execution | `{state: "running"}` |
| `progress` | arbitrary progress marker | `{step, message}` |
| `completed` | terminal status = completed | `{exit_code, duration}` |
| `failed` | terminal status = failed/error | `{exit_code, error, duration}` |

## Storage

`AgentEventStore` — singleton, in-memory:

- `MAX_EVENTS_PER_JOB = 100` (configurable)
- Per-job `deque(maxlen=MAX_EVENTS_PER_JOB)`
- `emit(job_id, agent_id, event_type, payload) -> AgentEvent`
- `get_events(job_id) -> list[AgentEvent]`
- `get_latest(job_id) -> AgentEvent | None`

Thread-safe via `asyncio` (single event loop). No external dependencies.

## Integration Points

In `JobManager._run_job` (`job_manager.py:530-787`):

1. **Start** (line 628-643): after `job.status = "running"`, emit `started`
2. **Heartbeat** (line 605-625): inside `_heartbeat_loop`, emit `heartbeat` each iteration
3. **Completion/Failure** (line 776-787): after terminal status set, emit `completed` or `failed`

## API

```
GET /api/agents/{job_id}/events
```

Response:
```json
{
  "job_id": "...",
  "events": [
    {"type": "started", "timestamp": "...", "payload": {...}},
    {"type": "heartbeat", "timestamp": "...", "payload": {...}},
    {"type": "completed", "timestamp": "...", "payload": {...}}
  ]
}
```

Requires scope: `jobs:read` (same as existing job endpoints).

## Testing

- Success lifecycle: create job → started → heartbeat(s) → completed
- Failure lifecycle: create job → started → failed
- Isolation: job A events != job B events
- Bounded storage: >MAX_EVENTS_PER_JOB triggers eviction
- API returns correct events for known job, 404 for unknown
