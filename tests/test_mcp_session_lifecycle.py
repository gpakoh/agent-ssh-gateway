from __future__ import annotations

import asyncio
import time
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
    released: list[Any] = []

    scoped = pool.get(base, mcp_session, owner)
    monkeypatch.setattr(scoped, "release", lambda: released.append(scoped))

    assert pool.release_owner(owner) == 1
    assert released == [scoped]
    assert len(pool._clients) == 0
    assert pool.release_owner(owner) == 0
    assert released == [scoped]


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


@pytest.mark.asyncio
async def test_lifespan_release_has_hard_deadline_even_if_cleanup_worker_blocks(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    class _BlockingPool(live_server.GatewayClientSessionPool):
        def release_owner(self, _owner: Any) -> int:
            time.sleep(1.0)
            return 0

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(live_server, "_gateway_client_sessions", _BlockingPool())
    monkeypatch.setattr(live_server, "_agent_client_sessions", _BlockingPool())
    monkeypatch.setattr(live_server, "_MCP_SESSION_RELEASE_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(live_server, "close_fleet_runtime", _noop_close)

    started = asyncio.get_running_loop().time()
    async with live_server._mcp_lifespan(live_server.mcp):
        pass
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.5
