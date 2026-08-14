"""Regression tests for durable async submission idempotency."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.exceptions import SubmissionConflictError, SubmissionUnavailableError
from app.job_manager import JobManager
from app.redis_queue import RedisJobQueue


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def zadd(self, key: str, mapping: dict):
        return len(mapping)

    async def zcard(self, key: str):
        return 0


def _queue() -> RedisJobQueue:
    queue = RedisJobQueue("redis://unused")
    queue._redis = _FakeRedis()
    return queue


def _stream(counter: list[int]):
    async def execute_stream(*args, **kwargs):
        counter[0] += 1
        yield "exit", "0"

    return execute_stream


def _manager(queue: RedisJobQueue | None, counter: list[int]) -> JobManager:
    ssh = AsyncMock()
    ssh.execute_stream = _stream(counter)
    return JobManager(ssh_manager=ssh, max_jobs=10, redis_queue=queue)


@pytest.mark.asyncio
async def test_redis_claim_is_atomic_and_raw_key_is_not_stored():
    queue = _queue()
    job_id, created = await queue.claim_submission(
        "task:project-1:agent-1",
        job_id="job-a",
        owner_id="owner-a",
        payload_hash="payload-a",
    )
    assert (job_id, created) == ("job-a", True)

    job_id, created = await queue.claim_submission(
        "task:project-1:agent-1",
        job_id="job-b",
        owner_id="owner-a",
        payload_hash="payload-a",
    )
    assert (job_id, created) == ("job-a", False)
    assert all("task:project-1:agent-1" not in key for key in queue._redis.values)


@pytest.mark.asyncio
async def test_submission_key_reuse_with_different_payload_is_rejected():
    queue = _queue()
    await queue.claim_submission(
        "task:project-1:agent-1",
        job_id="job-a",
        owner_id="owner-a",
        payload_hash="payload-a",
    )
    with pytest.raises(SubmissionConflictError):
        await queue.claim_submission(
            "task:project-1:agent-1",
            job_id="job-b",
            owner_id="owner-a",
            payload_hash="payload-b",
        )


@pytest.mark.asyncio
async def test_submission_key_reuse_by_different_owner_is_rejected():
    queue = _queue()
    await queue.claim_submission(
        "task:project-1:agent-1",
        job_id="job-a",
        owner_id="owner-a",
        payload_hash="payload-a",
    )
    with pytest.raises(SubmissionConflictError):
        await queue.find_submission(
            "task:project-1:agent-1",
            owner_id="owner-b",
            payload_hash="payload-a",
        )


@pytest.mark.asyncio
async def test_identical_retry_returns_same_job_and_executes_once():
    queue = _queue()
    calls = [0]
    manager = _manager(queue, calls)

    first = await manager.create_job(
        "session-a",
        "sh",
        owner_id="owner-a",
        stdin=b"echo hi\n",
        timeout=300,
        submission_key="task:project-1:agent-1",
    )
    job = await manager.get_job(first)
    assert job is not None
    await asyncio.wait_for(job.completed_event.wait(), timeout=2)

    second = await manager.create_job(
        "session-b",  # a reconnect may legitimately change the SSH session
        "sh",
        owner_id="owner-a",
        stdin=b"echo hi\n",
        timeout=300,
        submission_key="task:project-1:agent-1",
    )
    assert second == first
    assert calls[0] == 1


@pytest.mark.asyncio
async def test_concurrent_identical_submissions_launch_only_one_job():
    queue = _queue()
    calls = [0]
    manager = _manager(queue, calls)

    async def submit():
        return await manager.create_job(
            "session-a",
            "sh",
            owner_id="owner-a",
            stdin=b"echo hi\n",
            timeout=300,
            submission_key="task:project-1:agent-race",
        )

    first, second = await asyncio.gather(submit(), submit())
    assert first == second
    job = await manager.get_job(first)
    assert job is not None
    await asyncio.wait_for(job.completed_event.wait(), timeout=2)
    assert calls[0] == 1


@pytest.mark.asyncio
async def test_retry_after_manager_restart_returns_original_job_id():
    queue = _queue()
    calls1 = [0]
    manager1 = _manager(queue, calls1)
    first = await manager1.create_job(
        "session-a",
        "sh",
        owner_id="owner-a",
        stdin=b"echo hi\n",
        timeout=300,
        submission_key="task:project-1:agent-1",
    )
    job = await manager1.get_job(first)
    assert job is not None
    await asyncio.wait_for(job.completed_event.wait(), timeout=2)

    calls2 = [0]
    manager2 = _manager(queue, calls2)
    second = await manager2.create_job(
        "session-new",
        "sh",
        owner_id="owner-a",
        stdin=b"echo hi\n",
        timeout=300,
        submission_key="task:project-1:agent-1",
    )
    assert second == first
    assert calls2[0] == 0
    assert await manager2.get_job(first) is None
    persisted = await queue.get_job(first)
    assert persisted is not None
    assert persisted["status"] == "completed"


@pytest.mark.asyncio
async def test_keyed_submission_without_redis_fails_before_execution():
    calls = [0]
    manager = _manager(None, calls)
    with pytest.raises(SubmissionUnavailableError):
        await manager.create_job(
            "session-a",
            "sh",
            owner_id="owner-a",
            stdin=b"echo hi\n",
            timeout=300,
            submission_key="task:project-1:agent-1",
        )
    await asyncio.sleep(0)
    assert calls[0] == 0


@pytest.mark.asyncio
async def test_same_key_different_execution_payload_fails_without_second_run():
    queue = _queue()
    calls = [0]
    manager = _manager(queue, calls)
    first = await manager.create_job(
        "session-a",
        "sh",
        owner_id="owner-a",
        stdin=b"echo first\n",
        timeout=300,
        submission_key="task:project-1:agent-1",
    )
    job = await manager.get_job(first)
    assert job is not None
    await asyncio.wait_for(job.completed_event.wait(), timeout=2)

    with pytest.raises(SubmissionConflictError):
        await manager.create_job(
            "session-a",
            "sh",
            owner_id="owner-a",
            stdin=b"echo second\n",
            timeout=300,
            submission_key="task:project-1:agent-1",
        )
    assert calls[0] == 1


@pytest.mark.asyncio
async def test_redis_read_transport_failure_fails_before_execution():
    queue = RedisJobQueue("redis://unused")
    backend = AsyncMock()
    backend.get.side_effect = RedisConnectionError("redis down")
    queue._redis = backend
    calls = [0]
    manager = _manager(queue, calls)

    with pytest.raises(SubmissionUnavailableError, match="backend is unavailable"):
        await manager.create_job(
            "session-a",
            "sh",
            owner_id="owner-a",
            stdin=b"echo hi\n",
            timeout=300,
            submission_key="task:project-1:redis-read-down",
        )
    await asyncio.sleep(0)
    assert calls[0] == 0


@pytest.mark.asyncio
async def test_redis_claim_transport_failure_fails_before_execution():
    queue = RedisJobQueue("redis://unused")
    backend = AsyncMock()
    backend.get.return_value = None
    backend.set.side_effect = RedisConnectionError("redis down")
    queue._redis = backend
    calls = [0]
    manager = _manager(queue, calls)

    with pytest.raises(SubmissionUnavailableError, match="backend is unavailable"):
        await manager.create_job(
            "session-a",
            "sh",
            owner_id="owner-a",
            stdin=b"echo hi\n",
            timeout=300,
            submission_key="task:project-1:redis-write-down",
        )
    await asyncio.sleep(0)
    assert calls[0] == 0
