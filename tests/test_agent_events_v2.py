"""Persistence tests for AgentEventRecord + AgentEventStore.

Runs against a real PostgreSQL instance. Each test gets an isolated
throwaway database (created from settings.database_url, or the default
local dev URL) so create_all/drop never touch shared tables.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent_event_store import AgentEventStore
from app.agent_events import (
    AgentEventEmitter,
    DualWriteAgentEventEmitter,
    ObservabilityState,
)
from app.config import settings
from app.exceptions import ObservabilityDegradedError
from app.session_store import AgentEventRecord, Base

DEFAULT_DATABASE_URL = "postgresql+asyncpg://webssh:webssh@localhost:5432/webssh"


def _event_kwargs(**overrides):
    kwargs = {
        "job_id": str(uuid.uuid4()),
        "attempt_id": str(uuid.uuid4()),
        "owner_id": "owner-1",
        "agent_id": "gateway",
        "event_type": "JOB_STARTED",
        "payload": {"step": "init"},
    }
    kwargs.update(overrides)
    return kwargs


@pytest_asyncio.fixture
async def store():
    base_url = make_url(settings.database_url or DEFAULT_DATABASE_URL)
    db_name = f"agent_events_test_{uuid.uuid4().hex[:10]}"
    admin_url = base_url.set(database="postgres")
    test_url = base_url.set(database=db_name)

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except Exception as exc:  # noqa: BLE001 - any DBAPI/driver failure means no PG
        await admin_engine.dispose()
        pytest.skip(f"PostgreSQL unavailable for persistence tests: {exc}")

    engine = create_async_engine(test_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        yield AgentEventStore(maker)
    finally:
        await engine.dispose()
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_insert_returns_record_with_sequence(store):
    record = await store.insert(**_event_kwargs())
    assert isinstance(record, AgentEventRecord)
    assert record.id is not None
    assert record.sequence is not None
    assert record.created_at is not None
    assert record.payload == {"step": "init"}


@pytest.mark.asyncio
async def test_sequences_are_monotonic(store):
    job = str(uuid.uuid4())
    r1 = await store.insert(**_event_kwargs(job_id=job))
    r2 = await store.insert(**_event_kwargs(job_id=job))
    r3 = await store.insert(**_event_kwargs(job_id=job))
    assert r1.sequence < r2.sequence < r3.sequence


@pytest.mark.asyncio
async def test_get_events_ordering(store):
    job = str(uuid.uuid4())
    for event_type in ("EVENT_C", "EVENT_A", "EVENT_B"):
        await store.insert(**_event_kwargs(job_id=job, event_type=event_type))
    events = await store.get_events(job)
    assert len(events) == 3
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)
    assert [e.event_type for e in events] == ["EVENT_C", "EVENT_A", "EVENT_B"]


@pytest.mark.asyncio
async def test_get_events_after_up_to_filters(store):
    job = str(uuid.uuid4())
    inserted = [await store.insert(**_event_kwargs(job_id=job)) for _ in range(5)]
    seq2 = inserted[1].sequence
    seq4 = inserted[3].sequence
    events = await store.get_events(job, after_sequence=seq2, up_to_sequence=seq4)
    assert [e.sequence for e in events] == [
        inserted[2].sequence,
        inserted[3].sequence,
    ]


@pytest.mark.asyncio
async def test_get_latest_sequence(store):
    job = str(uuid.uuid4())
    assert await store.get_latest_sequence(job) is None
    records = [await store.insert(**_event_kwargs(job_id=job)) for _ in range(3)]
    latest = await store.get_latest_sequence(job)
    assert latest == max(r.sequence for r in records)


@pytest.mark.asyncio
async def test_get_owner_id(store):
    job = str(uuid.uuid4())
    assert await store.get_owner_id(job) is None
    await store.insert(**_event_kwargs(job_id=job, owner_id="tenant-a"))
    assert await store.get_owner_id(job) == "tenant-a"


# ---------------------------------------------------------------------------
# Task 2.1: DualWriteAgentEventEmitter — persist-first, degraded lifecycle
# (fake PG store: runs without PostgreSQL, CI-safe)
# ---------------------------------------------------------------------------


class _FakePGStore:
    """Scriptable stand-in for AgentEventStore with call-order logging."""

    def __init__(self, fail_first: int = 0):
        self.calls: list[tuple] = []
        self.queue_sizes_during_insert: list[int] = []
        self._seq = 0
        self._fail_first = fail_first

    async def insert(self, **kwargs):
        self.calls.append(("insert", kwargs["event_type"]))
        if self._fail_first > 0:
            self._fail_first -= 1
            raise RuntimeError("postgres unavailable")
        self._seq += 1
        return SimpleNamespace(sequence=self._seq, created_at=None)

    async def get_latest_sequence(self, job_id: str) -> int | None:
        self.calls.append(("get_latest_sequence", job_id))
        return self._seq if self._seq else None


def _make_emitter(fail_first: int = 0):
    pg = _FakePGStore(fail_first=fail_first)
    memory = AgentEventEmitter()
    emitter = DualWriteAgentEventEmitter(memory_emitter=memory, pg_store=pg)
    return emitter, pg


def test_degraded_state_machine_transitions():
    state = ObservabilityState()
    assert not state.is_degraded
    state.mark_degraded("boom")
    assert state.is_degraded and state.degraded_since is not None
    assert state.degraded_reason == "boom"
    first_since = state.degraded_since
    state.mark_degraded("boom-again")  # since must stay from FIRST failure
    assert state.degraded_since is first_since
    state.mark_healthy()
    assert not state.is_degraded
    assert state.degraded_since is None and state.degraded_reason is None


@pytest.mark.asyncio
async def test_pg_failure_marks_observability_degraded():
    emitter, _pg = _make_emitter(fail_first=1)
    with pytest.raises(ObservabilityDegradedError) as exc_info:
        await emitter.pg_emit(**_event_kwargs())
    st = emitter.observability_state
    assert st.is_degraded
    assert st.degraded_since is not None
    assert "postgres unavailable" in st.degraded_reason
    assert "postgres unavailable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_successful_write_clears_degraded():
    emitter, _pg = _make_emitter(fail_first=1)
    with pytest.raises(ObservabilityDegradedError):
        await emitter.pg_emit(**_event_kwargs())
    assert emitter.observability_state.is_degraded
    record = await emitter.pg_emit(**_event_kwargs(event_type="JOB_COMPLETED"))
    assert record is not None
    st = emitter.observability_state
    assert not st.is_degraded
    assert st.degraded_since is None and st.degraded_reason is None


@pytest.mark.asyncio
async def test_job_continues_when_observability_failed():
    """Caller pattern (heartbeat loop): catch degraded, keep working."""
    emitter, pg = _make_emitter(fail_first=2)
    memory = emitter.store
    processed: list[str] = []
    for step in ("step-1", "step-2", "step-3"):
        try:
            await emitter.pg_emit(**_event_kwargs(payload={"step": step}))
        except ObservabilityDegradedError:
            pass  # observability degraded != job failed
        memory.emit(str(_event_kwargs()["job_id"]), "gateway", "JOB_TICK")
        processed.append(step)
    assert processed == ["step-1", "step-2", "step-3"]
    assert len(pg.calls) == 3  # every attempt still tried to persist


@pytest.mark.asyncio
async def test_live_event_not_sent_before_commit():
    """Persist-first ordering: subscriber queue must be empty during insert."""
    emitter, pg = _make_emitter()
    sub = await emitter.subscribe("job-order")
    # capture queue size at the moment the fake store's insert runs
    original_insert = pg.insert

    async def probing_insert(**kwargs):
        pg.queue_sizes_during_insert.append(sub.queue.qsize())
        return await original_insert(**kwargs)

    pg.insert = probing_insert  # type: ignore[method-assign]
    await emitter.pg_emit(
        job_id="job-order",
        **{k: v for k, v in _event_kwargs(job_id="job-order").items() if k != "job_id"},
    )
    assert pg.queue_sizes_during_insert == [0], "fan-out happened before commit"
    assert sub.queue.qsize() == 1
    assert "insert" in [c[0] for c in pg.calls]


@pytest.mark.asyncio
async def test_pg_failure_raises_degraded_error_and_no_fanout():
    emitter, _pg = _make_emitter(fail_first=1)
    job_id = _event_kwargs()["job_id"]
    sub = await emitter.subscribe(job_id)
    with pytest.raises(ObservabilityDegradedError):
        await emitter.pg_emit(**_event_kwargs())
    assert sub.queue.empty()  # nothing committed -> nothing fanned out
    assert emitter.get_committed_sequence(job_id) is None


@pytest.mark.asyncio
async def test_live_fanout_after_commit():
    emitter, _pg = _make_emitter()
    job_id = _event_kwargs()["job_id"]
    sub = await emitter.subscribe(job_id)
    await emitter.pg_emit(**_event_kwargs(job_id=job_id))
    event = sub.queue.get_nowait()
    assert event["sequence"] == 1
    assert event["type"] == _event_kwargs()["event_type"]


@pytest.mark.asyncio
async def test_subscriber_gets_committed_watermark():
    """Fresh emitter (restart simulation) resolves watermark from PG."""
    pg = _FakePGStore()
    emitter_a = DualWriteAgentEventEmitter(memory_emitter=AgentEventEmitter(), pg_store=pg)
    job_id = _event_kwargs()["job_id"]
    await emitter_a.pg_emit(**_event_kwargs(job_id=job_id))
    await emitter_a.pg_emit(**_event_kwargs(job_id=job_id))

    # New process: no in-memory watermarks, must consult PG
    emitter_b = DualWriteAgentEventEmitter(memory_emitter=AgentEventEmitter(), pg_store=pg)
    sub = await emitter_b.subscribe(job_id)
    assert sub.watermark == 2
    assert ("get_latest_sequence", job_id) in pg.calls


@pytest.mark.asyncio
async def test_watermark_updates_after_emit():
    emitter, _pg = _make_emitter()
    job_id = _event_kwargs()["job_id"]
    assert emitter.get_committed_sequence(job_id) is None
    await emitter.pg_emit(**_event_kwargs(job_id=job_id))
    await emitter.pg_emit(**_event_kwargs(job_id=job_id))
    assert emitter.get_committed_sequence(job_id) == 2


@pytest.mark.asyncio
async def test_memory_cache_populated_after_commit():
    emitter, _pg = _make_emitter()
    job_id = _event_kwargs()["job_id"]
    await emitter.pg_emit(
        job_id=job_id,
        attempt_id=_event_kwargs()["attempt_id"],
        owner_id="owner-1",
        agent_id="gateway",
        event_type="HEARTBEAT",
        payload={"n": 1},
    )
    events = emitter.store.get_events(job_id)
    assert [e.event_type for e in events] == ["HEARTBEAT"]


@pytest.mark.asyncio
async def test_concurrent_emits_keep_unique_sequences():
    emitter, pg = _make_emitter()
    job_id = _event_kwargs()["job_id"]
    sub = await emitter.subscribe(job_id)

    async def one(i: int):
        await asyncio.sleep((i % 3) * 0.001)
        await emitter.pg_emit(**_event_kwargs(job_id=job_id, payload={"i": i}))

    await asyncio.gather(*(one(i) for i in range(5)))
    seqs = sorted(sub.queue.get_nowait()["sequence"] for _ in range(5))
    assert seqs == [1, 2, 3, 4, 5]
    assert emitter.get_committed_sequence(job_id) == 5
    assert not emitter.observability_state.is_degraded


# ---------------------------------------------------------------------------
# Task 2.2: lifespan wiring surface + /health degraded exposure
# ---------------------------------------------------------------------------


import app.routers.system as system_router  # noqa: E402
import app.state as app_state  # noqa: E402
from app.models import HealthComponentStatus  # noqa: E402
from app.session_store import SessionStore  # noqa: E402


def _wire_health(monkeypatch):
    """Bypass real infra probes; keep component aggregation logic intact."""

    async def fake_probes(*, redis_required, postgres_required, ssh_host, ssh_port):
        return {
            "redis": (True, None),
            "postgres": (True, None),
            "ssh": (True, None),
        }

    monkeypatch.setattr(system_router, "_run_health_probes", fake_probes)
    monkeypatch.setattr(settings, "persistent_sessions_enabled", True)


@pytest.mark.asyncio
async def test_health_reports_observability_degraded(monkeypatch):
    """Pinned aggregate rule: ANY degraded component flips gateway status."""
    _wire_health(monkeypatch)
    emitter, _pg = _make_emitter(fail_first=1)
    with pytest.raises(ObservabilityDegradedError):
        await emitter.pg_emit(**_event_kwargs())
    monkeypatch.setattr(app_state, "agent_event_emitter", emitter)

    resp = await system_router.health_check()

    obs = resp.components["observability"]
    assert isinstance(obs, HealthComponentStatus)
    assert obs.status == "degraded"
    assert obs.required is False
    assert obs.failure_class == "postgres_unavailable"
    assert "postgres unavailable" in (obs.reason or "")
    assert resp.status == "degraded"
    assert resp.ready is False


@pytest.mark.asyncio
async def test_health_clears_after_recovery_emit(monkeypatch):
    _wire_health(monkeypatch)
    emitter, _pg = _make_emitter(fail_first=1)
    with pytest.raises(ObservabilityDegradedError):
        await emitter.pg_emit(**_event_kwargs())
    await emitter.pg_emit(**_event_kwargs(event_type="JOB_COMPLETED"))
    monkeypatch.setattr(app_state, "agent_event_emitter", emitter)

    resp = await system_router.health_check()

    obs = resp.components["observability"]
    assert obs.status == "ok"
    assert obs.failure_class is None and obs.reason is None
    assert resp.status == "ok"


@pytest.mark.asyncio
async def test_health_ok_when_observability_not_wired(monkeypatch):
    _wire_health(monkeypatch)
    monkeypatch.setattr(app_state, "agent_event_emitter", None)

    resp = await system_router.health_check()

    obs = resp.components["observability"]
    assert obs.status == "ok"
    assert obs.required is False


def test_lifespan_wiring_uses_session_store_engine():
    """SessionStore exposes its session maker for the events store."""
    store = SessionStore("postgresql+asyncpg://u:p@localhost:5432/db")
    assert store.session_maker is None  # not connected yet — wiring must guard


def test_job_record_constructs_without_attempt_id():
    from app.job_manager import JobRecord

    job = JobRecord(job_id="j1", session_id="s1", command="true")
    assert job.attempt_id is None
    assert job.supervisor_state == "healthy"
    assert job.stale_since is None
    assert job.last_heartbeat_at is None
    assert job.heartbeat_seq == 0
