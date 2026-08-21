from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from scripts.mcp_streamable_http_route_probe import (
    build_streamable_http_app,
    find_free_port,
    run_ephemeral_server,
    stop_ephemeral_server,
)


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeGateway:
    """Thread-safe logical-session model for the MCP lifecycle integration tests."""

    def __init__(self, max_active: int = 10) -> None:
        self._lock = threading.Lock()
        self.max_active = max_active
        self.next_sid = 0
        self.active: dict[str, str] = {}
        self.disconnects: list[str] = []
        self.connect_reuse_flags: list[bool] = []
        self.peak_active = 0
        self._stale_on_execute: set[str] = set()

    def post(self, url: str, **kwargs: Any) -> _Response:
        payload = kwargs["json"]
        if url.endswith("/api/ssh/connect"):
            return self._connect(payload)
        if url.endswith("/api/ssh/disconnect"):
            return self._disconnect(payload)
        if url.endswith("/api/ssh/execute-argv"):
            return self._execute(payload)
        raise AssertionError(f"unexpected fake gateway URL: {url}")

    async def post_async(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float | int,
    ) -> dict[str, Any]:
        assert path == "/api/ssh/disconnect"
        assert timeout > 0
        response = self._disconnect(payload)
        if response.status_code >= 400:
            raise RuntimeError(f"disconnect failed: {response.status_code}")
        return response.json()

    def _connect(self, payload: dict[str, Any]) -> _Response:
        reuse = bool(payload.get("reuse_existing"))
        with self._lock:
            self.connect_reuse_flags.append(reuse)
            if reuse and self.active:
                return _Response({"session_id": next(iter(self.active))})
            if len(self.active) >= self.max_active:
                return _Response({"detail": {"message": "session limit"}}, 429)
            self.next_sid += 1
            sid = f"gateway-sid-{self.next_sid}"
            self.active[sid] = ""
            self.peak_active = max(self.peak_active, len(self.active))
            return _Response({"session_id": sid})

    def _disconnect(self, payload: dict[str, Any]) -> _Response:
        sid = payload["session_id"]
        with self._lock:
            if sid not in self.active:
                return _Response({"detail": {"message": "SESSION_NOT_FOUND"}}, 404)
            self.active.pop(sid)
            self.disconnects.append(sid)
            return _Response({"status": "disconnected"})

    def _execute(self, payload: dict[str, Any]) -> _Response:
        sid = payload["session_id"]
        argv = payload["argv"]
        with self._lock:
            if sid in self._stale_on_execute:
                self._stale_on_execute.remove(sid)
                self.active.pop(sid, None)
                return _Response({"detail": {"message": "SESSION_NOT_FOUND"}}, 404)
            if sid not in self.active:
                return _Response({"detail": {"message": "SESSION_NOT_FOUND"}}, 404)
            if argv[:2] == ["marker", "set"]:
                self.active[sid] = argv[2]
            marker = self.active[sid]
            return _Response(
                {
                    "stdout": marker,
                    "stderr": "",
                    "exit_code": 0,
                    "duration": 0.001,
                }
            )

    def stale(self, sid: str) -> None:
        with self._lock:
            self.active.pop(sid, None)

    def stale_on_next_execute(self, sid: str) -> None:
        with self._lock:
            self._stale_on_execute.add(sid)

    def active_sids(self) -> set[str]:
        with self._lock:
            return set(self.active)


async def _wait_for_active_sids(fake: _FakeGateway, expected: set[str], timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if fake.active_sids() == expected:
            return
        await asyncio.sleep(0.01)
    assert fake.active_sids() == expected


def _structured(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None) or []
    assert content
    return json.loads(content[0].text)


async def _open_mcp_client(
    stack: AsyncExitStack, url: str
) -> tuple[ClientSession, Any]:
    http_client = await stack.enter_async_context(httpx.AsyncClient(timeout=10.0))
    read, write, get_sid = await stack.enter_async_context(
        streamable_http_client(url, http_client=http_client)
    )
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session, get_sid


async def _probe(
    session: ClientSession, action: str, marker: str | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {"action": action}
    if marker is not None:
        args["marker"] = marker
    return _structured(await session.call_tool("_test_session_lifecycle_probe", args))


@pytest.mark.asyncio
async def test_five_real_mcp_transports_reconnect_and_disconnect_without_sid_cross_contamination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TEST-11: five real Streamable HTTP transports with selective lifecycle faults."""
    fake = _FakeGateway(max_active=10)
    monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
    monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
    monkeypatch.setenv("MCP_ACCESS_PROFILE", "mcp_client_safe")
    monkeypatch.setattr("gateway_client.httpx.post", fake.post)
    monkeypatch.setattr("gateway_client.GatewayClient._post_async", fake.post_async)

    app = build_streamable_http_app()
    import examples.mcp_server.server as srv

    monkeypatch.setattr(
        srv,
        "client",
        srv.GatewayClient(
            base_url="http://fake-gateway.invalid",
            api_key="test-key",
            session_id="seed-session",
            ssh_host="executor.invalid",
            ssh_user="tester",
        ),
    )

    @srv.mcp.tool(name="_test_session_lifecycle_probe")
    def _test_session_lifecycle_probe(
        action: str, marker: str | None = None
    ) -> dict[str, Any]:
        scoped = srv.get_gateway_client()
        if action == "connect":
            scoped.connect()
            scoped._test_marker = marker
            scoped.execute_argv(["marker", "set", marker or ""])
        elif action == "execute":
            scoped.execute_argv(["marker", "get"])
        else:
            raise ValueError(action)
        return {
            "client_identity": id(scoped),
            "session_id": scoped.session_id,
            "marker": getattr(scoped, "_test_marker", None),
        }

    host = "127.0.0.1"
    port = find_free_port()
    server, thread = run_ephemeral_server(app, host, port)
    url = f"http://{host}:{port}/mcp"
    try:
        async with AsyncExitStack() as outer:
            clients: list[ClientSession] = []
            wire_sids: list[str] = []
            for _ in range(4):
                client_session, get_sid = await _open_mcp_client(outer, url)
                clients.append(client_session)
                wire_sid = get_sid()
                assert wire_sid
                wire_sids.append(wire_sid)

            fifth = AsyncExitStack()
            client_e, get_sid_e = await _open_mcp_client(fifth, url)
            clients.append(client_e)
            wire_sid_e = get_sid_e()
            assert wire_sid_e
            wire_sids.append(wire_sid_e)
            assert len(set(wire_sids)) == 5

            initial = await asyncio.gather(
                *(_probe(client, "connect", f"marker-{index}") for index, client in enumerate(clients))
            )
            initial_sids = [item["session_id"] for item in initial]
            assert len(set(initial_sids)) == 5
            assert [item["marker"] for item in initial] == [f"marker-{i}" for i in range(5)]
            assert fake.connect_reuse_flags == [False] * 5

            # Only client 0 goes stale. Its retry must acquire a new unique SID;
            # every other scoped client's mutable marker and SID remain untouched.
            fake.stale(initial_sids[0])
            first_after = await _probe(clients[0], "execute")
            others_after = await asyncio.gather(*(_probe(client, "execute") for client in clients[1:]))
            assert first_after["session_id"] != initial_sids[0]
            assert first_after["marker"] == "marker-0"
            assert [item["session_id"] for item in others_after] == initial_sids[1:]
            assert [item["marker"] for item in others_after] == [f"marker-{i}" for i in range(1, 5)]

            # Concurrent SESSION_NOT_FOUND on two different MCP clients produces two
            # independent replacement SIDs, not a reused/shared logical SID.
            fake.stale(initial_sids[1])
            fake.stale(initial_sids[2])
            raced = await asyncio.gather(
                _probe(clients[1], "execute"),
                _probe(clients[2], "execute"),
            )
            assert raced[0]["session_id"] != raced[1]["session_id"]
            assert raced[0]["session_id"] not in initial_sids
            assert raced[1]["session_id"] not in initial_sids
            assert [item["marker"] for item in raced] == ["marker-1", "marker-2"]

            # Close client E while client A executes. ServerSession teardown releases
            # only E's owned SID after its in-flight handlers are cancelled/drained.
            e_sid = (await _probe(client_e, "execute"))["session_id"]
            a_before = (await _probe(clients[0], "execute"))["session_id"]
            a_task = asyncio.create_task(_probe(clients[0], "execute"))
            await asyncio.sleep(0)
            await fifth.aclose()
            a_during = await a_task
            await _wait_for_active_sids(fake, fake.active_sids() - {e_sid})
            assert e_sid not in fake.active_sids()
            assert a_during["session_id"] == a_before
            assert a_during["marker"] == "marker-0"

            remaining = await asyncio.gather(*(_probe(client, "execute") for client in clients[:4]))
            remaining_sids = [item["session_id"] for item in remaining]
            assert len(set(remaining_sids)) == 4
            assert e_sid not in remaining_sids
            assert [item["marker"] for item in remaining] == [f"marker-{i}" for i in range(4)]

        await _wait_for_active_sids(fake, set())
        assert fake.peak_active <= 5
    finally:
        stop_ephemeral_server(server, thread)


@pytest.mark.asyncio
async def test_fifty_streamable_http_connect_disconnect_cycles_release_owned_gateway_sids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """50-cycle churn stays below max_sessions_per_ip-like cap and leaks no SID."""
    fake = _FakeGateway(max_active=10)
    monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
    monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
    monkeypatch.setenv("MCP_ACCESS_PROFILE", "mcp_client_safe")
    monkeypatch.setattr("gateway_client.httpx.post", fake.post)
    monkeypatch.setattr("gateway_client.GatewayClient._post_async", fake.post_async)

    app = build_streamable_http_app()
    import examples.mcp_server.server as srv

    monkeypatch.setattr(
        srv,
        "client",
        srv.GatewayClient(
            base_url="http://fake-gateway.invalid",
            api_key="test-key",
            session_id="seed-session",
            ssh_host="executor.invalid",
            ssh_user="tester",
        ),
    )

    @srv.mcp.tool(name="_test_session_churn_probe")
    def _test_session_churn_probe() -> dict[str, Any]:
        scoped = srv.get_gateway_client()
        scoped.connect()
        return {"session_id": scoped.session_id}

    host = "127.0.0.1"
    port = find_free_port()
    server, thread = run_ephemeral_server(app, host, port)
    url = f"http://{host}:{port}/mcp"
    try:
        for _ in range(50):
            async with AsyncExitStack() as stack:
                session, _get_sid = await _open_mcp_client(stack, url)
                result = _structured(await session.call_tool("_test_session_churn_probe", {}))
                assert result["session_id"] in fake.active_sids()
            await _wait_for_active_sids(fake, set())

        assert fake.peak_active == 1
        assert fake.connect_reuse_flags == [False] * 50
        assert len(fake.disconnects) == 50
    finally:
        stop_ephemeral_server(server, thread)


_DUAL_POOL_REGISTERED_MCP_IDS: set[int] = set()


def _configure_dual_pool_server(srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    normal = srv.GatewayClient(
        base_url="http://fake-gateway.invalid",
        api_key="test-key",
        session_id="",
        ssh_host="executor.invalid",
        ssh_user="tester",
    )
    agent = srv.GatewayClient(
        base_url="http://fake-gateway.invalid",
        api_key="test-key",
        session_id="",
        ssh_host="agent-sshd",
        ssh_user="tester",
    )
    monkeypatch.setattr(srv, "client", normal)
    monkeypatch.setattr(srv, "agent_client", agent)
    monkeypatch.setattr(srv, "_agent_client_configured", True)
    monkeypatch.setattr(srv, "_gateway_client_sessions", srv.GatewayClientSessionPool())
    monkeypatch.setattr(srv, "_agent_client_sessions", srv.GatewayClientSessionPool())


def _ensure_dual_pool_probe(srv: Any) -> None:
    mcp_identity = id(srv.mcp)
    if mcp_identity in _DUAL_POOL_REGISTERED_MCP_IDS:
        return

    @srv.mcp.tool(name="_test_dual_pool_lifecycle_probe")
    def _test_dual_pool_lifecycle_probe(
        action: str,
        marker: str | None = None,
    ) -> dict[str, Any]:
        normal = srv.get_gateway_client()
        agent = srv.get_agent_client()
        normal_stdout: str | None = None
        agent_stdout: str | None = None
        error: str | None = None

        if action in {"connect_both", "connect_normal"}:
            normal.connect()
            normal.execute_argv(["marker", "set", f"normal-{marker or ''}"])
        if action in {"connect_both", "connect_agent"}:
            agent.connect()
            agent.execute_argv(["marker", "set", f"agent-{marker or ''}"])
        if action in {"execute_both", "execute_normal"}:
            normal_stdout = normal.execute_argv(["marker", "get"])["stdout"]
        if action in {"execute_both", "execute_agent"}:
            agent_stdout = agent.execute_argv(["marker", "get"])["stdout"]
        if action == "proactive_normal":
            try:
                normal.connect()
            except srv.GatewayClientError as exc:
                error = str(exc)
        if action == "proactive_agent":
            try:
                agent.connect()
            except srv.GatewayClientError as exc:
                error = str(exc)
        if action not in {
            "connect_both",
            "connect_normal",
            "connect_agent",
            "execute_both",
            "execute_normal",
            "execute_agent",
            "proactive_normal",
            "proactive_agent",
            "state",
        }:
            raise ValueError(action)

        return {
            "normal_sid": normal.session_id,
            "agent_sid": agent.session_id,
            "normal_stdout": normal_stdout,
            "agent_stdout": agent_stdout,
            "error": error,
        }

    _DUAL_POOL_REGISTERED_MCP_IDS.add(mcp_identity)


async def _dual_probe(
    session: ClientSession,
    action: str,
    marker: str | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"action": action}
    if marker is not None:
        args["marker"] = marker
    return _structured(
        await session.call_tool("_test_dual_pool_lifecycle_probe", args)
    )


async def _open_five_mcp_clients(
    owner: AsyncExitStack,
    url: str,
) -> tuple[list[ClientSession], list[AsyncExitStack]]:
    clients: list[ClientSession] = []
    stacks: list[AsyncExitStack] = []
    for _ in range(5):
        stack = await owner.enter_async_context(AsyncExitStack())
        client, _get_sid = await _open_mcp_client(stack, url)
        clients.append(client)
        stacks.append(stack)
    return clients, stacks


@pytest.mark.asyncio
async def test_five_real_mcp_transports_dual_pool_fill_cap_reconnect_and_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production-like TEST-11: five transports materialize both lazy pools."""
    fake = _FakeGateway(max_active=10)
    monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
    monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
    monkeypatch.setenv("MCP_ACCESS_PROFILE", "mcp_client_safe")
    monkeypatch.setattr("gateway_client.httpx.post", fake.post)
    monkeypatch.setattr("gateway_client.GatewayClient._post_async", fake.post_async)

    app = build_streamable_http_app()
    import examples.mcp_server.server as srv

    _configure_dual_pool_server(srv, monkeypatch)
    _ensure_dual_pool_probe(srv)

    host = "127.0.0.1"
    port = find_free_port()
    server, thread = run_ephemeral_server(app, host, port)
    url = f"http://{host}:{port}/mcp"
    try:
        async with AsyncExitStack() as owner:
            clients, stacks = await _open_five_mcp_clients(owner, url)
            initial = await asyncio.gather(
                *(
                    _dual_probe(client, "connect_both", str(index))
                    for index, client in enumerate(clients)
                )
            )

            normal_sids = [item["normal_sid"] for item in initial]
            agent_sids = [item["agent_sid"] for item in initial]
            assert len(set(normal_sids + agent_sids)) == 10
            assert len(fake.active_sids()) == 10
            assert fake.peak_active == 10
            assert fake.connect_reuse_flags == [False] * 10

            before_disconnects = list(fake.disconnects)
            proactive = await _dual_probe(clients[0], "proactive_normal")
            assert "429" in (proactive["error"] or "")
            assert proactive["normal_sid"] == normal_sids[0]
            assert len(fake.active_sids()) == 10
            assert fake.disconnects == before_disconnects

            proactive_agent = await _dual_probe(clients[1], "proactive_agent")
            assert "429" in (proactive_agent["error"] or "")
            assert proactive_agent["agent_sid"] == agent_sids[1]
            assert len(fake.active_sids()) == 10
            assert fake.disconnects == before_disconnects

            fake.stale(normal_sids[0])
            normal_after = await _dual_probe(clients[0], "execute_normal")
            assert normal_after["normal_sid"] != normal_sids[0]
            assert len(fake.active_sids()) == 10
            unaffected = await asyncio.gather(
                *(_dual_probe(client, "execute_both") for client in clients[2:])
            )
            for index, item in enumerate(unaffected, start=2):
                assert item["normal_sid"] == normal_sids[index]
                assert item["agent_sid"] == agent_sids[index]
                assert item["normal_stdout"] == f"normal-{index}"
                assert item["agent_stdout"] == f"agent-{index}"

            fake.stale(agent_sids[1])
            agent_after = await _dual_probe(clients[1], "execute_agent")
            assert agent_after["agent_sid"] != agent_sids[1]
            assert len(fake.active_sids()) == 10

            fifth_state = await _dual_probe(clients[4], "state")
            before_release = set(fake.active_sids())
            successful_disconnects = len(fake.disconnects)
            await stacks[4].aclose()
            expected = before_release - {
                fifth_state["normal_sid"],
                fifth_state["agent_sid"],
            }
            await _wait_for_active_sids(fake, expected)
            assert len(expected) == 8
            assert len(fake.disconnects) == successful_disconnects + 2

            remaining = await asyncio.gather(
                *(_dual_probe(client, "execute_both") for client in clients[:4])
            )
            assert len({item["normal_sid"] for item in remaining}) == 4
            assert len({item["agent_sid"] for item in remaining}) == 4
            assert all(item["normal_sid"] in expected for item in remaining)
            assert all(item["agent_sid"] in expected for item in remaining)

        await _wait_for_active_sids(fake, set())
    finally:
        stop_ephemeral_server(server, thread)


@pytest.mark.asyncio
async def test_partial_dual_pool_materialization_races_with_stale_normal_without_11th_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5 normal + 4 agent at 9; lazy agent materialization races stale normal."""
    fake = _FakeGateway(max_active=10)
    monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
    monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
    monkeypatch.setenv("MCP_ACCESS_PROFILE", "mcp_client_safe")
    monkeypatch.setattr("gateway_client.httpx.post", fake.post)
    monkeypatch.setattr("gateway_client.GatewayClient._post_async", fake.post_async)

    app = build_streamable_http_app()
    import examples.mcp_server.server as srv

    _configure_dual_pool_server(srv, monkeypatch)
    _ensure_dual_pool_probe(srv)

    host = "127.0.0.1"
    port = find_free_port()
    server, thread = run_ephemeral_server(app, host, port)
    url = f"http://{host}:{port}/mcp"
    try:
        async with AsyncExitStack() as owner:
            clients, _stacks = await _open_five_mcp_clients(owner, url)
            normal = await asyncio.gather(
                *(
                    _dual_probe(client, "connect_normal", str(index))
                    for index, client in enumerate(clients)
                )
            )
            agents = await asyncio.gather(
                *(
                    _dual_probe(clients[index], "connect_agent", str(index))
                    for index in range(4)
                )
            )
            assert len(fake.active_sids()) == 9

            normal0 = normal[0]["normal_sid"]
            fake.stale_on_next_execute(normal0)
            lazy_agent, stale_normal = await asyncio.gather(
                _dual_probe(clients[4], "connect_agent", "4"),
                _dual_probe(clients[0], "execute_normal"),
            )

            assert lazy_agent["agent_sid"]
            assert stale_normal["normal_sid"] != normal0
            assert len(fake.active_sids()) == 10
            assert fake.peak_active <= 10

            states = await asyncio.gather(
                *(_dual_probe(client, "state") for client in clients)
            )
            assert len({item["normal_sid"] for item in states}) == 5
            assert len({item["agent_sid"] for item in states}) == 5
            for index in range(1, 4):
                assert states[index]["normal_sid"] == normal[index]["normal_sid"]
                assert states[index]["agent_sid"] == agents[index]["agent_sid"]

        await _wait_for_active_sids(fake, set())
    finally:
        stop_ephemeral_server(server, thread)


@pytest.mark.asyncio
async def test_fifty_streamable_http_dual_pool_cycles_release_two_sids_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGateway(max_active=10)
    monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
    monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
    monkeypatch.setenv("MCP_ACCESS_PROFILE", "mcp_client_safe")
    monkeypatch.setattr("gateway_client.httpx.post", fake.post)
    monkeypatch.setattr("gateway_client.GatewayClient._post_async", fake.post_async)

    app = build_streamable_http_app()
    import examples.mcp_server.server as srv

    _configure_dual_pool_server(srv, monkeypatch)
    _ensure_dual_pool_probe(srv)

    host = "127.0.0.1"
    port = find_free_port()
    server, thread = run_ephemeral_server(app, host, port)
    url = f"http://{host}:{port}/mcp"
    try:
        for cycle in range(50):
            async with AsyncExitStack() as stack:
                session, _get_sid = await _open_mcp_client(stack, url)
                result = await _dual_probe(session, "connect_both", str(cycle))
                assert result["normal_sid"] in fake.active_sids()
                assert result["agent_sid"] in fake.active_sids()
                assert len(fake.active_sids()) == 2
            await _wait_for_active_sids(fake, set())

        assert fake.peak_active == 2
        assert fake.connect_reuse_flags == [False] * 100
        assert len(fake.disconnects) == 100
    finally:
        stop_ephemeral_server(server, thread)


@pytest.mark.asyncio
async def test_abrupt_client_task_cancellation_still_releases_both_owned_sids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGateway(max_active=10)
    monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "mcp_client")
    monkeypatch.setenv("MCP_CLIENT_SAFE_MODE", "true")
    monkeypatch.setenv("MCP_ACCESS_PROFILE", "mcp_client_safe")
    monkeypatch.setattr("gateway_client.httpx.post", fake.post)
    monkeypatch.setattr("gateway_client.GatewayClient._post_async", fake.post_async)

    app = build_streamable_http_app()
    import examples.mcp_server.server as srv

    _configure_dual_pool_server(srv, monkeypatch)
    _ensure_dual_pool_probe(srv)

    host = "127.0.0.1"
    port = find_free_port()
    server, thread = run_ephemeral_server(app, host, port)
    url = f"http://{host}:{port}/mcp"
    materialized = asyncio.Event()

    async def _own_transport() -> None:
        async with AsyncExitStack() as stack:
            session, _get_sid = await _open_mcp_client(stack, url)
            await _dual_probe(session, "connect_both", "cancel")
            materialized.set()
            await asyncio.Event().wait()

    try:
        task = asyncio.create_task(_own_transport())
        await asyncio.wait_for(materialized.wait(), timeout=3.0)
        assert len(fake.active_sids()) == 2

        started = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = asyncio.get_running_loop().time() - started

        await _wait_for_active_sids(fake, set(), timeout=3.0)
        assert elapsed < 3.0
        assert len(fake.disconnects) == 2
    finally:
        stop_ephemeral_server(server, thread)
