# Agent Events Observability Layer v2 — Design Spec

> Persistent agent lifecycle events with universal heartbeat, stale detection,
> and SSE replay. Supersedes the in-memory-only v1 shipped in `f889fb0`.

## 1. Goals

1. **Durable event history** — agent lifecycle events survive gateway restarts.
2. **Universal heartbeat** — every running job emits periodic heartbeat events, independent of worker output.
3. **Stale detection** — supervisor detects missing heartbeats and marks jobs as stale without changing lifecycle status.
4. **SSE stream with replay** — clients reconnect via `Last-Event-ID` without event loss.
5. **Observability-only** — events never affect job execution outcomes.

## 2. Architecture

```
                    PostgreSQL
               +------------------+
               |   agent_events   |  source of truth
               |  (sequence col)  |
               +--------+---------+
                        |
           +------------+-------------+
           |            |             |
        Query API    SSE replay    Supervisor
                                     |
                                     v
                              healthy / stale
                                 / recovered

Execution plane
   |
   +-- durable job
   |      +-- unified heartbeat timer
   |           +-- Redis lease renewal
   |           +-- lifecycle event emission
   |
   +-- non-durable job
          +-- lifecycle heartbeat timer

                        |
                        v
               AgentEventEmitter
                        |
                   persist first
                        |
                   live fan-out
```

## 3. Data Model

### 3.1 PostgreSQL table: agent_events

```sql
CREATE TABLE agent_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sequence    BIGSERIAL NOT NULL,
    job_id      VARCHAR(36) NOT NULL,
    attempt_id  VARCHAR(36) NOT NULL,
    owner_id    VARCHAR(128) NOT NULL,
    agent_id    VARCHAR(128) NOT NULL,
    event_type  VARCHAR(32) NOT NULL,
    payload     JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(sequence)
);

CREATE INDEX idx_agent_events_job_id ON agent_events(job_id);
CREATE INDEX idx_agent_events_job_seq ON agent_events(job_id, sequence);
CREATE INDEX idx_agent_events_job_attempt ON agent_events(job_id, attempt_id);
CREATE INDEX idx_agent_events_job_type_created
    ON agent_events(job_id, event_type, created_at DESC);
```

Fields:
- `sequence` BIGSERIAL NOT NULL UNIQUE — monotonically increasing, used as SSE cursor and replay watermark. Uniqueness enforced by constraint.
- `attempt_id` VARCHAR(36) NOT NULL — UUID generated on execution claim/start. Distinguishes different execution attempts of the same job (e.g. worker-1 dies, worker-2 recovers). Same `job_id` can have multiple attempts; events from different attempts are semantically independent.
- `owner_id` VARCHAR(128) NOT NULL — identity that owns the job (user, service account, API key). NOT the same as `agent_id`. Used for authorization.
- `agent_id` VARCHAR(128) NOT NULL — stable configured gateway/worker identity (e.g. `settings.AGENT_ID` or hostname). Does NOT change between attempts. May be absent in v1 (defaults to `"gateway"`).
- `event_type` — one of: `started`, `heartbeat`, `progress`, `completed`, `failed`, `cancelled`, `stale`, `recovered`.

### 3.2 SQLAlchemy model

```python
class AgentEventRecord(Base):
    __tablename__ = "agent_events"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    sequence = Column(BigInteger, Sequence("agent_events_sequence_seq"), nullable=False, unique=True)
    job_id = Column(String(36), nullable=False, index=True)
    attempt_id = Column(String(36), nullable=False, index=True)
    owner_id = Column(String(128), nullable=False)
    agent_id = Column(String(128), nullable=False)
    event_type = Column(String(32), nullable=False)
    payload = Column(JSONB, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
```

Uses explicit `Sequence("agent_events_sequence_seq")` to match the PostgreSQL `BIGSERIAL`. The `unique=True` constraint ensures the sequence is a valid cursor.

### 3.3 Alembic migration

`005_agent_events.py` — idempotent (check `_table_exists` before `create_table`), following existing migration patterns.

### 3.4 Retention

Heartbeat events at 30s cadence = 2880 events/job/day. Schema and indexes support retention queries. Automated retention cleanup is OUT OF SCOPE for v2 — can be added as a background task in a future iteration.

### 3.5 Execution attempts

Durable jobs can be recovered by a different worker after lease expiry:

```
job X
 attempt A (UUID-a) -> worker-1 -> dies
 attempt B (UUID-b) -> worker-2 -> recovery
```

Each attempt generates its own `attempt_id` on claim/start. All events for an attempt carry the same `attempt_id`. The query API filters by attempt when needed. Heartbeat/stale/recovered events from different attempts are unambiguous.

## 4. Dual-Write Emitter

### 4.1 Contract

```
emit(event)
  |
  +-- persist to PostgreSQL (source of truth)
  |       |
  |       +-- success -> record committed sequence
  |       +-- failure -> log warning, do NOT fail job
  |                     "observability degraded"
  |
  +-- publish to live subscribers / in-memory cache
          |
          +-- success -> done
          +-- failure -> event already persisted, replay covers it
```

Key rules:
- **Persist first** — PostgreSQL commit before live fan-out.
- PostgreSQL failure does NOT fail the job. Observability is degraded, not broken.
- `emit()` becomes `async` to await PG persistence. No `ensure_future` for source-of-truth writes.
- The emitter tracks the last committed sequence per job for watermark synchronization.
- Terminal events (completed/failed/cancelled) get retry/outbox in future versions.

### 4.2 Failure semantics

| Failure mode | Behavior | Signal |
|-------------|----------|--------|
| PG down | Event lost from query API, job continues | Warning log + `observability_degraded` health flag |
| Live fanout down | Event persisted, SSE replay covers it | No special signal (replay is the fallback) |
| Both down | Event lost entirely | Error log + health endpoint degraded |

### 4.3 API

```python
class AgentEventEmitter:
    async def emit(
        self,
        job_id: str,
        attempt_id: str,
        owner_id: str,
        agent_id: str,
        event_type: str,
        payload: dict | None = None,
    ) -> AgentEventRecord:
        """Persist to PG, then fan out to live subscribers.

        Returns the committed record. On PG failure, raises
        ObservabilityDegradedError (does NOT propagate to caller —
        the event is silently dropped with a warning log).
        """
        ...

    async def subscribe(self, job_id: str) -> EventSubscription:
        """Create a live subscription with committed watermark.

        Returns an EventSubscription with:
          - queue: asyncio.Queue for live events
          - watermark: last sequence committed to PG at subscription time
        """
        ...

    def get_committed_sequence(self, job_id: str) -> int | None:
        """Last sequence known committed to PG for this job."""
        ...
```

## 5. Heartbeat

### 5.1 One timer per job

```
durable job:
    unified heartbeat timer
        +-- Redis lease renewal (existing _heartbeat_loop logic)
        +-- lifecycle event emission (agent_events.emit)

non-durable job:
    lifecycle heartbeat timer only
```

Rationale: Two independent timers can diverge — Redis lease healthy but lifecycle loop dead, or vice versa. One timer, one source of truth for "is this job alive?"

**Exception isolation within the loop:** PG emit failure must NOT propagate to the lease renewal path. The two domains are isolated:

```python
if is_durable:
    try:
        ok = await self.redis_queue.heartbeat_durable_execution(...)
        if not ok:
            job.cancel_event.set()
            break
    except Exception:
        logger.warning("Lease renewal failed for %s", job_id)
        # Execution-level failure — existing semantics apply

try:
    job.last_heartbeat_at = time.time()
    job.heartbeat_seq += 1
    await agent_events.emit(...)
except ObservabilityDegradedError:
    logger.warning("Heartbeat emit failed for %s — observability degraded", job_id)
    # Observability failure — does NOT affect execution
```

PG outage must NOT kill the heartbeat task and cause false stale.

### 5.2 Initialization at running

When a job transitions to `running`, initialize heartbeat state immediately:

```python
job.status = "running"
job.last_heartbeat_at = time.time()   # <-- initialize HERE
job.supervisor_state = "healthy"
job.heartbeat_seq = 0
```

Without this, if the heartbeat task dies before its first tick (30s), `last_heartbeat_at` remains `None` and the stale loop skips the job forever.

### 5.3 Implementation

The existing `_heartbeat_loop` in `JobManager._run_job` is extended:

```python
HEARTBEAT_INTERVAL = 30  # seconds

async def _heartbeat_loop() -> None:
    interval = HEARTBEAT_INTERVAL

    while True:
        await asyncio.sleep(interval)
        if job.cancel_event.is_set() or job.status in TERMINAL_STATES:
            break

        # Durable: renew Redis lease (execution domain)
        if is_durable:
            try:
                ok = await self.redis_queue.heartbeat_durable_execution(...)
                if not ok:
                    job.cancel_event.set()
                    break
            except Exception:
                logger.warning("Lease renewal failed for %s", job_id)
                # Existing execution semantics apply — do not mask

        # All jobs: emit lifecycle heartbeat (observability domain)
        try:
            job.last_heartbeat_at = time.time()
            job.heartbeat_seq += 1
            await agent_events.emit(
                job_id, attempt_id, job.owner_id, agent_id,
                "heartbeat",
                {"state": job.status, "seq": job.heartbeat_seq},
            )
        except ObservabilityDegradedError:
            logger.warning("Heartbeat emit degraded for %s", job_id)
            # Timestamp still updated — stale detection continues

heartbeat_task = asyncio.create_task(_heartbeat_loop())
```

For non-durable jobs, the Redis lease block is skipped. The same timer drives both.

### 5.4 Terminal state handling

```python
# In finally block — do NOT set cancel_event on normal terminal path
heartbeat_task.cancel()
try:
    await heartbeat_task
except asyncio.CancelledError:
    pass
```

`cancel_event` remains exclusively for user/system cancellation requests. Heartbeat shutdown uses `heartbeat_task.cancel()`.

### 5.5 Configuration

```python
HEARTBEAT_INTERVAL = 30  # seconds, configurable via HEARTBEAT_INTERVAL env
STALE_THRESHOLD = 120    # seconds = 4 missed heartbeats, configurable via STALE_THRESHOLD env
```

## 6. Stale Detection

### 6.1 Two-dimensional status model

```
lifecycle_status:          supervisor_state:
    queued                     healthy
    running                    stale
    completed                  recovered
    failed                     needs_attention
    cancelled
```

**`stale` is NEVER written to `job.status`.** It lives in `job.supervisor_state`.

```python
job.status == "running"            # lifecycle — unchanged
job.supervisor_state == "stale"    # supervisor — new field
```

This ensures Redis fencing, terminal-state logic, `job_wait`, and all existing checks continue to work correctly.

### 6.2 Recovery flow

```
running + healthy
    |
    heartbeat stops
    |
    stale detection fires
    |
    running + stale  (stale_since = now)
    |
    heartbeat resumes (worker recovered)
    |
    recovered event emitted  (stale_duration = now - stale_since)
    |
    running + healthy  (stale_since = None)
```

### 6.3 Event types for stale lifecycle

- `stale` — emitted when detector fires. Payload: `last_heartbeat_at`, `missed_seconds`, `stale_since`.
- `recovered` — emitted when heartbeat resumes after stale period. Payload: `stale_duration` (now - stale_since), NOT the current `age` which would be small.

### 6.4 Supervisor independence

**Design requirement (v1):** Background loop inside `JobManager` is acceptable as initial implementation.

**Production requirement (future):** Stale detection must be runnable as an independent process/service, reading heartbeat timestamps from PostgreSQL. The current in-process loop is a stepping stone, not the final architecture.

```
Execution plane
    | heartbeat
    v
PostgreSQL agent_events
    ^
    |
Supervisor/control plane (independent process)
    |
    v
stale evaluation -> notifications / actions
```

The in-process stale detection loop is a v1 convenience. Production deployments must be able to run stale detection as an independent service reading from PostgreSQL.

### 6.5 Stale detection loop

```python
async def _stale_detection_loop(self):
    while True:
        await asyncio.sleep(30)
        async with self._lock:
            for job_id, job in self._jobs.items():
                if job.status != "running":
                    continue
                # last_heartbeat_at is always set at running transition (5.2)
                age = time.time() - job.last_heartbeat_at
                if age > STALE_THRESHOLD and job.supervisor_state != "stale":
                    job.supervisor_state = "stale"
                    job.stale_since = time.time()
                    await agent_events.emit(
                        job_id, job.attempt_id, job.owner_id, job.agent_id,
                        "stale",
                        {
                            "last_heartbeat_at": job.last_heartbeat_at,
                            "missed_seconds": age,
                            "stale_since": job.stale_since,
                        },
                    )
                    await job.notify_listeners({
                        "type": "status",
                        "status": "running",
                        "supervisor_state": "stale",
                        "message": "Heartbeat stale — needs attention",
                    })
                elif age <= STALE_THRESHOLD and job.supervisor_state == "stale":
                    stale_duration = time.time() - job.stale_since
                    job.supervisor_state = "healthy"
                    job.stale_since = None
                    await agent_events.emit(
                        job_id, job.attempt_id, job.owner_id, job.agent_id,
                        "recovered",
                        {"stale_duration": stale_duration},
                    )
                    await job.notify_listeners({
                        "type": "status",
                        "status": "running",
                        "supervisor_state": "healthy",
                        "message": "Heartbeat recovered",
                    })
```

## 7. SSE Stream with Replay

### 7.1 High-water-mark protocol

To close the replay/live race:

```
1. Subscribe via agent_events.subscribe() — captures committed PG watermark atomically
2. Replay DB events up to watermark
3. Stream live events > watermark from subscriber queue
```

The watermark is the **last sequence committed to PostgreSQL** at subscription time, not an in-memory counter. This ensures no events are lost between replay and live subscription.

```python
subscription = await agent_events.subscribe(job_id)
# subscription.watermark = last PG-committed sequence
# subscription.queue = live event queue
```

### 7.2 Endpoint

```
GET /api/agents/{job_id}/events/stream
Last-Event-ID: <sequence>
```

### 7.3 Last-Event-ID validation

```
non-integer      -> 400 Bad Request
negative         -> 400 Bad Request
cursor > watermark (future) -> 400 Bad Request (cursor beyond committed data)
cursor not belonging to this job -> 404 (no events found for job at that cursor)
cursor older than retention window -> 409 Conflict (replay_cursor_expired)
    Response: {"detail": "Replay cursor expired. Full resync required."}
    Client action: reconnect without Last-Event-ID to get full replay
```

Retention expiry is checked by querying the minimum sequence for the job: if `MIN(sequence) > requested_cursor`, the cursor is stale.

### 7.4 Queue overflow handling

When the subscriber queue is full (slow client):

```
queue overflow
    |
    subscriber marked as "gap detected"
    |
    live stream terminates with:
      event: error
      data: {"error": "queue_overflow", "last_sequence": <n>}
    |
    client reconnects with Last-Event-ID=<n>
    |
    PG replay recovers the gap
```

Lifecycle events must never be silently dropped. The stream explicitly terminates and signals the client to reconnect.

### 7.5 Terminal SSE condition

The stream must NOT depend on in-memory `job.status` for termination (the job record may not exist after restart). Termination is driven by:

1. **Terminal event received** — `completed`, `failed`, `cancelled` event type in the live stream.
2. **PG query on subscribe** — if the latest event for this job is terminal, replay it and close.
3. **Timeout** — `MAX_SSE_DURATION = 3600` (1 hour), same as existing job stream.

```python
async def event_generator():
    try:
        for ev in replay:
            yield f"id: {ev.sequence}\ndata: {json.dumps(ev.to_dict())}\n\n"
            if ev.event_type in ("completed", "failed", "cancelled"):
                return  # Terminal event in replay — done
        while True:
            try:
                event = await asyncio.wait_for(subscription.queue.get(), timeout=1.0)
                yield f"id: {event.sequence}\ndata: {json.dumps(event.to_dict())}\n\n"
                if event.event_type in ("completed", "failed", "cancelled"):
                    return  # Terminal event in live — done
            except asyncio.TimeoutError:
                yield ":keepalive\n\n"
            except QueueOverflowError:
                yield f"event: error\ndata: {{\"error\": \"queue_overflow\", \"last_sequence\": {subscription.watermark}}}\n\n"
                return
    finally:
        agent_events.store.remove_subscriber(job_id, subscription.queue)
```

### 7.6 Replay queries

```sql
-- Replay after sequence (for Last-Event-ID reconnect)
SELECT * FROM agent_events
WHERE job_id = $1 AND sequence > $2 AND sequence <= $3
ORDER BY sequence;

-- Full replay up to watermark (for initial connect)
SELECT * FROM agent_events
WHERE job_id = $1 AND sequence <= $2
ORDER BY sequence;

-- Check if cursor is expired (for 409 detection)
SELECT MIN(sequence) FROM agent_events WHERE job_id = $1;
```

Ordering by `sequence` (BIGSERIAL) — never by `created_at` alone (can have ties).

### 7.7 Ordering guarantee

`sequence` is the single ordering cursor. `created_at` is informational only. SSE `id` field carries the `sequence` value, which clients echo back as `Last-Event-ID` on reconnect.

## 8. CHECKS_RC=127 Normalization

### 8.1 Status model

Three-layer status, not one overloaded string:

```
execution_status:    completed | failed
verification_status: passed | failed | unavailable
acceptance_status:   accepted | rejected | needs_review
```

### 8.2 CHECKS_RC=127 semantics

```
worker exit != 0 + checks == 127 -> execution_status = FAILED (worker failure always wins)
worker exit == 0 + checks == 127 -> verification_status = unavailable, acceptance_status = needs_review
```

`needs_review` cannot pass automatic acceptance gate. Supervisor decision required.

### 8.3 Migration path

Current `needs-review-warning` status in `_build_opencode_script` is kept as-is for v2. Spec records the canonical enum for future normalization. The critical invariant: `needs_review != success` and `needs_review != failure`.

## 9. Identity Separation

```
owner_id  = identity that owns the job (user, service account, API key)
agent_id  = stable configured gateway/worker identity (does NOT change between attempts)
attempt_id = UUID generated on execution claim/start (unique per attempt)
```

- `owner_id` comes from auth middleware (`AuthIdentity.sub`). Used for authorization.
- `agent_id` comes from `settings.AGENT_ID` or platform hostname. Stable across attempts.
- `attempt_id` is a new UUID4 generated when a worker claims/starts a job. Different workers recovering the same durable job get different attempt IDs.
- Authorization checks use `owner_id` from the `agent_events` PostgreSQL table, NOT from the in-memory `JobRecord`. This ensures authorization survives gateway restarts.

### 9.1 Canonical source

| Field | Source | Lifetime |
|-------|--------|----------|
| `owner_id` | `AuthIdentity.sub` at job submission | Job lifetime |
| `agent_id` | `settings.AGENT_ID` or hostname | Gateway lifetime |
| `attempt_id` | `uuid4()` at execution claim/start | Attempt lifetime |

## 10. Testing Strategy

### Unit tests
- AgentEventStore: dual-write, PG persistence, live fan-out, failure semantics
- Heartbeat timer: unified loop for durable and non-durable, terminal cancellation, exception isolation
- Stale detection: threshold evaluation, recovery with stale_duration, two-dimensional status
- SSE replay: high-water-mark protocol, sequence ordering, queue overflow, terminal event detection

### Integration tests
- Full lifecycle: start -> heartbeat(s) -> completion, verify PG records
- Stale lifecycle: start -> heartbeat -> stall -> stale detection -> recovery -> verify stale_duration
- SSE replay: connect -> disconnect -> reconnect with Last-Event-ID, verify no gaps
- PG failure: emit during PG downtime, verify job continues, verify degradation signal
- Attempt isolation: worker-1 dies, worker-2 recovers, events from different attempts are distinguishable

### Adversarial tests
- Heartbeat cadence: verify periodic emission independent of worker output
- Stale detection: kill worker, verify stale event within threshold
- Heartbeat init: verify stale detection works even if heartbeat task dies before first tick
- Dual-write race: concurrent emit + PG failure, verify no crash
- SSE race: concurrent events during replay/live transition
- Queue overflow: slow client, verify stream terminates with error event and client can reconnect
- PG outage during emit: verify observability degraded but job continues, heartbeat timestamp still updated

## 11. Files Changed

| File | Action | Responsibility |
|------|--------|----------------|
| `app/agent_events.py` | Rewrite | Dual-write emitter (async), PG persistence, live fan-out, subscriber management, committed watermark tracking |
| `app/session_store.py` | Modify | Add `AgentEventRecord` model with `attempt_id`, `agent_id` |
| `app/job_manager.py` | Modify | Extend heartbeat loop (one timer, exception isolation), add `supervisor_state`/`stale_since`/`attempt_id`/`last_heartbeat_at` to JobRecord, stale detection loop |
| `app/routers/agents.py` | Rewrite | Query API from PG (ownership via PG, not in-memory), SSE stream with replay, queue overflow handling, Last-Event-ID validation |
| `app/config.py` | Modify | Add `HEARTBEAT_INTERVAL`, `STALE_THRESHOLD`, `AGENT_ID` env vars |
| `alembic/versions/005_agent_events.py` | Create | Migration with `attempt_id`, `UNIQUE(sequence)` constraint |
| `tests/test_agent_events_v2.py` | Create | Unit + integration + adversarial tests |

## 12. Known Limitations (v2)

- In-process stale detection loop (production needs independent service).
- No retry/outbox for terminal events (future hardening).
- No automated retention enforcement (schema supports it, cleanup is manual/out of scope).
- `needs-review-warning` not yet normalized to three-layer status model.
- `agent_id` defaults to `"gateway"` in v1 — multi-worker identity resolution is future work.
