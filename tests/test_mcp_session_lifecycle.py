from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from examples.mcp_server.mcp_infra._server_ref import server_module


@dataclass(eq=False)
class _McpSession:
    name: str


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def live_server() -> Any:
    return server_module()


def _base_client(live_server: Any) -> Any:
    return live_server.GatewayClient(
        base_url="http://gateway.invalid",
        api_key="test-key",
        session_id="seed-session",
        ssh_host="executor.invalid",
        ssh_user="tester",
    )


def test_scoped_reconnect_never_requests_reuse_of_another_logical_sid(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    base = _base_client(live_server)
    scoped = base.fork_session()
    seen_payloads: list[dict[str, Any]] = []

    def fake_post(_url: str, **kwargs: Any) -> _Response:
        seen_payloads.append(kwargs["json"])
        return _Response({"session_id": "owned-session"})

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    assert scoped.connect() == "owned-session"
    assert seen_payloads == [
        {
            "host": "executor.invalid",
            "port": 22,
            "username": "tester",
            "reuse_existing": False,
        }
    ]


def test_release_does_not_disconnect_borrowed_seed_but_disconnects_owned_sid_once(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    base = _base_client(live_server)
    scoped = base.fork_session()
    disconnects: list[str] = []

    def fake_post(url: str, **kwargs: Any) -> _Response:
        payload = kwargs["json"]
        if url.endswith("/api/ssh/connect"):
            return _Response({"session_id": "owned-session"})
        if url.endswith("/api/ssh/disconnect"):
            disconnects.append(payload["session_id"])
            return _Response({"status": "disconnected"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    scoped.release()
    assert disconnects == []
    assert base.session_id == "seed-session"

    scoped = base.fork_session()
    assert scoped.connect() == "owned-session"
    scoped.release()
    scoped.release()

    assert disconnects == ["owned-session"]
    assert scoped.session_id == ""
    assert base.session_id == "seed-session"


def test_pool_explicit_owner_release_is_deterministic_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    owner = object()
    mcp_session = _McpSession("one")
    disconnects: list[str] = []

    scoped = pool.get(base, mcp_session, owner)
    scoped.session_id = "owned-once"
    scoped._owns_session = True

    def fake_post(_path: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        disconnects.append(payload["session_id"])
        return {"status": "disconnected"}

    monkeypatch.setattr(scoped, "_post", fake_post)

    assert pool.release_owner(owner) == 1
    assert disconnects == ["owned-once"]
    assert len(pool._clients) == 0
    assert scoped._released is True
    assert scoped.session_id == ""
    assert pool.release_owner(owner) == 0
    assert disconnects == ["owned-once"]


def test_two_scoped_clients_reconnect_from_same_stale_seed_to_distinct_sids(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    base = _base_client(live_server)
    first = base.fork_session()
    second = base.fork_session()
    issued = iter(["sid-a", "sid-b"])

    def fake_post(_url: str, **kwargs: Any) -> _Response:
        assert kwargs["json"]["reuse_existing"] is False
        return _Response({"session_id": next(issued)})

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    assert first.connect() == "sid-a"
    assert second.connect() == "sid-b"
    assert first.session_id != second.session_id
    assert base.session_id == "seed-session"



def test_repeated_scoped_connect_releases_superseded_owned_sid(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    base = _base_client(live_server)
    scoped = base.fork_session()
    issued = iter(["sid-one", "sid-two"])
    disconnects: list[str] = []

    def fake_post(url: str, **kwargs: Any) -> _Response:
        payload = kwargs["json"]
        if url.endswith("/api/ssh/connect"):
            assert payload["reuse_existing"] is False
            return _Response({"session_id": next(issued)})
        if url.endswith("/api/ssh/disconnect"):
            disconnects.append(payload["session_id"])
            return _Response({"status": "disconnected"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    assert scoped.connect() == "sid-one"
    assert scoped.connect() == "sid-two"
    assert disconnects == ["sid-one"]

    scoped.release()
    assert disconnects == ["sid-one", "sid-two"]


def test_lifecycle_release_uses_dedicated_short_network_timeout(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    monkeypatch.setenv("MCP_GATEWAY_RELEASE_HTTP_TIMEOUT", "0.25")
    base = _base_client(live_server)
    scoped = base.fork_session()
    disconnect_timeouts: list[float] = []

    def fake_post(url: str, **kwargs: Any) -> _Response:
        if url.endswith("/api/ssh/connect"):
            return _Response({"session_id": "owned-session"})
        if url.endswith("/api/ssh/disconnect"):
            disconnect_timeouts.append(float(kwargs["timeout"]))
            raise httpx.ReadTimeout("gateway unavailable")
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    assert scoped.connect() == "owned-session"
    scoped.release()

    assert disconnect_timeouts == [0.25]
    assert scoped.session_id == ""
    assert base.session_id == "seed-session"


def test_proactive_connect_capacity_error_preserves_working_owned_sid(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    base = _base_client(live_server)
    scoped = base.fork_session()
    connect_attempts = 0
    disconnects: list[str] = []

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal connect_attempts
        if url.endswith("/api/ssh/connect"):
            connect_attempts += 1
            if connect_attempts == 1:
                return _Response({"session_id": "working-sid"})
            return _Response({"detail": {"message": "session limit"}}, 429)
        if url.endswith("/api/ssh/disconnect"):
            disconnects.append(kwargs["json"]["session_id"])
            return _Response({"status": "disconnected"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    assert scoped.connect() == "working-sid"
    with pytest.raises(live_server.GatewayClientError, match="429"):
        scoped.connect()

    assert scoped.session_id == "working-sid"
    assert scoped._owns_session is True
    assert disconnects == []


def _attach_owned_client(
    live_server: Any,
    pool: Any,
    owner: object,
    sid: str,
) -> tuple[Any, _McpSession]:
    base = _base_client(live_server)
    mcp_session = _McpSession(sid)
    scoped = pool.get(base, mcp_session, owner)
    scoped.session_id = sid
    scoped._owns_session = True
    scoped._release_http_timeout = 0.05
    return scoped, mcp_session


@pytest.mark.asyncio
async def test_lifespan_deadline_leaves_no_orphan_cleanup_side_effect(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    gateway_pool = live_server.GatewayClientSessionPool()
    agent_pool = live_server.GatewayClientSessionPool()
    started = asyncio.Event()
    late_side_effects: list[str] = []

    async def delayed_post(
        _client: Any, _path: str, payload: dict[str, Any], *, timeout: float | int
    ) -> dict[str, Any]:
        assert payload["session_id"] == "owned-gateway"
        assert timeout == 1.0
        started.set()
        await asyncio.sleep(0.25)
        late_side_effects.append("late-disconnect")
        return {"status": "disconnected"}

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(live_server, "_gateway_client_sessions", gateway_pool)
    monkeypatch.setattr(live_server, "_agent_client_sessions", agent_pool)
    monkeypatch.setattr(live_server, "_MCP_SESSION_RELEASE_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(live_server, "close_fleet_runtime", _noop_close)
    monkeypatch.setattr(live_server.GatewayClient, "_post_async", delayed_post)

    started_at = asyncio.get_running_loop().time()
    async with live_server._mcp_lifespan(live_server.mcp) as owner:
        scoped, keepalive = _attach_owned_client(
            live_server, gateway_pool, owner, "owned-gateway"
        )
        scoped._release_http_timeout = 1.0
        assert keepalive is not None
    elapsed = asyncio.get_running_loop().time() - started_at

    assert started.is_set()
    assert elapsed < 0.2
    assert len(gateway_pool._clients) == 0
    assert scoped._released is True
    assert scoped.session_id == ""
    assert scoped._owns_session is False
    assert late_side_effects == []

    await asyncio.sleep(0.35)
    assert late_side_effects == []


@pytest.mark.asyncio
async def test_lifespan_network_timeout_has_no_late_disconnect(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    gateway_pool = live_server.GatewayClientSessionPool()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    disconnects: list[str] = []

    async def hanging_post(
        _client: Any, _path: str, payload: dict[str, Any], *, timeout: float | int
    ) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.sleep(1.0)
        finally:
            cancelled.set()
        disconnects.append(payload["session_id"])
        return {}

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(live_server, "_gateway_client_sessions", gateway_pool)
    monkeypatch.setattr(
        live_server, "_agent_client_sessions", live_server.GatewayClientSessionPool()
    )
    monkeypatch.setattr(live_server, "_MCP_SESSION_RELEASE_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(live_server, "close_fleet_runtime", _noop_close)
    monkeypatch.setattr(live_server.GatewayClient, "_post_async", hanging_post)

    async with live_server._mcp_lifespan(live_server.mcp) as owner:
        scoped, keepalive = _attach_owned_client(
            live_server, gateway_pool, owner, "timeout-sid"
        )
        assert keepalive is not None

    assert started.is_set()
    assert cancelled.is_set()
    assert disconnects == []
    assert scoped._released is True
    await asyncio.sleep(0.1)
    assert disconnects == []


@pytest.mark.asyncio
async def test_lifespan_two_pools_one_success_one_timeout(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    gateway_pool = live_server.GatewayClientSessionPool()
    agent_pool = live_server.GatewayClientSessionPool()
    disconnects: list[str] = []
    agent_cancelled = asyncio.Event()

    async def mixed_post(
        _client: Any, _path: str, payload: dict[str, Any], *, timeout: float | int
    ) -> dict[str, Any]:
        sid = payload["session_id"]
        if sid == "normal-sid":
            disconnects.append(sid)
            return {"status": "disconnected"}
        try:
            await asyncio.sleep(1.0)
        finally:
            agent_cancelled.set()
        disconnects.append(sid)
        return {}

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(live_server, "_gateway_client_sessions", gateway_pool)
    monkeypatch.setattr(live_server, "_agent_client_sessions", agent_pool)
    monkeypatch.setattr(live_server, "_MCP_SESSION_RELEASE_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(live_server, "close_fleet_runtime", _noop_close)
    monkeypatch.setattr(live_server.GatewayClient, "_post_async", mixed_post)

    async with live_server._mcp_lifespan(live_server.mcp) as owner:
        normal, keep_normal = _attach_owned_client(
            live_server, gateway_pool, owner, "normal-sid"
        )
        agent, keep_agent = _attach_owned_client(
            live_server, agent_pool, owner, "agent-sid"
        )
        assert keep_normal is not None and keep_agent is not None

    assert disconnects == ["normal-sid"]
    assert agent_cancelled.is_set()
    assert normal._released and agent._released
    assert len(gateway_pool._clients) == 0
    assert len(agent_pool._clients) == 0


@pytest.mark.asyncio
async def test_lifespan_cancellation_during_release_is_bounded_and_joined(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    gateway_pool = live_server.GatewayClientSessionPool()
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()
    late_effects: list[str] = []

    async def cancellable_post(
        _client: Any, _path: str, payload: dict[str, Any], *, timeout: float | int
    ) -> dict[str, Any]:
        cleanup_started.set()
        try:
            await asyncio.sleep(1.0)
        finally:
            cleanup_cancelled.set()
        late_effects.append(payload["session_id"])
        return {}

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(live_server, "_gateway_client_sessions", gateway_pool)
    monkeypatch.setattr(
        live_server, "_agent_client_sessions", live_server.GatewayClientSessionPool()
    )
    monkeypatch.setattr(live_server, "_MCP_SESSION_RELEASE_DEADLINE_SECONDS", 0.08)
    monkeypatch.setattr(live_server, "close_fleet_runtime", _noop_close)
    monkeypatch.setattr(live_server.GatewayClient, "_post_async", cancellable_post)

    async def run_lifecycle() -> None:
        async with live_server._mcp_lifespan(live_server.mcp) as owner:
            scoped, keepalive = _attach_owned_client(
                live_server, gateway_pool, owner, "cancel-sid"
            )
            scoped._release_http_timeout = 1.0
            assert keepalive is not None

    task = asyncio.create_task(run_lifecycle())
    await cleanup_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleanup_cancelled.is_set()
    assert late_effects == []
    await asyncio.sleep(0.15)
    assert late_effects == []
    assert len(gateway_pool._clients) == 0


@pytest.mark.asyncio
async def test_lifespan_completion_near_deadline_has_single_effect(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    gateway_pool = live_server.GatewayClientSessionPool()
    disconnects: list[str] = []

    async def near_deadline_post(
        _client: Any, _path: str, payload: dict[str, Any], *, timeout: float | int
    ) -> dict[str, Any]:
        await asyncio.sleep(0.035)
        disconnects.append(payload["session_id"])
        return {}

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(live_server, "_gateway_client_sessions", gateway_pool)
    monkeypatch.setattr(
        live_server, "_agent_client_sessions", live_server.GatewayClientSessionPool()
    )
    monkeypatch.setattr(live_server, "_MCP_SESSION_RELEASE_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(live_server, "close_fleet_runtime", _noop_close)
    monkeypatch.setattr(live_server.GatewayClient, "_post_async", near_deadline_post)

    async with live_server._mcp_lifespan(live_server.mcp) as owner:
        scoped, keepalive = _attach_owned_client(
            live_server, gateway_pool, owner, "boundary-sid"
        )
        scoped._release_http_timeout = 1.0
        assert keepalive is not None

    assert disconnects == ["boundary-sid"]
    await asyncio.sleep(0.08)
    assert disconnects == ["boundary-sid"]


@pytest.mark.asyncio
async def test_old_lifecycle_cleanup_cannot_touch_next_transport(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    gateway_pool = live_server.GatewayClientSessionPool()
    disconnects: list[str] = []

    async def controlled_post(
        _client: Any, _path: str, payload: dict[str, Any], *, timeout: float | int
    ) -> dict[str, Any]:
        sid = payload["session_id"]
        if sid == "old-sid":
            await asyncio.sleep(0.2)
        disconnects.append(sid)
        return {}

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(live_server, "_gateway_client_sessions", gateway_pool)
    monkeypatch.setattr(
        live_server, "_agent_client_sessions", live_server.GatewayClientSessionPool()
    )
    monkeypatch.setattr(live_server, "_MCP_SESSION_RELEASE_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(live_server, "close_fleet_runtime", _noop_close)
    monkeypatch.setattr(live_server.GatewayClient, "_post_async", controlled_post)

    async with live_server._mcp_lifespan(live_server.mcp) as old_owner:
        old_scoped, old_key = _attach_owned_client(
            live_server, gateway_pool, old_owner, "old-sid"
        )
        old_scoped._release_http_timeout = 1.0
        assert old_key is not None

    async with live_server._mcp_lifespan(live_server.mcp) as new_owner:
        new_scoped, new_key = _attach_owned_client(
            live_server, gateway_pool, new_owner, "new-sid"
        )
        new_scoped._release_http_timeout = 1.0
        assert new_key is not None
        assert new_scoped.session_id == "new-sid"
        await asyncio.sleep(0.25)
        assert new_scoped.session_id == "new-sid"
        assert new_scoped._released is False
        assert "old-sid" not in disconnects

    assert disconnects == ["new-sid"]


def test_prepare_release_is_idempotent_and_never_returns_borrowed_seed(
    live_server: Any,
) -> None:
    borrowed = _base_client(live_server).fork_session()
    assert borrowed.prepare_release() == ""
    assert borrowed.prepare_release() == ""

    owned = _base_client(live_server).fork_session()
    owned.session_id = "owned-once"
    owned._owns_session = True
    assert owned.prepare_release() == "owned-once"
    assert owned.prepare_release() == ""
    assert owned.session_id == ""
    assert owned._released is True
    assert owned._owns_session is False
