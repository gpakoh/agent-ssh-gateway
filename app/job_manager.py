"""Background job management for long-running SSH commands."""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.ssh_manager import (
    SSHSessionManager,
    SSHManagerError,
    SessionNotFoundError,
    ExecutionError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job record
# ---------------------------------------------------------------------------

@dataclass
class JobRecord:
    """Stores a background job and its metadata."""

    job_id: str
    session_id: str
    command: str
    status: str = "pending"  # pending, running, completed, failed, cancelled
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error_message: Optional[str] = None
    progress: dict = field(default_factory=dict)
    _listeners: list = field(default_factory=list, repr=False)

    def touch(self) -> None:
        """Update last activity timestamp (for progress)."""
        self.progress["last_update"] = time.time()

    @property
    def duration(self) -> Optional[float]:
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
            "duration": self.duration,
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
        """Notify all SSE listeners."""
        dead = []
        for queue in self._listeners:
            try:
                await queue.put(event)
            except Exception:
                dead.append(queue)
        for q in dead:
            self.remove_listener(q)


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
    ) -> None:
        self._ssh_manager = ssh_manager
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()
        self._max_jobs = max_jobs
        self._job_timeout = job_timeout
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start_cleanup_task(self) -> None:
        """Start background cleanup of old jobs."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Job cleanup task started")

    async def stop_cleanup_task(self) -> None:
        """Stop background cleanup."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("Job cleanup task stopped")

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

    # ------------------------------------------------------------------
    # Create and run job
    # ------------------------------------------------------------------

    async def create_job(self, session_id: str, command: str) -> str:
        """Create a new background job."""
        async with self._lock:
            if len(self._jobs) >= self._max_jobs:
                raise ExecutionError("Maximum number of jobs reached")

            job_id = str(uuid.uuid4())
            job = JobRecord(
                job_id=job_id,
                session_id=session_id,
                command=command,
            )
            self._jobs[job_id] = job

        # Start the job in background
        asyncio.create_task(self._run_job(job_id))
        return job_id

    async def _run_job(self, job_id: str) -> None:
        """Execute a command in the background."""
        async with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return

        job.status = "running"
        job.started_at = time.time()
        await job.notify_listeners({
            "type": "status",
            "status": "running",
            "message": f"Started: {job.command}",
        })

        try:
            # Use streaming execution for real-time output
            async for msg_type, msg_data in self._ssh_manager.execute_stream(
                job.session_id, job.command
            ):
                job.touch()

                if msg_type == "stdout":
                    job.stdout += msg_data
                    await job.notify_listeners({
                        "type": "stdout",
                        "data": msg_data,
                    })
                elif msg_type == "stderr":
                    job.stderr += msg_data
                    await job.notify_listeners({
                        "type": "stderr",
                        "data": msg_data,
                    })
                elif msg_type == "exit":
                    job.exit_code = int(msg_data)
                    await job.notify_listeners({
                        "type": "exit",
                        "exit_code": job.exit_code,
                    })

            job.status = "completed" if (job.exit_code == 0) else "failed"
            if job.exit_code != 0:
                job.error_message = f"Exit code: {job.exit_code}"

        except SessionNotFoundError as exc:
            job.status = "failed"
            job.error_message = str(exc)
            await job.notify_listeners({
                "type": "error",
                "error": str(exc),
            })
        except Exception as exc:
            job.status = "failed"
            job.error_message = str(exc)
            await job.notify_listeners({
                "type": "error",
                "error": str(exc),
            })
        finally:
            job.completed_at = time.time()
            await job.notify_listeners({
                "type": "status",
                "status": job.status,
                "duration": job.duration,
                "exit_code": job.exit_code,
            })

    # ------------------------------------------------------------------
    # Get job
    # ------------------------------------------------------------------

    async def get_job(self, job_id: str) -> Optional[JobRecord]:
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
    # List jobs
    # ------------------------------------------------------------------

    async def list_jobs(
        self,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
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
    # Cancel job
    # ------------------------------------------------------------------

    async def cancel_job(self, job_id: str) -> None:
        """Cancel a running job."""
        job = await self.get_job(job_id)
        if not job:
            raise SessionNotFoundError(f"Job {job_id} not found")

        if job.status not in ("pending", "running"):
            raise ExecutionError(f"Cannot cancel job with status: {job.status}")

        job.status = "cancelled"
        job.completed_at = time.time()
        await job.notify_listeners({
            "type": "status",
            "status": "cancelled",
        })
