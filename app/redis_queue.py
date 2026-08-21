"""Redis-backed job queue for distributed processing."""

import asyncio
import hashlib
import json
import logging
import time
import uuid

import redis.asyncio as redis
from redis.exceptions import RedisError

from .exceptions import SubmissionConflictError, SubmissionUnavailableError
from .metrics import metrics
from .redis_compat import close_redis_client

logger = logging.getLogger(__name__)

FINISH_TERMINAL_MAX_ATTEMPTS = 3
FINISH_RETRY_DELAY_SECONDS = 0.1


def _decode_id(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    return str(value)


class RedisJobQueue:
    """Distributed job queue using Redis.

    Features:
    - Persistent jobs (survive gateway restarts)
    - Retry logic with exponential backoff
    - Priority queues
    - Job status tracking
    - Dead letter queue for failed jobs
    """

    def __init__(self, redis_url: str = "redis://redis:6379/0"):
        self._redis_url = redis_url
        self._redis: redis.Redis | None = None
        self._queue_key = "ssh_gateway:jobs:queue"
        self._processing_key = "ssh_gateway:jobs:processing"
        self._completed_key = "ssh_gateway:jobs:completed"
        self._dead_letter_key = "ssh_gateway:jobs:dead"
        self._job_prefix = "ssh_gateway:job:"
        self._lease_prefix = "ssh_gateway:lease:"
        self._submission_prefix = "ssh_gateway:submission:"
        self._submission_ttl_seconds = 7 * 86400

    async def _update_queue_depth_metrics(self):
        """Update Prometheus queue depth gauge from current Redis state."""
        if not self._redis:
            return
        try:
            pending = await self._redis.zcard(self._queue_key)
            processing = await self._redis.zcard(self._processing_key)
            dead = await self._redis.zcard(self._dead_letter_key)
            metrics.update_queue_depth(pending=pending, processing=processing, dead=dead)
        except Exception:
            pass  # metrics update is best-effort

    async def connect(self):
        """Connect to Redis."""
        try:
            self._redis = await redis.from_url(self._redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info("Connected to Redis")
        except Exception as exc:
            logger.error("Failed to connect to Redis: %s", exc)
            raise

    async def disconnect(self):
        """Disconnect from Redis."""
        if self._redis:
            await close_redis_client(self._redis)
            logger.info("Disconnected From Redis")

    def _submission_storage_key(self, submission_key: str) -> str:
        digest = hashlib.sha256(submission_key.encode("utf-8")).hexdigest()
        return f"{self._submission_prefix}{digest}"

    async def find_submission(
        self,
        submission_key: str,
        *,
        owner_id: str,
        payload_hash: str,
    ) -> str | None:
        """Resolve an existing durable async submission without creating one."""
        if not self._redis:
            raise SubmissionUnavailableError("Durable submission requires Redis")
        try:
            raw = await self._redis.get(self._submission_storage_key(submission_key))
        except RedisError as exc:
            raise SubmissionUnavailableError(
                "Durable submission backend is unavailable"
            ) from exc
        if raw is None:
            return None
        try:
            claim = json.loads(raw)
            job_id = str(claim["job_id"])
            stored_owner = str(claim["owner_id"])
            stored_payload = str(claim["payload_hash"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise SubmissionUnavailableError("Durable submission record is invalid") from exc
        if stored_owner != owner_id or stored_payload != payload_hash:
            raise SubmissionConflictError(
                "submission_key is already bound to a different request"
            )
        return job_id

    async def claim_submission(
        self,
        submission_key: str,
        *,
        job_id: str,
        owner_id: str,
        payload_hash: str,
    ) -> tuple[str, bool]:
        """Atomically reserve a stable async submission identity in Redis."""
        if not self._redis:
            raise SubmissionUnavailableError("Durable submission requires Redis")
        key = self._submission_storage_key(submission_key)
        claim = json.dumps(
            {
                "version": 1,
                "job_id": job_id,
                "owner_id": owner_id,
                "payload_hash": payload_hash,
                "created_at": time.time(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        for _ in range(3):
            try:
                created = await self._redis.set(
                    key, claim, nx=True, ex=self._submission_ttl_seconds
                )
            except RedisError as exc:
                raise SubmissionUnavailableError(
                    "Durable submission backend is unavailable"
                ) from exc
            if created:
                return job_id, True
            existing = await self.find_submission(
                submission_key, owner_id=owner_id, payload_hash=payload_hash
            )
            if existing is not None:
                return existing, False
        raise SubmissionUnavailableError("Unable to reserve durable submission")

    async def reserve_submission_with_job(
        self,
        submission_key: str,
        *,
        job_id: str,
        owner_id: str,
        payload_hash: str,
        envelope: dict,
    ) -> tuple[str, bool]:
        """Atomically reserve a submission key AND store the full durable
        executable envelope.  The submission identity and the full
        executable envelope are committed together so that any gateway
        worker (including a post-restart recovery scan) can reconstruct
        the job from the envelope alone.

        Returns (job_id, created=True) on first reserve, or
        (existing_job_id, created=False) if the key was already bound to
        the same owner+payload.  Raises SubmissionConflictError if the
        key is bound to a different owner or payload, and
        SubmissionUnavailableError on Redis transport failure.

        Never deletes or replaces an existing claim.
        """
        if not self._redis:
            raise SubmissionUnavailableError("Durable submission requires Redis")

        key = self._submission_storage_key(submission_key)
        claim = json.dumps(
            {
                "version": 1,
                "job_id": job_id,
                "owner_id": owner_id,
                "payload_hash": payload_hash,
                "created_at": time.time(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

        envelope_json = json.dumps(envelope, default=str, separators=(",", ":"))
        _RESERVE_ENVELOPE_LUA = """
        local sub_key = KEYS[1]
        local env_key = KEYS[2]
        local sub_claim = ARGV[1]
        local env_json  = ARGV[2]
        local owner     = ARGV[3]
        local payload   = ARGV[4]
        local ttl       = tonumber(ARGV[5])

        -- Try to create both atomically (NX only).
        local sub_created = redis.call('SET', sub_key, sub_claim, 'NX', 'EX', ttl)
        local env_created = redis.call('SET', env_key, env_json, 'NX', 'EX', ttl)
        if sub_created and env_created then
            return {1, 0}              -- both new
        end
        if sub_created and not env_created then
            -- Inconsistent — undo sub claim and fail.
            redis.call('DEL', sub_key)
            return {0, 0}
        end

        -- sub_key already existed — read it.
        local existing = redis.call('GET', sub_key)
        if not existing then
            return {0, 0}
        end
        local sub_claim_tbl = cjson.decode(existing)
        if sub_claim_tbl['owner_id'] ~= owner or sub_claim_tbl['payload_hash'] ~= payload then
            return {0, -1}            -- conflict
        end
        return {0, tonumber(sub_claim_tbl['job_id']) == 0 and 0 or 0, sub_claim_tbl['job_id']}
        """
        try:
            for _ in range(3):
                result = await self._redis.eval(
                    _RESERVE_ENVELOPE_LUA,
                    2,
                    key,
                    self._job_prefix + job_id,
                    claim,
                    envelope_json,
                    owner_id,
                    payload_hash,
                    str(self._submission_ttl_seconds),
                )
                if isinstance(result, (list, tuple)):
                    created_flag = int(result[0])
                    if created_flag == 1:
                        return job_id, True
                    if len(result) >= 3 and result[2]:
                        returned_id = _decode_id(result[2])
                        return returned_id, False
                else:
                    # Older fakeredis returns single value.
                    if result == 1 or result == b"1":
                        return job_id, True
                    # Try find_submission path for compat.
                    existing = await self.find_submission(
                        submission_key, owner_id=owner_id, payload_hash=payload_hash
                    )
                    if existing is not None:
                        return existing, False
        except RedisError as exc:
            raise SubmissionUnavailableError(
                "Durable submission backend is unavailable"
            ) from exc

        raise SubmissionUnavailableError("Unable to reserve durable submission")

    async def claim_durable_execution(
        self,
        job_id: str,
        *,
        worker_token: str,
        lease_ttl: int = 60,
    ) -> bool:
        """Atomically claim a durable job for execution by *worker_token*.

        Transitions status ``pending`` -> ``processing`` with lease.  If the
        job is already ``processing`` with the *same* worker_token, the
        claim is idempotent and renews the lease (recovery after a crash
        that didn't clear the old token).  Returns True on success, False
        if the job is in a terminal state or claimed by a different token.
        """
        if not self._redis:
            raise SubmissionUnavailableError("Durable execution claim requires Redis")

        _CLAIM_LUA = """
        local env_key = KEYS[1]
        local proc_key = KEYS[2]
        local lease_key = KEYS[3]
        local token = ARGV[1]
        local lease_ttl = tonumber(ARGV[2])
        local now = tonumber(ARGV[3])
        local env_json = redis.call('GET', env_key)
        if not env_json then
            return 0
        end
        local env = cjson.decode(env_json)
        local st = env['status']
        if st == 'completed' or st == 'failed' or st == 'cancelled' or st == 'ambiguous' then
            return 0
        end
        -- A crashed worker with acknowledged cancellation intent must never
        -- be automatically re-executed: its remote outcome is ambiguous.
        if st == 'processing' and env['cancel_requested'] then
            return 0
        end
        if st == 'processing' and env['worker_token'] == token then
            -- Same worker reclaiming after crash/restart.
            env['lease_expiry'] = now + lease_ttl
            env['last_heartbeat'] = now
            redis.call('SET', env_key, cjson.encode(env), 'EX', 86400)
            redis.call('SET', lease_key, token, 'EX', lease_ttl)
            redis.call('ZADD', proc_key, now, env['job_id'])
            return 1
        end
        if st == 'processing' then
            -- Different worker — only allow if lease expired.
            if env['lease_expiry'] and now <= env['lease_expiry'] then
                return 0
            end
        end
        env['status'] = 'processing'
        env['worker_token'] = token
        env['lease_expiry'] = now + lease_ttl
        env['last_heartbeat'] = now
        env['claimed_at'] = now
        redis.call('SET', env_key, cjson.encode(env), 'EX', 86400)
        redis.call('SET', lease_key, token, 'EX', lease_ttl)
        redis.call('ZADD', proc_key, now, env['job_id'])
        return 1
        """
        env_key = f"{self._job_prefix}{job_id}"
        proc_key = self._processing_key
        lease_key = f"{self._lease_prefix}{job_id}"
        now = time.time()
        try:
            result = await self._redis.eval(
                _CLAIM_LUA,
                3,
                env_key,
                proc_key,
                lease_key,
                worker_token,
                str(lease_ttl),
                str(now),
            )
            if isinstance(result, (list, tuple)):
                return int(result[0]) == 1
            return result == 1 or result == b"1"
        except RedisError as exc:
            raise SubmissionUnavailableError(
                "Durable execution claim backend is unavailable"
            ) from exc

    async def heartbeat_durable_execution(
        self,
        job_id: str,
        *,
        worker_token: str,
        lease_ttl: int = 60,
    ) -> bool:
        """Renew a durable processing lease, but only if *worker_token*
        matches the token that claimed the execution.  Returns True on
        success, False if the token doesn't match or the job isn't in
        processing state.
        """
        if not self._redis:
            return False

        _HB_LUA = """
        local env_key = KEYS[1]
        local proc_key = KEYS[2]
        local lease_key = KEYS[3]
        local token = ARGV[1]
        local lease_ttl = tonumber(ARGV[2])
        local now = tonumber(ARGV[3])
        local env_json = redis.call('GET', env_key)
        if not env_json then
            return 0
        end
        local env = cjson.decode(env_json)
        if env['status'] ~= 'processing' or env['worker_token'] ~= token then
            return 0
        end
        -- An expired owner cannot resurrect its lease. A recovery claimant may
        -- already have become eligible at this boundary.
        if not env['lease_expiry'] or now > env['lease_expiry'] then
            return 0
        end
        env['lease_expiry'] = now + lease_ttl
        env['last_heartbeat'] = now
        redis.call('SET', env_key, cjson.encode(env), 'EX', 86400)
        redis.call('SET', lease_key, token, 'EX', lease_ttl)
        redis.call('ZADD', proc_key, now, env['job_id'])
        return 1
        """
        env_key = f"{self._job_prefix}{job_id}"
        proc_key = self._processing_key
        lease_key = f"{self._lease_prefix}{job_id}"
        now = time.time()
        try:
            result = await self._redis.eval(
                _HB_LUA,
                3,
                env_key,
                proc_key,
                lease_key,
                worker_token,
                str(lease_ttl),
                str(now),
            )
            if isinstance(result, (list, tuple)):
                return int(result[0]) == 1
            return result == 1 or result == b"1"
        except RedisError:
            return False

    async def request_durable_cancellation(self, job_id: str) -> str | None:
        """Persist cancellation intent without violating execution fencing.

        ``pending`` can safely become terminal ``cancelled`` immediately. For
        ``processing`` we persist only ``cancel_requested`` because the remote
        outcome is not yet known. The token-owning worker may later publish a
        factual remote completion, or ``ambiguous`` after local interruption;
        channel closure alone is never promoted to durable ``cancelled``.
        """
        if not self._redis:
            raise SubmissionUnavailableError("Durable cancellation requires Redis")

        _CANCEL_LUA = """
        local env_key = KEYS[1]
        local proc_key = KEYS[2]
        local lease_key = KEYS[3]
        local dead_key = KEYS[4]
        local now = tonumber(ARGV[1])
        local raw = redis.call('GET', env_key)
        if not raw then return '' end
        local env = cjson.decode(raw)
        local st = env['status']
        if st == 'completed' or st == 'failed' or st == 'cancelled' or st == 'ambiguous' then
            return st
        end
        env['cancel_requested'] = true
        if st == 'pending' then
            env['status'] = 'cancelled'
            env['finished_at'] = now
            env['exit_code'] = -1
            env['remote_outcome'] = 'not_started'
            env['locally_interrupted'] = false
            redis.call('SET', env_key, cjson.encode(env), 'EX', 604800)
            redis.call('ZREM', proc_key, env['job_id'])
            redis.call('DEL', lease_key)
            redis.call('ZADD', dead_key, now, env['job_id'])
            return 'cancelled'
        end
        if st == 'processing' then
            redis.call('SET', env_key, cjson.encode(env), 'EX', 86400)
            return 'cancelling'
        end
        return ''
        """
        try:
            result = await self._redis.eval(
                _CANCEL_LUA,
                4,
                f"{self._job_prefix}{job_id}",
                self._processing_key,
                f"{self._lease_prefix}{job_id}",
                self._dead_letter_key,
                str(time.time()),
            )
        except RedisError as exc:
            raise SubmissionUnavailableError(
                "Durable cancellation backend is unavailable"
            ) from exc
        status = _decode_id(result) if result is not None else ""
        return status or None

    async def is_durable_cancellation_requested(
        self, job_id: str, *, worker_token: str
    ) -> bool:
        """Return cancellation intent only to the current token owner."""
        if not self._redis:
            return False
        _CHECK_CANCEL_LUA = """
        local raw = redis.call('GET', KEYS[1])
        if not raw then return 0 end
        local env = cjson.decode(raw)
        if env['status'] ~= 'processing' or env['worker_token'] ~= ARGV[1] then
            return 0
        end
        if env['cancel_requested'] then return 1 end
        return 0
        """
        try:
            result = await self._redis.eval(
                _CHECK_CANCEL_LUA, 1, f"{self._job_prefix}{job_id}", worker_token
            )
        except RedisError:
            return False
        if isinstance(result, (list, tuple)):
            return bool(result and int(result[0]) == 1)
        return result == 1 or result == b"1"

    async def finish_durable_execution(
        self,
        job_id: str,
        *,
        worker_token: str,
        status: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        error: str | None = None,
    ) -> bool:
        """Fenced terminal transition with bounded retry.

        Only succeeds if *worker_token* matches the token that claimed the
        execution AND the job is still in ``processing`` state.

        Transient ``RedisError`` failures are retried up to
        ``FINISH_TERMINAL_MAX_ATTEMPTS`` times with a small delay.  Permanent
        fencing failures (token mismatch, already terminal, envelope missing)
        are rejected immediately — we never fabricate durable completion.

        Returns:
            True on success.  False if ownership was lost, the job is already
            terminal, the envelope is gone, or all retry attempts were
            exhausted.
        """
        if status not in {"completed", "failed", "cancelled", "ambiguous"}:
            raise ValueError(f"Unsupported durable terminal status: {status}")
        if not self._redis:
            return False

        _FINISH_LUA = """
        local env_key = KEYS[1]
        local proc_key = KEYS[2]
        local lease_key = KEYS[3]
        local comp_key = KEYS[4]
        local dead_key = KEYS[5]
        local token = ARGV[1]
        local new_status = ARGV[2]
        local stdout = ARGV[3]
        local stderr = ARGV[4]
        local exit_code = ARGV[5]
        local error_msg = ARGV[6]
        local now = tonumber(ARGV[7])
        local env_json = redis.call('GET', env_key)
        if not env_json then
            return 0
        end
        local env = cjson.decode(env_json)
        if env['status'] ~= 'processing' or env['worker_token'] ~= token then
            return 0
        end
        env['status'] = new_status
        env['finished_at'] = now
        env['stdout'] = stdout
        env['stderr'] = stderr
        if new_status == 'ambiguous' then
            env['remote_outcome'] = 'ambiguous'
            env['locally_interrupted'] = true
        end
        if exit_code ~= '' then
            env['exit_code'] = tonumber(exit_code)
        end
        if error_msg ~= '' then
            env['error'] = error_msg
        end
        redis.call('SET', env_key, cjson.encode(env), 'EX', 604800)
        redis.call('ZREM', proc_key, env['job_id'])
        redis.call('DEL', lease_key)
        if new_status == 'completed' then
            redis.call('ZADD', comp_key, now, env['job_id'])
        else
            redis.call('ZADD', dead_key, now, env['job_id'])
        end
        return 1
        """
        env_key = f"{self._job_prefix}{job_id}"
        proc_key = self._processing_key
        lease_key = f"{self._lease_prefix}{job_id}"
        comp_key = self._completed_key
        dead_key = self._dead_letter_key
        now = time.time()

        for attempt in range(FINISH_TERMINAL_MAX_ATTEMPTS):
            try:
                result = await self._redis.eval(
                    _FINISH_LUA,
                    5,
                    env_key,
                    proc_key,
                    lease_key,
                    comp_key,
                    dead_key,
                    worker_token,
                    status,
                    stdout,
                    stderr,
                    str(exit_code) if exit_code is not None else "",
                    error or "",
                    str(now),
                )
                if isinstance(result, (list, tuple)):
                    ok = int(result[0]) == 1
                else:
                    ok = result == 1 or result == b"1"
                if ok:
                    return True
                # Lua returned 0: fencing failure (token mismatch, already
                # terminal, or envelope missing).  Do NOT retry — these are
                # permanent.
                return False
            except RedisError:
                if attempt < FINISH_TERMINAL_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(FINISH_RETRY_DELAY_SECONDS)
                    continue
                # All attempts exhausted — envelope remains nonterminal/recoverable.
                return False
        return False

    async def reconcile_expired_cancelled_processing(self) -> list[str]:
        """Classify expired ``processing + cancel_requested`` jobs ambiguous.

        This is restart reconciliation, not execution recovery. Once a worker
        crossed the remote execution boundary and acknowledged cancellation,
        expiry of its lease proves only that coordinator ownership was lost.
        It does not prove remote cancellation, so the durable job becomes
        ``ambiguous`` and is removed from processing/recovery indexes without
        executing again.
        """
        if not self._redis:
            return []

        _RECONCILE_CANCEL_LUA = """
        local proc_key = KEYS[1]
        local dead_key = KEYS[2]
        local env_prefix = ARGV[1]
        local lease_prefix = ARGV[2]
        local now = tonumber(ARGV[3])
        local candidates = redis.call('ZRANGEBYSCORE', proc_key, 0, now)
        local result = {}
        for _, jid in ipairs(candidates) do
            local env_key = env_prefix .. jid
            local raw = redis.call('GET', env_key)
            if raw then
                local env = cjson.decode(raw)
                if env['status'] == 'processing'
                   and env['cancel_requested']
                   and env['lease_expiry']
                   and now > env['lease_expiry'] then
                    env['status'] = 'ambiguous'
                    env['finished_at'] = now
                    env['remote_outcome'] = 'ambiguous'
                    env['locally_interrupted'] = true
                    if env['exit_code'] == nil then env['exit_code'] = -1 end
                    redis.call('SET', env_key, cjson.encode(env), 'EX', 604800)
                    redis.call('ZREM', proc_key, jid)
                    redis.call('DEL', lease_prefix .. jid)
                    redis.call('ZADD', dead_key, now, jid)
                    table.insert(result, jid)
                end
            end
        end
        return result
        """
        try:
            result = await self._redis.eval(
                _RECONCILE_CANCEL_LUA,
                2,
                self._processing_key,
                self._dead_letter_key,
                self._job_prefix,
                self._lease_prefix,
                str(time.time()),
            )
        except RedisError:
            return []
        if isinstance(result, (list, tuple)):
            return [_decode_id(jid) for jid in result]
        return []

    async def list_recoverable_durable_jobs(self) -> list[str]:
        """Return job_ids whose durable envelope is eligible for recovery:
        1. ``pending`` status — crash-before-claim; envelope exists but
           execution never started.
        2. ``processing`` status with an expired lease — worker died and
           heartbeat stopped.

        Both categories are safe for a fresh worker to pick up.
        """
        if not self._redis:
            return []

        _SCAN_PENDING_LUA = """
        local proc_key = KEYS[1]
        local prefix = ARGV[1]
        local now = tonumber(ARGV[2])
        -- Processing candidates: expired lease.
        local candidates = redis.call('ZRANGEBYSCORE', proc_key, 0, now)
        local result = {}
        for _, jid in ipairs(candidates) do
            local env_key = prefix .. jid
            local raw = redis.call('GET', env_key)
            if raw then
                local env = cjson.decode(raw)
                if env['status'] == 'processing'
                   and not env['cancel_requested']
                   and env['lease_expiry']
                   and now > env['lease_expiry'] then
                    table.insert(result, jid)
                end
            end
        end
        return result
        """
        now = time.time()
        recovered: list[str] = []

        # 1) Expired processing jobs.
        try:
            result = await self._redis.eval(
                _SCAN_PENDING_LUA,
                1,
                self._processing_key,
                self._job_prefix,
                str(now),
            )
            if isinstance(result, (list, tuple)):
                recovered.extend(_decode_id(jid) for jid in result)
        except RedisError:
            return []

        # 2) Pending envelopes that were never claimed.
        #    These live under the job_prefix but are NOT in any zset
        #    (never moved to processing/completed/dead).  We discover
        #    them via the submission claim key set + envelope key scan.
        #    A simpler approach: scan submission keys for pending envelopes.
        _SCAN_PENDING_ENV_LUA = """
        local sub_prefix = ARGV[1]
        local env_prefix = ARGV[2]
        local limit = tonumber(ARGV[3])
        local result = {}
        local cursor = '0'
        repeat
            local reply = redis.call('SCAN', cursor, 'MATCH', sub_prefix .. '*', 'COUNT', limit)
            cursor = tostring(reply[1])
            for _, sub_key in ipairs(reply[2]) do
                local raw = redis.call('GET', sub_key)
                if raw then
                    local claim = cjson.decode(raw)
                    local jid = tostring(claim['job_id'])
                    local env_raw = redis.call('GET', env_prefix .. jid)
                    if env_raw then
                        local env = cjson.decode(env_raw)
                        if env['status'] == 'pending' then
                            table.insert(result, jid)
                        end
                    end
                end
            end
        until cursor == '0'
        return result
        """
        try:
            result = await self._redis.eval(
                _SCAN_PENDING_ENV_LUA,
                0,
                self._submission_prefix,
                self._job_prefix,
                "100",
            )
            if isinstance(result, (list, tuple)):
                for jid in result:
                    decoded = _decode_id(jid)
                    if decoded not in recovered:
                        recovered.append(decoded)
        except RedisError:
            pass

        return recovered

    async def recover_durable_job(self, job_id: str) -> dict | None:
        """Read a durable envelope for *job_id*.  Returns the envelope dict
        if present, or None.  Does not mutate state — the caller (usually
        JobManager) is responsible for claiming execution.
        """
        if not self._redis:
            return None
        try:
            raw = await self._redis.get(f"{self._job_prefix}{job_id}")
        except RedisError:
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    async def save_terminal_job(
        self,
        job_id: str,
        *,
        session_id: str,
        command: str,
        owner_id: str,
        status: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        error: str | None = None,
        redact_path_prefix: str | None = None,
    ) -> None:
        """Persist a finished JobManager job so it survives a gateway restart.

        JobManager runs jobs immediately in-process (asyncio task per job) —
        it does not pull from this queue's pending/processing zsets, so
        enqueue()/dequeue() are the wrong fit for mirroring its lifecycle.
        This writes a snapshot directly under the same storage the rest of
        this class reads (_get_job, get_dead_letter_jobs, get_queue_stats),
        and records failed jobs in the dead-letter zset — the closest
        faithful mapping given JobManager has no retry concept of its own.
        No-op if Redis isn't connected; failures here must never affect the
        in-process job outcome, so callers should treat this as best-effort.
        """
        if not self._redis:
            return
        job_data = {
            "id": job_id,
            "session_id": session_id,
            "command": command,
            "status": status,
            "owner_id": owner_id,
            "completed_at": time.time(),
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "error": error,
            "redact_path_prefix": redact_path_prefix,
        }
        is_failure = status not in ("completed",)
        ttl = 86400 * 7 if is_failure else 86400
        await self._redis.set(
            f"{self._job_prefix}{job_id}",
            json.dumps(job_data, default=str),
            ex=ttl,
        )
        if is_failure:
            await self._redis.zadd(self._dead_letter_key, {job_id: time.time()})
        else:
            await self._redis.zadd(self._completed_key, {job_id: time.time()})
        await self._update_queue_depth_metrics()

    async def enqueue(
        self,
        session_id: str,
        command: str,
        priority: int = 0,
        max_retries: int = 3,
        timeout: int = 3600,
        owner_id: str = "",
    ) -> str:
        """Add job to queue.

        ``owner_id`` (identity fingerprint) travels with the job through
        retries into the dead letter queue, mirroring JobManager/JobRecord's
        owner_id — see job_visible_to() / T80.2. This pending/processing
        pull model (enqueue/dequeue/heartbeat/retry_job/recover_orphans) is
        for a genuine distributed worker pool and has no caller in this
        codebase yet — JobManager runs jobs immediately in-process rather
        than pulling from a queue, so it uses save_terminal_job() instead to
        mirror its own push-based lifecycle. Any future distributed-worker
        caller gets ownership tracking for free instead of it being bolted
        on after the fact.

        Returns:
            Job ID
        """
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job_data = {
            "id": job_id,
            "session_id": session_id,
            "command": command,
            "status": "pending",
            "priority": priority,
            "max_retries": max_retries,
            "retry_count": 0,
            "timeout": timeout,
            "owner_id": owner_id,
            "created_at": time.time(),
            "started_at": None,
            "completed_at": None,
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "error": None,
        }

        # Store Job Data + Add To Priority Queue Atomically
        async with self._redis.pipeline() as pipe:
            await pipe.set(
                f"{self._job_prefix}{job_id}",
                json.dumps(job_data, default=str),
                ex=86400,
            )
            await pipe.zadd(self._queue_key, {job_id: priority})
            await pipe.execute()

        logger.info("Job %s enqueued (priority=%d, session=%s)", job_id, priority, session_id)
        await self._update_queue_depth_metrics()
        return job_id

    async def dequeue(self, lease_ttl: int = 120) -> dict | None:
        """Get next job from queue.

        Sets a processing lease TTL — if the worker fails to heartbeat
        or complete the job within lease_ttl, the job becomes eligible
        for recovery via recover_orphans().

        Args:
            lease_ttl: Processing lease TTL in seconds

        Returns:
            Job data or None if queue is empty
        """
        # Get Job With Lowest Priority Score
        result = await self._redis.zpopmin(self._queue_key, count=1)
        if not result:
            return None

        job_id = _decode_id(result[0][0])
        job_data = await self._get_job(job_id)

        if job_data:
            job_data["status"] = "running"
            job_data["started_at"] = time.time()
            async with self._redis.pipeline() as pipe:
                await pipe.set(
                    f"{self._job_prefix}{job_id}",
                    json.dumps(job_data, default=str),
                    ex=86400,
                )
                await pipe.zadd(self._processing_key, {job_id: time.time()})
                await pipe.set(
                    f"{self._lease_prefix}{job_id}",
                    "1",
                    ex=lease_ttl,
                )
                await pipe.execute()

        if job_data:
            await self._update_queue_depth_metrics()
        return job_data

    async def heartbeat(self, job_id: str, lease_ttl: int = 120) -> bool:
        """Extend processing lease — call periodically from worker.

        Returns True if the lease was renewed, False if the job
        is no longer tracked as processing (e.g., already completed).
        """
        exists = await self._redis.zscore(self._processing_key, job_id)
        if exists is None:
            return False
        async with self._redis.pipeline() as pipe:
            await pipe.expire(f"{self._lease_prefix}{job_id}", lease_ttl)
            await pipe.zadd(self._processing_key, {job_id: time.time()})
            await pipe.execute()
        return True

    async def complete_job(
        self,
        job_id: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        error: str | None = None,
    ):
        """Mark job as completed."""
        job_data = await self._get_job(job_id)
        if not job_data:
            return

        job_data["status"] = "completed" if exit_code == 0 else "failed"
        job_data["completed_at"] = time.time()
        job_data["stdout"] = stdout
        job_data["stderr"] = stderr
        job_data["exit_code"] = exit_code
        job_data["error"] = error

        await self._redis.set(
            f"{self._job_prefix}{job_id}",
            json.dumps(job_data),
            ex=86400,
        )

        # Remove From Processing
        await self._redis.zrem(self._processing_key, job_id)
        await self._redis.delete(f"{self._lease_prefix}{job_id}")

        # Add To Completed Set
        await self._redis.zadd(self._completed_key, {job_id: time.time()})

        logger.info("Job %s completed (exit_code=%d)", job_id, exit_code)
        await self._update_queue_depth_metrics()

    async def retry_job(self, job_id: str, error: str) -> bool:
        """Retry failed job.

        Returns:
            True if job was requeued, False if max retries exceeded
        """
        job_data = await self._get_job(job_id)
        if not job_data:
            return False

        job_data["retry_count"] += 1
        job_data["error"] = error

        if job_data["retry_count"] >= job_data["max_retries"]:
            # Move To Dead Letter Queue
            job_data["status"] = "dead"
            await self._redis.set(
                f"{self._job_prefix}{job_id}",
                json.dumps(job_data, default=str),
                ex=86400 * 7,  # 7 days
            )
            await self._redis.zrem(self._processing_key, job_id)
            await self._redis.delete(f"{self._lease_prefix}{job_id}")
            await self._redis.zadd(self._dead_letter_key, {job_id: time.time()})
            logger.warning(
                "Job %s moved to dead letter queue after %d retries",
                job_id,
                job_data["retry_count"],
            )
            await self._update_queue_depth_metrics()
            return False

        # Requeue With Exponential Backoff
        backoff = 2 ** job_data["retry_count"]
        job_data["status"] = "pending"
        job_data["started_at"] = None
        await asyncio.sleep(backoff)

        await self._redis.set(
            f"{self._job_prefix}{job_id}",
            json.dumps(job_data),
            ex=86400,
        )
        await self._redis.zrem(self._processing_key, job_id)
        await self._redis.delete(f"{self._lease_prefix}{job_id}")
        await self._redis.zadd(self._queue_key, {job_id: job_data["priority"]})

        logger.info(
            "Job %s requeued (retry %d/%d, backoff=%ds)",
            job_id,
            job_data["retry_count"],
            job_data["max_retries"],
            backoff,
        )
        await self._update_queue_depth_metrics()
        return True

    async def get_job(self, job_id: str) -> dict | None:
        """Get job by ID."""
        return await self._get_job(job_id)

    async def _get_job(self, job_id: str) -> dict | None:
        """Internal: get job data from Redis."""
        if not self._redis:
            return None
        data = await self._redis.get(f"{self._job_prefix}{job_id}")
        if data:
            return json.loads(data)
        return None

    async def get_queue_stats(self) -> dict:
        """Get queue statistics."""
        pending = await self._redis.zcard(self._queue_key)
        processing = await self._redis.zcard(self._processing_key)
        completed = await self._redis.zcard(self._completed_key)
        dead = await self._redis.zcard(self._dead_letter_key)

        return {
            "pending": pending,
            "processing": processing,
            "completed": completed,
            "dead_letter": dead,
        }

    async def cleanup_old_jobs(self, max_age: int = 86400):
        """Remove old completed and dead-letter jobs."""
        cutoff = time.time() - max_age

        # Remove Old Completed Jobs
        completed = await self._redis.zrangebyscore(self._completed_key, 0, cutoff)
        for raw_id in completed:
            job_id = _decode_id(raw_id)
            await self._redis.delete(f"{self._job_prefix}{job_id}")
        await self._redis.zremrangebyscore(self._completed_key, 0, cutoff)

        # Remove Old Dead-letter Jobs (7-day Retention)
        dead_cutoff = time.time() - max_age * 7
        dead = await self._redis.zrangebyscore(self._dead_letter_key, 0, dead_cutoff)
        for raw_id in dead:
            job_id = _decode_id(raw_id)
            await self._redis.delete(f"{self._job_prefix}{job_id}")
        await self._redis.zremrangebyscore(self._dead_letter_key, 0, dead_cutoff)

        logger.info("Cleaned up %d completed + %d dead-letter jobs", len(completed), len(dead))

    async def get_dead_letter_jobs(self, limit: int = 100) -> list[dict]:
        """Get jobs from dead letter queue."""
        job_ids = await self._redis.zrange(self._dead_letter_key, 0, limit - 1, desc=True)
        jobs = []
        for raw_id in job_ids:
            job_id = _decode_id(raw_id)
            job = await self._get_job(job_id)
            if job:
                jobs.append(job)
        return jobs

    async def recover_orphans(self, lease_ttl: int = 120) -> int:
        """Move processing jobs with expired leases back to the queue.

        Uses zrangebyscore on the processing zset to find stale entries
        efficiently — the processing score is updated on each heartbeat.

        Args:
            lease_ttl: Jobs whose processing score is older than this
                       are considered orphaned and moved to the pending queue.

        Returns:
            Number of recovered jobs
        """
        cutoff = time.time() - lease_ttl
        stale = await self._redis.zrangebyscore(self._processing_key, 0, cutoff)
        if not stale:
            return 0

        recovered = 0
        for raw_id in stale:
            job_id = _decode_id(raw_id)
            job_data = await self._get_job(job_id)
            if job_data is None:
                await self._redis.zrem(self._processing_key, job_id)
                await self._redis.delete(f"{self._lease_prefix}{job_id}")
                continue
            job_data["status"] = "pending"
            job_data["started_at"] = None
            async with self._redis.pipeline() as pipe:
                await pipe.set(
                    f"{self._job_prefix}{job_id}",
                    json.dumps(job_data, default=str),
                    ex=86400,
                )
                await pipe.zrem(self._processing_key, job_id)
                await pipe.zadd(self._queue_key, {job_id: job_data["priority"]})
                await pipe.delete(f"{self._lease_prefix}{job_id}")
                await pipe.execute()
            recovered += 1
            logger.warning("Recovered orphan job %s from processing", job_id)

        logger.info("Recovered %d orphan jobs", recovered)
        return recovered
