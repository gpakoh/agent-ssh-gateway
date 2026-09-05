from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from examples.mcp_server.mcp_infra._server_ref import server_module


@dataclass(eq=False)
class _McpSession:
    name: str


@pytest.fixture
def live_server() -> Any:
    return server_module()


def _base_client(live_server: Any) -> Any:
    return live_server.GatewayClient(
        base_url="http://gateway.invalid",
        api_key="test-key",
        session_id="seed-session",
        ssh_host="executor.invalid",
        ssh_port=22,
        ssh_user="tester",
        ssh_password="",
        ssh_private_key="",
        ssh_key_path="",
    )


def _mark_owned(client: Any, sid: str) -> None:
    client.session_id = sid
    client._owns_session = True


def test_sequential_transports_reuse_same_owned_logical_sid(live_server: Any) -> None:
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    first_owner = object()
    first_session = _McpSession("first")

    first = pool.get(base, first_session, first_owner, reuse_key="auth-a")
    _mark_owned(first, "sid-a")

    assert pool.detach_owner(first_owner) == []
    assert first._released is False
    assert first.session_id == "sid-a"
    assert len(pool._idle) == 1

    second_owner = object()
    second = pool.get(
        base,
        _McpSession("second"),
        second_owner,
        reuse_key="auth-a",
    )

    assert second is first
    assert second.session_id == "sid-a"
    assert len(pool._idle) == 0


def test_parallel_transports_same_auth_never_share_active_client(live_server: Any) -> None:
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)

    first = pool.get(base, _McpSession("a"), object(), reuse_key="auth-a")
    second = pool.get(base, _McpSession("b"), object(), reuse_key="auth-a")
    _mark_owned(first, "sid-a")
    _mark_owned(second, "sid-b")

    assert first is not second
    assert first.session_id != second.session_id


def test_idle_client_is_not_reused_across_auth_keys(live_server: Any) -> None:
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    first_owner = object()
    first_session = _McpSession("first")
    first = pool.get(base, first_session, first_owner, reuse_key="auth-a")
    _mark_owned(first, "sid-a")
    assert pool.detach_owner(first_owner) == []

    second_session = _McpSession("second")
    second = pool.get(base, second_session, object(), reuse_key="auth-b")

    assert second is not first
    assert second.session_id == ""
    assert len(pool._idle) == 1


def test_retired_sid_debt_is_never_returned_to_reuse_pool(live_server: Any) -> None:
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    owner = object()
    mcp_session = _McpSession("debt")
    scoped = pool.get(base, mcp_session, owner, reuse_key="auth-a")
    _mark_owned(scoped, "current-sid")
    scoped._retired.add("retired-sid")

    detached = pool.detach_owner(owner)

    assert len(pool._idle) == 0
    assert len(detached) == 1
    assert detached[0][1].all_sids == {"current-sid", "retired-sid"}
    assert scoped._released is True


def test_idle_pool_bound_evicts_oldest_owned_sid(
    monkeypatch: pytest.MonkeyPatch,
    live_server: Any,
) -> None:
    monkeypatch.setenv("MCP_GATEWAY_REUSABLE_SESSION_IDLE_LIMIT", "1")
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)

    first_owner = object()
    first_session = _McpSession("first")
    first = pool.get(base, first_session, first_owner, reuse_key="auth-a")
    _mark_owned(first, "sid-a")
    assert pool.detach_owner(first_owner) == []

    second_owner = object()
    second_session = _McpSession("second")
    second = pool.get(base, second_session, second_owner, reuse_key="auth-b")
    _mark_owned(second, "sid-b")
    detached = pool.detach_owner(second_owner)

    assert len(pool._idle) == 1
    assert pool._idle[0][1] is second
    assert len(detached) == 1
    assert detached[0][0] is first
    assert detached[0][1].all_sids == {"sid-a"}
    assert first._released is True


def test_auth_reuse_key_is_stable_and_secret_not_exposed(
    monkeypatch: pytest.MonkeyPatch,
    live_server: Any,
) -> None:
    import mcp.server.auth.middleware.auth_context as auth_context

    token = SimpleNamespace(token="super-secret-bearer", client_id="chatgpt", scopes=[])
    monkeypatch.setattr(auth_context, "get_access_token", lambda: token)

    first = live_server._current_auth_reuse_key()
    second = live_server._current_auth_reuse_key()

    assert first == second
    assert first is not None
    assert "super-secret-bearer" not in first
    assert len(first) == 64

    monkeypatch.setattr(
        auth_context,
        "get_access_token",
        lambda: SimpleNamespace(token="different-bearer", client_id="chatgpt", scopes=[]),
    )
    assert live_server._current_auth_reuse_key() == first

    monkeypatch.setattr(
        auth_context,
        "get_access_token",
        lambda: SimpleNamespace(token="different-bearer", client_id="other-client", scopes=[]),
    )
    assert live_server._current_auth_reuse_key() != first

    monkeypatch.setattr(
        auth_context,
        "get_access_token",
        lambda: SimpleNamespace(token="static-a", client_id="mcp_static", scopes=[]),
    )
    static_key = live_server._current_auth_reuse_key()
    monkeypatch.setattr(
        auth_context,
        "get_access_token",
        lambda: SimpleNamespace(token="static-b", client_id="mcp_static", scopes=[]),
    )
    assert live_server._current_auth_reuse_key() != static_key


@pytest.mark.asyncio
async def test_production_lifespan_returns_authenticated_sid_to_idle_pool(
    monkeypatch: pytest.MonkeyPatch,
    live_server: Any,
) -> None:
    gateway_pool = live_server.GatewayClientSessionPool()
    agent_pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    disconnects: list[str] = []

    async def tracking_post(
        _client: Any,
        _path: str,
        payload: dict[str, Any],
        *,
        timeout: float | int,
    ) -> dict[str, Any]:
        assert timeout > 0
        disconnects.append(payload["session_id"])
        return {"status": "disconnected"}

    async def noop_close() -> None:
        return None

    monkeypatch.setattr(live_server, "_gateway_client_sessions", gateway_pool)
    monkeypatch.setattr(live_server, "_agent_client_sessions", agent_pool)
    monkeypatch.setattr(live_server, "close_fleet_runtime", noop_close)
    monkeypatch.setattr(live_server.GatewayClient, "_post_async", tracking_post)

    first_session = _McpSession("first")
    async with live_server._mcp_lifespan(live_server.mcp) as first_owner:
        first = gateway_pool.get(
            base,
            first_session,
            first_owner,
            reuse_key="auth-a",
        )
        _mark_owned(first, "stable-sid")

    assert disconnects == []
    assert len(gateway_pool._idle) == 1
    assert first._released is False

    second_session = _McpSession("second")
    async with live_server._mcp_lifespan(live_server.mcp) as second_owner:
        second = gateway_pool.get(
            base,
            second_session,
            second_owner,
            reuse_key="auth-a",
        )
        assert second is first
        assert second.session_id == "stable-sid"

    assert disconnects == []
    assert len(gateway_pool._idle) == 1

    drained = gateway_pool.drain_idle()
    assert len(drained) == 1
    assert drained[0][1].all_sids == {"stable-sid"}


@pytest.mark.asyncio
async def test_authenticated_idle_pool_keeps_finite_gateway_idle_policy(
    monkeypatch: pytest.MonkeyPatch,
    live_server: Any,
) -> None:
    gateway_pool = live_server.GatewayClientSessionPool()
    agent_pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    connect_payloads: list[dict[str, Any]] = []

    class _Response:
        status_code = 200
        text = "ok"

        @staticmethod
        def json() -> dict[str, str]:
            return {"session_id": "stable-reusable-sid"}

    def fake_post(_url: str, **kwargs: Any) -> _Response:
        connect_payloads.append(kwargs["json"])
        return _Response()

    async def noop_close() -> None:
        return None

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)
    monkeypatch.setattr(live_server, "_gateway_client_sessions", gateway_pool)
    monkeypatch.setattr(live_server, "_agent_client_sessions", agent_pool)
    monkeypatch.setattr(live_server, "close_fleet_runtime", noop_close)
    now = [1_000.0]
    monkeypatch.setattr("gateway_client.time.time", lambda: now[0])

    first_session = _McpSession("first-reusable")
    async with live_server._mcp_lifespan(live_server.mcp) as first_owner:
        first = gateway_pool.get(
            base,
            first_session,
            first_owner,
            reuse_key="auth-a",
        )
        assert first.connect() == "stable-reusable-sid"

    assert connect_payloads == [
        {
            "host": "executor.invalid",
            "port": 22,
            "username": "tester",
            "reuse_existing": False,
            "ephemeral": True,
            "idle_timeout_seconds": 300,
        }
    ]
    assert len(gateway_pool._idle) == 1
    assert gateway_pool._idle[0][1] is first
    assert first.session_id == "stable-reusable-sid"
    assert not any(
        "heartbeat" in repr(task.get_coro()).lower()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )

    # The local reusable pool keeps the object for auth-key reuse, but the remote
    # gateway owns the 300-second SID TTL. Advancing only this local fake clock
    # therefore must not mutate pool membership by itself.
    now[0] += 301
    second_session = _McpSession("second-reusable")
    async with live_server._mcp_lifespan(live_server.mcp) as second_owner:
        second = gateway_pool.get(
            base,
            second_session,
            second_owner,
            reuse_key="auth-a",
        )
        assert second is first
        assert second.session_id == "stable-reusable-sid"

    assert len(connect_payloads) == 1


def test_production_client_resolvers_forward_authenticated_reuse_key(
    monkeypatch: pytest.MonkeyPatch,
    live_server: Any,
) -> None:
    session = _McpSession("wire")
    owner = object()
    gateway_base = _base_client(live_server)
    agent_base = _base_client(live_server)
    calls: list[tuple[Any, Any, Any, str | None]] = []

    class _Pool:
        def __init__(self, result: str) -> None:
            self.result = result

        def get(
            self,
            base: Any,
            mcp_session: Any,
            lifecycle_owner: Any,
            reuse_key: str | None = None,
        ) -> str:
            calls.append((base, mcp_session, lifecycle_owner, reuse_key))
            return self.result

    monkeypatch.setattr(live_server, "client", gateway_base)
    monkeypatch.setattr(live_server, "agent_client", agent_base)
    monkeypatch.setattr(live_server, "_agent_client_configured", True)
    monkeypatch.setattr(live_server, "_gateway_client_sessions", _Pool("gateway"))
    monkeypatch.setattr(live_server, "_agent_client_sessions", _Pool("agent"))
    monkeypatch.setattr(live_server, "_current_mcp_session", lambda: session)
    monkeypatch.setattr(live_server, "_current_mcp_lifecycle_owner", lambda: owner)
    monkeypatch.setattr(live_server, "_current_auth_reuse_key", lambda: "auth-key")

    assert live_server.get_gateway_client() == "gateway"
    assert live_server.get_agent_client() == "agent"
    assert calls == [
        (gateway_base, session, owner, "auth-key"),
        (agent_base, session, owner, "auth-key"),
    ]


def test_parallel_authenticated_transports_do_not_share_borrowed_seed(
    monkeypatch: pytest.MonkeyPatch,
    live_server: Any,
) -> None:
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    issued = iter(["owned-a", "owned-b"])

    class _Response:
        status_code = 200
        text = "ok"

        def __init__(self, sid: str) -> None:
            self._sid = sid

        def json(self) -> dict[str, str]:
            return {"session_id": self._sid}

    def fake_post(_url: str, **_kwargs: Any) -> _Response:
        return _Response(next(issued))

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)
    first_session = _McpSession("parallel-a")
    second_session = _McpSession("parallel-b")
    first = pool.get(base, first_session, object(), reuse_key="auth-a")
    second = pool.get(base, second_session, object(), reuse_key="auth-a")

    assert first.session_id == ""
    assert second.session_id == ""
    assert first.connect() == "owned-a"
    assert second.connect() == "owned-b"
