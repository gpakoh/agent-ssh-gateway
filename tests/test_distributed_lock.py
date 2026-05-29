"""Tests for DistributedLock — TTL, concurrency, split-brain prevention.

Uses fakeredis (in-memory) so no Redis server is needed.
release() and extend() use Lua eval which fakeredis doesn't support,
so they are tested indirectly via raw Redis ops or skipped.
"""

import asyncio

import pytest

from app.distributed_lock import DistributedLock


@pytest.fixture
async def lock():
    dl = DistributedLock(redis_url="redis://localhost:6379/0")
    import fakeredis.aioredis
    dl._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await dl._redis.ping()
    yield dl
    await dl.disconnect()


def test_imports():
    from app.distributed_lock import DistributedLock
    assert DistributedLock


class TestDistributedLockBasics:
    @pytest.mark.asyncio
    async def test_acquire_returns_token(self, lock):
        token = await lock.acquire("file1", ttl=30, blocking=False)
        assert token is not None
        assert len(token) > 0

    @pytest.mark.asyncio
    async def test_is_locked_true_after_acquire(self, lock):
        await lock.acquire("file1", ttl=30, blocking=False)
        assert await lock.is_locked("file1") is True

    @pytest.mark.asyncio
    async def test_acquire_same_resource_blocked(self, lock):
        t1 = await lock.acquire("file1", ttl=30, blocking=False)
        assert t1 is not None
        t2 = await lock.acquire("file1", ttl=30, blocking=False)
        assert t2 is None

    @pytest.mark.asyncio
    async def test_different_resources_independent(self, lock):
        t1 = await lock.acquire("file1", ttl=30, blocking=False)
        t2 = await lock.acquire("file2", ttl=30, blocking=False)
        assert t1 is not None
        assert t2 is not None

    @pytest.mark.asyncio
    async def test_get_lock_info_returns_token_and_ttl(self, lock):
        await lock.acquire("file1", ttl=30, blocking=False)
        info = await lock.get_lock_info("file1")
        assert info is not None
        assert info["resource"] == "file1"
        assert "token" in info
        assert info["ttl_remaining"] > 0

    @pytest.mark.asyncio
    async def test_get_lock_info_nonexistent(self, lock):
        info = await lock.get_lock_info("nonexistent")
        assert info is None

    @pytest.mark.asyncio
    async def test_blocking_acquire_without_contention(self, lock):
        token = await lock.acquire("file2", ttl=30, blocking=True, blocking_timeout=1)
        assert token is not None

    @pytest.mark.asyncio
    async def test_blocking_acquire_timeout(self, lock):
        t1 = await lock.acquire("file1", ttl=30, blocking=False)
        assert t1 is not None

        t2 = await lock.acquire("file1", ttl=30, blocking=True, blocking_timeout=1)
        assert t2 is None  # timed out


# ═══════════════════════════════════════════════════════════════════════════════
# Split-brain Scenarios
# ═══════════════════════════════════════════════════════════════════════════════

class TestSplitBrain:
    @pytest.mark.asyncio
    async def test_ttl_expiry_allows_other_acquire(self, lock):
        """Task B can acquire after Task A's lock TTL expires."""
        t1 = await lock.acquire("file1", ttl=1, blocking=False)
        assert t1 is not None

        await asyncio.sleep(1.1)  # TTL expires

        t2 = await lock.acquire("file1", ttl=30, blocking=False)
        assert t2 is not None

    @pytest.mark.asyncio
    async def test_expired_key_does_not_block(self, lock):
        """Manually expired key should not block new acquire."""
        token = await lock.acquire("file1", ttl=30, blocking=False)
        assert token is not None

        # Manually expire the key
        await lock._redis.expire("ssh_gateway:lock:file1", 0)
        await asyncio.sleep(0.1)

        t2 = await lock.acquire("file1", ttl=30, blocking=False)
        assert t2 is not None

    @pytest.mark.asyncio
    async def test_blocking_acquire_eventually_succeeds_after_ttl(self, lock):
        """blocking=True should eventually succeed after lock expires."""
        t1 = await lock.acquire("file1", ttl=1, blocking=False)
        assert t1 is not None

        token = await lock.acquire("file1", ttl=30, blocking=True, blocking_timeout=2)
        assert token is not None

    @pytest.mark.asyncio
    async def test_watchdog_renews_before_ttl(self, lock):
        """Watchdog calls extend() before TTL expires."""
        from unittest.mock import patch

        token = await lock.acquire("file1", ttl=3, blocking=False)
        assert token is not None

        with patch.object(lock, "extend", return_value=True) as mock_extend:
            await asyncio.sleep(1.5)  # watchdog interval = max(1, 3//3) = 1s
            assert mock_extend.called, "watchdog should have called extend()"


# ═══════════════════════════════════════════════════════════════════════════════
# Disconnect — Watchdog Cleanup
# ═══════════════════════════════════════════════════════════════════════════════

class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_cancels_watchdog(self, lock):
        token = await lock.acquire("file1", ttl=30, blocking=False)
        assert token is not None
        assert "file1" in lock._renewal_tasks

        await lock.disconnect()
        assert "file1" not in lock._renewal_tasks

    @pytest.mark.asyncio
    async def test_multiple_locks_all_cleaned_on_disconnect(self, lock):
        for i in range(5):
            t = await lock.acquire(f"file{i}", ttl=30, blocking=False)
            assert t is not None

        assert len(lock._renewal_tasks) == 5

        await lock.disconnect()
        assert len(lock._renewal_tasks) == 0
