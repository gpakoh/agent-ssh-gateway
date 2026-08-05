"""Tests for RedisJobQueue.save_terminal_job() — the JobManager wiring point.

JobManager runs jobs immediately in-process rather than pulling from this
queue's pending/processing zsets, so save_terminal_job() writes a snapshot
directly under the same job-key storage the rest of this class reads
(_get_job, get_dead_letter_jobs, get_queue_stats), bypassing enqueue()/
dequeue() entirely.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.redis_queue import RedisJobQueue


def _make_queue():
    rq = RedisJobQueue("redis://localhost:6379")
    rq._redis = AsyncMock()
    rq._redis.zcard = AsyncMock(return_value=0)
    return rq


class TestSaveTerminalJob:
    @pytest.mark.asyncio
    async def test_noop_when_not_connected(self):
        rq = RedisJobQueue("redis://localhost:6379")
        rq._redis = None
        # Must not raise even though nothing is mocked.
        await rq.save_terminal_job(
            "job-1",
            session_id="s1",
            command="echo hi",
            owner_id="fp-a",
            status="completed",
        )

    @pytest.mark.asyncio
    async def test_completed_job_written_and_not_dead_lettered(self):
        rq = _make_queue()
        await rq.save_terminal_job(
            "job-1",
            session_id="s1",
            command="echo hi",
            owner_id="fp-a",
            status="completed",
            stdout="hi\n",
            exit_code=0,
        )
        set_call = rq._redis.set.call_args
        assert set_call.args[0] == f"{rq._job_prefix}job-1"
        stored = json.loads(set_call.args[1])
        assert stored["status"] == "completed"
        assert stored["owner_id"] == "fp-a"
        assert stored["stdout"] == "hi\n"

        rq._redis.zadd.assert_called_once()
        zadd_args = rq._redis.zadd.call_args.args
        assert zadd_args[0] == rq._completed_key
        assert "job-1" in zadd_args[1]

    @pytest.mark.asyncio
    async def test_failed_job_written_to_dead_letter(self):
        rq = _make_queue()
        await rq.save_terminal_job(
            "job-2",
            session_id="s1",
            command="false",
            owner_id="fp-a",
            status="failed",
            exit_code=1,
            error="Exit code: 1",
        )
        rq._redis.zadd.assert_called_once()
        zadd_args = rq._redis.zadd.call_args.args
        assert zadd_args[0] == rq._dead_letter_key
        assert "job-2" in zadd_args[1]

    @pytest.mark.asyncio
    async def test_saved_job_is_readable_via_get_job(self):
        """The snapshot must land in the same storage _get_job() reads —
        i.e. GET /api/jobs/{id}/status still works after a restart."""
        rq = _make_queue()
        stored_holder = {}

        async def fake_set(key, value, ex=None):
            stored_holder[key] = value

        rq._redis.set = AsyncMock(side_effect=fake_set)
        rq._redis.get = AsyncMock(side_effect=lambda key: stored_holder.get(key))

        await rq.save_terminal_job(
            "job-3",
            session_id="s1",
            command="echo hi",
            owner_id="fp-a",
            status="completed",
            stdout="hi\n",
            exit_code=0,
        )
        job = await rq.get_job("job-3")
        assert job is not None
        assert job["status"] == "completed"
        assert job["stdout"] == "hi\n"
