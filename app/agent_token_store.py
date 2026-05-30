"""Redis-backed agent token store — atomic, TTL, no race conditions."""

import hashlib
import logging
from typing import Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class AgentTokenStore:
    """Stores current agent token hash in Redis with TTL.

    SET agent_token:current → sha256(token)  (with EX = ttl)
    Overwrite on generate/refresh instantly invalidates the old token.
    """

    def __init__(self, redis_url: str):
        self._redis_url = redis_url
        self._redis: Optional[redis.Redis] = None

    async def connect(self):
        if self._redis is None:
            try:
                client = await redis.from_url(self._redis_url, decode_responses=True)
                await client.ping()
                self._redis = client
                logger.info("Agenttokenstore Connected To Redis")
            except Exception:
                # Ensure _redis Stays None When Connect Fails
                self._redis = None
                raise

    async def disconnect(self):
        if self._redis is not None:
            await self._redis.close()
            self._redis = None
            logger.info("Agenttokenstore Disconnected")

    @property
    def connected(self) -> bool:
        return self._redis is not None

    def _hash(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def set_token(self, token: str, ttl: int) -> None:
        """Store the current token hash with TTL (overwrites previous)."""
        if self._redis is None:
            raise RuntimeError("AgentTokenStore not connected to Redis")
        key = self._hash(token)
        await self._redis.set("agent_token:current", key, ex=ttl)

    async def validate_token(self, token: str) -> bool:
        """Check if the provided token matches the stored hash."""
        if not token or self._redis is None:
            return False
        stored = await self._redis.get("agent_token:current")
        if stored is None:
            return False
        return stored == self._hash(token)

    async def clear_token(self) -> None:
        """Immediately invalidate the current token."""
        if self._redis is None:
            return
        await self._redis.delete("agent_token:current")
