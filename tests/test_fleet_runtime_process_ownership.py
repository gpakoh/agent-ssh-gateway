from __future__ import annotations

import asyncio
import importlib
import threading
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

import examples.mcp_server.fleet_runtime as runtime_module
from examples.mcp_server.fleet_runtime import FleetRuntime


@pytest.fixture
def proxy_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
    monkeypatch.setenv("MCP_SCOPE_ENFORCEMENT", "off")
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://public.test")
    import examples.mcp_client_remote.server as server

    return importlib.reload(server)


@pytest.mark.asyncio
async def test_process_lifespan_closes_fleet_once_before_shared_upstream_on_exception(
    proxy_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outer Starlette lifespan is the single process-level owner.

    A transport/session lifespan is intentionally not involved here. Even when
    application execution exits by exception, FleetRuntime is joined first and
    the #117 shared upstream client is closed afterwards, each exactly once.
    """
    events: list[str] = []

    async def startup_upstream() -> None:
        events.append("upstream-start")

    async def close_fleet() -> None:
        events.append("fleet-close")

    async def close_upstream() -> None:
        events.append("upstream-close")

    monkeypatch.setattr(proxy_server, "_startup_upstream_client", startup_upstream)
    monkeypatch.setattr(proxy_server._mcp_mod, "close_fleet_runtime", close_fleet)
    monkeypatch.setattr(proxy_server, "_shutdown_upstream_client", close_upstream)

    with pytest.raises(RuntimeError, match="boom"):
        async with proxy_server._lifespan(proxy_server.proxy_app):
            events.append("body")
            raise RuntimeError("boom")

    assert events == ["upstream-start", "body", "fleet-close", "upstream-close"]


@pytest.mark.asyncio
async def test_process_lifespan_shields_resource_join_from_parent_cancellation(
    proxy_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation of the serving scope cannot orphan process resources."""
    events: list[str] = []
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def startup_upstream() -> None:
        events.append("upstream-start")

    async def close_fleet() -> None:
        events.append("fleet-close-start")
        cleanup_started.set()
        await cleanup_release.wait()
        events.append("fleet-close-done")

    async def close_upstream() -> None:
        events.append("upstream-close")

    monkeypatch.setattr(proxy_server, "_startup_upstream_client", startup_upstream)
    monkeypatch.setattr(proxy_server._mcp_mod, "close_fleet_runtime", close_fleet)
    monkeypatch.setattr(proxy_server, "_shutdown_upstream_client", close_upstream)

    async def serve_until_cancelled() -> None:
        async with proxy_server._lifespan(proxy_server.proxy_app):
            events.append("body")
            await anyio.sleep_forever()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(serve_until_cancelled)
        while "body" not in events:
            await anyio.sleep(0)
        task_group.cancel_scope.cancel()
        await cleanup_started.wait()
        cleanup_release.set()

    assert events == [
        "upstream-start",
        "body",
        "fleet-close-start",
        "fleet-close-done",
        "upstream-close",
    ]


@pytest.mark.asyncio
async def test_close_fleet_runtime_marshals_to_runtime_owner_loop() -> None:
    """Outer process loop never directly awaits owner-loop tasks/asyncpg state."""
    owner_loop = asyncio.new_event_loop()
    loop_started = threading.Event()
    close_calls: list[asyncio.AbstractEventLoop] = []

    class FakeRuntime:
        async def close(self) -> None:
            close_calls.append(asyncio.get_running_loop())

    def run_owner_loop() -> None:
        asyncio.set_event_loop(owner_loop)
        loop_started.set()
        owner_loop.run_forever()

    thread = threading.Thread(target=run_owner_loop, daemon=True)
    thread.start()
    assert loop_started.wait(timeout=1)

    runtime_module._runtime = FakeRuntime()  # type: ignore[assignment]
    runtime_module._runtime_loop = owner_loop
    runtime_module._runtime_lock = None
    try:
        await runtime_module.close_fleet_runtime()
        assert close_calls == [owner_loop]
        assert runtime_module._runtime is None
        assert runtime_module._runtime_loop is None
    finally:
        runtime_module._runtime = None
        runtime_module._runtime_lock = None
        runtime_module._runtime_loop = None
        owner_loop.call_soon_threadsafe(owner_loop.stop)
        thread.join(timeout=1)
        owner_loop.close()


@pytest.mark.asyncio
async def test_fleet_close_waits_for_gateway_executor_before_state_close() -> None:
    """Process shutdown does not abandon an already-running gateway worker thread."""
    state = MagicMock()
    state.close = AsyncMock()
    runtime = FleetRuntime(
        state,
        pool_name="ssh-gateway/sshd",
        capacity=2,
        coordinator_id="gpt-a",
        gateway_io_concurrency=1,
    )
    runtime._schema_ready = True

    worker_started = threading.Event()
    worker_release = threading.Event()

    def blocking_gateway_call() -> dict[str, str]:
        worker_started.set()
        assert worker_release.wait(timeout=2)
        return {"status": "running"}

    gateway_task = asyncio.create_task(runtime._run_gateway_io(blocking_gateway_call))
    assert await asyncio.to_thread(worker_started.wait, 1)

    close_task = asyncio.create_task(runtime.close())
    await asyncio.sleep(0.03)
    assert not close_task.done(), "close must wait for in-flight fleet-gateway work"
    state.close.assert_not_awaited()

    worker_release.set()
    await gateway_task
    await close_task

    state.close.assert_awaited_once()
    assert runtime._closed is True
    assert runtime._watchers_by_job == {}
