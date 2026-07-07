"""Distributed locks using Redis (Redlock algorithm)."""

import asyncio
import logging
import time
import uuid
from typing import cast

import redis.asyncio as redis

from .redis_compat import close_redis_client

logger = logging.getLogger(__name__)


class DistributedLock:
    """Distributed lock using Redis.

    Prevents concurrent modifications to the same file by multiple agents.
    Includes automatic lease renewal (watchdog) to prevent split-brain.
    """

    def __init__(self, redis_url: str = "redis://redis:6379/0"):
        self._redis_url = redis_url
        self._redis: redis.Redis | None = None
        self._lock_prefix = "ssh_gateway:lock:"
        self._renewal_tasks: dict[str, asyncio.Task] = {}

    async def connect(self):
        """Connect to Redis."""
        self._redis = await redis.from_url(self._redis_url, decode_responses=True)
        await self._redis.ping()
        logger.info("Lock Manager Connected To Redis")

    async def disconnect(self):
        """Disconnect from Redis and cancel all renewal tasks."""
        for _, task in list(self._renewal_tasks.items()):
            task.cancel()
        self._renewal_tasks.clear()
        if self._redis:
            await close_redis_client(self._redis)

    async def acquire(
        self,
        resource: str,
        ttl: int = 30,
        blocking: bool = True,
        blocking_timeout: int = 10,
    ) -> str | None:
        """Acquire lock on resource.

        Args:
            resource: Resource identifier (e.g., file path)
            ttl: Lock TTL in seconds
            blocking: Wait for lock if True
            blocking_timeout: Max wait time in seconds

        Returns:
            Lock token if acquired, None otherwise
        """
        lock_key = f"{self._lock_prefix}{resource}"
        token = str(uuid.uuid4())

        start_time = time.time()
        delay = 0.05
        max_delay = 1.0
        while True:
            acquired = await self._redis.set(lock_key, token, nx=True, ex=ttl)

            if acquired:
                logger.debug("Lock acquired for %s (token=%s)", resource, token[:8])
                # Start Watchdog For Lease Renewal
                task = asyncio.create_task(self._renew_loop(resource, token, ttl))
                self._renewal_tasks[resource] = task
                return token

            if not blocking:
                return None

            if time.time() - start_time >= blocking_timeout:
                logger.warning("Lock acquisition timeout for %s", resource)
                return None

            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

    async def _renew_loop(self, resource: str, token: str, ttl: int):
        """Background watchdog — renews lease every ttl/3 seconds via self.extend()."""
        interval = max(1, ttl // 3)
        try:
            while True:
                await asyncio.sleep(interval)
                if not await self.extend(resource, token, ttl):
                    logger.warning(
                        "Lock renewal failed for %s — lock lost or expired",
                        resource,
                    )
                    return
        except asyncio.CancelledError:
            pass

    async def release(self, resource: str, token: str) -> bool:
        """Release lock on resource.

        Args:
            resource: Resource identifier
            token: Lock token returned by acquire

        Returns:
            True if lock was released, False otherwise
        """
        lock_key = f"{self._lock_prefix}{resource}"

        # Cancel Watchdog
        task = self._renewal_tasks.pop(resource, None)
        if task is not None and not task.done():
            task.cancel()

        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """

        result = await self._redis.eval(lua_script, 1, lock_key, token)

        if result:
            logger.debug("Lock released for %s", resource)
            return True
        else:
            logger.warning("Lock release failed for %s (wrong token or expired)", resource)
            return False

    async def extend(self, resource: str, token: str, ttl: int = 30) -> bool:
        """Extend lock TTL.

        Args:
            resource: Resource identifier
            token: Lock token
            ttl: New TTL in seconds

        Returns:
            True if extended, False otherwise
        """
        lock_key = f"{self._lock_prefix}{resource}"

        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """

        result = await self._redis.eval(lua_script, 1, lock_key, token, str(ttl))
        return bool(result)

    async def is_locked(self, resource: str) -> bool:
        """Check if resource is locked."""
        lock_key = f"{self._lock_prefix}{resource}"
        return await self._redis.exists(lock_key) > 0

    async def get_lock_info(self, resource: str) -> dict | None:
        """Get lock information."""
        lock_key = f"{self._lock_prefix}{resource}"
        token = await self._redis.get(lock_key)
        ttl = await self._redis.ttl(lock_key)

        if token:
            t = cast(str, token)
            return {
                "resource": resource,
                "token": t[:8] + "...",
                "ttl_remaining": ttl,
            }
        return None
