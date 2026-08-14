"""Tests for JobManager capacity semantics.

Regression: create_job() previously rejected new work whenever
len(self._jobs) >= max_jobs, so completed/failed/cancelled jobs retained
for result/history consumed execution capacity until cleanup. Capacity now
limits only active jobs (pending + running); terminal JobRecord objects
stay queryable and do not block new work.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.job_manager import JobManager, JobRecord
from app.ssh_manager import ExecutionError

ACTIVE_STATES = ("pending", "running")
TERMINAL_STATES = ("completed", "failed", "cancelled")


def _make_job_manager(max_jobs: int = 3, block_on_run: bool = False):
    """Build a JobManager whose SSH manager never touches a remote host.

    When block_on_run is True, execute_stream() waits on an asyncio.Event
    so accepted jobs stay in a running state until the test releases it.
    """
    mock_ssh = AsyncMock()
    block = asyncio.Event()

    async def _stream(*args, **kwargs):
        if block_on_run:
            await block.wait()
        if False:
            yield  # pragma: no cover — makes this an async generator
        return

    mock_ssh.execute_stream = _stream
    jm = JobManager(ssh_manager=mock_ssh, max_jobs=max_jobs)
    return jm, block


def _record(job_id: str, status: str) -> JobRecord:
    rec = JobRecord(job_id=job_id, session_id="s1", command="echo hi", status=status)
    if status in TERMINAL_STATES:
        rec.completed_at = 1.0
        rec.completed_at_mono = 1.0
        rec.completed_event.set()
    return rec


class TestCapacityCountsActiveJobsOnly:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", TERMINAL_STATES)
    async def test_terminal_jobs_do_not_consume_capacity(self, status):
        """max_jobs terminal records do not block a new job."""
        jm, _ = _make_job_manager(max_jobs=1)
        jm._jobs["old"] = _record("old", status)

        job_id = await jm.create_job("s1", "echo hi", owner_id="u1")

        assert job_id
        new = await jm.get_job(job_id)
        assert new is not None and new.status == "pending"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ACTIVE_STATES)
    async def test_active_jobs_consume_capacity(self, status):
        """max_jobs pending/running records still reject a new job."""
        jm, _ = _make_job_manager(max_jobs=1)
        jm._jobs["active"] = _record("active", status)

        with pytest.raises(ExecutionError, match="Maximum number of jobs reached"):
            await jm.create_job("s1", "echo hi", owner_id="u1")


class TestConcurrentCapacity:
    @pytest.mark.asyncio
    async def test_concurrent_creates_cannot_oversubscribe_active_limit(self):
        """Concurrent create_job calls cannot exceed max_jobs."""
        jm, block = _make_job_manager(max_jobs=2, block_on_run=True)

        async def _create():
            try:
                return await jm.create_job("s1", "echo hi", owner_id="u1")
            except ExecutionError:
                return None

        results = await asyncio.gather(*(_create() for _ in range(10)))
        job_ids = [r for r in results if r is not None]
        assert len(job_ids) == 2

        jobs = [await jm.get_job(jid) for jid in job_ids]
        assert all(job is not None and job.status == "running" for job in jobs)

        # Release the fake streams so the running jobs finish cleanly.
        block.set()
        await asyncio.wait_for(
            asyncio.gather(*(job.completed_event.wait() for job in jobs)),
            timeout=5,
        )


class TestTerminalHistoryRetained:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", TERMINAL_STATES)
    async def test_terminal_records_remain_queryable_after_new_jobs(self, status):
        """Terminal records survive acceptance of new jobs."""
        jm, _ = _make_job_manager(max_jobs=1)
        jm._jobs["old"] = _record("old", status)

        job_id = await jm.create_job("s1", "echo hi", owner_id="u1")
        new = await jm.get_job(job_id)
        await asyncio.wait_for(new.completed_event.wait(), timeout=5)

        old = await jm.get_job("old")
        assert old is not None and old.status == status
        assert old.to_dict()["job_id"] == "old"

        assert [j.job_id for j in await jm.list_jobs()] == ["old", job_id]


class TestJobTimeoutPropagation:
    """Regression: background jobs must use the per-job timeout, not
    execute_stream's hard-coded 600s default."""

    @staticmethod
    def _make_capturing_manager():
        """JobManager whose execute_stream records its call kwargs."""
        mock_ssh = AsyncMock()
        calls: list[dict] = []

        async def _stream(*args, **kwargs):
            calls.append(kwargs)
            if False:
                yield  # pragma: no cover — makes this an async generator

        mock_ssh.execute_stream = _stream
        jm = JobManager(ssh_manager=mock_ssh)
        return jm, calls

    @pytest.mark.asyncio
    async def test_create_job_defaults_timeout_to_3600(self):
        """create_job without a timeout keeps the 3600s default."""
        jm, calls = self._make_capturing_manager()

        job_id = await jm.create_job("s1", "echo hi", owner_id="u1")
        job = await jm.get_job(job_id)
        assert job.timeout == 3600

        await asyncio.wait_for(job.completed_event.wait(), timeout=5)
        assert calls[-1]["timeout"] == 3600

    @pytest.mark.asyncio
    async def test_create_job_stores_explicit_timeout(self):
        """create_job stores the caller-provided timeout on the job."""
        jm, calls = self._make_capturing_manager()

        job_id = await jm.create_job("s1", "echo hi", owner_id="u1", timeout=120)
        job = await jm.get_job(job_id)
        assert job.timeout == 120

        await asyncio.wait_for(job.completed_event.wait(), timeout=5)
        assert calls[-1]["timeout"] == 120
        assert calls[-1]["cancel_event"] is job.cancel_event
        assert calls[-1]["stdin_data"] == b""

    @pytest.mark.asyncio
    async def test_default_job_timeout_does_not_override_explicit(self):
        """The manager-level default only applies when the caller omits timeout."""
        jm, _ = self._make_capturing_manager()
        jm._job_timeout = 999

        job_id = await jm.create_job("s1", "echo hi", owner_id="u1", timeout=42)
        job = await jm.get_job(job_id)
        assert job.timeout == 42
