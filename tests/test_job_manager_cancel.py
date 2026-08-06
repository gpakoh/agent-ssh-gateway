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
        await jm.cancel_job(job_id)

        # Let the already-scheduled _run_job task actually run.
        await asyncio.sleep(0.05)

        assert calls == [], "cancelled pending job must never reach execute_stream()"
        job = await jm.get_job(job_id)
        assert job.status == "cancelled"
