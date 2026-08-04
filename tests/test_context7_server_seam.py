"""Tests for the Context7 MCP adapter's session lifecycle.

Regression context: no test file existed for this adapter at all — the same
"untested = where the bug hides" pattern found in every other fleet adapter
audited today. _reset_session() used to just nil out the module-level
_session/_exit_stack globals without ever closing the old _exit_stack, which
holds a live `npx @upstash/context7-mcp` child process (via stdio_client).
Every failure that triggered _call_upstream's one-retry logic leaked that
process as an orphan — Node subprocesses are far heavier than a dropped
Python reference, and this runs in a service with no restart.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"))

import fleet.context7_server as ctx7  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_globals():
    """Every test starts and ends with a clean slate — these are real
    module-level globals shared across the whole test file.
    """
    ctx7._session = None
    ctx7._exit_stack = None
    yield
    ctx7._session = None
    ctx7._exit_stack = None


class TestResetSessionClosesTheOldExitStack:
    @pytest.mark.asyncio
    async def test_closes_the_old_stack(self):
        old_stack = AsyncMock()
        ctx7._session = AsyncMock()
        ctx7._exit_stack = old_stack

        await ctx7._reset_session()

        old_stack.aclose.assert_awaited_once()
        assert ctx7._session is None
        assert ctx7._exit_stack is None

    @pytest.mark.asyncio
    async def test_noop_when_nothing_to_close(self):
        """Must not crash on the very first call, before any session exists."""
        assert ctx7._exit_stack is None
        await ctx7._reset_session()
        assert ctx7._session is None
        assert ctx7._exit_stack is None

    @pytest.mark.asyncio
    async def test_swallows_close_errors(self):
        """A broken pipe / already-dead subprocess during cleanup must not
        prevent the reset — the whole point is to recover and reconnect.
        """
        old_stack = AsyncMock()
        old_stack.aclose.side_effect = RuntimeError("pipe already closed")
        ctx7._exit_stack = old_stack

        await ctx7._reset_session()  # must not raise

        assert ctx7._exit_stack is None


class TestCallUpstreamRetryClosesTheOldSession:
    @pytest.mark.asyncio
    async def test_retry_closes_stale_exit_stack_before_reconnecting(self, monkeypatch):
        old_stack = AsyncMock()
        failing_session = AsyncMock()
        failing_session.call_tool.side_effect = RuntimeError("upstream hiccup")
        ctx7._session = failing_session
        ctx7._exit_stack = old_stack

        fresh_session = AsyncMock()
        fresh_result = AsyncMock()
        fresh_result.content = [type("C", (), {"text": "ok"})()]
        fresh_session.call_tool.return_value = fresh_result

        calls = {"n": 0}

        async def _fake_get_session():
            calls["n"] += 1
            if calls["n"] == 1:
                return failing_session
            ctx7._session = fresh_session
            return fresh_session

        monkeypatch.setattr(ctx7, "_get_session", _fake_get_session)

        result = await ctx7._call_upstream("resolve-library-id", {"query": "x", "libraryName": "y"})

        assert result == "ok"
        old_stack.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_second_failure_propagates_without_infinite_retry(self, monkeypatch):
        always_failing = AsyncMock()
        always_failing.call_tool.side_effect = RuntimeError("still down")
        monkeypatch.setattr(ctx7, "_get_session", AsyncMock(return_value=always_failing))

        with pytest.raises(RuntimeError, match="still down"):
            await ctx7._call_upstream("resolve-library-id", {"query": "x", "libraryName": "y"})

        assert always_failing.call_tool.await_count == 2
