from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.ssh_manager import (
    ExecutionError,
    SessionLimitError,
    SessionRecord,
    SSHSessionManager,
)


def _record(*, sid: str = "sid", source_ip: str = "172.19.0.17") -> SessionRecord:
    client = MagicMock()
    client.get_transport.return_value.is_active.return_value = True
    return SessionRecord(
        session_id=sid,
        client=client,
        host="target.invalid",
        port=22,
        username="tester",
        source_ip=source_ip,
        effective_idle_timeout=300,
        ephemeral=True,
    )


def _manager() -> SSHSessionManager:
    manager = SSHSessionManager.__new__(SSHSessionManager)
    manager._sessions = {}
    manager._pending_sessions_by_ip = {}
    manager._lock = asyncio.Lock()
    manager._session_timeout = 3600
    manager._pool = None
    manager._circuit_breakers = None
    manager._secret_manager = None
    manager._host_key_store = None
    manager._strict_host_key = False
    return manager


def _command_streams(record: SessionRecord) -> tuple[Any, Any, Any]:
    stdin = MagicMock()
    stdin.channel = MagicMock()
    stdout = MagicMock()
    stdout.channel = MagicMock()
    stderr = MagicMock()
    record.client.exec_command.return_value = (stdin, stdout, stderr)
    return stdin, stdout, stderr


@pytest.mark.asyncio
async def test_cleanup_does_not_reap_expired_busy_session() -> None:
    manager = _manager()
    record = _record(sid="busy")
    record.last_activity = time.time() - 600
    record.active_operations = 1
    manager._sessions[record.session_id] = record

    assert await manager.cleanup_stale_sessions() == 0
    assert manager._sessions[record.session_id] is record
    record.client.close.assert_not_called()

    record.active_operations = 0
    assert await manager.cleanup_stale_sessions() == 1
    assert record.session_id not in manager._sessions
    record.client.close.assert_called_once()


@pytest.mark.asyncio
async def test_admission_reap_refuses_busy_expired_slot_then_reclaims_when_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    monkeypatch.setattr(settings, "max_sessions_per_ip", 1)
    record = _record(sid="busy-cap")
    record.last_activity = time.time() - 600
    record.active_operations = 1
    manager._sessions[record.session_id] = record
    manager._create_session_unreserved = AsyncMock(return_value="replacement")

    with pytest.raises(SessionLimitError):
        await manager.create_session(
            host="target.invalid",
            port=22,
            username="tester",
            password="pw",
            source_ip=record.source_ip,
            ephemeral=True,
            idle_timeout_seconds=300,
        )
    manager._create_session_unreserved.assert_not_awaited()
    record.client.close.assert_not_called()

    record.active_operations = 0
    assert (
        await manager.create_session(
            host="target.invalid",
            port=22,
            username="tester",
            password="pw",
            source_ip=record.source_ip,
            ephemeral=True,
            idle_timeout_seconds=300,
        )
        == "replacement"
    )
    assert record.session_id not in manager._sessions
    record.client.close.assert_called_once()
    manager._create_session_unreserved.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_busy_counter_returns_to_zero_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    record = _record()
    manager._sessions[record.session_id] = record
    _command_streams(record)
    monkeypatch.setattr("app.ssh_manager._emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        manager,
        "_drain_command_streams",
        AsyncMock(return_value=(b"ok", b"", 0)),
    )

    result = await manager.execute(record.session_id, "true", timeout=5)

    assert result["exit_code"] == 0
    assert record.active_operations == 0


@pytest.mark.asyncio
async def test_execute_busy_counter_returns_to_zero_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    record = _record()
    manager._sessions[record.session_id] = record
    _command_streams(record)
    monkeypatch.setattr("app.ssh_manager._emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        manager,
        "_drain_command_streams",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    with pytest.raises(ExecutionError, match="boom"):
        await manager.execute(record.session_id, "true", timeout=5)

    assert record.active_operations == 0


@pytest.mark.asyncio
async def test_execute_cancellation_releases_busy_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    record = _record()
    manager._sessions[record.session_id] = record
    _command_streams(record)
    monkeypatch.setattr("app.ssh_manager._emit", lambda *_args, **_kwargs: None)
    entered = asyncio.Event()

    async def blocked_drain(*_args: Any, **_kwargs: Any) -> tuple[bytes, bytes, int]:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(manager, "_drain_command_streams", blocked_drain)
    task = asyncio.create_task(manager.execute(record.session_id, "true", timeout=30))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert record.active_operations == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert record.active_operations == 0


@pytest.mark.asyncio
async def test_execute_argv_busy_session_is_not_reaped_until_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    record = _record()
    manager._sessions[record.session_id] = record
    _command_streams(record)
    monkeypatch.setattr("app.ssh_manager._emit", lambda *_args, **_kwargs: None)
    entered = asyncio.Event()

    async def blocked_drain(*_args: Any, **_kwargs: Any) -> tuple[bytes, bytes, int]:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(manager, "_drain_command_streams", blocked_drain)
    task = asyncio.create_task(
        manager.execute_argv(record.session_id, "sleep 30", b"", timeout=30)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert record.active_operations == 1

    record.last_activity = time.time() - 600
    assert await manager.cleanup_stale_sessions() == 0
    assert record.session_id in manager._sessions

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert record.active_operations == 0

    record.last_activity = time.time() - 600
    assert await manager.cleanup_stale_sessions() == 1
    assert record.session_id not in manager._sessions


@pytest.mark.asyncio
async def test_execute_stream_cancellation_releases_busy_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    record = _record()
    manager._sessions[record.session_id] = record
    _stdin, stdout, stderr = _command_streams(record)
    stdout.channel.exit_status_ready.return_value = False
    stdout.channel.recv_ready.return_value = False
    stderr.channel.recv_stderr_ready.return_value = False
    monkeypatch.setattr("app.ssh_manager._emit", lambda *_args, **_kwargs: None)

    async def consume() -> None:
        async for _item in manager.execute_stream(record.session_id, "sleep 30", timeout=30):
            pass

    task = asyncio.create_task(consume())
    deadline = asyncio.get_running_loop().time() + 1
    while record.active_operations != 1 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert record.active_operations == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert record.active_operations == 0
    stdout.channel.close.assert_called_once()
