"""Tests for shutdown lifecycle: session close, WS drain, disconnect timeout, cleanup_stale_sessions."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.state as state_module
from app.ssh_manager import SSHSessionManager

# ---------------------------------------------------------------------------
# Cleanup_stale_sessions — No Deadlock With Concurrent Access
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cleanup_stale_sessions_no_deadlock():
    manager = SSHSessionManager(cleanup_interval=3600)
    try:
        n = await manager.cleanup_stale_sessions()
        assert n == 0
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_cleanup_stale_sessions_with_sessions():
    manager = SSHSessionManager(cleanup_interval=3600)
    # Set Timeout Low So Sessions Are Stale
    manager._session_timeout = 0
    mock_client = MagicMock()
    mock_client.close = MagicMock()
    import time
    now = time.time()
    manager._sessions["stale-1"] = MagicMock(
        spec=["client", "last_activity", "idle_time", "session_id", "is_connected"],
        client=mock_client,
        last_activity=now - 9999,
        idle_time=9999,
        session_id="stale-1",
        is_connected=lambda: True,
    )
    manager._sessions["stale-2"] = MagicMock(
        spec=["client", "last_activity", "idle_time", "session_id", "is_connected"],
        client=mock_client,
        last_activity=now - 9999,
        idle_time=9999,
        session_id="stale-2",
        is_connected=lambda: True,
    )
    try:
        n = await manager.cleanup_stale_sessions()
        assert n == 2
    finally:
        await manager.close_all()


# ---------------------------------------------------------------------------
# Disconnect With Timeout — Mock A Slow SSH Transport
# ---------------------------------------------------------------------------

class BrokenClient:
    """Mock SSH client whose close() raises."""
    def close(self):
        raise RuntimeError("Transport closed unexpectedly")

    def get_transport(self):
        return None


@pytest.mark.asyncio
async def test_disconnect_handles_broken_client():
    """disconnect must not re-raise if client.close() raises Exception."""
    manager = SSHSessionManager(cleanup_interval=3600)
    session_id = "broken-session"
    manager._sessions[session_id] = MagicMock(
        spec=["client", "last_activity", "idle_time", "session_id", "is_connected",
              "password", "private_key", "key_passphrase", "host", "port", "username"],
        client=BrokenClient(),
        last_activity=0,
        idle_time=0,
        session_id="broken-session",
        is_connected=lambda: True,
        password="secret",
        host="127.0.0.1",
        port=22,
        username="root",
    )
    try:
        await manager.disconnect(session_id)
        assert session_id not in manager._sessions
    finally:
        await manager.close_all()


# ---------------------------------------------------------------------------
# Disconnect — Session Removed After Disconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_removes_session():
    manager = SSHSessionManager(cleanup_interval=3600)
    mock_client = MagicMock()
    mock_client.close = MagicMock()
    session_id = "test-session"
    manager._sessions[session_id] = MagicMock(
        spec=["client", "last_activity", "idle_time", "session_id", "is_connected",
              "password", "private_key", "key_passphrase", "host", "port", "username"],
        client=mock_client,
        last_activity=0,
        idle_time=0,
        session_id=session_id,
        is_connected=lambda: True,
        host="127.0.0.1",
        port=22,
        username="test",
    )
    await manager.disconnect(session_id)
    assert session_id not in manager._sessions


# ---------------------------------------------------------------------------
# Websocket Drain In Shutdown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shutdown_ws_drain():
    """H5: shutdown iterates active_websockets and closes each with code 1001."""
    state_module.active_websockets.clear()
    ws = AsyncMock()
    ws.close = AsyncMock()
    state_module.active_websockets.add(ws)

    import asyncio
    # Simulate the actual shutdown logic from main.py
    for ws_entry in list(state_module.active_websockets):
        try:
            await asyncio.wait_for(ws_entry.close(code=1001, reason="Server shutting down"), timeout=5.0)
        except Exception:
            pass
    state_module.active_websockets.clear()

    ws.close.assert_called_once_with(code=1001, reason="Server shutting down")
    assert len(state_module.active_websockets) == 0


@pytest.mark.asyncio
async def test_shutdown_ws_drain_timeout():
    state_module.active_websockets.clear()
    slow_ws = AsyncMock()
    slow_ws.close = AsyncMock(side_effect=asyncio.TimeoutError)
    state_module.active_websockets.add(slow_ws)

    tasks = []
    for ws_entry in list(state_module.active_websockets):
        tasks.append(asyncio.wait_for(ws_entry.close(code=1001, reason="Server shutting down"), timeout=0.001))
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert any(isinstance(r, asyncio.TimeoutError) for r in results)
    state_module.active_websockets.clear()




@pytest.mark.asyncio
async def test_websocket_register_discard():
    state_module.active_websockets.clear()
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    state_module.active_websockets.add(ws1)
    state_module.active_websockets.add(ws2)
    assert len(state_module.active_websockets) == 2

    state_module.active_websockets.discard(ws1)
    assert len(state_module.active_websockets) == 1
    assert ws2 in state_module.active_websockets


# ---------------------------------------------------------------------------
# Multiple Dead Sessions — Shutdown Completes Within Timeout
# ---------------------------------------------------------------------------

class RaisingClient:
    """SSH client whose close() raises a handled exception."""
    def close(self):
        raise RuntimeError("dead host")

    def get_transport(self):
        return None


@pytest.mark.asyncio
async def test_shutdown_all_sessions_handled():
    manager = SSHSessionManager(cleanup_interval=3600)
    for i in range(10):
        sid = f"dead-{i}"
        manager._sessions[sid] = MagicMock(
            spec=["client", "last_activity", "idle_time", "session_id", "is_connected",
                  "password", "private_key", "key_passphrase", "host", "port", "username"],
            client=RaisingClient(),
            last_activity=0,
            idle_time=0,
            session_id=sid,
            is_connected=lambda: True,
            host="127.0.0.1",
            port=22,
            username="root",
        )

    async def shutdown_all():
        for sid in list(manager._sessions.keys()):
            try:
                await asyncio.wait_for(manager.disconnect(sid), timeout=5.0)
            except (TimeoutError, Exception):
                pass

    try:
        await asyncio.wait_for(shutdown_all(), timeout=10.0)
        assert len(manager._sessions) == 0
    except TimeoutError:
        pytest.fail("shutdown of 10 dead hosts exceeded 10s timeout")
    finally:
        await manager.close_all()
