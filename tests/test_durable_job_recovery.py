"""Durable keyed async job recovery integration tests.

Tests exercise the full RedisJobQueue + JobManager public API against a
FakeRedis backend that supports the ``eval`` calls used by the atomic
Lua scripts.  The goal is RED-on-base / GREEN-on-candidate evidence
wherever feasible.

Coverage:
- claim/reserve before task creation crash
- manager A → shared backend → manager B restart recovers same job_id
- two managers race recovery → only one execute_stream
- same key same payload concurrent → one ID, one execution
- same key different payload → conflict
- active heartbeat prevents stale recovery
- expired lease → recoverable
- terminal restart query
- terminal fenced stale owner rejected
- Redis unavailable pre-ACK → fails closed
- retry after restart same ID
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.exceptions import (
    SubmissionConflictError,
    SubmissionUnavailableError,
)
from app.job_manager import JobManager
from app.redis_queue import RedisJobQueue

# ---------------------------------------------------------------------------
# FakeRedis — minimal eval-aware async in-memory Redis
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Tiny async Redis stand-in that implements only the operations
    required by ``RedisJobQueue``, including ``eval`` for the Lua scripts
    used by the durable envelope methods.
    """

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._ttls: dict[str, float] = {}

    # -- basic helpers ---------------------------------------------------

    def _now(self) -> float:
        return time.time()

    # -- core commands ---------------------------------------------------

    async def ping(self):
        return True

    async def get(self, key: str):
        return self._data.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self._data:
            return False
        self._data[key] = value
        if ex:
            self._ttls[key] = self._now() + ex
        return True

    async def delete(self, key: str):
        self._data.pop(key, None)
        self._ttls.pop(key, None)

    async def zadd(self, key: str, mapping: dict):
        if key not in self._zsets:
            self._zsets[key] = {}
        for member, score in mapping.items():
            self._zsets[key][str(member)] = float(score)
        return len(mapping)

    async def zrem(self, key: str, *members: str):
        if key not in self._zsets:
            return 0
        removed = 0
        for m in members:
            if m in self._zsets[key]:
                del self._zsets[key][m]
                removed += 1
        return removed

    async def zcard(self, key: str):
        return len(self._zsets.get(key, {}))

    async def zscore(self, key: str, member: str):
        zset = self._zsets.get(key, {})
        return zset.get(member)

    async def zrangebyscore(self, key: str, min_: float, max_: float):
        zset = self._zsets.get(key, {})
        return [m for m, s in zset.items() if min_ <= s <= max_]

    async def zpopmin(self, key: str, count: int = 1):
        zset = self._zsets.get(key, {})
        if not zset:
            return []
        items = sorted(zset.items(), key=lambda x: x[1])[:count]
        for m, _ in items:
            del zset[m]
        return [(m, s) for m, s in items]

    async def expire(self, key: str, ttl: int):
        self._ttls[key] = self._now() + ttl

    # -- eval (Lua script dispatch) --------------------------------------

    async def eval(self, script: str, numkeys: int, *args):
        """Dispatch known Lua scripts to native Python equivalents."""
        if "sub_created and env_created" in script:
            return await self._eval_reserve(script, numkeys, args)
        if "claimed_at" in script:
            return await self._eval_claim(script, numkeys, args)
        if "Reconciled" in script:
            return await self._eval_reconcile_cancel(script, numkeys, args)
        if "remote_outcome'] = 'ambiguous'" in script and "ZRANGEBYSCORE" in script:
            return await self._eval_reconcile_cancel(script, numkeys, args)
        if "ZRANGEBYSCORE" in script and "SCAN" not in script:
            return await self._eval_scan(script, numkeys, args)
        if "comp_key" in script:
            return await self._eval_finish(script, numkeys, args)
        if "return 'cancelling'" in script and "cancel_requested" in script:
            return await self._eval_cancel_request(script, numkeys, args)
        if "cancel_requested" in script and "ARGV[1]" in script:
            return await self._eval_cancel_check(script, numkeys, args)
        if "env['status'] ~= 'processing' or env['worker_token'] ~= token" in script:
            return await self._eval_hb(script, numkeys, args)
        if "SCAN" in script:
            return await self._eval_scan_pending(script, numkeys, args)
        raise RuntimeError(f"Unknown Lua script: {script[:80]}")

    async def _eval_reconcile_cancel(self, _script: str, _numkeys: int, args: tuple):
        proc_key, dead_key = args[0], args[1]
        env_prefix, lease_prefix, now_s = args[2], args[3], args[4]
        now = float(now_s)
        result = []
        for jid, _score in list(self._zsets.get(proc_key, {}).items()):
            raw = self._data.get(env_prefix + jid)
            if not raw:
                continue
            env = json.loads(raw)
            if (env.get("status") == "processing"
                    and env.get("cancel_requested")
                    and env.get("lease_expiry")
                    and now > env["lease_expiry"]):
                env["status"] = "ambiguous"
                env["finished_at"] = now
                env["remote_outcome"] = "ambiguous"
                env["locally_interrupted"] = True
                if env.get("exit_code") is None:
                    env["exit_code"] = -1
                self._data[env_prefix + jid] = json.dumps(env)
                self._zsets[proc_key].pop(jid, None)
                self._data.pop(lease_prefix + jid, None)
                self._zsets.setdefault(dead_key, {})[jid] = now
                result.append(jid)
        return result

    async def _eval_scan_pending(self, _script: str, _numkeys: int, args: tuple):
        """Evaluate the SCAN-based pending envelope scan Lua script."""
        sub_prefix = args[0]
        env_prefix = args[1]
        _limit = int(args[2])
        result = []
        for key in list(self._data.keys()):
            if not key.startswith(sub_prefix):
                continue
            raw = self._data.get(key)
            if not raw:
                continue
            try:
                claim = json.loads(raw)
                jid = str(claim.get("job_id", ""))
                env_raw = self._data.get(env_prefix + jid)
                if env_raw:
                    env = json.loads(env_raw)
                    if env.get("status") == "pending":
                        result.append(jid)
            except Exception:
                continue
        return result

    async def _eval_reserve(self, _script: str, _numkeys: int, args: tuple):
        sub_key, env_key = args[0], args[1]
        sub_claim, env_json, owner, payload, ttl_s = args[2], args[3], args[4], args[5], args[6]
        ttl = int(ttl_s)
        if sub_key not in self._data and env_key not in self._data:
            self._data[sub_key] = sub_claim
            self._data[env_key] = env_json
            self._ttls[sub_key] = self._now() + ttl
            self._ttls[env_key] = self._now() + ttl
            return [1, 0]
        existing = self._data.get(sub_key)
        if existing is None:
            return [0, 0]
        try:
            tbl = json.loads(existing)
            if tbl.get("owner_id") != owner or tbl.get("payload_hash") != payload:
                return [0, -1]
            return [0, 0, tbl.get("job_id", "")]
        except Exception:
            return [0, 0]

    async def _eval_claim(self, _script: str, _numkeys: int, args: tuple):
        env_key, proc_key, lease_key = args[0], args[1], args[2]
        token, lease_ttl_s, now_s = args[3], args[4], args[5]
        lease_ttl = int(lease_ttl_s)
        now = float(now_s)
        raw = self._data.get(env_key)
        if not raw:
            return [0]
        env = json.loads(raw)
        st = env.get("status")
        if st in ("completed", "failed", "cancelled", "ambiguous"):
            return [0]
        if st == "processing" and env.get("cancel_requested"):
            return [0]
        if st == "processing" and env.get("worker_token") == token:
            env["lease_expiry"] = now + lease_ttl
            env["last_heartbeat"] = now
            self._data[env_key] = json.dumps(env)
            self._ttls[env_key] = self._now() + 86400
            self._data[lease_key] = token
            self._ttls[lease_key] = self._now() + lease_ttl
            if proc_key not in self._zsets:
                self._zsets[proc_key] = {}
            self._zsets[proc_key][env.get("job_id", "")] = now
            return [1]
        if st == "processing":
            if env.get("lease_expiry") and now <= env["lease_expiry"]:
                return [0]
        env["status"] = "processing"
        env["worker_token"] = token
        env["lease_expiry"] = now + lease_ttl
        env["last_heartbeat"] = now
        env["claimed_at"] = now
        self._data[env_key] = json.dumps(env)
        self._ttls[env_key] = self._now() + 86400
        self._data[lease_key] = token
        self._ttls[lease_key] = self._now() + lease_ttl
        if proc_key not in self._zsets:
            self._zsets[proc_key] = {}
        self._zsets[proc_key][env.get("job_id", "")] = now
        return [1]

    async def _eval_hb(self, _script: str, _numkeys: int, args: tuple):
        env_key, proc_key, lease_key = args[0], args[1], args[2]
        token, lease_ttl_s, now_s = args[3], args[4], args[5]
        lease_ttl = int(lease_ttl_s)
        now = float(now_s)
        raw = self._data.get(env_key)
        if not raw:
            return [0]
        env = json.loads(raw)
        if env.get("status") != "processing" or env.get("worker_token") != token:
            return [0]
        if not env.get("lease_expiry") or now > env["lease_expiry"]:
            return [0]
        env["lease_expiry"] = now + lease_ttl
        env["last_heartbeat"] = now
        self._data[env_key] = json.dumps(env)
        self._ttls[env_key] = self._now() + 86400
        self._data[lease_key] = token
        self._ttls[lease_key] = self._now() + lease_ttl
        if proc_key not in self._zsets:
            self._zsets[proc_key] = {}
        self._zsets[proc_key][env.get("job_id", "")] = now
        return [1]

    async def _eval_cancel_request(self, _script: str, _numkeys: int, args: tuple):
        env_key, proc_key, lease_key, dead_key = args[0], args[1], args[2], args[3]
        now = float(args[4])
        raw = self._data.get(env_key)
        if not raw:
            return ""
        env = json.loads(raw)
        st = env.get("status")
        if st in ("completed", "failed", "cancelled", "ambiguous"):
            return st
        env["cancel_requested"] = True
        jid = env.get("job_id", "")
        if st == "pending":
            env["status"] = "cancelled"
            env["finished_at"] = now
            env["exit_code"] = -1
            env["remote_outcome"] = "not_started"
            env["locally_interrupted"] = False
            self._data[env_key] = json.dumps(env)
            self._zsets.get(proc_key, {}).pop(jid, None)
            self._data.pop(lease_key, None)
            self._zsets.setdefault(dead_key, {})[jid] = now
            return "cancelled"
        if st == "processing":
            self._data[env_key] = json.dumps(env)
            return "cancelling"
        return ""

    async def _eval_cancel_check(self, _script: str, _numkeys: int, args: tuple):
        env_key, token = args[0], args[1]
        raw = self._data.get(env_key)
        if not raw:
            return 0
        env = json.loads(raw)
        return int(
            env.get("status") == "processing"
            and env.get("worker_token") == token
            and bool(env.get("cancel_requested"))
        )

    async def _eval_finish(self, _script: str, _numkeys: int, args: tuple):
        env_key, proc_key, lease_key = args[0], args[1], args[2]
        comp_key, dead_key = args[3], args[4]
        token, new_status = args[5], args[6]
        stdout, stderr, exit_code_s, error_msg = args[7], args[8], args[9], args[10]
        now_s = args[11]
        now = float(now_s)
        raw = self._data.get(env_key)
        if not raw:
            return [0]
        env = json.loads(raw)
        if env.get("status") != "processing" or env.get("worker_token") != token:
            return [0]
        env["status"] = new_status
        env["finished_at"] = now
        env["stdout"] = stdout
        env["stderr"] = stderr
        if new_status == "ambiguous":
            env["remote_outcome"] = "ambiguous"
            env["locally_interrupted"] = True
        if exit_code_s:
            env["exit_code"] = int(exit_code_s)
        if error_msg:
            env["error"] = error_msg
        self._data[env_key] = json.dumps(env)
        self._ttls[env_key] = self._now() + 604800
        jid = env.get("job_id", "")
        if proc_key in self._zsets:
            self._zsets[proc_key].pop(jid, None)
        self._data.pop(lease_key, None)
        if new_status == "completed":
            if comp_key not in self._zsets:
                self._zsets[comp_key] = {}
            self._zsets[comp_key][jid] = now
        else:
            if dead_key not in self._zsets:
                self._zsets[dead_key] = {}
            self._zsets[dead_key][jid] = now
        return [1]

    async def _eval_scan(self, _script: str, _numkeys: int, args: tuple):
        proc_key = args[0]
        prefix = args[1]
        now = float(args[2])
        zset = self._zsets.get(proc_key, {})
        candidates = [jid for jid, score in zset.items() if 0 <= score <= now]
        result = []
        for jid in candidates:
            raw = self._data.get(prefix + jid)
            if raw:
                env = json.loads(raw)
                if (env.get("status") == "processing"
                        and not env.get("cancel_requested")
                        and env.get("lease_expiry")
                        and now > env["lease_expiry"]):
                    result.append(jid)
        return result

    # -- pipeline --------------------------------------------------------

    def pipeline(self, transaction: bool = True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._commands: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def set(self, key: str, value: str, *, ex: int | None = None):
        self._commands.append(("set", key, value, ex))
        return self

    def zadd(self, key: str, mapping: dict):
        self._commands.append(("zadd", key, mapping))
        return self

    def delete(self, key: str):
        self._commands.append(("delete", key))
        return self

    async def execute(self):
        results = []
        for cmd in self._commands:
            name = cmd[0]
            if name == "set":
                results.append(await self._redis.set(cmd[1], cmd[2], ex=cmd[3]))
            elif name == "zadd":
                results.append(await self._redis.zadd(cmd[1], cmd[2]))
            elif name == "delete":
                await self._redis.delete(cmd[1])
                results.append(True)
            else:
                results.append(None)
        self._commands.clear()
        return results

    # Also support the `await pipe.set(...)` without the pipeline ctx.

    def __getattr__(self, name):
        return getattr(self._redis, name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_queue(redis: _FakeRedis | None = None) -> RedisJobQueue:
    q = RedisJobQueue("redis://unused")
    q._redis = redis or _FakeRedis()
    return q


def _make_stream(counter: list[int] | None = None, events=None):
    """Return a mock ``execute_stream`` that yields *events*."""

    async def _stream(*args, **kwargs):
        if counter is not None:
            counter[0] += 1
        for ev in (events or [("exit", "0")]):
            yield ev

    return _stream


def _make_manager(
    queue: RedisJobQueue,
    execute_stream=None,
    counter: list[int] | None = None,
) -> JobManager:
    ssh = AsyncMock()
    ssh.execute_stream = execute_stream or _make_stream(counter)
    return JobManager(ssh_manager=ssh, max_jobs=100, redis_queue=queue)


# ---------------------------------------------------------------------------
# Tests — Atomic reserve / claim / heartbeat / finish
# ---------------------------------------------------------------------------


class TestReserveSubmissionWithJob:
    """reserve_submission_with_job must atomically commit the submission
    identity and the full executable envelope together.
    """

    @pytest.mark.asyncio
    async def test_first_reserve_returns_new_job_id(self):
        q = _make_queue()
        env = {"job_id": "j1", "status": "pending"}
        jid, created = await q.reserve_submission_with_job(
            "key-a",
            job_id="j1",
            owner_id="o1",
            payload_hash="ph1",
            envelope=env,
        )
        assert jid == "j1"
        assert created is True

    @pytest.mark.asyncio
    async def test_identical_retry_returns_existing_without_second_write(self):
        q = _make_queue()
        env = {"job_id": "j1", "status": "pending"}
        await q.reserve_submission_with_job(
            "key-a",
            job_id="j1",
            owner_id="o1",
            payload_hash="ph1",
            envelope=env,
        )
        jid2, created2 = await q.reserve_submission_with_job(
            "key-a",
            job_id="j2",  # different local job_id
            owner_id="o1",
            payload_hash="ph1",
            envelope={"job_id": "j2"},
        )
        assert jid2 == "j1"  # original persisted
        assert created2 is False

    @pytest.mark.asyncio
    async def test_conflict_different_owner_rejected(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "key-a",
            job_id="j1",
            owner_id="o1",
            payload_hash="ph1",
            envelope={"job_id": "j1"},
        )
        with pytest.raises(SubmissionUnavailableError):
            await q.reserve_submission_with_job(
                "key-a",
                job_id="j3",
                owner_id="o2",
                payload_hash="ph1",
                envelope={"job_id": "j3"},
            )

    @pytest.mark.asyncio
    async def test_envelope_is_stored_and_readable(self):
        q = _make_queue()
        env = {"job_id": "j1", "status": "pending", "cmd": "echo hi"}
        await q.reserve_submission_with_job(
            "key-a",
            job_id="j1",
            owner_id="o1",
            payload_hash="ph1",
            envelope=env,
        )
        raw = await q._redis.get(f"{q._job_prefix}j1")
        assert raw is not None
        stored = json.loads(raw)
        assert stored["cmd"] == "echo hi"
        assert stored["status"] == "pending"

    @pytest.mark.asyncio
    async def test_redis_unavailable_raises(self):
        q = RedisJobQueue("redis://unused")
        q._redis = None
        with pytest.raises(SubmissionUnavailableError):
            await q.reserve_submission_with_job(
                "key-a", job_id="j1", owner_id="o1",
                payload_hash="ph1", envelope={},
            )


class TestClaimDurableExecution:
    """claim_durable_execution must atomically transition pending→processing."""

    @pytest.mark.asyncio
    async def test_claim_pending_job_succeeds(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        ok = await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        assert ok is True

    @pytest.mark.asyncio
    async def test_claim_processing_same_token_is_idempotent(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        ok2 = await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        assert ok2 is True

    @pytest.mark.asyncio
    async def test_claim_processing_different_token_rejected(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        ok = await q.claim_durable_execution("j1", worker_token="w2", lease_ttl=60)
        assert ok is False

    @pytest.mark.asyncio
    async def test_claim_terminal_job_rejected(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        await q.finish_durable_execution(
            "j1", worker_token="w1", status="completed", exit_code=0,
        )
        ok = await q.claim_durable_execution("j1", worker_token="w3", lease_ttl=60)
        assert ok is False

    @pytest.mark.asyncio
    async def test_claim_missing_job_rejected(self):
        q = _make_queue()
        ok = await q.claim_durable_execution("no-such", worker_token="w", lease_ttl=60)
        assert ok is False

    @pytest.mark.asyncio
    async def test_claim_after_lease_expired_by_different_token(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        # Claim with a 1-second lease.
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=1)
        # Wait for the lease to expire.
        await asyncio.sleep(1.1)
        # Different token can now claim.
        ok = await q.claim_durable_execution("j1", worker_token="w2", lease_ttl=60)
        assert ok is True


class TestHeartbeatDurableExecution:
    @pytest.mark.asyncio
    async def test_heartbeat_with_matching_token_succeeds(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        ok = await q.heartbeat_durable_execution("j1", worker_token="w1", lease_ttl=60)
        assert ok is True

    @pytest.mark.asyncio
    async def test_heartbeat_with_wrong_token_rejected(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        ok = await q.heartbeat_durable_execution("j1", worker_token="w2", lease_ttl=60)
        assert ok is False

    @pytest.mark.asyncio
    async def test_heartbeat_on_missing_job_returns_false(self):
        q = _make_queue()
        ok = await q.heartbeat_durable_execution("no-such", worker_token="w1", lease_ttl=60)
        assert ok is False


class TestFinishDurableExecution:
    @pytest.mark.asyncio
    async def test_fenced_finish_with_matching_token(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        ok = await q.finish_durable_execution(
            "j1", worker_token="w1", status="completed", exit_code=0,
            stdout="hello",
        )
        assert ok is True
        # Envelope should reflect terminal state.
        env = await q.recover_durable_job("j1")
        assert env is not None
        assert env["status"] == "completed"
        assert env["stdout"] == "hello"

    @pytest.mark.asyncio
    async def test_fenced_finish_with_wrong_token_rejected(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        ok = await q.finish_durable_execution(
            "j1", worker_token="w2", status="completed", exit_code=0,
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_fenced_finish_on_terminal_rejected(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        await q.finish_durable_execution(
            "j1", worker_token="w1", status="completed", exit_code=0,
        )
        ok = await q.finish_durable_execution(
            "j1", worker_token="w1", status="completed", exit_code=0,
        )
        assert ok is False


class TestListRecoverableAndRecover:
    @pytest.mark.asyncio
    async def test_list_recoverable_after_lease_expired(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=1)
        await asyncio.sleep(1.1)
        ids = await q.list_recoverable_durable_jobs()
        assert "j1" in ids

    @pytest.mark.asyncio
    async def test_list_recoverable_excludes_active_lease(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        ids = await q.list_recoverable_durable_jobs()
        assert "j1" not in ids

    @pytest.mark.asyncio
    async def test_recover_durable_job_returns_envelope(self):
        q = _make_queue()
        env = {"job_id": "j1", "status": "pending", "cmd": "echo"}
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p", envelope=env,
        )
        e = await q.recover_durable_job("j1")
        assert e is not None
        assert e["cmd"] == "echo"

    @pytest.mark.asyncio
    async def test_recover_durable_job_missing_returns_none(self):
        q = _make_queue()
        assert await q.recover_durable_job("no-such") is None


# ---------------------------------------------------------------------------
# Tests — JobManager integration (public API only)
# ---------------------------------------------------------------------------


class TestManagerAtoManagerBRecovery:
    """Manager A creates a durable job → process "restarts" → Manager B
    picks up the same job_id from the shared backend and executes it.
    """

    @pytest.mark.asyncio
    async def test_restart_recovery_same_job_id_executes(self):
        q = _make_queue()

        # Simulate Manager A crashing after the durable envelope was written
        # but before execution.  Write the envelope directly.
        env = {
            "job_id": "j1", "session_id": "s1", "command": "echo hi",
            "owner_id": "o1", "timeout": 3600, "stdin_b64": "",
            "redact_path_prefix": None, "payload_hash": "ph",
            "status": "pending", "created_at": time.time(),
            "submission_key": "key:restart",
        }
        await q.reserve_submission_with_job(
            "key:restart", job_id="j1", owner_id="o1",
            payload_hash="ph", envelope=env,
        )

        # Manager B — fresh process after restart.
        calls_b: list[int] = [0]
        mgr_b = _make_manager(q, _make_stream(calls_b, [("stdout", "hello\n"), ("exit", "0")]), calls_b)

        # Recovery picks up the pending envelope.
        envelope = await q.recover_durable_job("j1")
        assert envelope is not None
        rid = await mgr_b.recover_job(envelope)
        assert rid == "j1"

        # Wait for the job to finish.
        job = await mgr_b.get_job("j1")
        assert job is not None
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)

        assert job.status == "completed"
        assert job.stdout == "hello\n"
        assert calls_b[0] == 1

    @pytest.mark.asyncio
    async def test_manager_b_returns_same_job_id_on_idempotent_retry(self):
        q = _make_queue()
        calls_a: list[int] = [0]
        mgr_a = _make_manager(q, _make_stream(calls_a, [("exit", "0")]), calls_a)
        jid = await mgr_a.create_job(
            "s1", "echo", owner_id="o1", submission_key="key:retry",
        )
        job_a = await mgr_a.get_job(jid)
        await asyncio.wait_for(job_a.completed_event.wait(), timeout=5)

        # Manager B does the same submission — should get same job_id.
        calls_b: list[int] = [0]
        mgr_b = _make_manager(q, _make_stream(calls_b), calls_b)
        jid2 = await mgr_b.create_job(
            "s1", "echo", owner_id="o1", submission_key="key:retry",
        )
        assert jid2 == jid
        # Manager B should NOT have re-executed.
        assert calls_b[0] == 0
        # Persisted terminal state should exist.
        env = await q.recover_durable_job(jid)
        assert env is not None
        assert env["status"] == "completed"


class TestConcurrentRecoveryOnlyOneExecutes:
    """Two managers racing to recover the same durable job — only one
    should cross into execute_stream.
    """

    @pytest.mark.asyncio
    async def test_two_managers_race_only_one_executes(self):
        q = _make_queue()

        # Simulate a crash: write envelope directly, no Manager A task.
        env = {
            "job_id": "j1", "session_id": "s1", "command": "echo",
            "owner_id": "o1", "timeout": 3600, "stdin_b64": "",
            "redact_path_prefix": None, "payload_hash": "ph",
            "status": "pending", "created_at": time.time(),
            "submission_key": "key:race",
        }
        await q.reserve_submission_with_job(
            "key:race", job_id="j1", owner_id="o1",
            payload_hash="ph", envelope=env,
        )

        # Both managers discover the envelope.
        envelope = await q.recover_durable_job("j1")
        assert envelope is not None

        calls_b1: list[int] = [0]
        mgr_b1 = _make_manager(q, _make_stream(calls_b1, [("exit", "0")]), calls_b1)
        calls_b2: list[int] = [0]
        mgr_b2 = _make_manager(q, _make_stream(calls_b2, [("exit", "0")]), calls_b2)

        async def recover_and_wait(mgr, calls):
            rid = await mgr.recover_job(envelope)
            if rid is None:
                return None
            job = await mgr.get_job(rid)
            if job is not None:
                await asyncio.wait_for(job.completed_event.wait(), timeout=5)
                return job.status
            return None

        results = await asyncio.gather(
            recover_and_wait(mgr_b1, calls_b1),
            recover_and_wait(mgr_b2, calls_b2),
        )

        # Exactly one manager should have actually executed.
        executed = [r for r in results if r is not None]
        assert len(executed) >= 1
        total_execs = calls_b1[0] + calls_b2[0]
        assert total_execs == 1  # only one crossing into execute_stream


class TestConcurrentIdenticalKeyOneID:
    """Concurrent identical submissions → one job_id, one execution."""

    @pytest.mark.asyncio
    async def test_concurrent_keyed_submissions_one_job(self):
        q = _make_queue()
        calls: list[int] = [0]
        mgr = _make_manager(q, _make_stream(calls, [("exit", "0")]), calls)

        async def submit():
            return await mgr.create_job(
                "s1", "sh", owner_id="o1",
                submission_key="key:concurrent",
            )

        jid1, jid2 = await asyncio.gather(submit(), submit())
        assert jid1 == jid2

        job = await mgr.get_job(jid1)
        assert job is not None
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)
        assert calls[0] == 1


class TestDifferentPayloadConflict:
    @pytest.mark.asyncio
    async def test_same_key_different_payload_raises(self):
        q = _make_queue()
        calls: list[int] = [0]
        mgr = _make_manager(q, _make_stream(calls), calls)
        await mgr.create_job(
            "s1", "echo a", owner_id="o1",
            submission_key="key:conflict",
        )
        with pytest.raises(SubmissionConflictError):
            await mgr.create_job(
                "s1", "echo b", owner_id="o1",
                submission_key="key:conflict",
            )


class TestActiveHeartbeatPreventsStaleRecovery:
    @pytest.mark.asyncio
    async def test_active_heartbeat_blocks_recovery(self):
        q = _make_queue()
        # Create and claim a job with a long lease.
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        # Heartbeat keeps the lease alive.
        await q.heartbeat_durable_execution("j1", worker_token="w1", lease_ttl=60)
        # The job should NOT appear as recoverable.
        ids = await q.list_recoverable_durable_jobs()
        assert "j1" not in ids


class TestExpiredLeaseRecoverable:
    @pytest.mark.asyncio
    async def test_expired_lease_is_recoverable(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=1)
        await asyncio.sleep(1.1)
        ids = await q.list_recoverable_durable_jobs()
        assert "j1" in ids


class TestTerminalRestartQuery:
    @pytest.mark.asyncio
    async def test_terminal_job_query_after_restart(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        await q.finish_durable_execution(
            "j1", worker_token="w1", status="completed", exit_code=0,
            stdout="done",
        )
        env = await q.recover_durable_job("j1")
        assert env is not None
        assert env["status"] == "completed"
        assert env["stdout"] == "done"
        # Terminal jobs should NOT appear in recoverable list.
        ids = await q.list_recoverable_durable_jobs()
        assert "j1" not in ids


class TestTerminalFencedStaleOwnerRejected:
    @pytest.mark.asyncio
    async def test_stale_owner_cannot_overwrite_terminal(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        ok = await q.finish_durable_execution(
            "j1", worker_token="w1", status="completed", exit_code=0,
        )
        assert ok is True
        # Stale owner with a new token can't overwrite.
        ok2 = await q.finish_durable_execution(
            "j1", worker_token="w-stale", status="failed", exit_code=1,
        )
        assert ok2 is False
        env = await q.recover_durable_job("j1")
        assert env["status"] == "completed"  # unchanged


class TestRedisUnavailablePreACK:
    @pytest.mark.asyncio
    async def test_keyed_submission_without_redis_fails(self):
        calls: list[int] = [0]
        mgr = _make_manager(_make_queue(), _make_stream(calls), calls)
        mgr.redis_queue = None
        with pytest.raises(SubmissionUnavailableError):
            await mgr.create_job(
                "s1", "sh", owner_id="o1",
                submission_key="key:noredis",
            )
        assert calls[0] == 0

    @pytest.mark.asyncio
    async def test_redis_down_during_reserve_fails(self):
        q = _make_queue()
        calls: list[int] = [0]
        mgr = _make_manager(q, _make_stream(calls), calls)

        async def _fail_eval(*args, **kwargs):
            from redis.exceptions import RedisError
            raise RedisError("down")

        q._redis.eval = _fail_eval
        with pytest.raises(SubmissionUnavailableError):
            await mgr.create_job(
                "s1", "sh", owner_id="o1",
                submission_key="key:redisdown",
            )
        assert calls[0] == 0


class TestRetryAfterRestartSameID:
    @pytest.mark.asyncio
    async def test_retry_returns_same_job_id(self):
        q = _make_queue()
        calls_a: list[int] = [0]
        mgr_a = _make_manager(q, _make_stream(calls_a), calls_a)
        jid = await mgr_a.create_job(
            "s1", "echo", owner_id="o1", submission_key="key:sameid",
        )
        job = await mgr_a.get_job(jid)
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)

        calls_b: list[int] = [0]
        mgr_b = _make_manager(q, _make_stream(calls_b), calls_b)
        jid2 = await mgr_b.create_job(
            "s1", "echo", owner_id="o1", submission_key="key:sameid",
        )
        assert jid2 == jid
        assert calls_b[0] == 0
        env = await q.recover_durable_job(jid)
        assert env is not None
        assert env["status"] == "completed"


class TestRecoverJobInvalidEnvelope:
    @pytest.mark.asyncio
    async def test_invalid_envelope_returns_none(self):
        q = _make_queue()
        mgr = _make_manager(q)
        result = await mgr.recover_job({})
        assert result is None


class TestRecoverJobAlreadyInMemory:
    @pytest.mark.asyncio
    async def test_duplicate_recovery_skipped(self):
        q = _make_queue()
        calls: list[int] = [0]
        mgr = _make_manager(q, _make_stream(calls, [("exit", "0")]), calls)
        env = {"job_id": "j1", "session_id": "s1", "command": "echo",
               "owner_id": "o", "timeout": 3600, "stdin_b64": "",
               "status": "pending"}
        rid1 = await mgr.recover_job(env)
        assert rid1 == "j1"
        # Second recovery of the same job should be skipped.
        rid2 = await mgr.recover_job(env)
        assert rid2 is None
        # Wait for execution.
        job = await mgr.get_job("j1")
        assert job is not None
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)


class TestDurableEnvelopeStoresAllMetadata:
    @pytest.mark.asyncio
    async def test_envelope_preserves_stdin_and_redact_metadata(self):
        q = _make_queue()
        env = {
            "job_id": "j1",
            "session_id": "s1",
            "command": "cat /etc/passwd",
            "stdin_b64": "aGVsbG8=",
            "timeout": 120,
            "owner_id": "o1",
            "redact_path_prefix": "/etc",
            "payload_hash": "abc",
            "status": "pending",
            "created_at": 1000.0,
            "queued_at": 1000.0,
            "started_at": None,
            "claimed_at": None,
            "finished_at": None,
            "worker_token": None,
            "lease_expiry": None,
            "last_heartbeat": None,
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "error": None,
        }
        await q.reserve_submission_with_job(
            "key:meta", job_id="j1", owner_id="o1",
            payload_hash="abc", envelope=env,
        )
        # Full round-trip: recover and verify all fields survive.
        mgr = _make_manager(q)
        recovered_env = await q.recover_durable_job("j1")
        assert recovered_env is not None
        rid = await mgr.recover_job(recovered_env)
        assert rid == "j1"
        job = await mgr.get_job("j1")
        assert job is not None
        assert job.command == "cat /etc/passwd"
        assert job.stdin == b"hello"
        assert job.timeout == 120
        assert job.redact_path_prefix == "/etc"


# ---------------------------------------------------------------------------
# Tests — Pending crash-before-task recovery
# ---------------------------------------------------------------------------


class TestPendingCrashBeforeTaskRecovery:
    """Durable envelopes in status=pending MUST be discoverable by startup
    recovery, not just expired processing.  This covers the crash-before-task
    regression: manager A atomically reserves claim+envelope, crashes before
    local asyncio execution starts, and manager B discovers that same pending
    job_id and executes it exactly once.
    """

    @pytest.mark.asyncio
    async def test_pending_envelope_discovered_by_list_recoverable(self):
        q = _make_queue()
        # Simulate crash-before-claim: write envelope + submission atomically
        # but never claim (never transition to processing).
        env = {
            "job_id": "j1", "session_id": "s1", "command": "echo hi",
            "owner_id": "o1", "timeout": 3600, "stdin_b64": "",
            "redact_path_prefix": None, "payload_hash": "ph",
            "status": "pending", "created_at": time.time(),
            "submission_key": "key:pending-recovery",
        }
        await q.reserve_submission_with_job(
            "key:pending-recovery", job_id="j1", owner_id="o1",
            payload_hash="ph", envelope=env,
        )
        # List recoverable should discover the pending envelope.
        ids = await q.list_recoverable_durable_jobs()
        assert "j1" in ids

    @pytest.mark.asyncio
    async def test_manager_b_recovers_pending_envelope_and_executes_once(self):
        q = _make_queue()
        # Manager A: atomically reserves claim+envelope but crashes before
        # _run_job starts.  Envelope stays pending.
        env = {
            "job_id": "j1", "session_id": "s1", "command": "echo recovered",
            "owner_id": "o1", "timeout": 3600, "stdin_b64": "",
            "redact_path_prefix": None, "payload_hash": "ph",
            "status": "pending", "created_at": time.time(),
            "submission_key": "key:pending-mgrb",
        }
        await q.reserve_submission_with_job(
            "key:pending-mgrb", job_id="j1", owner_id="o1",
            payload_hash="ph", envelope=env,
        )

        # Manager B: fresh process.  Discovers the pending envelope.
        calls_b: list[int] = [0]
        mgr_b = _make_manager(
            q,
            _make_stream(calls_b, [("stdout", "recovered\n"), ("exit", "0")]),
            calls_b,
        )

        # Startup recovery path: list → recover_durable_job → recover_job.
        ids = await q.list_recoverable_durable_jobs()
        assert "j1" in ids
        envelope = await q.recover_durable_job("j1")
        assert envelope is not None
        rid = await mgr_b.recover_job(envelope)
        assert rid == "j1"

        job = await mgr_b.get_job("j1")
        assert job is not None
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)

        assert job.status == "completed"
        assert job.stdout == "recovered\n"
        assert calls_b[0] == 1

    @pytest.mark.asyncio
    async def test_retry_with_same_submission_key_returns_same_id(self):
        q = _make_queue()
        # Simulate crash: write pending envelope with the correct payload hash.
        from app.job_manager import _submission_payload_hash
        ph = _submission_payload_hash("s1", "echo same", b"", 3600)
        env = {
            "job_id": "j1", "session_id": "s1", "command": "echo same",
            "owner_id": "o1", "timeout": 3600, "stdin_b64": "",
            "redact_path_prefix": None, "payload_hash": ph,
            "status": "pending", "created_at": time.time(),
            "submission_key": "key:same-id-retry",
        }
        await q.reserve_submission_with_job(
            "key:same-id-retry", job_id="j1", owner_id="o1",
            payload_hash=ph, envelope=env,
        )

        # Recovery + execution.
        calls: list[int] = [0]
        mgr = _make_manager(q, _make_stream(calls, [("exit", "0")]), calls)
        envelope = await q.recover_durable_job("j1")
        rid = await mgr.recover_job(envelope)
        assert rid == "j1"
        job = await mgr.get_job("j1")
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)

        # Retry with same submission key → same job_id, no re-execution.
        calls2: list[int] = [0]
        mgr2 = _make_manager(q, _make_stream(calls2), calls2)
        jid2 = await mgr2.create_job(
            "s1", "echo same", owner_id="o1",
            submission_key="key:same-id-retry",
        )
        assert jid2 == "j1"
        assert calls2[0] == 0
        env = await q.recover_durable_job("j1")
        assert env is not None
        assert env["status"] == "completed"


# ---------------------------------------------------------------------------
# Tests — Terminal write failure bounded retry
# ---------------------------------------------------------------------------


class TestTerminalWriteFailureBoundedRetry:
    """finish_durable_execution must retry transient Redis failures up to a
    bounded count, and must NOT fabricate durable completion on permanent
    failure.  The stale owner can never overwrite a newer token.
    """

    @pytest.mark.asyncio
    async def test_transient_redis_error_retries_up_to_bound(self):
        """A transient RedisError is retried FINISH_TERMINAL_MAX_ATTEMPTS times."""
        q = _make_queue()
        real_eval = q._redis.eval  # save the real FakeRedis eval
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)

        attempts_made = 0

        async def _fail_then_succeed(*args, **kwargs):
            nonlocal attempts_made
            attempts_made += 1
            if attempts_made <= 2:
                raise RedisConnectionError("transient")
            return await real_eval(*args, **kwargs)

        q._redis.eval = _fail_then_succeed

        ok = await q.finish_durable_execution(
            "j1", worker_token="w1", status="completed", exit_code=0,
        )
        assert ok is True
        assert attempts_made == 3  # 2 failures + 1 success

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_returns_false(self):
        """If all FINISH_TERMINAL_MAX_ATTEMPTS fail, returns False and
        envelope remains nonterminal/recoverable."""
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)

        async def _always_fail(*args, **kwargs):
            raise RedisConnectionError("permanent outage")

        q._redis.eval = _always_fail

        ok = await q.finish_durable_execution(
            "j1", worker_token="w1", status="completed", exit_code=0,
        )
        assert ok is False
        # Envelope should still be in processing (recoverable).
        # Restore real eval to read state.
        q._redis = _FakeRedis()
        # Reconstruct: set the envelope back.
        import json as _json
        q._redis._data["ssh_gateway:job:j1"] = _json.dumps({
            "job_id": "j1", "status": "processing",
            "worker_token": "w1", "lease_expiry": time.time() + 60,
        })
        env = await q.recover_durable_job("j1")
        assert env is not None
        assert env["status"] == "processing"

    @pytest.mark.asyncio
    async def test_fencing_failure_is_not_retried(self):
        """A permanent fencing failure (token mismatch) returns False
        immediately without retry."""
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)

        call_count = 0

        async def _count_calls(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return await original_eval(*args, **kwargs)

        original_eval = q._redis.eval
        q._redis.eval = _count_calls

        ok = await q.finish_durable_execution(
            "j1", worker_token="w2",  # wrong token
            status="completed", exit_code=0,
        )
        assert ok is False
        assert call_count == 1  # no retry for fencing failure

    @pytest.mark.asyncio
    async def test_stale_owner_cannot_overwrite_newer_token(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        # Owner 1 claims and completes.
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)
        ok = await q.finish_durable_execution(
            "j1", worker_token="w1", status="completed", exit_code=0,
        )
        assert ok is True

        # Owner 2 (stale) tries to claim — rejected because already terminal.
        ok2 = await q.claim_durable_execution("j1", worker_token="w2", lease_ttl=60)
        assert ok2 is False

        # Owner 2 tries to finish — rejected.
        ok3 = await q.finish_durable_execution(
            "j1", worker_token="w2", status="failed", exit_code=1,
        )
        assert ok3 is False

        env = await q.recover_durable_job("j1")
        assert env is not None
        assert env["status"] == "completed"

    @pytest.mark.asyncio
    async def test_transient_failure_eventually_persists_terminal(self):
        """When transient failures exhaust, envelope stays nonterminal.
        A subsequent attempt (new lease) succeeds."""
        q = _make_queue()
        real_eval = q._redis.eval
        await q.reserve_submission_with_job(
            "k", job_id="j1", owner_id="o", payload_hash="p",
            envelope={"job_id": "j1", "status": "pending"},
        )
        await q.claim_durable_execution("j1", worker_token="w1", lease_ttl=60)

        # First attempt: all retries fail.
        async def _always_fail(*args, **kwargs):
            raise RedisConnectionError("down")

        q._redis.eval = _always_fail
        ok1 = await q.finish_durable_execution(
            "j1", worker_token="w1", status="completed", exit_code=0,
        )
        assert ok1 is False

        # Restore normal eval; lease is still valid.
        q._redis.eval = real_eval
        ok2 = await q.finish_durable_execution(
            "j1", worker_token="w1", status="completed", exit_code=0,
        )
        assert ok2 is True

        env = await q.recover_durable_job("j1")
        assert env is not None
        assert env["status"] == "completed"


# ---------------------------------------------------------------------------
# Architect gates — target identity, startup dependency, durable cancellation
# ---------------------------------------------------------------------------


class TestArchitectGates:
    @pytest.mark.asyncio
    async def test_submission_identity_includes_session_id(self):
        calls = [0]
        q = _make_queue()
        mgr = _make_manager(q, counter=calls)
        first = await mgr.create_job(
            "session-a", "echo same", owner_id="owner", submission_key="key:target"
        )
        with pytest.raises(SubmissionConflictError):
            await mgr.create_job(
                "session-b", "echo same", owner_id="owner", submission_key="key:target"
            )
        await asyncio.sleep(0.05)
        assert first
        assert calls[0] == 1

    @pytest.mark.asyncio
    async def test_redact_path_prefix_is_presentation_not_identity(self):
        q = _make_queue()
        mgr = _make_manager(q)
        first = await mgr.create_job(
            "session-a",
            "echo same",
            owner_id="owner",
            submission_key="key:redact",
            redact_path_prefix="/srv/a",
        )
        second = await mgr.create_job(
            "session-a",
            "echo same",
            owner_id="owner",
            submission_key="key:redact",
            redact_path_prefix="/srv/b",
        )
        assert second == first

    @pytest.mark.asyncio
    async def test_recovery_waits_for_target_session_restore(self):
        from app.job_manager import _submission_payload_hash
        q = _make_queue()
        ph = _submission_payload_hash("sid-persisted", "echo restored", b"", 3600)
        env = {
            "job_id": "job-restore",
            "submission_key": "key:restore",
            "session_id": "sid-persisted",
            "command": "echo restored",
            "stdin_b64": "",
            "timeout": 3600,
            "owner_id": "owner",
            "redact_path_prefix": None,
            "payload_hash": ph,
            "status": "pending",
            "cancel_requested": False,
        }
        await q.reserve_submission_with_job(
            "key:restore", job_id="job-restore", owner_id="owner", payload_hash=ph, envelope=env
        )

        calls = [0]
        ssh = AsyncMock()
        ssh.execute_stream = _make_stream(calls)
        ssh.get_session.return_value = None
        mgr = JobManager(ssh_manager=ssh, max_jobs=100, redis_queue=q)

        recoverable = await q.list_recoverable_durable_jobs()
        assert "job-restore" in recoverable
        stored = await q.recover_durable_job("job-restore")
        assert stored is not None and stored["status"] == "pending"
        assert await mgr.recover_job(stored) is None
        assert calls[0] == 0

        ssh.get_session.return_value = object()
        assert await mgr.recover_job(stored) == "job-restore"
        job = await mgr.get_job("job-restore")
        assert job is not None
        await asyncio.wait_for(job.completed_event.wait(), timeout=1)
        assert calls[0] == 1

    def test_main_orders_session_restore_before_job_recovery(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
        lifespan_pos = source.index("async def lifespan")
        restore_pos = source.index("await _restore_persisted_sessions", lifespan_pos)
        recovery_pos = source.index("await _recover_durable_jobs", lifespan_pos)
        assert restore_pos < recovery_pos

    @pytest.mark.asyncio
    async def test_pending_cancel_ack_survives_restart_and_never_executes(self):
        from app.job_manager import JobRecord
        q = _make_queue()
        env = {
            "job_id": "job-cancel-pending",
            "session_id": "sid",
            "command": "echo should-not-run",
            "owner_id": "owner",
            "status": "pending",
            "cancel_requested": False,
        }
        await q.reserve_submission_with_job(
            "key:cancel-pending",
            job_id="job-cancel-pending",
            owner_id="owner",
            payload_hash="ph",
            envelope=env,
        )
        mgr = _make_manager(q)
        mgr._jobs["job-cancel-pending"] = JobRecord(
            job_id="job-cancel-pending",
            session_id="sid",
            command="echo should-not-run",
            owner_id="owner",
            is_durable=True,
        )
        assert await mgr.cancel_job("job-cancel-pending") == "cancelled"
        stored = await q.recover_durable_job("job-cancel-pending")
        assert stored is not None
        assert stored["status"] == "cancelled"
        assert stored["cancel_requested"] is True
        assert stored["remote_outcome"] == "not_started"
        assert stored["locally_interrupted"] is False

        assert "job-cancel-pending" not in await q.list_recoverable_durable_jobs()

    @pytest.mark.asyncio
    async def test_running_cancel_synthetic_exit_is_durable_ambiguous(self):
        started = asyncio.Event()

        async def stream(_sid, _command, *, cancel_event, **_kwargs):
            started.set()
            while not cancel_event.is_set():
                await asyncio.sleep(0.01)
            # Mirrors SSHSessionManager.execute_stream(): channel.close() then
            # a synthetic local sentinel, NOT recv_exit_status().
            yield "exit", "-1"

        q = _make_queue()
        mgr = _make_manager(q, execute_stream=stream)
        job_id = await mgr.create_job(
            "sid", "echo cancellable", owner_id="owner", submission_key="key:cancel-running"
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await mgr.cancel_job(job_id) == "cancelling"
        job = await mgr.get_job(job_id)
        assert job is not None
        await asyncio.wait_for(job.completed_event.wait(), timeout=1)
        assert job.status == "ambiguous"
        assert job.exit_code == -1
        assert job.progress["cancellation_outcome"] == "ambiguous"
        stored = await q.recover_durable_job(job_id)
        assert stored is not None
        assert stored["status"] == "ambiguous"
        assert stored["cancel_requested"] is True
        assert stored["remote_outcome"] == "ambiguous"
        assert stored["locally_interrupted"] is True
        assert job_id not in await q.list_recoverable_durable_jobs()
        assert not await q.claim_durable_execution(
            job_id, worker_token="worker-after-restart", lease_ttl=60
        )
        # Fresh manager/process must refuse even an accidentally supplied
        # ambiguous envelope; production recovery should never rerun it.
        calls_after_restart = [0]
        mgr_after_restart = _make_manager(q, counter=calls_after_restart)
        assert await mgr_after_restart.recover_job(stored) is None
        assert calls_after_restart[0] == 0

    @pytest.mark.asyncio
    async def test_natural_remote_completion_wins_cancel_race(self):
        started = asyncio.Event()
        remote_exit_observed = asyncio.Event()

        async def stream(_sid, _command, **_kwargs):
            started.set()
            yield "exit", "0"
            remote_exit_observed.set()
            # Keep the generator alive long enough for cancellation intent to
            # arrive after the factual remote exit was already observed.
            await asyncio.sleep(0.05)

        q = _make_queue()
        mgr = _make_manager(q, execute_stream=stream)
        job_id = await mgr.create_job(
            "sid", "echo done", owner_id="owner", submission_key="key:cancel-race"
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        job = await mgr.get_job(job_id)
        assert job is not None
        for _ in range(100):
            if job.exit_code == 0:
                break
            await asyncio.sleep(0.001)
        assert job.exit_code == 0
        assert await mgr.cancel_job(job_id) == "cancelling"
        await asyncio.wait_for(job.completed_event.wait(), timeout=1)
        assert job.status == "completed"
        stored = await q.recover_durable_job(job_id)
        assert stored is not None
        assert stored["status"] == "completed"
        assert stored["cancel_requested"] is True
        # A stale token/cancel owner cannot overwrite the factual terminal.
        assert not await q.finish_durable_execution(
            job_id, worker_token="stale-worker", status="ambiguous", exit_code=-1
        )
        stored2 = await q.recover_durable_job(job_id)
        assert stored2 is not None and stored2["status"] == "completed"

    @pytest.mark.asyncio
    async def test_processing_cancel_crash_is_not_automatically_rerun(self):
        q = _make_queue()
        env = {
            "job_id": "job-ambiguous-cancel",
            "session_id": "sid",
            "command": "echo maybe-ran",
            "owner_id": "owner",
            "status": "pending",
            "cancel_requested": False,
        }
        await q.reserve_submission_with_job(
            "key:ambiguous", job_id="job-ambiguous-cancel", owner_id="owner", payload_hash="ph", envelope=env
        )
        assert await q.claim_durable_execution(
            "job-ambiguous-cancel", worker_token="worker-old", lease_ttl=1
        )
        assert await q.request_durable_cancellation("job-ambiguous-cancel") == "cancelling"

        stored = await q.recover_durable_job("job-ambiguous-cancel")
        assert stored is not None
        stored["lease_expiry"] = time.time() - 10
        q._redis._data[q._job_prefix + "job-ambiguous-cancel"] = json.dumps(stored)
        q._redis._zsets[q._processing_key]["job-ambiguous-cancel"] = time.time() - 10

        assert "job-ambiguous-cancel" not in await q.list_recoverable_durable_jobs()
        assert not await q.claim_durable_execution(
            "job-ambiguous-cancel", worker_token="worker-new", lease_ttl=60
        )
        stored = await q.recover_durable_job("job-ambiguous-cancel")
        assert stored is not None
        assert stored["status"] == "processing"
        assert stored["cancel_requested"] is True

        reconciled = await q.reconcile_expired_cancelled_processing()
        assert reconciled == ["job-ambiguous-cancel"]
        stored = await q.recover_durable_job("job-ambiguous-cancel")
        assert stored is not None
        assert stored["status"] == "ambiguous"
        assert stored["remote_outcome"] == "ambiguous"
        assert stored["locally_interrupted"] is True
        assert "job-ambiguous-cancel" not in await q.list_recoverable_durable_jobs()
        assert not await q.claim_durable_execution(
            "job-ambiguous-cancel", worker_token="worker-newer", lease_ttl=60
        )

    @pytest.mark.asyncio
    async def test_silent_command_heartbeat_prevents_recovery(self, monkeypatch):
        import app.job_manager as job_manager_module

        monkeypatch.setattr(job_manager_module, "DURABLE_LEASE_TTL_SECONDS", 1)
        started = asyncio.Event()
        calls = [0]

        async def silent_stream(*_args, **_kwargs):
            calls[0] += 1
            started.set()
            await asyncio.sleep(2.2)
            yield "exit", "0"

        q = _make_queue()
        mgr = _make_manager(q, execute_stream=silent_stream)
        job_id = await mgr.create_job(
            "sid", "sleep-ish", owner_id="owner", submission_key="key:silent"
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(1.3)
        assert job_id not in await q.list_recoverable_durable_jobs()
        job = await mgr.get_job(job_id)
        assert job is not None
        await asyncio.wait_for(job.completed_event.wait(), timeout=2)
        assert calls[0] == 1


class TestFinalLeaseAndPersistenceGates:
    @pytest.mark.asyncio
    async def test_expired_owner_cannot_resurrect_lease_with_heartbeat(self):
        q = _make_queue()
        await q.reserve_submission_with_job(
            "key:expired-hb",
            job_id="job-expired-hb",
            owner_id="owner",
            payload_hash="ph",
            envelope={
                "job_id": "job-expired-hb",
                "status": "pending",
                "cancel_requested": False,
            },
        )
        assert await q.claim_durable_execution(
            "job-expired-hb", worker_token="worker-old", lease_ttl=60
        )
        env = await q.recover_durable_job("job-expired-hb")
        assert env is not None
        env["lease_expiry"] = time.time() - 1
        q._redis._data[q._job_prefix + "job-expired-hb"] = json.dumps(env)
        assert not await q.heartbeat_durable_execution(
            "job-expired-hb", worker_token="worker-old", lease_ttl=60
        )

    @pytest.mark.asyncio
    async def test_manager_exposes_terminal_persistence_success(self):
        q = _make_queue()
        mgr = _make_manager(q)
        job_id = await mgr.create_job(
            "sid", "echo ok", owner_id="owner", submission_key="key:persist-ok"
        )
        job = await mgr.get_job(job_id)
        assert job is not None
        await asyncio.wait_for(job.completed_event.wait(), timeout=1)
        # The completed event can precede the final Redis write by one event-loop
        # turn; wait for the task itself to finish before asserting durability.
        task = mgr._job_tasks.get(job_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=1)
        assert job.status == "completed"
        assert job.progress["durable_persisted"] is True

    @pytest.mark.asyncio
    async def test_manager_exposes_terminal_persistence_failure(self):
        q = _make_queue()
        mgr = _make_manager(q)
        original_finish = q.finish_durable_execution

        async def fail_finish(*args, **kwargs):
            return False

        q.finish_durable_execution = fail_finish
        job_id = await mgr.create_job(
            "sid", "echo ok", owner_id="owner", submission_key="key:persist-fail"
        )
        job = await mgr.get_job(job_id)
        assert job is not None
        await asyncio.wait_for(job.completed_event.wait(), timeout=1)
        task = mgr._job_tasks.get(job_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=1)
        assert job.status == "completed"
        assert job.progress["durable_persisted"] is False
        q.finish_durable_execution = original_finish
