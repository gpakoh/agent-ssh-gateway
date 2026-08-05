"""Tests for JobManager <-> RedisJobQueue wiring.

JobManager runs jobs immediately in-process (asyncio task per job) rather
than pulling from RedisJobQueue's pending/processing queue, so the wiring
is one-way: on every terminal transition (completed/failed/denied by
policy), JobManager mirrors the finished job to Redis via
save_terminal_job() so job history/results survive a gateway restart.
This must never affect the in-process job outcome even if Redis is
unavailable or raises.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.job_manager import JobManager


def _make_stream(events):
    async def _stream(*args, **kwargs):
        for event in events:
            yield event

    return _stream


def _make_job_manager(execute_stream, redis_queue=None):
    mock_ssh = AsyncMock()
    mock_ssh.execute_stream = execute_stream
    return JobManager(ssh_manager=mock_ssh, max_jobs=10, redis_queue=redis_queue)


class TestJobManagerPersistsToRedis:
    @pytest.mark.asyncio
    async def test_completed_job_calls_save_terminal_job(self):
        redis_queue = AsyncMock()
        stream = _make_stream(
            [("stdout", "hi\n"), ("exit", "0")]
        )
        jm = _make_job_manager(stream, redis_queue=redis_queue)
        job_id = await jm.create_job("s1", "echo hi", owner_id="fp-a")
        job = await jm.get_job(job_id)
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)

        redis_queue.save_terminal_job.assert_awaited_once()
        kwargs = redis_queue.save_terminal_job.call_args.kwargs
        assert kwargs["session_id"] == "s1"
        assert kwargs["command"] == "echo hi"
        assert kwargs["owner_id"] == "fp-a"
        assert kwargs["status"] == "completed"
        assert kwargs["exit_code"] == 0
        assert redis_queue.save_terminal_job.call_args.args[0] == job_id

    @pytest.mark.asyncio
    async def test_failed_job_calls_save_terminal_job(self):
        redis_queue = AsyncMock()
        stream = _make_stream([("stdout", "err\n"), ("exit", "1")])
        jm = _make_job_manager(stream, redis_queue=redis_queue)
        job_id = await jm.create_job("s1", "false", owner_id="fp-a")
        job = await jm.get_job(job_id)
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)

        redis_queue.save_terminal_job.assert_awaited_once()
        assert redis_queue.save_terminal_job.call_args.kwargs["status"] == "failed"

    @pytest.mark.asyncio
    async def test_no_redis_queue_configured_is_a_noop(self):
        stream = _make_stream([("exit", "0")])
        jm = _make_job_manager(stream, redis_queue=None)
        job_id = await jm.create_job("s1", "echo hi", owner_id="fp-a")
        job = await jm.get_job(job_id)
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)
        # No exception raised — nothing to assert on since there's no mock.

    @pytest.mark.asyncio
    async def test_redis_failure_does_not_break_job_completion(self):
        """A Redis error while persisting must never surface as a job failure."""
        redis_queue = AsyncMock()
        redis_queue.save_terminal_job.side_effect = RuntimeError("redis down")
        stream = _make_stream([("exit", "0")])
        jm = _make_job_manager(stream, redis_queue=redis_queue)
        job_id = await jm.create_job("s1", "echo hi", owner_id="fp-a")
        job = await jm.get_job(job_id)
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)

        assert job.status == "completed"
        redis_queue.save_terminal_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_policy_denied_job_calls_save_terminal_job(self, monkeypatch):
        redis_queue = AsyncMock()
        stream = _make_stream([("exit", "0")])
        jm = _make_job_manager(stream, redis_queue=redis_queue)

        from app.command_policy import CommandPolicyDecision

        monkeypatch.setattr(
            "app.job_manager.evaluate_command_policy",
            lambda command, mode, profile: CommandPolicyDecision(
                allowed=False,
                reason="blocked",
                profile=profile,
                mode=mode,
                command_root="rm",
            ),
        )
        job_id = await jm.create_job("s1", "rm -rf /", owner_id="fp-a")
        job = await jm.get_job(job_id)
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)

        redis_queue.save_terminal_job.assert_awaited_once()
        assert redis_queue.save_terminal_job.call_args.kwargs["status"] == "failed"
