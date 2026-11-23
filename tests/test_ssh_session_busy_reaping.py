from __future__ import annotations

import asyncio
import threading
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
    task = asyncio.create_task(manager.execute_argv(record.session_id, "sleep 30", b"", timeout=30))
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
    streaming = asyncio.Event()

    def channel_is_still_running() -> bool:
        # Signal only after execute_stream reaches its polling loop, not
        # while shutdown_write is still running in a worker thread. The
        # earlier signal could race with the command's startup/timeout.
        streaming.set()
        return False

    stdout.channel.exit_status_ready.side_effect = channel_is_still_running
    stdout.channel.recv_ready.return_value = False
    stderr.channel.recv_stderr_ready.return_value = False
    monkeypatch.setattr("app.ssh_manager._emit", lambda *_args, **_kwargs: None)

    async def consume() -> None:
        async for _item in manager.execute_stream(record.session_id, "sleep 30", timeout=300):
            pass

    task = asyncio.create_task(consume())
    # Observe the actual streaming loop before cancelling; the longer remote
    # deadline is not a test wait and avoids a loaded CI worker racing timeout.
    await asyncio.wait_for(streaming.wait(), timeout=5)
    assert record.active_operations == 1
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert record.active_operations == 0
    stdout.channel.close.assert_called_once()


@pytest.mark.asyncio
async def test_execute_stream_cancel_before_executor_starts_has_no_channel_to_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    record = _record()
    manager._sessions[record.session_id] = record
    _stdin, stdout, _stderr = _command_streams(record)
    monkeypatch.setattr("app.ssh_manager._emit", lambda *_args, **_kwargs: None)
    loop = asyncio.get_running_loop()
    queued = loop.create_future()
    submitted = asyncio.Event()

    def enqueue(_executor: Any, _callback: Any) -> asyncio.Future[Any]:
        submitted.set()
        return queued

    monkeypatch.setattr(loop, "run_in_executor", enqueue)

    async def consume() -> None:
        async for _item in manager.execute_stream(record.session_id, "sleep 30", timeout=30):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(submitted.wait(), timeout=5)
    assert record.active_operations == 1
    record.client.exec_command.assert_not_called()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert record.active_operations == 0
    assert queued.cancelled()
    stdout.channel.close.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_step", ["stdin_write", "shutdown_write", "exit_status"])
async def test_execute_stream_blocking_paramiko_step_keeps_event_loop_responsive(
    monkeypatch: pytest.MonkeyPatch,
    blocked_step: str,
) -> None:
    """A blocking Paramiko operation must not stall /health on the ASGI worker."""
    manager = _manager()
    record = _record()
    manager._sessions[record.session_id] = record
    stdin, stdout, stderr = _command_streams(record)
    stdout.channel.exit_status_ready.return_value = True
    stdout.channel.recv_ready.return_value = False
    stderr.channel.recv_stderr_ready.return_value = False
    stdout.channel.recv_exit_status.return_value = 0
    monkeypatch.setattr("app.ssh_manager._emit", lambda *_args, **_kwargs: None)

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    timestamps: list[float] = []

    def stalled_paramiko_step(*_args: Any) -> int:
        timestamps.append(time.monotonic())
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=2.5)  # safe even when the pre-fix event loop blocks
        return 0

    if blocked_step == "stdin_write":
        stdin.write.side_effect = stalled_paramiko_step
    elif blocked_step == "shutdown_write":
        stdin.channel.shutdown_write.side_effect = stalled_paramiko_step
    else:
        stdout.channel.recv_exit_status.side_effect = stalled_paramiko_step

    async def consume() -> list[tuple[str, str]]:
        return [
            event
            async for event in manager.execute_stream(
                record.session_id,
                "true",
                timeout=10,
                stdin_data=b"x" if blocked_step == "stdin_write" else b"",
            )
        ]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), timeout=4.0)
        assert time.monotonic() - timestamps[0] < 1.2, (
            f"{blocked_step} blocked the event loop while Paramiko waited"
        )
    finally:
        release.set()
        assert await asyncio.wait_for(task, timeout=4.0) == [("exit", "0")]
        assert record.active_operations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_step", ["stdin_write", "shutdown_write", "exit_status"])
async def test_execute_stream_paramiko_step_timeout_closes_channel(
    monkeypatch: pytest.MonkeyPatch,
    blocked_step: str,
) -> None:
    manager = _manager()
    record = _record()
    manager._sessions[record.session_id] = record
    stdin, stdout, stderr = _command_streams(record)
    stdout.channel.exit_status_ready.return_value = True
    stdout.channel.recv_ready.return_value = False
    stderr.channel.recv_stderr_ready.return_value = False
    monkeypatch.setattr("app.ssh_manager._emit", lambda *_args, **_kwargs: None)

    release = threading.Event()
    stdout.channel.close.side_effect = release.set

    def wait_for_close(*_args: Any) -> int:
        release.wait(timeout=4)
        return 0

    if blocked_step == "stdin_write":
        stdin.write.side_effect = wait_for_close
    elif blocked_step == "shutdown_write":
        stdin.channel.shutdown_write.side_effect = wait_for_close
    else:
        stdout.channel.recv_exit_status.side_effect = wait_for_close

    try:
        events = [
            event
            async for event in manager.execute_stream(
                record.session_id,
                "true",
                timeout=1,
                stdin_data=b"x" if blocked_step == "stdin_write" else b"",
            )
        ]
    finally:
        release.set()

    assert len(events) == 1
    assert events[0][0] == "error"
    assert "timed out after 1s" in events[0][1]
    stdout.channel.close.assert_called_once()
    assert record.active_operations == 0


@pytest.mark.asyncio
async def test_execute_stream_cancel_while_stdin_write_waits_closes_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    record = _record()
    manager._sessions[record.session_id] = record
    stdin, stdout, _stderr = _command_streams(record)
    monkeypatch.setattr("app.ssh_manager._emit", lambda *_args, **_kwargs: None)

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def wait_for_cancel(_data: bytes) -> int:
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=4)
        return 0

    stdin.write.side_effect = wait_for_cancel
    stdout.channel.close.side_effect = release.set

    async def consume() -> None:
        async for _event in manager.execute_stream(
            record.session_id, "true", timeout=10, stdin_data=b"x"
        ):
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert record.active_operations == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
    assert record.active_operations == 0
    stdout.channel.close.assert_called_once()
