"""Tests for redis-py close/aclose compatibility helper."""

from __future__ import annotations

import pytest

from app.redis_compat import close_redis_client


class AsyncAcloseRedis:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class AsyncCloseRedis:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class SyncCloseRedis:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_close_redis_client_prefers_aclose():
    client = AsyncAcloseRedis()
    await close_redis_client(client)
    assert client.closed is True


@pytest.mark.asyncio
async def test_close_redis_client_supports_async_close():
    client = AsyncCloseRedis()
    await close_redis_client(client)
    assert client.closed is True


@pytest.mark.asyncio
async def test_close_redis_client_supports_sync_close():
    client = SyncCloseRedis()
    await close_redis_client(client)
    assert client.closed is True
