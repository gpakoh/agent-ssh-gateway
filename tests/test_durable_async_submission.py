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

    async def eval(self, script: str, numkeys: int, *args):
        import json as _json
        if "sub_created and env_created" in script:
            sub_key, env_key = args[0], args[1]
            sub_claim, env_json, owner, payload, _ttl_s = args[2], args[3], args[4], args[5], args[6]
            if sub_key not in self.values and env_key not in self.values:
                self.values[sub_key] = sub_claim
                self.values[env_key] = env_json
                return [1, 0]
            existing = self.values.get(sub_key)
            if existing is None:
                return [0, 0]
            try:
                tbl = _json.loads(existing)
                if tbl.get("owner_id") != owner or tbl.get("payload_hash") != payload:
                    return [0, -1]
                return [0, 0, tbl.get("job_id", "")]
            except Exception:
                return [0, 0]
        if "claimed_at" in script:
            env_key, _proc_key, lease_key = args[0], args[1], args[2]
            token, lease_ttl_s, now_s = args[3], args[4], args[5]
            lease_ttl = int(lease_ttl_s)
            now = float(now_s)
            raw = self.values.get(env_key)
            if not raw:
                return [0]
            env = _json.loads(raw)
            st = env.get("status")
            if st in ("completed", "failed", "cancelled", "ambiguous"):
                return [0]
            if st == "processing" and env.get("worker_token") == token:
                env["lease_expiry"] = now + lease_ttl
                env["last_heartbeat"] = now
                self.values[env_key] = _json.dumps(env)
                self.values[lease_key] = token
                return [1]
            if st == "processing":
                if env.get("lease_expiry") and now <= env["lease_expiry"]:
                    return [0]
            env["status"] = "processing"
            env["worker_token"] = token
            env["lease_expiry"] = now + lease_ttl
            env["last_heartbeat"] = now
            env["claimed_at"] = now
            self.values[env_key] = _json.dumps(env)
            self.values[lease_key] = token
            return [1]
        if "comp_key" in script:
            env_key = args[0]
            _proc_key, lease_key = args[1], args[2]
            _comp_key, _dead_key = args[3], args[4]
            token, new_status = args[5], args[6]
            stdout, stderr = args[7], args[8]
            exit_code_s, error_msg = args[9], args[10]
            now_s = args[11]
            now = float(now_s)
            raw = self.values.get(env_key)
            if not raw:
                return [0]
            env = _json.loads(raw)
            if env.get("status") != "processing" or env.get("worker_token") != token:
                return [0]
            env["status"] = new_status
            env["finished_at"] = now
            env["stdout"] = stdout
            env["stderr"] = stderr
            if exit_code_s:
                env["exit_code"] = int(exit_code_s)
            if error_msg:
                env["error"] = error_msg
            self.values[env_key] = _json.dumps(env)
            return [1]
        if "ZRANGEBYSCORE" in script and "SCAN" not in script:
            return []
        if "env['status'] ~= 'processing' or env['worker_token'] ~= token" in script:
            env_key = args[0]
            token = args[3]
            lease_ttl = int(args[4])
            now = float(args[5])
            raw = self.values.get(env_key)
            if not raw:
                return [0]
            env = _json.loads(raw)
            if env.get("status") != "processing" or env.get("worker_token") != token:
                return [0]
            env["lease_expiry"] = now + lease_ttl
            env["last_heartbeat"] = now
            self.values[env_key] = _json.dumps(env)
            return [1]
        if "SCAN" in script:
            sub_prefix = args[0]
            env_prefix = args[1]
            _limit = int(args[2])
            result = []
            for key in list(self.values.keys()):
                if not key.startswith(sub_prefix):
                    continue
                raw = self.values.get(key)
                if not raw:
                    continue
                try:
                    claim = _json.loads(raw)
                    jid = str(claim.get("job_id", ""))
                    env_raw = self.values.get(env_prefix + jid)
                    if env_raw:
                        env = _json.loads(env_raw)
                        if env.get("status") == "pending":
                            result.append(jid)
                except Exception:
                    continue
            return result
        return [0]


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
        "session-a",
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
        "session-a",
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
    backend.eval.side_effect = RedisConnectionError("redis down")
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
