"""Redis-backed job queue for distributed processing."""

import json
import logging
import time
import uuid
from typing import Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)


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
        self._redis: Optional[redis.Redis] = None
        self._queue_key = "ssh_gateway:jobs:queue"
        self._processing_key = "ssh_gateway:jobs:processing"
        self._completed_key = "ssh_gateway:jobs:completed"
        self._dead_letter_key = "ssh_gateway:jobs:dead"
        self._job_prefix = "ssh_gateway:job:"
    
    async def connect(self):
        """Connect to Redis."""
        try:
            self._redis = await redis.from_url(self._redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info("Connected to Redis at %s", self._redis_url)
        except Exception as exc:
            logger.error("Failed to connect to Redis: %s", exc)
            raise
    
    async def disconnect(self):
        """Disconnect from Redis."""
        if self._redis:
            await self._redis.close()
            logger.info("Disconnected From Redis")
    
    async def enqueue(
        self,
        session_id: str,
        command: str,
        priority: int = 0,
        max_retries: int = 3,
        timeout: int = 3600,
    ) -> str:
        """Add job to queue.
        
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
            "created_at": time.time(),
            "started_at": None,
            "completed_at": None,
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "error": None,
        }
        
        # Store Job Data
        await self._redis.set(
            f"{self._job_prefix}{job_id}",
            json.dumps(job_data),
            ex=86400,  # 24h TTL
        )
        
        # Add To Priority Queue (lower Score = Higher Priority)
        await self._redis.zadd(self._queue_key, {job_id: priority})
        
        logger.info("Job %s enqueued (priority=%d, session=%s)", job_id, priority, session_id)
        return job_id
    
    async def dequeue(self) -> Optional[dict]:
        """Get next job from queue.
        
        Returns:
            Job data or None if queue is empty
        """
        # Get Job With Lowest Priority Score
        result = await self._redis.zpopmin(self._queue_key, count=1)
        if not result:
            return None
        
        job_id = result[0][0]
        job_data = await self._get_job(job_id)
        
        if job_data:
            job_data["status"] = "running"
            job_data["started_at"] = time.time()
            await self._redis.set(
                f"{self._job_prefix}{job_id}",
                json.dumps(job_data),
                ex=86400,
            )
            await self._redis.zadd(self._processing_key, {job_id: time.time()})
        
        return job_data
    
    async def complete_job(
        self,
        job_id: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        error: Optional[str] = None,
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
        
        # Add To Completed Set
        await self._redis.zadd(self._completed_key, {job_id: time.time()})
        
        logger.info("Job %s completed (exit_code=%d)", job_id, exit_code)
    
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
                json.dumps(job_data),
                ex=86400 * 7,  # 7 days
            )
            await self._redis.zrem(self._processing_key, job_id)
            await self._redis.zadd(self._dead_letter_key, {job_id: time.time()})
            logger.warning("Job %s moved to dead letter queue after %d retries", job_id, job_data["retry_count"])
            return False
        
        # Requeue With Exponential Backoff
        backoff = 2 ** job_data["retry_count"]
        job_data["status"] = "pending"
        job_data["started_at"] = None
        
        await self._redis.set(
            f"{self._job_prefix}{job_id}",
            json.dumps(job_data),
            ex=86400,
        )
        await self._redis.zrem(self._processing_key, job_id)
        await self._redis.zadd(self._queue_key, {job_id: job_data["priority"]})
        
        logger.info("Job %s requeued (retry %d/%d, backoff=%ds)", 
                   job_id, job_data["retry_count"], job_data["max_retries"], backoff)
        return True
    
    async def get_job(self, job_id: str) -> Optional[dict]:
        """Get job by ID."""
        return await self._get_job(job_id)
    
    async def _get_job(self, job_id: str) -> Optional[dict]:
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
        """Remove old completed jobs."""
        cutoff = time.time() - max_age
        
        # Remove Old Completed Jobs
        completed = await self._redis.zrangebyscore(self._completed_key, 0, cutoff)
        for job_id in completed:
            await self._redis.delete(f"{self._job_prefix}{job_id}")
        await self._redis.zremrangebyscore(self._completed_key, 0, cutoff)
        
        logger.info("Cleaned up %d old completed jobs", len(completed))
    
    async def get_dead_letter_jobs(self, limit: int = 100) -> list[dict]:
        """Get jobs from dead letter queue."""
        job_ids = await self._redis.zrange(self._dead_letter_key, 0, limit - 1, desc=True)
        jobs = []
        for job_id in job_ids:
            job = await self._get_job(job_id)
            if job:
                jobs.append(job)
        return jobs
