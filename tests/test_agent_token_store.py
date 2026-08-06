"""Tests for AgentTokenStore — atomicity of set_token()'s two Redis writes.

No test coverage existed for this module before this file.
"""

from __future__ import annotations

import pytest

from app.agent_token_store import AgentTokenStore


@pytest.fixture
def store():
    s = AgentTokenStore(redis_url="redis://localhost:6379/0")
    import fakeredis.aioredis

    s._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return s


class TestSetAndValidate:
    @pytest.mark.asyncio
    async def test_validate_correct_token(self, store):
        await store.set_token("tok-123", ttl=60, scopes=["a", "b"], role="agent")
        ok, scopes, meta = await store.validate_token("tok-123")
        assert ok is True
        assert scopes == ["a", "b"]
        assert meta["role"] == "agent"

    @pytest.mark.asyncio
    async def test_validate_wrong_token(self, store):
        await store.set_token("tok-123", ttl=60)
        ok, _, _ = await store.validate_token("wrong")
        assert ok is False

    @pytest.mark.asyncio
    async def test_refresh_invalidates_old_token(self, store):
        await store.set_token("tok-old", ttl=60, scopes=["old"])
        await store.set_token("tok-new", ttl=60, scopes=["new"])
        ok_old, _, _ = await store.validate_token("tok-old")
        ok_new, scopes_new, _ = await store.validate_token("tok-new")
        assert ok_old is False
        assert ok_new is True
        assert scopes_new == ["new"]


class TestSetTokenAtomicity:
    @pytest.mark.asyncio
    async def test_uses_a_single_atomic_pipeline(self, store, monkeypatch):
        """Regression: the class's own docstring claims "atomic ... no race
        conditions", but set_token() issued two independent, unrelated
        `set()` calls (current-hash, then meta) -- two separate round-trips
        with no MULTI/EXEC around them. A reader calling validate_token()
        strictly between the two writes landing would see the NEW token
        hash (so the token check passes) paired with the OLD scopes/role/
        labels -- validating a rotated token under its predecessor's
        permissions for that window.

        A real cross-connection race needs a live Redis server to observe
        (fakeredis's in-process pipeline never actually interleaves with a
        concurrent caller either way), so this verifies the fix at the
        level a unit test actually can: both writes must be queued on one
        `pipeline(transaction=True)` -- MULTI/EXEC -- and sent as exactly
        one `execute()`, which is what removes the two-round-trip window
        entirely rather than just narrowing it.
        """
        real_pipeline = store._redis.pipeline
        pipelines_created: list = []

        def _tracking_pipeline(*args, **kwargs):
            assert kwargs.get("transaction") is True, (
                "set_token() must use an actual MULTI/EXEC transaction "
                "pipeline, not a best-effort batch"
            )
            pipe = real_pipeline(*args, **kwargs)
            pipelines_created.append(pipe)
            return pipe

        monkeypatch.setattr(store._redis, "pipeline", _tracking_pipeline)

        await store.set_token("tok-new", ttl=60, scopes=["new-scope"])

        assert len(pipelines_created) == 1, (
            "set_token() must queue both writes on the SAME pipeline "
            f"(got {len(pipelines_created)} separate pipelines/round-trips)"
        )

        ok, scopes, _ = await store.validate_token("tok-new")
        assert ok is True
        assert scopes == ["new-scope"]
