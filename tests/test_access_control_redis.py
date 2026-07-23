"""Tests for access control Redis backing — Phase 12B."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.access_control import AccessControlStore, make_access_key_hash

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_redis_mock():
    """Create a mock redis.asyncio client with scan/hset/hgetall/expire."""
    r = AsyncMock()
    r.scan = AsyncMock(return_value=(0, []))
    r.hset = AsyncMock()
    r.hgetall = AsyncMock(return_value={})
    r.expire = AsyncMock()
    return r


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRedisSaveAndLoad:
    @pytest.mark.asyncio
    async def test_redis_save_and_load(self):
        """Saved entries are loaded back on startup."""
        store = AccessControlStore(
            pending_ttl=900,
            allow_ttl=86400,
            deny_ttl=86400,
            redis_url="redis://localhost:6379/0",
        )
        mock_redis = _make_redis_mock()

        key_hash = make_access_key_hash("fp1", "1.2.3.4")
        now = time.time()
        mock_redis.scan = AsyncMock(
            return_value=(0, [f"ac:{key_hash}"])
        )
        mock_redis.hgetall = AsyncMock(
            return_value={
                "key_hash": key_hash,
                "decision": "allowed",
                "actor_fingerprint": "fp1",
                "source_ip": "1.2.3.4",
                "reason": "test",
                "decided_by": "operator",
                "created_at": str(now),
                "expires_at": str(now + 3600),
            }
        )

        with patch.object(store, "_get_redis", return_value=mock_redis):
            loaded = await store.load_from_redis()

        assert loaded == 1
        entry = store.get("fp1", "1.2.3.4")
        assert entry is not None
        assert entry.decision == "allowed"

    @pytest.mark.asyncio
    async def test_redis_unavailable_does_not_break(self):
        """Redis connection error is non-fatal — store continues with in-memory state."""
        store = AccessControlStore(
            pending_ttl=900,
            allow_ttl=86400,
            deny_ttl=86400,
            redis_url="redis://localhost:6379/0",
        )

        with patch.object(
            store,
            "_get_redis",
            side_effect=Exception("Connection refused"),
        ):
            loaded = await store.load_from_redis()

        assert loaded == 0
        # In-memory store still works
        store.set("fp1", "1.2.3.4", "allowed", "test", "system")
        entry = store.get("fp1", "1.2.3.4")
        assert entry is not None
        assert entry.decision == "allowed"

    @pytest.mark.asyncio
    async def test_redis_key_contains_no_raw_ip_or_fingerprint(self):
        """Redis keys use hashed form, never raw IP or fingerprint."""
        store = AccessControlStore(
            pending_ttl=900,
            allow_ttl=86400,
            deny_ttl=86400,
            redis_url="redis://localhost:6379/0",
        )
        mock_redis = _make_redis_mock()

        with patch.object(store, "_get_redis", return_value=mock_redis):
            store.set("actor_fp_abc123", "10.0.0.1", "allowed", "test", "system")
            # Allow the fire-and-forget task to complete
            await asyncio.sleep(0.05)

        # Check all hset calls
        for call_args in mock_redis.hset.call_args_list:
            key = call_args.args[0] if call_args.args else call_args.kwargs.get("key", "")
            assert "actor_fp_abc123" not in key
            assert "10.0.0.1" not in key
            assert key.startswith("ac:")
