"""Tests for execute_argv on SSHSessionManager."""

import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest


def _make_mock_record():
    """Create a mock SessionRecord with a working SSH client."""
    record = MagicMock()
    record.is_connected.return_value = True
    record.touch = MagicMock()

    stdin_file = MagicMock()
    stdout_file = MagicMock()
    stderr_file = MagicMock()

    stdout_file.read.return_value = b"output"
    stdout_file.channel.recv_exit_status.return_value = 0
    stderr_file.read.return_value = b""

    record.client.exec_command.return_value = (stdin_file, stdout_file, stderr_file)
    record.client.get_transport.return_value = MagicMock()
    record.client.get_transport.return_value.is_active.return_value = True

    return record, stdin_file, stdout_file, stderr_file


@pytest.mark.asyncio
async def test_execute_argv_sends_stdin():
    from app.ssh_manager import SSHSessionManager

    manager = SSHSessionManager()
    record, stdin_file, stdout_file, stderr_file = _make_mock_record()
    manager._sessions["test-id"] = record

    result = await manager.execute_argv(
        session_id="test-id",
        command_str="python3 -c print('hi')",
        stdin_data=b"input data",
        timeout=10,
    )

    assert result["exit_code"] == 0
    assert result["stdout"] == "output"
    # stdin should have been written to and shutdown_write called
    stdin_file.write.assert_called_once_with(b"input data")


@pytest.mark.asyncio
async def test_execute_argv_empty_stdin():
    from app.ssh_manager import SSHSessionManager

    manager = SSHSessionManager()
    record, stdin_file, stdout_file, stderr_file = _make_mock_record()
    manager._sessions["test-id"] = record

    result = await manager.execute_argv(
        session_id="test-id",
        command_str="ls",
        stdin_data=b"",
        timeout=10,
    )

    assert result["exit_code"] == 0
    # stdin.write should NOT be called for empty data
    stdin_file.write.assert_not_called()


@pytest.mark.asyncio
async def test_drain_command_streams_reads_stdout_stderr_concurrently():
    from app.ssh_manager import SSHSessionManager

    manager = SSHSessionManager()
    stderr_started = threading.Event()
    channel = MagicMock()
    channel.recv_exit_status.return_value = 0

    stdout = MagicMock()
    stdout.channel = channel

    def _stdout_read():
        assert stderr_started.wait(timeout=0.5), "stderr read did not start concurrently"
        return b"out"

    stderr = MagicMock()

    def _stderr_read():
        stderr_started.set()
        return b"err"

    stdout.read.side_effect = _stdout_read
    stderr.read.side_effect = _stderr_read

    out, err, code = await manager._drain_command_streams(
        asyncio.get_running_loop(),
        stdout,
        stderr,
        deadline=time.monotonic() + 1.0,
    )
    assert (out, err, code) == (b"out", b"err", 0)


@pytest.mark.asyncio
async def test_drain_command_streams_closes_channel_on_deadline():
    from app.ssh_manager import SSHSessionManager

    manager = SSHSessionManager()
    release = threading.Event()
    channel = MagicMock()
    stdout = MagicMock()
    stdout.channel = channel
    stderr = MagicMock()
    stdout.read.side_effect = lambda: release.wait(timeout=1.0) or b""
    stderr.read.side_effect = lambda: release.wait(timeout=1.0) or b""

    try:
        with pytest.raises(TimeoutError):
            await manager._drain_command_streams(
                asyncio.get_running_loop(),
                stdout,
                stderr,
                deadline=time.monotonic() + 0.02,
            )
        channel.close.assert_called()
    finally:
        release.set()
