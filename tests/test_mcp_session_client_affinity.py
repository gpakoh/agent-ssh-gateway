from __future__ import annotations

import asyncio
import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest
from mcp.server.lowlevel.server import request_ctx
from mcp.server.session import ServerSession
from mcp.shared.context import RequestContext

from examples.mcp_server.mcp_infra._server_ref import server_module
from examples.mcp_server.mcp_infra.adapters import agent, gateway


@dataclass(eq=False)
class _McpSession:
    name: str


class _Context:
    def __init__(self, session: _McpSession) -> None:
        self.session = session


@pytest.fixture
def live_server() -> Any:
    """Resolve the current facade after tests that deliberately re-import MCP modules."""
    return server_module()


def _base_client(live_server: Any):
    return live_server.GatewayClient(
        base_url="http://gateway.invalid",
        api_key="test-key",
        session_id="seed-session",
        ssh_host="executor.invalid",
        ssh_user="tester",
    )


def test_gateway_client_mutable_session_state_is_scoped_per_mcp_session(
    monkeypatch, live_server
):
    base = _base_client(live_server)
    monkeypatch.setattr(live_server, "client", base)

    current = {"session": _McpSession("a")}
    monkeypatch.setattr(live_server.mcp, "get_context", lambda: _Context(current["session"]))

    client_a = gateway._server_client()
    client_a_again = gateway._server_client()

    current["session"] = _McpSession("b")
    client_b = gateway._server_client()

    assert client_a is client_a_again
    assert client_a is not client_b
    assert client_a is not base
    assert client_b is not base

    client_a.session_id = "session-a"
    client_b.session_id = "session-b"

    assert client_a.session_id == "session-a"
    assert client_b.session_id == "session-b"
    assert base.session_id == "seed-session"


def test_agent_gateway_surface_uses_the_same_request_scoped_gateway_client(
    monkeypatch, live_server
):
    base = _base_client(live_server)
    monkeypatch.setattr(live_server, "client", base)

    current = {"session": _McpSession("a")}
    monkeypatch.setattr(live_server.mcp, "get_context", lambda: _Context(current["session"]))

    assert agent._server_client() is gateway._server_client()

    current["session"] = _McpSession("b")
    assert agent._server_client() is gateway._server_client()
    assert agent._server_client() is not base


def test_no_request_context_preserves_process_global_client_for_compatibility(
    monkeypatch, live_server
):
    base = _base_client(live_server)
    monkeypatch.setattr(live_server, "client", base)

    def _outside_request():
        raise LookupError("no request")

    monkeypatch.setattr(live_server.mcp, "get_context", _outside_request)

    assert gateway._server_client() is base
    assert agent._server_client() is base


def test_context_without_usable_session_falls_back_to_process_global_client(
    monkeypatch, live_server
):
    base = _base_client(live_server)
    monkeypatch.setattr(live_server, "client", base)

    class _NoSessionContext:
        @property
        def session(self):
            raise ValueError("Context is not available outside of a request")

    monkeypatch.setattr(live_server.mcp, "get_context", lambda: _NoSessionContext())

    assert gateway._server_client() is base


def test_missing_sdk_session_attribute_fails_closed(monkeypatch, live_server):
    base = _base_client(live_server)
    monkeypatch.setattr(live_server, "client", base)

    class _BrokenSdkContext:
        pass

    monkeypatch.setattr(live_server.mcp, "get_context", lambda: _BrokenSdkContext())

    with pytest.raises(AttributeError):
        gateway._server_client()


def test_dedicated_agent_executor_client_is_also_scoped_per_mcp_session(
    monkeypatch, live_server
):
    dedicated = _base_client(live_server)
    monkeypatch.setattr(live_server, "agent_client", dedicated)
    monkeypatch.setattr(live_server, "_agent_client_configured", True)

    current = {"session": _McpSession("a")}
    monkeypatch.setattr(live_server.mcp, "get_context", lambda: _Context(current["session"]))

    client_a = agent._server_agent_client()
    current["session"] = _McpSession("b")
    client_b = agent._server_agent_client()

    assert client_a is not client_b
    assert client_a is not dedicated
    assert client_b is not dedicated


def test_scoped_client_preserves_static_default_session_without_sharing_mutation(
    monkeypatch, live_server
):
    base = _base_client(live_server)
    monkeypatch.setattr(live_server, "client", base)
    monkeypatch.setattr(live_server.mcp, "get_context", lambda: _Context(_McpSession("a")))

    scoped = gateway._server_client()

    assert scoped.session_id == "seed-session"
    scoped.session_id = "reconnected-session-a"
    assert base.session_id == "seed-session"


def test_sdk_server_session_identity_is_hashable_and_weakrefable():
    session = object.__new__(ServerSession)

    assert hash(session)
    assert weakref.ref(session)() is session


def test_resolver_reads_the_real_fastmcp_request_contextvar(monkeypatch, live_server):
    base = _base_client(live_server)
    monkeypatch.setattr(live_server, "client", base)
    session = _McpSession("sdk-context")
    token = request_ctx.set(
        RequestContext(
            request_id="request-1",
            meta=None,
            session=session,
            lifespan_context={},
        )
    )
    try:
        assert live_server._current_mcp_session() is session
        assert gateway._server_client() is not base
    finally:
        request_ctx.reset(token)


def test_request_context_affinity_survives_asyncio_worker_thread(monkeypatch, live_server):
    base = _base_client(live_server)
    monkeypatch.setattr(live_server, "client", base)
    session = _McpSession("thread-context")
    token = request_ctx.set(
        RequestContext(
            request_id="request-thread",
            meta=None,
            session=session,
            lifespan_context={},
        )
    )

    async def _resolve_in_worker():
        return await asyncio.to_thread(gateway._server_client)

    try:
        scoped = asyncio.run(_resolve_in_worker())
        assert scoped is not base
        assert scoped is gateway._server_client()
    finally:
        request_ctx.reset(token)


def test_session_pool_is_weak_and_releases_closed_mcp_session_keys(live_server):
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    mcp_session = _McpSession("short-lived")
    session_ref = weakref.ref(mcp_session)

    assert pool.get(base, mcp_session) is not base
    assert len(pool._clients) == 1

    del mcp_session
    gc.collect()

    assert session_ref() is None
    assert len(pool._clients) == 0


def test_concurrent_resolution_for_one_mcp_session_returns_one_scoped_client(live_server):
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    mcp_session = _McpSession("shared")

    with ThreadPoolExecutor(max_workers=8) as executor:
        clients = list(executor.map(lambda _: pool.get(base, mcp_session), range(32)))

    first = clients[0]
    assert first is not base
    assert all(item is first for item in clients)


def test_unsupported_session_key_fails_closed_instead_of_sharing_global_state(live_server):
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)

    with pytest.raises(TypeError):
        pool.get(base, [])


@pytest.mark.parametrize("adapter_getter", [gateway._server_client, agent._server_client])
def test_non_gatewayclient_test_double_is_preserved(monkeypatch, live_server, adapter_getter):
    marker = object()
    monkeypatch.setattr(live_server, "client", marker)
    monkeypatch.setattr(live_server.mcp, "get_context", lambda: _Context(_McpSession("a")))

    assert adapter_getter() is marker
