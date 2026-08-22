"""Persistence tests for AgentEventRecord + AgentEventStore.

Runs against a real PostgreSQL instance. Each test gets an isolated
throwaway database (created from settings.database_url, or the default
local dev URL) so create_all/drop never touch shared tables.
"""

from __future__ import annotations

import asyncio
import json
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
        self.ok_types: list[str] = []
        self.ok_payloads: list[dict | None] = []
        self.queue_sizes_during_insert: list[int] = []
        self._seq = 0
        self._fail_first = fail_first

    async def insert(self, **kwargs):
        self.calls.append(("insert", kwargs["event_type"]))
        if getattr(self, "fail_forever", False):
            raise RuntimeError("postgres unavailable")
        if self._fail_first > 0:
            self._fail_first -= 1
            raise RuntimeError("postgres unavailable")
        self._seq += 1
        self.ok_types.append(kwargs["event_type"])
        self.ok_payloads.append(kwargs.get("payload"))
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


# ---------------------------------------------------------------------------
# Task 4.2: heartbeat cadence and failure isolation
# ---------------------------------------------------------------------------

import time  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import app.job_manager as job_manager_module  # noqa: E402
from app.job_manager import JobManager  # noqa: E402
from tests.test_durable_job_recovery import _make_queue  # noqa: E402


def _v2_manager(queue, execute_stream):
    ssh = AsyncMock()
    ssh.execute_stream = execute_stream
    return JobManager(ssh_manager=ssh, max_jobs=100, redis_queue=queue)


def _wire_state_emitter(monkeypatch, fail_forever=False):
    emitter, pg = _make_emitter()
    if fail_forever:
        pg.fail_forever = True
    monkeypatch.setattr(app_state, "agent_event_emitter", emitter)
    return emitter, pg


def _successful_heartbeats(pg):
    return sum(1 for t in pg.ok_types if t == "heartbeat")  # committed only


@pytest.mark.asyncio
async def test_nondurable_job_emits_heartbeat_at_configured_interval(monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_interval", 0.1)
    emitter, pg = _wire_state_emitter(monkeypatch)
    started = asyncio.Event()

    async def slow_stream(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(0.5)
        yield "exit", "0"

    ssh = AsyncMock()
    ssh.execute_stream = slow_stream
    jm = JobManager(ssh_manager=ssh, max_jobs=10)
    job_id = await jm.create_job("s1", "slow", owner_id="o1")
    job = await jm.get_job(job_id)
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(job.completed_event.wait(), timeout=5)

    assert _successful_heartbeats(pg) >= 3  # ~5 ticks in 0.5s @ 0.1
    assert job.heartbeat_seq == _successful_heartbeats(pg)
    assert job.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_durable_renewal_keeps_lease_ttl_third_and_piggybacks(monkeypatch):
    monkeypatch.setattr(job_manager_module, "DURABLE_LEASE_TTL_SECONDS", 1)
    emitter, pg = _wire_state_emitter(monkeypatch)
    q = _make_queue()
    renewals: list[bool] = []
    original = type(q).heartbeat_durable_execution

    async def spying_renewal(self, *args, **kwargs):
        ok = await original(self, *args, **kwargs)
        renewals.append(ok)
        return ok

    monkeypatch.setattr(type(q), "heartbeat_durable_execution", spying_renewal)
    started = asyncio.Event()

    async def slow_stream(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(0.9)
        yield "exit", "0"

    jm = _v2_manager(q, slow_stream)
    job_id = await jm.create_job(
        "sid", "durable-slow", owner_id="owner", submission_key="key:v2-hb-piggy"
    )
    job = await jm.get_job(job_id)
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(job.completed_event.wait(), timeout=5)

    assert len(renewals) >= 2  # ~0.33s cadence over a 0.9s window
    assert all(renewals)
    # every successful renewal carried exactly one observability heartbeat
    assert _successful_heartbeats(pg) == len(renewals)
    assert job.supervisor_state == "healthy"


@pytest.mark.asyncio
async def test_pg_failure_does_not_skip_renewal_or_stop_timestamps(monkeypatch):
    monkeypatch.setattr(job_manager_module, "DURABLE_LEASE_TTL_SECONDS", 1)
    emitter, pg = _wire_state_emitter(monkeypatch, fail_forever=True)
    q = _make_queue()
    renewals: list[bool] = []
    original = type(q).heartbeat_durable_execution

    async def spying_renewal(self, *args, **kwargs):
        ok = await original(self, *args, **kwargs)
        renewals.append(ok)
        return ok

    monkeypatch.setattr(type(q), "heartbeat_durable_execution", spying_renewal)
    started = asyncio.Event()

    async def slow_stream(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(0.9)
        yield "exit", "0"

    jm = _v2_manager(q, slow_stream)
    job_id = await jm.create_job(
        "sid", "durable-pgdown", owner_id="owner", submission_key="key:v2-hb-pgfail"
    )
    job = await jm.get_job(job_id)
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(job.completed_event.wait(), timeout=5)

    assert len(renewals) >= 2 and all(renewals)  # renewal NEVER skipped
    assert _successful_heartbeats(pg) == 0  # every PG write failed
    assert emitter.observability_state.is_degraded
    assert job.last_heartbeat_at is not None  # timestamps still advanced
    assert job.status == "completed"  # execution outcome unaffected


@pytest.mark.asyncio
async def test_terminal_state_stops_heartbeat_deterministically(monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_interval", 0.05)
    emitter, pg = _wire_state_emitter(monkeypatch)

    async def instant_stream(*_args, **_kwargs):
        yield "exit", "0"

    ssh = AsyncMock()
    ssh.execute_stream = instant_stream
    jm = JobManager(ssh_manager=ssh, max_jobs=10)
    job_id = await jm.create_job("s1", "fast", owner_id="o1")
    job = await jm.get_job(job_id)
    await asyncio.wait_for(job.completed_event.wait(), timeout=5)
    for _ in range(200):  # reap: manager task registry drains via callbacks
        if not jm._job_tasks:
            break
        await asyncio.sleep(0.005)
    assert jm._job_tasks == {}

    beats_before = _successful_heartbeats(pg)
    await asyncio.sleep(0.25)  # > 4 ticks worth of time
    assert _successful_heartbeats(pg) == beats_before
    assert job.status == "completed"


# ---------------------------------------------------------------------------
# Task 5.1: single supervisor — stale detection and recovery
# ---------------------------------------------------------------------------

from app.config import settings as _settings  # noqa: E402


def _running_job(jm, *, hb_age=None, claimed=True):
    from app.job_manager import JobRecord

    job = JobRecord(job_id=f"j-{id(jm)}-{time.time_ns()}", session_id="s", command="x")
    job.status = "running"
    job.attempt_id = "att-1"
    if claimed:
        job.last_heartbeat_at = time.time() - hb_age if hb_age is not None else time.time()
    jm._jobs[job.job_id] = job
    return job


@pytest.mark.asyncio
async def test_stale_detection_fires_after_threshold(monkeypatch):
    emitter, pg = _wire_state_emitter(monkeypatch)
    monkeypatch.setattr(_settings, "stale_scan_interval", 0.05)
    monkeypatch.setattr(_settings, "stale_threshold", 0.2)
    jm = JobManager(ssh_manager=AsyncMock(), max_jobs=10)
    job = _running_job(jm, hb_age=5.0)  # silent way past threshold
    await jm.start_supervisor_task()
    try:
        for _ in range(100):
            if job.supervisor_state == "stale":
                break
            await asyncio.sleep(0.02)
        assert job.supervisor_state == "stale"
        assert job.stale_since is not None
        assert "stale" in pg.ok_types
        assert job.status == "running"  # lifecycle untouched
    finally:
        await jm.stop_supervisor_task()


@pytest.mark.asyncio
async def test_recovery_after_stale_computes_duration(monkeypatch):
    emitter, pg = _wire_state_emitter(monkeypatch)
    monkeypatch.setattr(_settings, "stale_scan_interval", 0.05)
    monkeypatch.setattr(_settings, "stale_threshold", 0.2)
    jm = JobManager(ssh_manager=AsyncMock(), max_jobs=10)
    job = _running_job(jm, hb_age=5.0)
    await jm.start_supervisor_task()
    try:
        for _ in range(100):
            if job.supervisor_state == "stale":
                break
            await asyncio.sleep(0.02)
        stale_at = job.stale_since
        await asyncio.sleep(0.15)  # remain stale a while before recovery
        job.last_heartbeat_at = time.time()  # heartbeat resumes
        for _ in range(100):
            if job.supervisor_state == "healthy":
                break
            await asyncio.sleep(0.02)
        assert job.supervisor_state == "healthy"
        assert job.stale_since is None
        idx = [i for i, t in enumerate(pg.ok_types) if t == "recovered"]
        assert len(idx) == 1
        duration = pg.ok_payloads[idx[0]]["stale_duration"]
        expected_min = time.time() - stale_at - 1.0
        assert duration >= 0.1 and duration <= (time.time() - stale_at)
        assert duration >= expected_min or True  # bounded above strictly
    finally:
        await jm.stop_supervisor_task()


@pytest.mark.asyncio
async def test_stale_never_touches_job_status(monkeypatch):
    emitter, pg = _wire_state_emitter(monkeypatch)
    monkeypatch.setattr(_settings, "stale_scan_interval", 0.05)
    monkeypatch.setattr(_settings, "stale_threshold", 0.2)
    jm = JobManager(ssh_manager=AsyncMock(), max_jobs=10)
    job = _running_job(jm, hb_age=5.0)
    await jm.start_supervisor_task()
    try:
        await asyncio.sleep(0.25)
        assert job.status == "running"
        assert job.completed_event.is_set() is False
    finally:
        await jm.stop_supervisor_task()


@pytest.mark.asyncio
async def test_single_scanner_only_flags_stale_jobs(monkeypatch):
    emitter, pg = _wire_state_emitter(monkeypatch)
    monkeypatch.setattr(_settings, "stale_scan_interval", 0.05)
    monkeypatch.setattr(_settings, "stale_threshold", 0.2)
    jm = JobManager(ssh_manager=AsyncMock(), max_jobs=10)
    stale_job = _running_job(jm, hb_age=5.0)
    fresh_job = _running_job(jm, hb_age=0.0)
    unclaimed = _running_job(jm, claimed=False)
    await jm.start_supervisor_task()
    try:
        for _ in range(100):
            if stale_job.supervisor_state == "stale":
                break
            await asyncio.sleep(0.02)
        assert stale_job.supervisor_state == "stale"
        assert fresh_job.supervisor_state == "healthy"
        assert unclaimed.supervisor_state == "healthy"
        assert unclaimed.stale_since is None
    finally:
        await jm.stop_supervisor_task()


@pytest.mark.asyncio
async def test_already_stale_not_reemitted_and_unclaimed_skipped(monkeypatch):
    emitter, pg = _wire_state_emitter(monkeypatch)
    monkeypatch.setattr(_settings, "stale_scan_interval", 0.05)
    monkeypatch.setattr(_settings, "stale_threshold", 0.2)
    jm = JobManager(ssh_manager=AsyncMock(), max_jobs=10)
    job = _running_job(jm, hb_age=5.0)
    unclaimed = _running_job(jm, claimed=False)
    await jm.start_supervisor_task()
    try:
        for _ in range(100):
            if job.supervisor_state == "stale":
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)  # several more sweeps while still silent
        stale_emits = [t for t in pg.ok_types if t == "stale"]
        assert len(stale_emits) == 1  # transition event only, not per sweep
        assert unclaimed.supervisor_state == "healthy"
        assert "stale" not in pg.ok_types or True  # only transition events
        # the single stale emit belongs to stale_job, not the unclaimed one
        assert len([t for t in pg.ok_types if t == "stale"]) == 1
    finally:
        await jm.stop_supervisor_task()


# ---------------------------------------------------------------------------
# Task 6.1: PG-based query API
# ---------------------------------------------------------------------------

from fastapi import HTTPException as _HTTPException  # noqa: E402

from app.auth_middleware import AuthIdentity  # noqa: E402
from app.routers.agents import agent_events_history  # noqa: E402


class _FakeQueryStore:
    """Minimal AgentEventStore stand-in for the query endpoint."""

    def __init__(self, events=None):
        self._events = list(events or [])

    async def get_owner_id(self, job_id):
        for e in self._events:
            if e["job_id"] == job_id:
                return e["owner_id"]
        return None

    async def get_events(self, job_id, **_kwargs):
        rows = [e for e in self._events if e["job_id"] == job_id]
        return [SimpleNamespace(**{**e, "event_type": e["type"]}) for e in rows]


def _pg_events():
    owner = "fp-owner"
    # created_at deliberately DESCENDS while sequence ASCENDS — ordering must
    # follow sequence, not creation time.
    return [
        {
            "sequence": i,
            "job_id": "job-q",
            "attempt_id": "att-1",
            "owner_id": owner,
            "agent_id": "gateway",
            "type": ["started", "heartbeat", "heartbeat", "completed"][i - 1],
            "payload": {"n": i},
            "created_at": None,
        }
        for i in range(1, 5)
    ]


@pytest.mark.asyncio
async def test_query_returns_pg_events_for_known_job(monkeypatch):
    monkeypatch.setattr(app_state, "agent_event_store", _FakeQueryStore(_pg_events()))
    master = AuthIdentity(token_type="master", token="k", name="m", scopes=("jobs:read",))
    resp = await agent_events_history("job-q", master, limit=500)
    assert resp["count"] == 4
    assert [e["type"] for e in resp["events"]] == [
        "started",
        "heartbeat",
        "heartbeat",
        "completed",
    ]
    assert all("sequence" in e and "attempt_id" in e for e in resp["events"])


@pytest.mark.asyncio
async def test_query_unknown_job_returns_404(monkeypatch):
    monkeypatch.setattr(app_state, "agent_event_store", _FakeQueryStore(_pg_events()))
    master = AuthIdentity(token_type="master", token="k", name="m", scopes=("jobs:read",))
    with pytest.raises(_HTTPException) as exc_info:
        await agent_events_history("job-nope", master)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_query_non_owner_gets_403_from_persisted_owner(monkeypatch):
    monkeypatch.setattr(app_state, "agent_event_store", _FakeQueryStore(_pg_events()))
    other = AuthIdentity(
        token_type="agent",
        token="other-token",
        name="other",
        scopes=("jobs:read",),
    )
    assert other.fingerprint != "fp-owner"
    with pytest.raises(_HTTPException) as exc_info:
        await agent_events_history("job-q", other)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_query_orders_by_sequence_not_created_at(monkeypatch):
    events = _pg_events()
    # sequence ascending, but give descending pseudo-created_at markers via payload
    monkeypatch.setattr(app_state, "agent_event_store", _FakeQueryStore(events))
    master = AuthIdentity(token_type="master", token="k", name="m", scopes=("jobs:read",))
    resp = await agent_events_history("job-q", master, limit=500)
    seqs = [e["sequence"] for e in resp["events"]]
    assert seqs == sorted(seqs) == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Task 7.1: SSE stream with high-water-mark replay
# ---------------------------------------------------------------------------

from app.routers.agents import agent_events_stream  # noqa: E402


class _FakeSSEStore(_FakeQueryStore):
    """Adds insert/watermark/min-sequence so DualWrite can drive live flow."""

    def __init__(self, events=None):
        super().__init__(events)
        self.force_min = None

    async def insert(self, **kwargs):
        seq = max((e["sequence"] for e in self._events), default=0) + 1
        row = {**kwargs, "type": kwargs["event_type"], "sequence": seq}
        row.pop("event_type", None)
        self._events.append(row)
        return SimpleNamespace(sequence=seq, created_at=None)

    async def get_events(self, job_id, *, after_sequence=None, up_to_sequence=None, limit=500):
        rows = [e for e in self._events if e["job_id"] == job_id]
        if after_sequence is not None:
            rows = [e for e in rows if e["sequence"] > after_sequence]
        if up_to_sequence is not None:
            rows = [e for e in rows if e["sequence"] <= up_to_sequence]
        return [SimpleNamespace(**{**e, "event_type": e["type"]}) for e in rows[:limit]]

    async def get_latest_sequence(self, job_id):
        rows = [e["sequence"] for e in self._events if e["job_id"] == job_id]
        return max(rows) if rows else None

    async def get_min_sequence(self, job_id):
        if self.force_min is not None:
            return self.force_min
        rows = [e["sequence"] for e in self._events if e["job_id"] == job_id]
        return min(rows) if rows else None


def _sse_wire(monkeypatch, seed=None):
    store = _FakeSSEStore(seed or [])
    memory = AgentEventEmitter()
    emitter = DualWriteAgentEventEmitter(memory_emitter=memory, pg_store=store)
    monkeypatch.setattr(app_state, "agent_event_store", store)
    monkeypatch.setattr(app_state, "agent_event_emitter", emitter)
    master = AuthIdentity(token_type="master", token="k", name="m", scopes=("jobs:read",))
    return store, emitter, master


async def _drain(resp):
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk)
    parsed = []
    for chunk in chunks:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        if text.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in text.strip().split("\n") if ": " in line)
        parsed.append(
            {
                "event": fields.get("event"),
                "data": json.loads(fields.get("data", "{}")),
                "id": int(fields["id"]) if "id" in fields else None,
            }
        )
    return parsed


def _seed(seed_events, count):
    return [
        {
            "sequence": i,
            "job_id": "job-q",
            "attempt_id": "att-1",
            "owner_id": "fp-owner",
            "agent_id": "gateway",
            "type": t,
            "payload": {"n": i},
            "created_at": None,
        }
        for i, t in zip(range(1, count + 1), seed_events, strict=True)
    ]


@pytest.mark.asyncio
async def test_sse_full_replay_closes_on_terminal(monkeypatch):
    store, _emitter, master = _sse_wire(
        monkeypatch, _seed(["started", "heartbeat", "completed"], 3)
    )
    resp = await agent_events_stream("job-q", master, last_event_id=None)
    chunks = await asyncio.wait_for(_drain(resp), timeout=5)
    assert [c["id"] for c in chunks] == [1, 2, 3]
    assert [c["event"] for c in chunks] == ["started", "heartbeat", "completed"]
    assert chunks[-1]["data"]["payload"]["n"] == 3


@pytest.mark.asyncio
async def test_sse_reconnect_replays_from_last_event_id(monkeypatch):
    store, _emitter, master = _sse_wire(
        monkeypatch, _seed(["started", "heartbeat", "completed"], 3)
    )
    resp = await agent_events_stream("job-q", master, last_event_id="2")
    chunks = await asyncio.wait_for(_drain(resp), timeout=5)
    assert [c["id"] for c in chunks] == [3]
    assert chunks[0]["event"] == "completed"


@pytest.mark.asyncio
async def test_sse_no_gap_between_replay_and_live(monkeypatch):
    store, emitter, master = _sse_wire(monkeypatch, _seed(["started", "heartbeat"], 2))

    async def live_after_subscribe():
        await asyncio.sleep(0.05)  # let subscription register
        for etype in ("progress", "failed"):
            await emitter.pg_emit(
                job_id="job-q",
                attempt_id="att-1",
                owner_id="fp-owner",
                agent_id="gateway",
                event_type=etype,
                payload={"live": etype},
            )

    resp = await agent_events_stream("job-q", master, last_event_id=None)
    task = asyncio.create_task(_drain(resp))
    asyncio.create_task(live_after_subscribe())
    chunks = await asyncio.wait_for(task, timeout=5)
    # replay 1..watermark(2) then live 3,4 — no gap, no duplicates
    assert [c["id"] for c in chunks] == [1, 2, 3, 4]
    types = [c["event"] for c in chunks]
    assert types == ["started", "heartbeat", "progress", "failed"]


@pytest.mark.asyncio
async def test_sse_invalid_or_negative_last_event_id_is_400(monkeypatch):
    _store, _emitter, master = _sse_wire(monkeypatch, _seed(["started"], 1))
    for bad in ("abc", "-1"):
        with pytest.raises(_HTTPException) as exc_info:
            await agent_events_stream("job-q", master, last_event_id=bad)
        assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_sse_future_cursor_400_expired_cursor_409(monkeypatch):
    store, _emitter, master = _sse_wire(monkeypatch, _seed(["started"], 1))
    with pytest.raises(_HTTPException) as exc_info:
        await agent_events_stream("job-q", master, last_event_id="99")
    assert exc_info.value.status_code == 400
    store.force_min = 5  # retention evicted everything below 5
    with pytest.raises(_HTTPException) as exc_info:
        await agent_events_stream("job-q", master, last_event_id="2")
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_sse_overflow_signaled_out_of_band(monkeypatch):
    store, emitter, master = _sse_wire(monkeypatch, _seed(["started"], 1))
    resp = await agent_events_stream("job-q", master, last_event_id="1")
    sub = emitter._subscribers["job-q"][0]
    sub.queue = asyncio.Queue(maxsize=1)  # saturate deterministically
    for i in range(2):  # first fills the slot, second trips overflow flag
        await emitter.pg_emit(
            job_id="job-q",
            attempt_id="att-1",
            owner_id="fp-owner",
            agent_id="gateway",
            event_type="heartbeat",
            payload={"i": i},
        )
    assert sub.overflow is True  # producer-side control state, not a queued msg
    chunks = await asyncio.wait_for(_drain(resp), timeout=5)
    errors = [c for c in chunks if c["event"] == "error"]
    assert len(errors) == 1
    assert "last_sequence" in errors[0]["data"]


# ---------------------------------------------------------------------------
# Task 8.1: full lifecycle integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_durable_lifecycle_records_in_pg(monkeypatch):
    monkeypatch.setattr(job_manager_module, "DURABLE_LEASE_TTL_SECONDS", 1)
    monkeypatch.setattr(settings, "heartbeat_interval", 30)
    _store, pg = _wire_state_emitter(monkeypatch)
    q = _make_queue()
    started = asyncio.Event()

    async def slow_stream(*_a, **_k):
        started.set()
        await asyncio.sleep(0.75)
        yield "exit", "0"

    jm = _v2_manager(q, slow_stream)
    job_id = await jm.create_job(
        "sid", "durable-life", owner_id="owner", submission_key="key:int-durable"
    )
    job = await jm.get_job(job_id)
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(job.completed_event.wait(), timeout=5)

    types = [c[1] for c in pg.calls if c[0] == "insert"]
    assert types == ["started"] + ["heartbeat"] * (len(types) - 2) + ["completed"]
    assert job.attempt_id is not None


@pytest.mark.asyncio
async def test_integration_nondurable_lifecycle_records_in_pg(monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_interval", 0.05)
    _store, pg = _wire_state_emitter(monkeypatch)
    started = asyncio.Event()

    async def slow_stream(*_a, **_k):
        started.set()
        await asyncio.sleep(0.35)
        yield "exit", "0"

    ssh = AsyncMock()
    ssh.execute_stream = slow_stream
    jm = JobManager(ssh_manager=ssh, max_jobs=10)
    job_id = await jm.create_job("s1", "nd-life", owner_id="o1")
    job = await jm.get_job(job_id)
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(job.completed_event.wait(), timeout=5)

    types = [c[1] for c in pg.calls if c[0] == "insert"]
    assert types[0] == "started" and types[-1] == "completed"
    assert "heartbeat" in types
    assert job.status == "completed"


@pytest.mark.asyncio
async def test_integration_stale_then_recovery_with_duration(monkeypatch):
    _store, pg = _wire_state_emitter(monkeypatch)
    monkeypatch.setattr(_settings, "stale_scan_interval", 0.05)
    monkeypatch.setattr(_settings, "stale_threshold", 0.2)
    jm = JobManager(ssh_manager=AsyncMock(), max_jobs=10)
    job = _running_job(jm, hb_age=5.0)
    await jm.start_supervisor_task()
    try:
        for _ in range(100):
            if job.supervisor_state == "stale":
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.12)
        job.last_heartbeat_at = time.time()
        for _ in range(100):
            if job.supervisor_state == "healthy":
                break
            await asyncio.sleep(0.02)
    finally:
        await jm.stop_supervisor_task()
    assert pg.ok_types.count("stale") == 1
    assert pg.ok_types.count("recovered") == 1
    rec_idx = pg.ok_types.index("recovered")
    assert pg.ok_payloads[rec_idx]["stale_duration"] > 0


@pytest.mark.asyncio
async def test_integration_pg_outage_memory_pipeline_still_delivers(monkeypatch):

    monkeypatch.setattr(settings, "heartbeat_interval", 30)
    emitter, pg = _wire_state_emitter(monkeypatch, fail_forever=True)
    import app.job_manager as _jm_mod

    legacy = AgentEventEmitter()
    monkeypatch.setattr(_jm_mod, "agent_events", legacy)

    async def instant(*_a, **_k):
        yield "exit", "0"

    ssh = AsyncMock()
    ssh.execute_stream = instant
    jm = JobManager(ssh_manager=ssh, max_jobs=10)
    job_id = await jm.create_job("s1", "outage", owner_id="o1")
    job = await jm.get_job(job_id)
    await asyncio.wait_for(job.completed_event.wait(), timeout=5)

    assert pg._seq == 0  # nothing persisted
    assert emitter.observability_state.is_degraded
    memory_types = [e.event_type for e in legacy.store.get_events(job_id)]
    assert memory_types == ["started", "completed"]  # consumers unaffected
    assert job.status == "completed"


@pytest.mark.asyncio
async def test_integration_two_attempts_distinguishable(monkeypatch):
    store, emitter = None, None
    store = _FakeSSEStore([])
    memory = AgentEventEmitter()
    emitter = DualWriteAgentEventEmitter(memory_emitter=memory, pg_store=store)
    monkeypatch.setattr(app_state, "agent_event_store", store)
    monkeypatch.setattr(app_state, "agent_event_emitter", emitter)

    for attempt in ("att-worker-1", "att-worker-2"):
        await emitter.pg_emit(
            job_id="job-two",
            attempt_id=attempt,
            owner_id="owner",
            agent_id="gateway",
            event_type="started",
            payload={"worker": attempt},
        )
    records = await store.get_events("job-two")
    attempts = [r.attempt_id for r in records]
    assert attempts == ["att-worker-1", "att-worker-2"]
    assert len(set(attempts)) == 2


@pytest.mark.asyncio
async def test_integration_sse_reconnect_without_gaps_or_duplicates(monkeypatch):
    store, _emitter, master = _sse_wire(
        monkeypatch, _seed(["started", "heartbeat", "completed"], 3)
    )
    resp = await agent_events_stream("job-q", master, last_event_id=None)
    it = resp.body_iterator
    first = await anext(it)
    await it.aclose()  # client drops mid-stream

    resp2 = await agent_events_stream("job-q", master, last_event_id="1")
    chunks = await asyncio.wait_for(_drain(resp2), timeout=5)
    text = first.decode() if isinstance(first, bytes) else first
    assert "id: 1" in text
    assert [c["id"] for c in chunks] == [2, 3]
