"""Regression tests for durable async submission idempotency."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.exceptions import SubmissionConflictError, SubmissionUnavailableError
from app.job_manager import JobManager
from app.redis_queue import RedisJobQueue


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.scan_calls = 0

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

    async def scan(self, cursor=0, *, match: str | None = None, count: int | None = None):
        import fnmatch

        self.scan_calls += 1
        keys = list(self.values)
        if match is not None:
            keys = [key for key in keys if fnmatch.fnmatch(key, match)]
        return 0, keys

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
async def test_job_manager_retains_submission_identity_for_in_memory_recovery():
    queue = _queue()
    manager = _manager(queue, [0])
    prefix = "task:project-1:agent-memory:attempt:"
    submission_key = prefix + ("a" * 32)
    job_id = await manager.create_job(
        "session-a",
        "sh",
        owner_id="owner-a",
        stdin=b"echo hi\n",
        timeout=300,
        submission_key=submission_key,
    )

    exact = await manager.resolve_submission_claim_in_memory(submission_key)
    family = await manager.resolve_submission_claim_in_memory(prefix, family=True)
    job = await manager.get_job(job_id)
    assert job is not None

    assert exact is not None and exact["job_id"] == job_id
    assert exact["owner_id"] == "owner-a"
    assert len(exact["payload_hash"]) == 64
    assert family == exact
    assert "submission_key" not in job.to_dict()
    await asyncio.wait_for(job.completed_event.wait(), timeout=2)


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
async def test_resolve_submission_claim_recovers_exact_historical_identity_without_envelope():
    queue = _queue()
    payload_hash = "a" * 64
    await queue.claim_submission(
        "task:historical-project:historical-task",
        job_id="job-historical",
        owner_id="owner-a",
        payload_hash=payload_hash,
    )

    claim = await queue.resolve_submission_claim(
        "task:historical-project:historical-task"
    )

    assert claim == {
        "job_id": "job-historical",
        "owner_id": "owner-a",
        "payload_hash": payload_hash,
    }


@pytest.mark.asyncio
async def test_resolve_submission_claim_recovers_exact_identity_from_retained_envelope():
    queue = _queue()
    submission_key = "task:historical-project:attempted:attempt:abc123"
    payload_hash = "c" * 64
    queue._redis.values["ssh_gateway:job:job-envelope"] = json.dumps(
        {
            "version": 1,
            "job_id": "job-envelope",
            "submission_key": submission_key,
            "owner_id": "owner-a",
            "payload_hash": payload_hash,
            "status": "completed",
        }
    )

    claim = await queue.resolve_submission_claim(submission_key)

    assert claim == {
        "job_id": "job-envelope",
        "owner_id": "owner-a",
        "payload_hash": payload_hash,
    }


@pytest.mark.asyncio
async def test_resolve_submission_claim_amortizes_envelope_scan_across_exact_keys():
    queue = _queue()
    for suffix, payload_hex in (("one", "a"), ("two", "b")):
        submission_key = f"task:historical-project:{suffix}:attempt:abc123"
        queue._redis.values[f"ssh_gateway:job:job-{suffix}"] = json.dumps(
            {
                "version": 1,
                "job_id": f"job-{suffix}",
                "submission_key": submission_key,
                "owner_id": "owner-a",
                "payload_hash": payload_hex * 64,
                "status": "completed",
            }
        )

    first = await queue.resolve_submission_claim(
        "task:historical-project:one:attempt:abc123"
    )
    second = await queue.resolve_submission_claim(
        "task:historical-project:two:attempt:abc123"
    )

    assert first is not None and first["job_id"] == "job-one"
    assert second is not None and second["job_id"] == "job-two"
    assert queue._redis.scan_calls == 1


@pytest.mark.asyncio
async def test_resolve_submission_family_claim_recovers_one_strict_attempt_member():
    queue = _queue()
    prefix = "task:historical-project:task-1:attempt:"
    submission_key = prefix + ("a" * 32)
    queue._redis.values["ssh_gateway:job:job-family"] = json.dumps(
        {
            "version": 1,
            "job_id": "job-family",
            "submission_key": submission_key,
            "owner_id": "owner-a",
            "payload_hash": "c" * 64,
            "status": "completed",
        }
    )

    claim = await queue.resolve_submission_family_claim(prefix)

    assert claim == {
        "job_id": "job-family",
        "owner_id": "owner-a",
        "payload_hash": "c" * 64,
    }
    assert queue._redis.scan_calls == 1
    assert await queue.resolve_submission_family_claim(prefix) == claim
    assert queue._redis.scan_calls == 1


@pytest.mark.asyncio
async def test_resolve_submission_family_claim_returns_none_for_zero_members():
    queue = _queue()

    assert (
        await queue.resolve_submission_family_claim(
            "task:historical-project:missing:attempt:"
        )
        is None
    )
    assert queue._redis.scan_calls == 1


@pytest.mark.asyncio
async def test_resolve_submission_family_claim_rejects_malformed_attempt_suffix():
    queue = _queue()
    prefix = "task:historical-project:malformed:attempt:"
    queue._redis.values["ssh_gateway:job:job-malformed"] = json.dumps(
        {
            "version": 1,
            "job_id": "job-malformed",
            "submission_key": prefix + ("A" * 32),
            "owner_id": "owner-a",
            "payload_hash": "d" * 64,
            "status": "completed",
        }
    )

    with pytest.raises(SubmissionUnavailableError, match="attempt family is invalid"):
        await queue.resolve_submission_family_claim(prefix)


@pytest.mark.asyncio
async def test_resolve_submission_family_claim_rejects_multiple_members():
    queue = _queue()
    prefix = "task:historical-project:ambiguous-family:attempt:"
    for index, attempt_id in enumerate(("a" * 32, "b" * 32), start=1):
        queue._redis.values[f"ssh_gateway:job:job-family-{index}"] = json.dumps(
            {
                "version": 1,
                "job_id": f"job-family-{index}",
                "submission_key": prefix + attempt_id,
                "owner_id": "owner-a",
                "payload_hash": f"{index}" * 64,
                "status": "completed",
            }
        )

    with pytest.raises(SubmissionUnavailableError, match="attempt family is ambiguous"):
        await queue.resolve_submission_family_claim(prefix)


@pytest.mark.asyncio
async def test_resolve_submission_claim_rejects_duplicate_exact_envelopes():
    queue = _queue()
    submission_key = "task:historical-project:ambiguous:attempt:abc123"
    payload_hash = "d" * 64
    envelope = {
        "version": 1,
        "submission_key": submission_key,
        "owner_id": "owner-a",
        "payload_hash": payload_hash,
        "status": "completed",
    }
    queue._redis.values["ssh_gateway:job:job-envelope-a"] = json.dumps(
        {**envelope, "job_id": "job-envelope-a"}
    )
    queue._redis.values["ssh_gateway:job:job-envelope-b"] = json.dumps(
        {**envelope, "job_id": "job-envelope-b"}
    )

    with pytest.raises(SubmissionUnavailableError, match="identity is ambiguous"):
        await queue.resolve_submission_claim(submission_key)


@pytest.mark.asyncio
async def test_resolve_submission_claim_rejects_mismatched_envelope_storage_identity():
    queue = _queue()
    submission_key = "task:historical-project:storage-mismatch:attempt:abc123"
    queue._redis.values["ssh_gateway:job:job-storage-a"] = json.dumps(
        {
            "version": 1,
            "job_id": "job-storage-b",
            "submission_key": submission_key,
            "owner_id": "owner-a",
            "payload_hash": "e" * 64,
            "status": "completed",
        }
    )

    with pytest.raises(SubmissionUnavailableError, match="storage identity is inconsistent"):
        await queue.resolve_submission_claim(submission_key)


@pytest.mark.asyncio
async def test_resolve_submission_claim_does_not_scan_non_task_keyspace():
    queue = _queue()

    assert await queue.resolve_submission_claim("external-submission-key") is None
    assert queue._redis.scan_calls == 0


@pytest.mark.asyncio
async def test_resolve_submission_claim_rejects_corrupt_claim():
    queue = _queue()
    key = queue._submission_storage_key("task:historical-project:corrupt")
    queue._redis.values[key] = '{"version":1,"job_id":"job-x","owner_id":"owner-a","payload_hash":"short"}'

    with pytest.raises(SubmissionUnavailableError, match="record is invalid"):
        await queue.resolve_submission_claim("task:historical-project:corrupt")


@pytest.mark.asyncio
async def test_resolve_submission_claim_rejects_inconsistent_retained_envelope():
    queue = _queue()
    payload_hash = "b" * 64
    submission_key = "task:historical-project:mismatch"
    await queue.claim_submission(
        submission_key,
        job_id="job-historical",
        owner_id="owner-a",
        payload_hash=payload_hash,
    )
    queue._redis.values["ssh_gateway:job:job-historical"] = (
        '{"job_id":"job-other","owner_id":"owner-a"}'
    )

    with pytest.raises(SubmissionUnavailableError, match="binding is inconsistent"):
        await queue.resolve_submission_claim(submission_key)


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
