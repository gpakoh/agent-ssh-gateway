"""Tests for monotonic timestamp fields on JobRecord."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from app.exceptions import JobNotFoundError, PermissionDeniedError
from app.job_manager import JobManager, JobRecord


class TestJobRecordMonoTimestamps:
    """Verify mono timestamp fields exist and to_dict includes them."""

    def test_new_fields_exist_with_none_defaults(self):
        job = JobRecord(job_id="j1", session_id="s1", command="echo hi")
        assert job.queued_at_mono is None
        assert job.acquired_at_mono is None
        assert job.command_started_at_mono is None
        assert job.command_finished_at_mono is None
        assert job.completed_at_mono is None
        assert job.ssh_connect_started_at_mono is None
        assert job.ssh_connected_at_mono is None

    def test_to_dict_includes_mono_timestamps(self):
        job = JobRecord(job_id="j1", session_id="s1", command="echo hi")
        d = job.to_dict()
        assert "queued_at_mono" in d
        assert "completed_at_mono" in d
        assert d["queued_at_mono"] is None

    def test_to_dict_includes_completed_at_wall_clock(self):
        job = JobRecord(job_id="j1", session_id="s1", command="echo hi")
        d = job.to_dict()
        assert "completed_at" in d


class TestWaitForCompletion:
    """Tests for JobManager.wait_for_completion()."""

    def _make_job_manager(self):
        mock_ssh = AsyncMock()

        async def _blocking_stream(*args, **kwargs):
            await asyncio.sleep(3600)
            yield  # pragma: no cover

        mock_ssh.execute_stream = _blocking_stream
        return JobManager(ssh_manager=mock_ssh, max_jobs=10)

    @pytest.mark.asyncio
    async def test_raises_job_not_found(self):
        jm = self._make_job_manager()
        with pytest.raises(JobNotFoundError):
            await jm.wait_for_completion("nonexistent", "user:admin", 1.0)

    @pytest.mark.asyncio
    async def test_raises_permission_denied(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "echo hi", owner_id="user:admin")
        with pytest.raises(PermissionDeniedError):
            await jm.wait_for_completion(job_id, "user:other", 1.0)

    @pytest.mark.asyncio
    async def test_returns_dict_on_already_completed(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "echo hi", owner_id="user:admin")
        job = await jm.get_job(job_id)
        job.status = "completed"
        job.completed_at = time.time()
        job.completed_event.set()
        result = await jm.wait_for_completion(job_id, "user:admin", 1.0)
        assert result["job_id"] == job_id
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_timeout_returns_running_with_timed_out(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "sleep 999", owner_id="user:admin")
        result = await jm.wait_for_completion(job_id, "user:admin", 0.1)
        assert result["wait_timed_out"] is True
        assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_completed_during_wait(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "echo hi", owner_id="user:admin")
        job = await jm.get_job(job_id)

        async def _complete_later():
            await asyncio.sleep(0.05)
            job.status = "completed"
            job.completed_at = time.time()
            job.completed_event.set()

        asyncio.create_task(_complete_later())
        result = await jm.wait_for_completion(job_id, "user:admin", 5.0)
        assert result["status"] == "completed"
        assert result.get("wait_timed_out") is not True

    @pytest.mark.asyncio
    async def test_cancelled_error_re_raised(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "sleep 999", owner_id="user:admin")

        async def _cancel():
            await asyncio.sleep(0.05)
            task.cancel()

        task = asyncio.create_task(jm.wait_for_completion(job_id, "user:admin", 5.0))
        asyncio.create_task(_cancel())
        with pytest.raises(asyncio.CancelledError):
            await task
