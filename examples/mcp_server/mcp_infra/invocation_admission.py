"""Lifetime-bound MCP invocation admission.

FastMCP runs on asyncio. Keep the raw Task.cancel shield and strong task
ownership here; AnyIO cancel scopes alone do not shield direct task
cancellation. The tool registry remains responsible for registration and
dispatches synchronous handlers through AnyIO workers.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from tool_results import tool_error

# This interlock covers every registered MCP invocation in this process,
# including delivery and generic command tools. It is deliberately not a
# claim about REST, other processes, detached jobs or host-side writers.
_EXCLUSIVE_INVOCATION = "candidate_discard_untracked_files"
_OBSERVATION_ONLY = frozenset({"health", "tools_manifest", "gateway_catalog", "gateway_skill"})


class _InvocationAdmission:
    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._shared = 0
        self._exclusive = False

    def try_enter(self, name: str) -> Callable[[], None] | None:
        if name in _OBSERVATION_ONLY:
            return lambda: None
        exclusive = name == _EXCLUSIVE_INVOCATION
        with self._mutex:
            if self._exclusive or (exclusive and self._shared):
                return None
            if exclusive:
                self._exclusive = True
            else:
                self._shared += 1

        def release() -> None:
            with self._mutex:
                if exclusive:
                    self._exclusive = False
                else:
                    self._shared -= 1

        return release


_invocation_admission = _InvocationAdmission()
_inflight_async_calls: set[asyncio.Task[Any]] = set()


def _admission_denied(name: str) -> dict[str, Any]:
    return tool_error(
        tool=name, code="WORKSPACE_CONTENDED",
        message="MCP invocation conflicts with exclusive candidate file maintenance",
        retryable=True,
        details={"admission": "mcp_process", "mutation_occurred": False},
    )


def admitted_sync_call(name: str, call: Callable[[], Any]) -> Any:
    # Acquire/release inside the worker, not around the awaiting transport.
    # Even direct asyncio cancellation cannot release a living thread's lease.
    release = _invocation_admission.try_enter(name)
    if release is None:
        return _admission_denied(name)
    try:
        return call()
    finally:
        release()


async def admitted_async_call(name: str, call: Callable[[], Awaitable[Any]]) -> Any:
    if name in _OBSERVATION_ONLY:
        return await call()
    release = _invocation_admission.try_enter(name)
    if release is None:
        return _admission_denied(name)

    async def invoke() -> Any:
        return await call()

    try:
        task = asyncio.create_task(invoke())
    except BaseException:
        release()
        raise
    _inflight_async_calls.add(task)

    def completed(done: asyncio.Task[Any]) -> None:
        # The transport may have disappeared. Retain a strong task reference
        # and the lease until the real invocation (including its thread work)
        # ends. Retrieve exceptions even when no caller remains to await them.
        try:
            if not done.cancelled():
                done.exception()
        finally:
            _inflight_async_calls.discard(done)
            release()

    task.add_done_callback(completed)
    return await asyncio.shield(task)
