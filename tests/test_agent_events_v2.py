"""Persistence tests for AgentEventRecord + AgentEventStore.

Runs against a real PostgreSQL instance. Each test gets an isolated
throwaway database (created from settings.database_url, or the default
local dev URL) so create_all/drop never touch shared tables.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent_event_store import AgentEventStore
from app.config import settings
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
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))

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
