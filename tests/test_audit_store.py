"""Tests for persistent audit log storage (PostgreSQL audit_log table).

Covers insert, query, filter, pagination, retention cleanup, migration
compat (auto-create idempotency), and execute-endpoint wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.audit import AuditEvent, AuditEventType, Decision
from app.audit_store import AuditLogStore
from app.session_store import AuditLogEntry, Base


def _make_event(**kwargs) -> AuditEvent:
    defaults = {
        "event_type": AuditEventType.COMMAND_EXECUTE,
        "actor_type": "api_key",
        "actor_name": "test-agent",
        "actor_fingerprint": "abc123def456",
        "source_ip": "10.0.0.1",
        "route": "POST /api/ssh/execute",
        "request_id": "req-1",
        "target_type": "session",
        "target_id": "sess-1",
        "policy": "default",
        "profile": "default",
        "decision": Decision.ALLOWED,
    }
    defaults.update(kwargs)
    return AuditEvent(**defaults)


def test_orm_model_defined():
    assert AuditLogEntry.__tablename__ == "audit_log"
    assert AuditLogEntry.id is not None
    assert AuditLogEntry.command is not None
    assert AuditLogEntry.exit_code is not None
    assert AuditLogEntry.duration_ms is not None
    assert AuditLogEntry.session_id is not None


def test_base_metadata_includes_audit_log():
    assert "audit_log" in Base.metadata.tables


@pytest_asyncio.fixture
async def store():
    s = AuditLogStore("sqlite+aiosqlite:///:memory:")
    await s.create_tables()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_event(store):
    rid = await store.insert_event(_make_event())
    assert rid is not None
    rows = await store.query()
    assert len(rows) == 1
    assert rows[0].session_id == "sess-1"
    assert rows[0].decision == Decision.ALLOWED


@pytest.mark.asyncio
async def test_insert_event_with_execution_details(store):
    event = _make_event()
    await store.insert_event(event, command="ls -la", exit_code=0, duration_ms=150)
    rows = await store.query()
    assert len(rows) == 1
    assert rows[0].command == "ls -la"
    assert rows[0].exit_code == 0
    assert rows[0].duration_ms == 150


@pytest.mark.asyncio
async def test_insert_event_tolerates_failure(tmp_path):
    bad = AuditLogStore("sqlite+aiosqlite:///does-not-exist/dir/audit.db")
    result = await bad.insert_event(_make_event())
    assert result is None
    await bad.close()


@pytest.mark.asyncio
async def test_insert_command_execution(store):
    rid = await store.insert_command_execution(
        session_id="sess-2",
        command="echo hi",
        exit_code=0,
        duration_ms=42,
        actor_type="agent_token",
        actor_name="ci",
        source_ip="10.0.0.2",
        route="POST /api/ssh/execute",
        request_id="req-2",
    )
    assert rid is not None
    rows = await store.query(session_id="sess-2")
    assert len(rows) == 1
    row = rows[0]
    assert row.command == "echo hi"
    assert row.exit_code == 0
    assert row.duration_ms == 42
    assert row.actor_type == "agent_token"
    assert row.target_id == "sess-2"


@pytest.mark.asyncio
async def test_insert_command_execution_denied(store):
    await store.insert_command_execution(
        session_id="sess-3",
        command="rm -rf /",
        exit_code=None,
        duration_ms=0,
        decision="denied",
        reason="blocked",
        event_type="command.deny",
    )
    rows = await store.query(decision="denied")
    assert len(rows) == 1
    assert rows[0].reason == "blocked"
    assert rows[0].exit_code is None


# ---------------------------------------------------------------------------
# Query / filters / pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_newest_first(store):
    for i in range(3):
        await store.insert_event(_make_event(target_id=f"sess-{i}"))
    rows = await store.query()
    assert len(rows) == 3
    assert rows[0].target_id == "sess-2"


@pytest.mark.asyncio
async def test_query_filter_session_id(store):
    await store.insert_event(_make_event(target_id="sess-a"))
    await store.insert_event(_make_event(target_id="sess-b"))
    rows = await store.query(session_id="sess-a")
    assert len(rows) == 1
    assert rows[0].session_id == "sess-a"


@pytest.mark.asyncio
async def test_query_filter_event_type(store):
    await store.insert_event(_make_event(event_type=AuditEventType.COMMAND_EXECUTE))
    await store.insert_event(
        _make_event(event_type=AuditEventType.COMMAND_DENY, decision=Decision.DENIED)
    )
    rows = await store.query(event_type=AuditEventType.COMMAND_DENY)
    assert len(rows) == 1
    assert rows[0].event_type == AuditEventType.COMMAND_DENY


@pytest.mark.asyncio
async def test_query_filter_decision(store):
    await store.insert_event(_make_event())
    await store.insert_event(
        _make_event(decision=Decision.DENIED, event_type=AuditEventType.COMMAND_DENY)
    )
    rows = await store.query(decision=Decision.DENIED)
    assert len(rows) == 1
    assert rows[0].decision == Decision.DENIED


@pytest.mark.asyncio
async def test_query_filter_since_until(store):
    old = AuditEvent(timestamp=(datetime.now(UTC) - timedelta(days=2)).isoformat())
    new = AuditEvent(timestamp=datetime.now(UTC).isoformat())
    await store.insert_event(old)
    await store.insert_event(new)
    since = datetime.now(UTC) - timedelta(days=1)
    until = datetime.now(UTC) + timedelta(days=1)
    rows = await store.query(since=since, until=until)
    assert len(rows) == 1
    assert rows[0].event_id == new.event_id


@pytest.mark.asyncio
async def test_query_pagination(store):
    for i in range(5):
        await store.insert_event(_make_event(target_id=f"sess-{i}"))
    rows = await store.query(limit=2, offset=0)
    assert len(rows) == 2
    assert rows[0].target_id == "sess-4"
    rows_page2 = await store.query(limit=2, offset=2)
    assert len(rows_page2) == 2
    assert rows_page2[0].target_id == "sess-2"


@pytest.mark.asyncio
async def test_query_empty(store):
    rows = await store.query()
    assert rows == []


@pytest.mark.asyncio
async def test_count(store):
    await store.insert_event(_make_event())
    await store.insert_event(_make_event(target_id="other"))
    assert await store.count() == 2
    assert await store.count(session_id="sess-1") == 1
    assert await store.count(session_id="nope") == 0


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_retention_removes_old(store):
    old = AuditEvent(timestamp=(datetime.now(UTC) - timedelta(days=30)).isoformat())
    recent = AuditEvent(timestamp=datetime.now(UTC).isoformat())
    await store.insert_event(old)
    await store.insert_event(recent)
    removed = await store.cleanup_retention(retention_days=7)
    assert removed == 1
    rows = await store.query()
    assert len(rows) == 1
    assert rows[0].event_id == recent.event_id


@pytest.mark.asyncio
async def test_cleanup_retention_keeps_recent(store):
    for i in range(3):
        await store.insert_event(_make_event(target_id=f"sess-{i}"))
    removed = await store.cleanup_retention(retention_days=7)
    assert removed == 0
    assert await store.count() == 3


@pytest.mark.asyncio
async def test_cleanup_retention_disabled(store):
    old = AuditEvent(timestamp=(datetime.now(UTC) - timedelta(days=30)).isoformat())
    await store.insert_event(old)
    removed = await store.cleanup_retention(retention_days=0)
    assert removed == 0
    assert await store.count() == 1


@pytest.mark.asyncio
async def test_cleanup_retention_batches(store):
    # insert 5 old + 1 recent; batch_size=2 forces multiple rounds
    for _i in range(5):
        await store.insert_event(
            AuditEvent(timestamp=(datetime.now(UTC) - timedelta(days=30)).isoformat())
        )
    await store.insert_event(_make_event())
    removed = await store.cleanup_retention(retention_days=7, batch_size=2)
    assert removed == 5
    assert await store.count() == 1


# ---------------------------------------------------------------------------
# Migration compat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tables_idempotent():
    s = AuditLogStore("sqlite+aiosqlite:///:memory:")
    await s.create_tables()
    await s.create_tables()  # second call must not raise
    await s.insert_event(_make_event())
    await s.close()


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------


def test_audit_log_entry_to_dict():
    entry = AuditLogEntry(
        id="row-1",
        event_id="evt-1",
        event_type="command.execute",
        session_id="sess-1",
        command="ls",
        exit_code=0,
        duration_ms=10,
    )
    d = entry.to_dict()
    assert d["event_type"] == "command.execute"
    assert d["command"] == "ls"
    assert d["exit_code"] == 0
    assert d["duration_ms"] == 10
    assert d["session_id"] == "sess-1"
    assert d["metadata"] == {}


# ---------------------------------------------------------------------------
# Execute-endpoint wiring (audit store no-op when not configured)
# ---------------------------------------------------------------------------


def test_audit_store_not_configured_is_noop():
    """When state.audit_log_store is None, the router helper does nothing."""
    from app.routers.ssh import _persist_command_audit

    async def run():
        # store is None in fresh state — call must not raise
        import app.state as st

        saved = st.audit_log_store
        st.audit_log_store = None
        try:
            await _persist_command_audit(
                session_id="s",
                command="ls",
                exit_code=0,
                duration_ms=5,
                actor_type="api_key",
                actor_name="",
                actor_fingerprint="x",
                source_ip="127.0.0.1",
                route="POST /api/ssh/execute",
                request_id="r",
            )
        finally:
            st.audit_log_store = saved

    import asyncio

    asyncio.run(run())


@pytest.mark.asyncio
async def test_audit_store_wired_inserts_row(store):
    """When store is configured, the helper persists a command row."""
    import app.state as st
    from app.routers.ssh import _persist_command_audit

    saved = st.audit_log_store
    st.audit_log_store = store
    try:
        await _persist_command_audit(
            session_id="sess-wired",
            command="whoami",
            exit_code=0,
            duration_ms=7,
            actor_type="api_key",
            actor_name="tester",
            actor_fingerprint="finger",
            source_ip="127.0.0.1",
            route="POST /api/ssh/execute",
            request_id="req-wired",
        )
    finally:
        st.audit_log_store = saved
    rows = await store.query(session_id="sess-wired")
    assert len(rows) == 1
    assert rows[0].command == "whoami"
    assert rows[0].exit_code == 0
