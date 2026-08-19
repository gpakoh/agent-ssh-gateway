"""Tests for JobManager.cancel_job() semantics on still-pending jobs."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.job_manager import JobManager


def _make_job_manager():
    mock_ssh = AsyncMock()
    calls: list[tuple] = []

    async def _stream(*args, **kwargs):
        calls.append(args)
        if False:
            yield  # pragma: no cover — makes this an async generator
        return

    mock_ssh.execute_stream = _stream
    jm = JobManager(ssh_manager=mock_ssh, max_jobs=10)
    return jm, calls


class TestCancelPendingJob:
    @pytest.mark.asyncio
    async def test_cancel_before_run_prevents_remote_execution(self):
        """Regression: cancel_job() on a still-pending job (its asyncio
        Task has been scheduled via create_task() but hasn't run yet) only
        flipped job.status to "cancelled" and set cancel_event/
        completed_event -- it never cancelled the underlying Task itself.
        When that Task's _run_job() coroutine actually ran moments later,
        it unconditionally set job.status back to "running" and went on to
        call ssh_manager.execute_stream() (which itself calls
        client.exec_command() on the remote host before ever checking
        cancel_event) with no check that the job had already been
        cancelled. A job "cancelled" while still pending still actually
        executed its command on the remote host.
        """
        jm, calls = _make_job_manager()
        job_id = await jm.create_job("s1", "echo hi", owner_id="user:admin")
        status = await jm.cancel_job(job_id)
        assert status == "cancelled"

        # Let the already-scheduled _run_job task actually run.
        await asyncio.sleep(0.05)

        assert calls == [], "cancelled pending job must never reach execute_stream()"
        job = await jm.get_job(job_id)
        assert job.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_running_waits_for_remote_ack_before_terminal_state():
    """A running job is only terminal after execute_stream has stopped."""
    started = asyncio.Event()
    stopped = asyncio.Event()
    mock_ssh = AsyncMock()

    async def _stream(*args, cancel_event=None, **kwargs):
        started.set()
        while cancel_event is not None and not cancel_event.is_set():
            await asyncio.sleep(0.001)
        # Model a small remote-channel shutdown delay after cancellation.
        await asyncio.sleep(0.02)
        stopped.set()
        yield ("exit", "-1")

    mock_ssh.execute_stream = _stream
    jm = JobManager(ssh_manager=mock_ssh, max_jobs=10)
    job_id = await jm.create_job("s1", "echo hi", owner_id="owner-a")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    status = await jm.cancel_job(job_id)
    assert status == "cancelling"
    job = await jm.get_job(job_id)
    assert job is not None
    assert job.status == "cancelling"
    assert not job.completed_event.is_set()
    assert not stopped.is_set()

    result = await jm.wait_for_completion(job_id, "owner-a", timeout_s=1.0)
    assert stopped.is_set()
    assert result["status"] == "cancelled"
    assert result["exit_code"] == -1
    assert result["error_message"] is None


@pytest.mark.asyncio
async def test_force_cleanup_persists_pending_cancel_without_remote_execution():
    """A task prevented from ever starting has a provable cancelled outcome."""
    redis_queue = AsyncMock()
    remote_calls: list[tuple] = []
    mock_ssh = AsyncMock()

    async def _stream(*args, **kwargs):
        remote_calls.append(args)
        if False:
            yield  # pragma: no cover

    mock_ssh.execute_stream = _stream
    jm = JobManager(ssh_manager=mock_ssh, max_jobs=10, redis_queue=redis_queue)
    job_id = await jm.create_job("s1", "echo hi", owner_id="owner-a")
    job = await jm.get_job(job_id)
    assert job is not None
    assert job.status == "pending"

    assert await jm.force_cleanup() == 1

    assert remote_calls == []
    assert job.status == "cancelled"
    assert job.completed_event.is_set()
    redis_queue.save_terminal_job.assert_awaited_once()
    assert redis_queue.save_terminal_job.call_args.args[0] == job_id
    assert redis_queue.save_terminal_job.call_args.kwargs["status"] == "cancelled"
    assert await jm.get_job(job_id) is None


@pytest.mark.asyncio
async def test_force_cleanup_does_not_synthesize_terminal_for_running_job():
    """Local Task cancellation is not proof that an already-started remote stopped."""
    redis_queue = AsyncMock()
    started = asyncio.Event()
    block_forever = asyncio.Event()
    mock_ssh = AsyncMock()

    async def _stream(*args, **kwargs):
        started.set()
        await block_forever.wait()
        if False:
            yield  # pragma: no cover

    mock_ssh.execute_stream = _stream
    jm = JobManager(ssh_manager=mock_ssh, max_jobs=10, redis_queue=redis_queue)
    job_id = await jm.create_job("s1", "echo hi", owner_id="owner-a")
    job = await jm.get_job(job_id)
    assert job is not None
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert job.status == "running"

    assert await jm.force_cleanup() == 1

    assert job.status == "cancelling"
    assert not job.completed_event.is_set()
    redis_queue.save_terminal_job.assert_not_awaited()
    assert await jm.get_job(job_id) is None
