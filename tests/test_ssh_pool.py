"""Tests for SSH connection pooling and session pre-warming (issue #5)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ssh_pool import ConnectionPool


def _mock_client(alive: bool = True):
    client = MagicMock()
    transport = MagicMock()
    transport.is_active.return_value = alive
    client.get_transport.return_value = transport
    return client


# ---------------------------------------------------------------------------
# ConnectionPool basics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_disabled_by_default():
    pool = ConnectionPool(max_size=0)
    assert not pool.enabled
    client = _mock_client()
    key = ("host", 22, "user", "password")
    assert await pool.acquire(key) is None
    await pool.release(key, client)
    assert pool.idle_count() == 0
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_pool_acquire_miss_counts():
    pool = ConnectionPool(max_size=4)
    key = ("host", 22, "user", "password")
    assert await pool.acquire(key) is None
    assert pool.stats()["misses"] == 1
    assert pool.stats()["hits"] == 0


@pytest.mark.asyncio
async def test_pool_release_then_acquire_hit():
    pool = ConnectionPool(max_size=4)
    key = ("host", 22, "user", "password")
    client = _mock_client()
    await pool.release(key, client)
    assert pool.idle_count() == 1
    got = await pool.acquire(key)
    assert got is client
    assert pool.stats()["hits"] == 1
    assert pool.idle_count() == 0


@pytest.mark.asyncio
async def test_pool_key_isolated_by_auth_method():
    pool = ConnectionPool(max_size=4)
    client = _mock_client()
    await pool.release(("h", 22, "u", "password"), client)
    # different auth_method (key) must be a miss
    assert await pool.acquire(("h", 22, "u", "key")) is None
    assert pool.stats()["misses"] == 1


@pytest.mark.asyncio
async def test_pool_drops_dead_connection():
    pool = ConnectionPool(max_size=4)
    key = ("host", 22, "user", "password")
    dead = _mock_client(alive=False)
    await pool.release(key, dead)
    assert await pool.acquire(key) is None
    dead.close.assert_called_once()
    assert pool.stats()["misses"] == 1
    assert pool.stats()["hits"] == 0


@pytest.mark.asyncio
async def test_pool_ttl_expiry():
    pool = ConnectionPool(max_size=4, ttl_seconds=1)
    key = ("host", 22, "user", "password")
    client = _mock_client()
    await pool.release(key, client)
    await asyncio.sleep(1.2)
    # acquire must not return the stale connection (dropped)
    assert await pool.acquire(key) is None
    assert pool.stats()["misses"] == 1


@pytest.mark.asyncio
async def test_pool_lru_eviction_when_full():
    pool = ConnectionPool(max_size=2)
    k1 = ("h1", 22, "u", "password")
    k2 = ("h2", 22, "u", "password")
    c1 = _mock_client()
    c2 = _mock_client()
    c3 = _mock_client()
    await pool.release(k1, c1)
    await pool.release(k2, c2)
    # pool is full now; releasing a third evicts the LRU (c1)
    await pool.release(k1, c3)
    assert pool.stats()["evictions"] == 1
    assert pool.idle_count() == 2
    c1.close.assert_called_once()
    # c1 is gone, so acquiring k1 hits the newly added c3
    got = await pool.acquire(k1)
    assert got is c3


@pytest.mark.asyncio
async def test_pool_evict_stale_by_ttl():
    pool = ConnectionPool(max_size=4, ttl_seconds=1)
    key = ("host", 22, "user", "password")
    client = _mock_client()
    await pool.release(key, client)
    await asyncio.sleep(1.2)
    dropped = await pool.evict_stale()
    assert dropped == 1
    assert pool.idle_count() == 0
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_pool_close_all():
    pool = ConnectionPool(max_size=4)
    clients = [_mock_client(), _mock_client()]
    await pool.release(("a", 22, "u", "password"), clients[0])
    await pool.release(("b", 22, "u", "password"), clients[1])
    closed = await pool.close_all()
    assert closed == 2
    assert pool.idle_count() == 0
    for c in clients:
        c.close.assert_called_once()


# ---------------------------------------------------------------------------
# SSHSessionManager pool integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_pool_off_reuses_nothing():
    from app.ssh_manager import SSHSessionManager

    manager = SSHSessionManager(connection_pool_size=0)
    assert manager.pool_stats is None
    await manager.close_pool()  # no-op, must not raise


@pytest.mark.asyncio
async def test_manager_acquire_pooled_client_on_create():
    from app.ssh_manager import SSHSessionManager

    manager = SSHSessionManager(connection_pool_size=4)
    assert manager.pool_stats is not None
    pooled = _mock_client(alive=True)
    await manager._pool.release(("h", 22, "u", "password"), pooled)

    # Patch create so the fresh-connect path never runs (pooled path used).
    manager._load_private_key = MagicMock()
    async def fake_connect(client, *a, **k):
        pass
    import paramiko

    # create_session must use the pooled client, no real connect happens.
    original = paramiko.SSHClient
    calls = []

    class FakeClient(original):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            calls.append(self)
            self.set_missing_host_key_policy = MagicMock()
            self.get_transport = MagicMock(return_value=pooled.get_transport.return_value)

    paramiko.SSHClient = FakeClient
    try:
        sid = await manager.create_session(
            host="h", port=22, username="u", password="pw"
        )
    finally:
        paramiko.SSHClient = original

    assert calls == []  # fresh connect never used
    record = await manager.get_session(sid)
    assert record is not None
    assert record.client is pooled


@pytest.mark.asyncio
async def test_manager_disconnect_releases_to_pool():
    from app.ssh_manager import SSHSessionManager

    manager = SSHSessionManager(connection_pool_size=4)
    pooled = _mock_client(alive=True)
    await manager._pool.release(("h", 22, "u", "password"), pooled)

    sid = await manager.create_session(host="h", port=22, username="u", password="pw")
    await manager.disconnect(sid)
    # transport returned to pool, not closed
    assert manager._pool.idle_count() == 1
    pooled.close.assert_not_called()


@pytest.mark.asyncio
async def test_manager_disconnect_closes_without_pool():
    from app.ssh_manager import SSHSessionManager

    manager = SSHSessionManager(connection_pool_size=0)
    client = _mock_client(alive=True)

    record = MagicMock()
    record.session_id = "s1"
    record.client = client
    record.host = "h"
    record.port = 22
    record.username = "u"
    record.auth_method = "password"
    manager._sessions["s1"] = record

    await manager.disconnect("s1")
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_manager_pool_ttl_applied_on_acquire():
    from app.ssh_manager import SSHSessionManager

    manager = SSHSessionManager(connection_pool_size=4, connection_pool_ttl_seconds=1)
    stale = _mock_client(alive=True)
    await manager._pool.release(("h", 22, "u", "password"), stale)
    # make it stale
    for conn in manager._pool._idle[("h", 22, "u", "password")]:
        conn.last_used -= 10

    # fresh-connect path must not do real networking — mock SSHClient
    import paramiko

    original = paramiko.SSHClient
    fresh = _mock_client(alive=True)

    class FakeClient(original):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.set_missing_host_key_policy = MagicMock()
            self.get_transport = MagicMock(return_value=fresh.get_transport.return_value)

        def connect(self, *a, **k):
            pass

    paramiko.SSHClient = FakeClient
    try:
        sid = await manager.create_session(host="h", port=22, username="u", password="pw")
    finally:
        paramiko.SSHClient = original

    record = await manager.get_session(sid)
    assert record is not None
    # stale conn dropped, fresh client created
    assert record.client is not fresh
    assert record.client is not stale


# ---------------------------------------------------------------------------
# Pre-warm endpoint wiring
# ---------------------------------------------------------------------------


def test_prewarm_models():
    from app.models import PrewarmRequest, PrewarmResponse

    req = PrewarmRequest(host="h", port=22, username="u", password="pw")
    assert req.host == "h"
    assert req.port == 22
    resp = PrewarmResponse(session_id="abc")
    assert resp.status == "prewarming"
    assert "background" in resp.message


def test_await_prewarm_noop_when_not_prewarmed():
    from app.routers.ssh import _await_prewarm

    async def run():
        import app.state as st

        st.prewarm_tasks.clear()
        await _await_prewarm("nope")

    asyncio.run(run())


@pytest.mark.asyncio
async def test_await_prewarm_waits_for_task():
    import app.state as st
    from app.routers.ssh import _await_prewarm

    st.prewarm_tasks.clear()
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(0.05)

    st.prewarm_tasks["s1"] = asyncio.create_task(slow())
    await started.wait()
    # task still running — _await_prewarm must block until done
    await _await_prewarm("s1")
    assert "s1" not in st.prewarm_tasks


def test_prewarm_endpoint_returns_session_id_immediately(monkeypatch):
    """POST /api/ssh/prewarm returns session_id without waiting for connect."""
    from starlette.testclient import TestClient

    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "secret-42")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

    from app import state as st

    st.manager = MagicMock()
    st.audit_logger = MagicMock()
    st.event_audit_logger = MagicMock()
    st.session_store = None
    st.access_control_store = None
    st.prewarm_tasks.clear()

    # manager.create_session is a background task — it must not run yet
    # (we only assert the endpoint returns before it finishes).
    st.manager.create_session = AsyncMock(return_value="sess-prewarmed")

    with TestClient(app) as client:
        resp = client.post(
            "/api/ssh/prewarm",
            headers={"X-API-Key": "secret-42"},
            json={"host": "10.0.0.1", "port": 22, "username": "root", "password": "pw"},
        )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["session_id"]
    assert body["status"] == "prewarming"
    # background task registered for the returned session id
    assert body["session_id"] in st.prewarm_tasks
    # cleanup: cancel the background task
    task = st.prewarm_tasks.pop(body["session_id"], None)
    if task:
        task.cancel()
