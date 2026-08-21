"""Background job management for long-running SSH commands."""

import asyncio
import base64
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field

from app.command_policy import evaluate_command_policy
from app.config import settings
from app.exceptions import (
    JobNotFoundError,
    PermissionDeniedError,
    SubmissionUnavailableError,
)
from app.output_redaction import should_redact_command_output
from app.redis_queue import RedisJobQueue
from app.security import redact_secrets
from app.ssh_manager import (
    ExecutionError,
    SessionNotFoundError,
    SSHSessionManager,
)

logger = logging.getLogger(__name__)

MAX_STDOUT_SIZE = 10 * 1024 * 1024  # 10 MB per job
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "ambiguous"})
ACTIVE_STATES = frozenset({"pending", "running", "cancelling"})
SSE_LISTENER_QUEUE_SIZE = 256
DURABLE_LEASE_TTL_SECONDS = 60


def _submission_payload_hash(session_id: str, command: str, stdin: bytes, timeout: int) -> str:
    """Hash immutable execution semantics for a keyed submission.

    ``redact_path_prefix`` is intentionally excluded: it affects presentation
    only, not the SSH target or remote execution semantics.
    """
    digest = hashlib.sha256()
    digest.update(session_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(command.encode("utf-8"))
    digest.update(b"\0")
    digest.update(stdin)
    digest.update(b"\0")
    digest.update(str(timeout).encode("ascii"))
    return digest.hexdigest()


def _make_job_error_logger(job_id: str):
    """Build a done callback that logs job crashes."""

    def _log(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error("Job %s crashed: %s", job_id, exc)

    return _log


# ---------------------------------------------------------------------------
# Job Record
# ---------------------------------------------------------------------------


@dataclass
class JobRecord:
    """Stores a background job and its metadata."""

    job_id: str
    session_id: str
    command: str
    status: str = "pending"  # pending, running, cancelling, completed, failed, cancelled, ambiguous
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    owner_id: str = ""
    error_message: str | None = None
    # Absolute host path prefix to strip from command/stdout/stderr/
    # error_message when redaction is applied (M8) -- deliberately excluded
    # from to_dict()'s allowlist, since it's consumed internally by
    # job_serializer.py, not returned to API callers itself.
    redact_path_prefix: str | None = None
    # Data written to the command's stdin before shutdown_write (mirrors
    # execute-argv's stdin). Never returned to API callers.
    stdin: bytes = b""
    # Per-job command timeout (seconds) forwarded to execute_stream. Defaults
    # to the manager's job_timeout (3600) when create_job omits it. Never
    # returned to API callers via to_dict().
    timeout: int = 3600
    # Set to True when the job was created under a submission_key and a
    # durable envelope was atomically committed to Redis.  Used by
    # ``_run_job`` to decide whether to claim/heartbeat/finish via Redis
    # instead of the legacy in-process path.
    is_durable: bool = False

    # Monotonic timestamps (relative to process start; do NOT survive restart)
    queued_at_mono: float | None = None
    acquired_at_mono: float | None = None
    command_started_at_mono: float | None = None
    command_finished_at_mono: float | None = None
    completed_at_mono: float | None = None
    ssh_connect_started_at_mono: float | None = None
    ssh_connected_at_mono: float | None = None
    progress: dict = field(default_factory=dict)
    _listeners: list = field(default_factory=list, repr=False)
    _listener_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    completed_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def touch(self) -> None:
        """Update last activity timestamp (for progress)."""
        self.progress["last_update"] = time.time()

    @property
    def duration(self) -> float | None:
        """Job duration in seconds."""
        if self.started_at is None:
            return None
        end = self.completed_at or time.time()
        return round(end - self.started_at, 3)

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "command": self.command,
            "status": self.status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "owner_id": self.owner_id,
            "duration": self.duration,
            "queued_at_mono": self.queued_at_mono,
            "completed_at_mono": self.completed_at_mono,
            "error_message": self.error_message,
            "progress": self.progress,
        }

    def add_listener(self, queue: asyncio.Queue) -> None:
        """Add an SSE listener queue."""
        self._listeners.append(queue)

    def remove_listener(self, queue: asyncio.Queue) -> None:
        """Remove an SSE listener queue."""
        if queue in self._listeners:
            self._listeners.remove(queue)

    async def notify_listeners(self, event: dict) -> None:
        """Notify listeners without letting slow SSE consumers block jobs.

        Output chunks are best-effort under backpressure. Control events
        displace one queued item so terminal state remains observable.
        Each listener receives a private event dict because the HTTP layer
        may redact payloads per subscriber.
        """
        async with self._listener_lock:
            dead = []
            for queue in self._listeners:
                try:
                    queue.put_nowait(dict(event))
                except asyncio.QueueFull:
                    if event.get("type") in {"stdout", "stderr"}:
                        continue
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        queue.put_nowait(dict(event))
                    except asyncio.QueueFull:
                        dead.append(queue)
                except Exception:
                    dead.append(queue)
            for q in dead:
                if q in self._listeners:
                    self._listeners.remove(q)


# ---------------------------------------------------------------------------
# Job Manager
# ---------------------------------------------------------------------------


class JobManager:
    """Manages background jobs for SSH sessions."""

    def __init__(
        self,
        ssh_manager: SSHSessionManager,
        max_jobs: int = 100,
        job_timeout: int = 3600,
        redis_queue: RedisJobQueue | None = None,
    ) -> None:
        self._ssh_manager = ssh_manager
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()
        self._max_jobs = max_jobs
        self._job_timeout = job_timeout
        self._cleanup_task: asyncio.Task | None = None
        # Set post-construction in main.py's lifespan — RedisJobQueue is
        # created after JobManager. Mirrors terminal job state so job
        # history/results survive a gateway restart; see save_terminal_job().
        self.redis_queue = redis_queue
        self._job_tasks: dict[str, asyncio.Task] = {}

    async def start_cleanup_task(self) -> None:
        """Start background cleanup of old jobs."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Job Cleanup Task Started")

    async def stop_cleanup_task(self) -> None:
        """Stop background cleanup."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("Job Cleanup Task Stopped")

    async def _cleanup_loop(self) -> None:
        """Remove completed jobs older than 1 hour."""
        while True:
            try:
                await asyncio.sleep(300)  # Every 5 minutes
                await self.cleanup_old_jobs()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Job cleanup loop error: %s", exc)

    async def cleanup_old_jobs(self) -> int:
        """Remove completed jobs older than 1 hour."""
        cutoff = time.time() - 3600
        to_remove: list[str] = []

        async with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.completed_at and job.completed_at < cutoff:
                    to_remove.append(job_id)

        for job_id in to_remove:
            async with self._lock:
                self._jobs.pop(job_id, None)
            logger.info("Cleaned up old job %s", job_id)

        return len(to_remove)

    async def force_cleanup(self) -> int:
        """Cancel local job tasks without inventing unproven remote outcomes.

        A still-pending job can be declared cancelled here because holding the
        manager lock while cancelling its scheduled Task guarantees that
        ``_run_job`` cannot transition it to running or reach the remote host.
        Once a job is running, cancelling the local asyncio Task is not proof
        that an executor thread / SSH command has stopped, so it remains
        non-terminal unless the normal execution path proves otherwise.
        """
        proven_terminal: list[JobRecord] = []
        async with self._lock:
            for job in self._jobs.values():
                job.cancel_event.set()
                if job.status == "pending":
                    job.status = "cancelled"
                    job.completed_at = job.completed_at or time.time()
                    job.completed_at_mono = job.completed_at_mono or time.monotonic()
                    job.completed_event.set()
                    proven_terminal.append(job)
                elif job.status in {"running", "cancelling"}:
                    job.status = "cancelling"
                elif job.status in TERMINAL_STATES:
                    # The result was already decided before forced shutdown.
                    # Re-persisting after local task cancellation is safe and
                    # prevents cancelling an in-flight Redis write from losing
                    # a proven terminal snapshot.
                    proven_terminal.append(job)
            tasks = list(self._job_tasks.values())
            self._job_tasks.clear()
            for task in tasks:
                task.cancel()
            count = len(self._jobs)
            self._jobs.clear()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for job in proven_terminal:
            await self._persist_terminal_job(job)
        logger.warning("Force-cleaned %d jobs (%d tasks cancelled)", count, len(tasks))
        return count

    # ------------------------------------------------------------------
    # Create And Run Job
    # ------------------------------------------------------------------

    async def create_job(
        self,
        session_id: str,
        command: str,
        owner_id: str = "",
        redact_path_prefix: str | None = None,
        stdin: bytes = b"",
        timeout: int | None = None,
        submission_key: str | None = None,
    ) -> str:
        """Create a background job, optionally under a durable idempotency key.

        Keyed submissions fail closed: Redis must be available and the key is
        atomically reserved before any SSH command is scheduled. Repeating an
        identical request returns the original job_id, including after a
        coordinator/Gateway retry or process restart.

        For keyed submissions the ACK is issued only after the immutable
        submission identity and the full executable envelope are durably
        committed together in Redis (``reserve_submission_with_job``).
        Unkeyed ``create_job`` remains explicitly in-process / best-effort.
        """
        resolved_timeout = self._job_timeout if timeout is None else timeout
        payload_hash = _submission_payload_hash(session_id, command, stdin, resolved_timeout)

        if submission_key:
            if self.redis_queue is None:
                raise SubmissionUnavailableError("Durable submission requires Redis")
            existing = await self.redis_queue.find_submission(
                submission_key, owner_id=owner_id, payload_hash=payload_hash
            )
            if existing is not None:
                return existing

        async with self._lock:
            active_jobs = sum(1 for job in self._jobs.values() if job.status in ACTIVE_STATES)
            if active_jobs >= self._max_jobs:
                if submission_key and self.redis_queue is not None:
                    existing = await self.redis_queue.find_submission(
                        submission_key, owner_id=owner_id, payload_hash=payload_hash
                    )
                    if existing is not None:
                        return existing
                raise ExecutionError("Maximum number of jobs reached")

            job_id = str(uuid.uuid4())
            if submission_key:
                if self.redis_queue is None:
                    raise SubmissionUnavailableError("Durable submission requires Redis")

                # Build the full executable envelope that will be atomically
                # committed together with the submission claim.  On restart a
                # gateway worker can reconstruct the job from this envelope alone.
                now = time.time()
                envelope: dict = {
                    "version": 1,
                    "job_id": job_id,
                    "submission_key": submission_key,
                    "session_id": session_id,
                    "command": command,
                    "stdin_b64": base64.b64encode(stdin).decode("ascii"),
                    "timeout": resolved_timeout,
                    "owner_id": owner_id,
                    "redact_path_prefix": redact_path_prefix,
                    "payload_hash": payload_hash,
                    "status": "pending",
                    "created_at": now,
                    "queued_at": now,
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
                    "cancel_requested": False,
                }
                reserved_job_id, created = await self.redis_queue.reserve_submission_with_job(
                    submission_key,
                    job_id=job_id,
                    owner_id=owner_id,
                    payload_hash=payload_hash,
                    envelope=envelope,
                )
                if not created:
                    # A previous submission with the same key+payload exists —
                    # the durable envelope was already committed, so the ACK
                    # contract is satisfied.
                    return reserved_job_id
                job_id = reserved_job_id

            job = JobRecord(
                job_id=job_id,
                session_id=session_id,
                command=command,
                owner_id=owner_id,
                redact_path_prefix=redact_path_prefix,
                stdin=stdin,
                timeout=resolved_timeout,
                is_durable=bool(submission_key),
            )
            job.queued_at_mono = time.monotonic()
            if job.is_durable:
                job.progress["durable_persisted"] = False
            self._jobs[job_id] = job

        # Schedule only after a keyed submission is durably reserved.
        task = asyncio.create_task(self._run_job(job_id))
        task.add_done_callback(_make_job_error_logger(job_id))
        self._job_tasks[job_id] = task
        task.add_done_callback(lambda _: self._job_tasks.pop(job_id, None))
        return job_id

    async def _persist_terminal_job(self, job: JobRecord) -> None:
        """Mirror a finished job to Redis, best-effort.

        Must never raise — a Redis hiccup is not allowed to affect the
        in-process job outcome that's already been decided.
        """
        if self.redis_queue is None or job.status not in TERMINAL_STATES:
            return
        try:
            await self.redis_queue.save_terminal_job(
                job.job_id,
                session_id=job.session_id,
                command=job.command,
                owner_id=job.owner_id,
                status=job.status,
                stdout=job.stdout,
                stderr=job.stderr,
                exit_code=job.exit_code,
                error=job.error_message,
                redact_path_prefix=job.redact_path_prefix,
            )
        except Exception:
            logger.warning("Failed to persist job %s to Redis", job.job_id, exc_info=True)

    # ------------------------------------------------------------------
    # Recovery — Reconstruct from Durable Envelope
    # ------------------------------------------------------------------

    async def recover_job(self, envelope: dict) -> str | None:
        """Reconstruct a ``JobRecord`` from a durable envelope and schedule
        it for execution.  Returns the *job_id* on success, or ``None``
        if the envelope is invalid or the job is already in memory (e.g.
        started by a concurrent worker).

        The caller is responsible for ensuring that only recoverable
        envelopes (those in ``pending`` or expired-``processing`` state)
        are passed here.  The actual execution claim inside ``_run_job``
        will serialize concurrent recovery attempts via the atomic
        ``claim_durable_execution`` Lua script — only one token-holder
        may cross into ``execute_stream``.
        """
        try:
            job_id = str(envelope["job_id"])
            session_id = str(envelope["session_id"])
            command = str(envelope["command"])
            owner_id = str(envelope.get("owner_id", ""))
            redact_path_prefix = envelope.get("redact_path_prefix")
            timeout = int(envelope.get("timeout", 3600))
            try:
                stdin = base64.b64decode(envelope.get("stdin_b64", ""))
            except Exception:
                stdin = b""
        except (KeyError, TypeError, ValueError):
            logger.warning("Invalid durable envelope — skipping recovery")
            return None

        durable_status = str(envelope.get("status", ""))
        if durable_status not in {"pending", "processing"}:
            # Terminal and explicitly ambiguous outcomes are never executable
            # recovery candidates, even if a caller accidentally supplies them.
            return None
        if durable_status == "processing":
            if bool(envelope.get("cancel_requested")):
                # A prior worker crossed the remote execution boundary and a
                # cancellation was acknowledged. Its outcome is ambiguous and
                # must never be automatically replayed after restart.
                return None
            lease_expiry = envelope.get("lease_expiry")
            if lease_expiry is not None and float(lease_expiry) >= time.time():
                # Still owned by a live lease; recovery callers must not race it.
                return None

        # Recovery is dependency-aware: do not run before the target SID
        # has actually been restored. Missing sessions stay nonterminal in Redis.
        if await self._ssh_manager.get_session(session_id) is None:
            logger.warning(
                "Durable job %s waiting for restored session %s", job_id, session_id
            )
            return None

        async with self._lock:
            if job_id in self._jobs:
                return None  # already scheduled (concurrent recovery)

            job = JobRecord(
                job_id=job_id,
                session_id=session_id,
                command=command,
                owner_id=owner_id,
                redact_path_prefix=redact_path_prefix,
                stdin=stdin,
                timeout=timeout,
                is_durable=True,
            )
            job.queued_at_mono = time.monotonic()
            job.progress["durable_persisted"] = False
            self._jobs[job_id] = job

        task = asyncio.create_task(self._run_job(job_id))
        task.add_done_callback(_make_job_error_logger(job_id))
        self._job_tasks[job_id] = task
        task.add_done_callback(lambda _: self._job_tasks.pop(job_id, None))
        logger.info("Recovered durable job %s from envelope", job_id)
        return job_id

    async def _run_job(self, job_id: str) -> None:
        """Execute a command in the background.

        For keyed durable jobs the execution flow is:
          1. ``claim_durable_execution`` — atomic Redis claim (pending→processing)
          2. ``execute_stream`` — the actual SSH command
          3. ``finish_durable_execution`` — fenced terminal transition

        A heartbeat loop runs concurrently with the SSH command and renews
        the processing lease.  If the lease cannot be renewed (stale owner
        detected), the heartbeat fails and the fenced-finish transition is
        rejected — the new token-holder wins.

        Unkeyed jobs (``redis_queue.durable_job_ids`` does not contain the
        job_id) skip the claim/heartbeat/finish cycle entirely and behave
        exactly as before (in-process, best-effort).
        """
        async with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return

        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.completed_at = job.completed_at or time.time()
            job.completed_at_mono = job.completed_at_mono or time.monotonic()
            job.completed_event.set()
            if not job.is_durable:
                await self._persist_terminal_job(job)
            return

        # Check whether this is a durable keyed job that requires a Redis
        # execution claim before crossing into execute_stream.
        is_durable = (
            job.is_durable
            and self.redis_queue is not None
            and self.redis_queue._redis is not None
        )

        worker_token: str | None = None
        lease_ttl = DURABLE_LEASE_TTL_SECONDS
        heartbeat_task: asyncio.Task | None = None

        if is_durable:
            worker_token = uuid.uuid4().hex
            claimed = await self.redis_queue.claim_durable_execution(
                job_id, worker_token=worker_token, lease_ttl=lease_ttl,
            )
            if not claimed:
                # Another worker may own this job. The losing coordinator
                # must not write an unfenced terminal snapshot over the winner.
                job.completed_event.set()
                async with self._lock:
                    self._jobs.pop(job_id, None)
                return

            if await self.redis_queue.is_durable_cancellation_requested(
                job_id, worker_token=worker_token
            ):
                job.cancel_event.set()
                job.status = "cancelled"
                job.exit_code = -1
                job.completed_at = time.time()
                job.completed_at_mono = time.monotonic()
                job.completed_event.set()
                persisted = await self.redis_queue.finish_durable_execution(
                    job_id, worker_token=worker_token, status="cancelled", exit_code=-1
                )
                job.progress["durable_persisted"] = persisted
                if not persisted:
                    logger.warning(
                        "Durable terminal state for job %s was not persisted", job_id
                    )
                return

            async def _heartbeat_loop() -> None:
                # Renew well before expiry; fractional intervals are important
                # for short test/configured leases and avoid TTL-boundary races.
                interval = max(0.1, lease_ttl / 3)
                while True:
                    await asyncio.sleep(interval)
                    ok = await self.redis_queue.heartbeat_durable_execution(
                        job_id, worker_token=worker_token, lease_ttl=lease_ttl,
                    )
                    if not ok:
                        # Partition or ownership loss makes the remote outcome
                        # ambiguous. Ask the transport to stop; fenced finish
                        # prevents stale persistence if another worker took over.
                        job.cancel_event.set()
                        break
                    if await self.redis_queue.is_durable_cancellation_requested(
                        job_id, worker_token=worker_token
                    ):
                        job.cancel_event.set()

            heartbeat_task = asyncio.create_task(_heartbeat_loop())

        try:
            job.status = "running"
            job.started_at = time.time()
            job.acquired_at_mono = time.monotonic()

            _started_command = (
                redact_secrets(job.command)
                if should_redact_command_output(None)
                else job.command
            )
            await job.notify_listeners(
                {
                    "type": "status",
                    "status": "running",
                    "message": f"Started: {_started_command}",
                }
            )

            decision = evaluate_command_policy(
                job.command,
                mode=settings.command_policy_mode,
                profile=settings.command_policy_profile,
            )
            if not decision.allowed:
                job.status = "failed"
                job.error_message = f"Command denied by policy: {decision.reason}"
                job.completed_at = time.time()
                job.completed_at_mono = time.monotonic()
                job.completed_event.set()
                logger.warning(
                    "Job %s denied by policy: %s", job.job_id, decision.reason
                )
                await job.notify_listeners(
                    {"type": "error", "error": job.error_message}
                )
                await job.notify_listeners(
                    {
                        "type": "status",
                        "status": "failed",
                        "duration": job.duration,
                        "exit_code": -1,
                    }
                )
                return  # finally block handles fenced-finish

            try:
                job.command_started_at_mono = time.monotonic()
                async for msg_type, msg_data in self._ssh_manager.execute_stream(
                    job.session_id,
                    job.command,
                    timeout=job.timeout,
                    cancel_event=job.cancel_event,
                    stdin_data=job.stdin,
                ):
                    job.touch()

                    if msg_type == "stdout":
                        remaining = MAX_STDOUT_SIZE - len(job.stdout)
                        if remaining > 0:
                            job.stdout += msg_data[:remaining]
                            if remaining < len(msg_data) and "[truncated]" not in job.stdout:
                                job.stdout += "\n... [output truncated, exceeded 10MB]"
                        await job.notify_listeners(
                            {"type": "stdout", "data": msg_data}
                        )
                    elif msg_type == "stderr":
                        remaining = MAX_STDOUT_SIZE - len(job.stderr)
                        if remaining > 0:
                            job.stderr += msg_data[:remaining]
                            if remaining < len(msg_data) and "[truncated]" not in job.stderr:
                                job.stderr += "\n... [output truncated, exceeded 10MB]"
                        await job.notify_listeners(
                            {"type": "stderr", "data": msg_data}
                        )
                    elif msg_type == "exit":
                        job.exit_code = int(msg_data)
                        job.command_finished_at_mono = time.monotonic()
                        await job.notify_listeners(
                            {"type": "exit", "exit_code": job.exit_code}
                        )

                if is_durable and job.cancel_event.is_set():
                    # A non-negative recv_exit_status() is factual remote
                    # completion and wins a concurrent/late cancel request.
                    # The -1 sentinel from SSHSessionManager.execute_stream()
                    # is synthetic after local channel.close(), so it cannot
                    # prove that remote side effects stopped.
                    if job.exit_code is not None and job.exit_code >= 0:
                        job.status = "completed" if job.exit_code == 0 else "failed"
                        if job.status == "failed":
                            job.error_message = f"Exit code: {job.exit_code}"
                    else:
                        job.status = "ambiguous"
                        if job.exit_code is None:
                            job.exit_code = -1
                        job.error_message = (
                            "Local SSH channel interrupted after cancellation; "
                            "remote command outcome is unproven"
                        )
                        job.progress["cancellation_outcome"] = "ambiguous"
                        job.progress["locally_interrupted"] = True
                elif job.cancel_event.is_set():
                    # Preserve legacy best-effort semantics for unkeyed jobs.
                    job.status = "cancelled"
                    if job.exit_code is None:
                        job.exit_code = -1
                    job.error_message = None
                else:
                    job.status = "completed" if (job.exit_code == 0) else "failed"
                    if job.status == "failed" and job.exit_code != 0:
                        job.error_message = f"Exit code: {job.exit_code}"

            except SessionNotFoundError as exc:
                job.status = "failed"
                job.error_message = str(exc)
                await job.notify_listeners({"type": "error", "error": str(exc)})
            except Exception as exc:
                job.status = "failed"
                job.error_message = str(exc)
                await job.notify_listeners({"type": "error", "error": str(exc)})
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

            if is_durable and self.redis_queue and job.status in TERMINAL_STATES:
                assert worker_token is not None
                persisted = await self.redis_queue.finish_durable_execution(
                    job_id,
                    worker_token=worker_token,
                    status=job.status,
                    stdout=job.stdout,
                    stderr=job.stderr,
                    exit_code=job.exit_code,
                    error=job.error_message,
                )
                job.progress["durable_persisted"] = persisted
                if not persisted:
                    logger.warning(
                        "Durable terminal state for job %s was not persisted; "
                        "remote outcome is known locally but restart recovery remains pending",
                        job_id,
                    )
            elif job.status in TERMINAL_STATES:
                await self._persist_terminal_job(job)

            if job.status in TERMINAL_STATES:
                job.completed_at = time.time()
                job.completed_at_mono = time.monotonic()
                job.completed_event.set()
                await job.notify_listeners(
                    {
                        "type": "status",
                        "status": job.status,
                        "duration": job.duration,
                        "exit_code": job.exit_code,
                    }
                )

    # ------------------------------------------------------------------
    # Get Job
    # ------------------------------------------------------------------

    async def get_job(self, job_id: str) -> JobRecord | None:
        """Get a job by ID."""
        async with self._lock:
            return self._jobs.get(job_id)

    async def get_job_status(self, job_id: str) -> dict:
        """Get job status (lightweight)."""
        job = await self.get_job(job_id)
        if not job:
            raise SessionNotFoundError(f"Job {job_id} not found")
        return {
            "job_id": job.job_id,
            "status": job.status,
            "progress": job.progress,
            "duration": job.duration,
        }

    async def get_job_result(self, job_id: str) -> dict:
        """Get full job result."""
        job = await self.get_job(job_id)
        if not job:
            raise SessionNotFoundError(f"Job {job_id} not found")
        return job.to_dict()

    # ------------------------------------------------------------------
    # List Jobs
    # ------------------------------------------------------------------

    async def list_jobs(
        self,
        session_id: str | None = None,
        status: str | None = None,
    ) -> list[JobRecord]:
        """List jobs, optionally filtered."""
        async with self._lock:
            jobs = list(self._jobs.values())

        if session_id:
            jobs = [j for j in jobs if j.session_id == session_id]
        if status:
            jobs = [j for j in jobs if j.status == status]

        return jobs

    # ------------------------------------------------------------------
    # Cancel Job
    # ------------------------------------------------------------------

    async def cancel_job(self, job_id: str) -> str:
        """Request cancellation, durably for keyed jobs.

        Pending durable jobs become terminal ``cancelled`` before ACK.
        Processing jobs persist cancellation intent. A factual non-negative
        remote exit remains completed/failed; local channel interruption is
        durably classified ``ambiguous`` and is never auto-replayed.
        """
        job = await self.get_job(job_id)
        if not job:
            raise SessionNotFoundError(f"Job {job_id} not found")

        if job.status not in ACTIVE_STATES:
            raise ExecutionError(f"Cannot cancel job with status: {job.status}")

        if job.is_durable:
            if self.redis_queue is None:
                raise SubmissionUnavailableError("Durable cancellation requires Redis")
            durable_status = await self.redis_queue.request_durable_cancellation(job_id)
            if durable_status == "cancelled":
                job.cancel_event.set()
                job.status = "cancelled"
                job.completed_at = time.time()
                job.completed_at_mono = time.monotonic()
                job.completed_event.set()
                await job.notify_listeners({"type": "status", "status": "cancelled"})
                return "cancelled"
            if durable_status != "cancelling":
                raise ExecutionError("Durable job is no longer cancellable")

        job.cancel_event.set()
        if job.status == "pending":
            job.status = "cancelled"
            job.completed_at = time.time()
            job.completed_at_mono = time.monotonic()
            job.completed_event.set()
        else:
            job.status = "cancelling"
        await job.notify_listeners(
            {
                "type": "status",
                "status": job.status,
            }
        )
        return job.status

    async def wait_for_completion(
        self, job_id: str, identity_sub: str, timeout_s: float
    ) -> dict:
        """Long-poll: wait for job completion or timeout.

        Returns job.to_dict() on completion, or
        {"job_id": ..., "status": "running", "wait_timed_out": True} on timeout.
        Raises JobNotFoundError, PermissionDeniedError, re-raises CancelledError.
        """
        job = await self.get_job(job_id)
        if not job:
            raise JobNotFoundError(job_id)

        if job.owner_id != identity_sub:
            raise PermissionDeniedError("Job belongs to a different owner")

        if job.status in TERMINAL_STATES:
            return job.to_dict()

        event = job.completed_event
        # Re-check after subscribe (race with fast jobs)
        if job.status in TERMINAL_STATES:
            return job.to_dict()

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
        except TimeoutError:
            return {"job_id": job_id, "status": job.status, "wait_timed_out": True}
        except asyncio.CancelledError:
            # Client disconnected — job UNCHANGED
            raise

        return job.to_dict()

    async def wait_for_all_jobs(self) -> None:
        """Wait for all active (pending/running) jobs to complete."""
        while True:
            async with self._lock:
                active_events = [
                    job.completed_event
                    for job in self._jobs.values()
                    if job.status in ACTIVE_STATES
                ]
            if not active_events:
                return
            logger.info("Waiting for %d active jobs to complete...", len(active_events))
            # Wait on the whole snapshot. If the outer shutdown deadline
            # cancels this coroutine, gather propagates cancellation to every
            # Event.wait child instead of leaving orphan wait tasks behind.
            await asyncio.gather(*(event.wait() for event in active_events))
