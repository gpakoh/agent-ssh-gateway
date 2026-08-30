from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from examples.mcp_server.mcp_infra._server_ref import server_module

# ────────────────────────────────────────────────────────────────────
# Adversarial classification matrix
# ────────────────────────────────────────────────────────────────────
# Each test is classified as RED (proves a bug/violation) or GREEN
# (proves an invariant holds after a fix).  RED tests MUST fail on
# unfixed code; GREEN tests MUST pass on fixed code.
#
# Test   │ Type   │ Invariant proved
# ───────┼────────┼─────────────────────────────────────────────────
# A      │ GREEN  │ Concurrent SESSION_NOT_FOUND serializes reconnect,
#        │        │ both threads complete, final session valid.
# B      │ GREEN  │ 429 during reconnect preserves working owned SID.
# C      │ GREEN  │ Old-SID disconnect failure doesn't break new session.
# C2     │ GREEN  │ Leaked old SID doesn't block subsequent lifecycle
#        │        │ teardown (release disconnects current SID).
# D      │ GREEN  │ pool.get() rejects base replacement, preventing
#        │        │ orphan (previously RED on unfixed code).
# E      │ GREEN  │ Two forks from same base reconnect independently.
# F      │ GREEN  │ Borrowed seed expiry triggers reconnect.
# G      │ GREEN  │ Release of one scoped client doesn't affect sibling.
# H      │ GREEN  │ Released scoped client reconnect raises error.
# I      │ GREEN  │ Repeated reconnect doesn't accumulate active sessions
#        │        │ (max 2 concurrent per scoped client).
# J      │ GREEN  │ Pool detach_owner() rejects subsequent reconnect.
# K      │ GREEN  │ Per-owner persistent SIDs bounded by pool_size * 2.
# L      │ GREEN  │ Dual-pool persistent SIDs bounded by
#        │        │ (pool_size + agent_pool_size) * 2.
# M      │ GREEN  │ Failed disconnect doesn't cause unbounded SID
#        │        │ accumulation (bounded by leaked + 2).
#
# RED evidence: original test D proved pool.get() orphan on unfixed
# code.  That test was replaced by the GREEN regression after the fix
# in gateway_client.py pool.get().
# ────────────────────────────────────────────────────────────────────


def _SESSION_NOT_FOUND_ERROR() -> Any:
    return server_module().GatewayClientError("SESSION_NOT_FOUND: stale")


@dataclass(eq=False)
class _McpSession:
    name: str


class _Response:
    def __init__(
        self,
        payload: dict[str, Any],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
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
            "ephemeral": True,
            "idle_timeout_seconds": 600,
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
async def test_active_lifecycle_heartbeats_owned_sid_until_teardown(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    gateway_pool = live_server.GatewayClientSessionPool()
    agent_pool = live_server.GatewayClientSessionPool()
    heartbeat_seen = asyncio.Event()
    heartbeats: list[str] = []
    disconnects: list[str] = []

    async def tracking_post_async(
        _client: Any, path: str, payload: dict[str, Any], *, timeout: float | int
    ) -> dict[str, Any]:
        sid = payload["session_id"]
        if path == "/api/ssh/heartbeat":
            heartbeats.append(sid)
            heartbeat_seen.set()
            return {"status": "ok"}
        if path == "/api/ssh/disconnect":
            disconnects.append(sid)
            return {"status": "disconnected"}
        raise AssertionError(path)

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(live_server, "_gateway_client_sessions", gateway_pool)
    monkeypatch.setattr(live_server, "_agent_client_sessions", agent_pool)
    monkeypatch.setattr(live_server, "_MCP_SESSION_KEEPALIVE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(live_server, "close_fleet_runtime", _noop_close)
    monkeypatch.setattr(live_server.GatewayClient, "_post_async", tracking_post_async)

    async with live_server._mcp_lifespan(live_server.mcp) as owner:
        scoped, mcp_session = _attach_owned_client(
            live_server, gateway_pool, owner, "active-owned-sid"
        )
        assert mcp_session is not None
        await asyncio.wait_for(heartbeat_seen.wait(), timeout=0.2)
        assert heartbeats == ["active-owned-sid"]
        assert scoped.session_id == "active-owned-sid"
        assert scoped._owns_session is True

    heartbeat_count_after_teardown = len(heartbeats)
    await asyncio.sleep(0.04)

    assert len(heartbeats) == heartbeat_count_after_teardown
    assert disconnects == ["active-owned-sid"]
    assert scoped._released is True


@pytest.mark.asyncio
async def test_owned_session_heartbeat_skips_borrowed_and_released_clients(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    scoped = _base_client(live_server).fork_session()
    calls: list[tuple[str, str]] = []

    async def tracking_post_async(
        path: str, payload: dict[str, Any], *, timeout: float | int
    ) -> dict[str, Any]:
        calls.append((path, payload["session_id"]))
        return {"status": "ok"}

    monkeypatch.setattr(scoped, "_post_async", tracking_post_async)

    assert scoped.session_id == "seed-session"
    assert scoped._owns_session is False
    assert await scoped.heartbeat_owned_session_async() is False
    assert calls == []

    scoped.session_id = "owned-sid"
    scoped._owns_session = True
    assert await scoped.heartbeat_owned_session_async() is True
    assert calls == [("/api/ssh/heartbeat", "owned-sid")]

    targets = scoped.prepare_release()
    assert targets.current_sid == "owned-sid"
    assert await scoped.heartbeat_owned_session_async() is False
    assert calls == [("/api/ssh/heartbeat", "owned-sid")]


@pytest.mark.asyncio
async def test_owned_session_heartbeat_failure_never_reconnects(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    scoped = _base_client(live_server).fork_session()
    scoped.session_id = "owned-sid"
    scoped._owns_session = True
    reconnect_attempts = 0

    async def failed_post_async(
        path: str, payload: dict[str, Any], *, timeout: float | int
    ) -> dict[str, Any]:
        assert path == "/api/ssh/heartbeat"
        assert payload == {"session_id": "owned-sid"}
        raise live_server.GatewayClientError("heartbeat unavailable")

    def forbidden_reconnect() -> None:
        nonlocal reconnect_attempts
        reconnect_attempts += 1
        raise AssertionError("heartbeat must never reconnect")

    monkeypatch.setattr(scoped, "_post_async", failed_post_async)
    monkeypatch.setattr(scoped, "_reconnect_session", forbidden_reconnect)

    assert await scoped.heartbeat_owned_session_async() is False
    assert reconnect_attempts == 0
    assert scoped.session_id == "owned-sid"
    assert scoped._owns_session is True


def test_keepalive_snapshot_is_owner_scoped_and_owned_only(live_server: Any) -> None:
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    owner_a = object()
    owner_b = object()
    session_a = _McpSession("keepalive-a")
    session_b = _McpSession("keepalive-b")
    session_borrowed = _McpSession("keepalive-borrowed")

    owned_a = pool.get(base, session_a, owner_a)
    owned_a.session_id = "owned-a"
    owned_a._owns_session = True

    owned_b = pool.get(base, session_b, owner_b)
    owned_b.session_id = "owned-b"
    owned_b._owns_session = True

    borrowed = pool.get(base, session_borrowed, owner_a)
    assert borrowed._owns_session is False

    assert pool.owned_clients_for_owner(owner_a) == (owned_a,)
    assert pool.owned_clients_for_owner(owner_b) == (owned_b,)


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
        scoped, keepalive = _attach_owned_client(live_server, gateway_pool, owner, "owned-gateway")
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
        scoped, keepalive = _attach_owned_client(live_server, gateway_pool, owner, "timeout-sid")
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
        normal, keep_normal = _attach_owned_client(live_server, gateway_pool, owner, "normal-sid")
        agent, keep_agent = _attach_owned_client(live_server, agent_pool, owner, "agent-sid")
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
            scoped, keepalive = _attach_owned_client(live_server, gateway_pool, owner, "cancel-sid")
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
        scoped, keepalive = _attach_owned_client(live_server, gateway_pool, owner, "boundary-sid")
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
        old_scoped, old_key = _attach_owned_client(live_server, gateway_pool, old_owner, "old-sid")
        old_scoped._release_http_timeout = 1.0
        assert old_key is not None

    async with live_server._mcp_lifespan(live_server.mcp) as new_owner:
        new_scoped, new_key = _attach_owned_client(live_server, gateway_pool, new_owner, "new-sid")
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
    assert borrowed.prepare_release().current_sid == ""
    assert borrowed.prepare_release().current_sid == ""

    owned = _base_client(live_server).fork_session()
    owned.session_id = "owned-once"
    owned._owns_session = True
    assert owned.prepare_release().current_sid == "owned-once"
    assert owned.prepare_release().current_sid == ""
    assert owned.session_id == ""
    assert owned._released is True
    assert owned._owns_session is False


# ---------------------------------------------------------------------------
# Adversarial tests A-J: session leak / reconnect amplification matrix
# ---------------------------------------------------------------------------


def _count_server_sessions(
    monkeypatch: pytest.MonkeyPatch,
    live_server: Any,
    base: Any,
    n_scoped: int,
) -> int:
    """Create N scoped clients from same base, connect each, count connect calls."""
    connect_count = 0

    def fake_post(_url: str, **kwargs: Any) -> _Response:
        nonlocal connect_count
        connect_count += 1
        return _Response({"session_id": f"sid-{connect_count}"})

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    for _ in range(n_scoped):
        scoped = base.fork_session()
        scoped.connect()
    return connect_count


def test_a_concurrent_reconnect_on_same_scoped_client_creates_one_session(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """A: Two concurrent operations on same scoped client hitting SESSION_NOT_FOUND
    must coalesce to exactly one reconnect.

    Uses threading.Barrier to deterministically ensure BOTH threads have
    entered the execute call before either returns.  Both raise
    SESSION_NOT_FOUND on first attempt.  The coalescing check
    (self.session_id == stale_sid under lock) ensures exactly one
    reconnect; the other thread sees the fresh SID and skips reconnect,
    retrying with the fresh session.

    Invariant: connect_calls == 1, both threads complete successfully.
    """
    import threading

    base = _base_client(live_server)
    scoped = base.fork_session()
    scoped._owns_session = True
    scoped.session_id = "stale-sid"

    connect_calls = 0
    disconnect_calls = 0
    execute_attempts = 0
    # Barrier ensures both threads enter the execute handler before either returns.
    both_entered = threading.Barrier(2, timeout=5)
    execute_lock = threading.Lock()

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal connect_calls, disconnect_calls, execute_attempts
        if url.endswith("/api/ssh/connect"):
            connect_calls += 1
            return _Response({"session_id": "fresh-sid"})
        if url.endswith("/api/ssh/execute"):
            with execute_lock:
                execute_attempts += 1
                attempt = execute_attempts
            if attempt <= 2:
                # First round: both threads see SESSION_NOT_FOUND.
                # Barrier ensures both enter before either returns.
                try:
                    both_entered.wait(timeout=3)
                except threading.BrokenBarrierError:
                    pass
                raise _SESSION_NOT_FOUND_ERROR()
            # Retry round: succeed
            return _Response({"exit_code": 0, "stdout": "ok", "stderr": "", "duration": 0.0})
        if url.endswith("/api/ssh/disconnect"):
            disconnect_calls += 1
            return _Response({"status": "disconnected"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)
    monkeypatch.setattr("gateway_client.validate_readonly_command", lambda cmd: cmd)

    results: list[str] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            result = scoped.execute_restricted("echo test")
            results.append(result.get("stdout", ""))
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Errors: {errors}"
    assert len(results) == 2
    assert all(r == "ok" for r in results)
    # Coalescing: both threads captured stale-sid before the lock.
    # First to acquire lock reconnects (session_id changes).
    # Second sees session_id != stale_sid and skips reconnect.
    assert connect_calls == 1
    assert disconnect_calls == 1
    assert scoped.session_id == "fresh-sid"
    assert scoped._owns_session is True


def test_b_429_on_reconnect_preserves_working_session(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """B: When a scoped client reconnects and hits 429 (quota), the previously
    working session must remain owned and intact."""
    base = _base_client(live_server)
    scoped = base.fork_session()
    call_n = 0

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal call_n
        if url.endswith("/api/ssh/connect"):
            call_n += 1
            if call_n == 1:
                return _Response({"session_id": "working-sid"})
            return _Response({"detail": "quota"}, 429)
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    assert scoped.connect() == "working-sid"
    assert scoped._owns_session is True

    with pytest.raises(Exception, match="429"):
        scoped.connect()

    assert scoped.session_id == "working-sid"
    assert scoped._owns_session is True
    assert not scoped._released


def test_c_old_sid_disconnect_failure_preserves_new_session(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """C: When disconnecting the old owned SID fails (network error), the new
    session must still be correctly owned."""
    base = _base_client(live_server)
    scoped = base.fork_session()
    scoped.session_id = "old-stale"
    scoped._owns_session = True

    call_n = 0

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal call_n
        if url.endswith("/api/ssh/connect"):
            call_n += 1
            return _Response({"session_id": "new-fresh"})
        if url.endswith("/api/ssh/disconnect"):
            raise OSError("gateway unreachable")
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    scoped.connect()
    assert scoped.session_id == "new-fresh"
    assert scoped._owns_session is True
    assert not scoped._released


def test_c2_old_sid_leak_retired_and_drained_on_teardown(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """C2: When old-SID disconnect fails during reconnect, the leaked SID
    enters the retired set (lifecycle ownership graph).  During teardown,
    release() attempts BOTH the current SID and every retired SID.
    Borrowed seed is never attempted.  After release the client is released
    and can no longer reconnect."""
    base = _base_client(live_server)
    scoped = base.fork_session()
    scoped.session_id = "old-stale"
    scoped._owns_session = True

    disconnect_calls: list[str] = []

    def fake_post(url: str, **kwargs: Any) -> _Response:
        if url.endswith("/api/ssh/connect"):
            return _Response({"session_id": "new-fresh"})
        if url.endswith("/api/ssh/disconnect"):
            payload = kwargs.get("json", {})
            sid = payload.get("session_id", "unknown")
            disconnect_calls.append(sid)
            if sid == "old-stale":
                raise OSError("gateway unreachable for old SID")
            return _Response({"status": "disconnected"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    # Reconnect: old SID disconnect fails → enters retired set
    scoped.connect()
    assert scoped.session_id == "new-fresh"
    assert scoped._owns_session is True
    assert "old-stale" in scoped._retired

    # Teardown: release() attempts BOTH current and retired SIDs
    scoped.release()
    assert scoped._released is True
    assert scoped.session_id == ""

    # Both SIDs were attempted during release
    assert "old-stale" in disconnect_calls
    assert "new-fresh" in disconnect_calls

    # Retired set is cleared after release (prepare_release freezes + clears)
    assert len(scoped._retired) == 0

    # Client is released — reconnect is rejected
    with pytest.raises(live_server.GatewayClientError, match="MCP session is closed"):
        scoped.connect()


def test_d_pool_base_change_prevents_orphan(
    live_server: Any,
) -> None:
    """D GREEN: pool.get() rejects base replacement, keeping the original
    scoped client and its owned SID in the lifecycle ownership graph.

    The old scoped client remains reachable via detach_owner() and can be
    properly released during lifecycle teardown.
    """
    pool = live_server.GatewayClientSessionPool()
    base1 = _base_client(live_server)
    owner = object()
    mcp_session = _McpSession("no-orphan")

    scoped1 = pool.get(base1, mcp_session, owner)
    scoped1.session_id = "sid-first"
    scoped1._owns_session = True

    base2 = _base_client(live_server)
    with pytest.raises(RuntimeError, match="base identity changed"):
        pool.get(base2, mcp_session, owner)

    # Original entry intact — scoped1 is still in the pool
    assert pool._clients[mcp_session][1] is scoped1

    # detach_owner returns scoped1 with its owned SID — no orphan
    detached = pool.detach_owner(owner)
    detached_sids = [t.current_sid for _, t in detached]
    assert "sid-first" in detached_sids
    assert len(detached) == 1

    # scoped1 is properly released
    assert scoped1._released is True
    assert scoped1.session_id == ""


def test_e_two_forks_from_same_base_reconnect_to_independent_sessions(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """E: Two scoped clients forked from the same base must get independent
    owned sessions that don't interfere."""
    base = _base_client(live_server)
    fork1 = base.fork_session()
    fork2 = base.fork_session()
    connect_n = 0

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal connect_n
        if url.endswith("/api/ssh/connect"):
            connect_n += 1
            return _Response({"session_id": f"independent-{connect_n}"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    sid1 = fork1.connect()
    sid2 = fork2.connect()
    assert sid1 != sid2
    assert fork1._owns_session and fork2._owns_session
    assert base.session_id == "seed-session"


def test_f_borrowed_seed_expiry_triggers_reconnect(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """F: A scoped client with a borrowed seed that the server has expired must
    reconnect and get its own owned session."""
    base = _base_client(live_server)
    scoped = base.fork_session()
    # scoped inherits seed-session as borrowed — not owned
    assert scoped.session_id == "seed-session"
    assert scoped._owns_session is False

    call_n = 0

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal call_n
        if url.endswith("/api/ssh/execute"):
            call_n += 1
            if call_n == 1:
                raise _SESSION_NOT_FOUND_ERROR()
            return _Response({"exit_code": 0, "stdout": "ok", "stderr": "", "duration": 0.0})
        if url.endswith("/api/ssh/connect"):
            return _Response({"session_id": "newly-owned"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)
    # Bypass command policy so execute_restricted doesn't reject the command
    monkeypatch.setattr("gateway_client.validate_readonly_command", lambda cmd: cmd)

    result = scoped.execute_restricted("echo ok")
    assert result["exit_code"] == 0
    assert scoped.session_id == "newly-owned"
    assert scoped._owns_session is True


def test_g_release_one_scoped_does_not_affect_sibling(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """G: Releasing one scoped client must not affect another scoped client
    from the same base (isolation)."""
    base = _base_client(live_server)
    s1 = base.fork_session()
    s2 = base.fork_session()
    disconnects: list[str] = []

    def fake_post(url: str, **kwargs: Any) -> _Response:
        payload = kwargs["json"]
        if url.endswith("/api/ssh/connect"):
            return _Response({"session_id": payload.get("username", "x") + "-sid"})
        if url.endswith("/api/ssh/disconnect"):
            disconnects.append(payload["session_id"])
            return _Response({"status": "disconnected"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    s1.session_id = "s1-owned"
    s1._owns_session = True
    s2.session_id = "s2-owned"
    s2._owns_session = True

    s1.release()
    assert disconnects == ["s1-owned"]
    assert s2.session_id == "s2-owned"
    assert s2._owns_session is True


def test_h_released_scoped_client_reconnect_raises(
    live_server: Any,
) -> None:
    """H: A released scoped client must not be able to reconnect — it must
    raise immediately without making network calls."""
    base = _base_client(live_server)
    scoped = base.fork_session()
    scoped.session_id = "will-be-released"
    scoped._owns_session = True
    scoped.prepare_release()

    with pytest.raises(live_server.GatewayClientError, match="MCP session is closed"):
        scoped.connect()


def test_i_stress_repeated_reconnect_does_not_accumulate_active_sessions(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """I: After N reconnect cycles, there should be at most 2 active sessions
    at any time (old + new during the connect→disconnect window). Old SIDs
    must be disconnected before or shortly after new ones are established."""
    base = _base_client(live_server)
    scoped = base.fork_session()
    all_sids_ever: list[str] = []
    active_count = 0
    max_concurrent = 0
    sid_counter = 0

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal active_count, max_concurrent, sid_counter
        if url.endswith("/api/ssh/connect"):
            sid_counter += 1
            active_count += 1
            max_concurrent = max(max_concurrent, active_count)
            sid = f"sid-{sid_counter:03d}"
            all_sids_ever.append(sid)
            return _Response({"session_id": sid})
        if url.endswith("/api/ssh/disconnect"):
            active_count -= 1
            return _Response({"status": "disconnected"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    for _ in range(10):
        scoped.connect()

    # During each _reconnect_session: new SID is created BEFORE old SID is
    # disconnected (lines 278-289 of gateway_client.py).  That means at most
    # 2 sessions are alive simultaneously per scoped client at any point.
    assert max_concurrent == 2
    assert len(all_sids_ever) == 10

    scoped.release()
    assert active_count == 0


def test_k_per_owner_persistent_sid_bounded_by_pool_size(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """K: Multiple scoped clients sharing one owner cannot accumulate more
    persistent SIDs than pool_size * 2 (the 2 accounts for the reconnect
    window where old + new coexist)."""
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    owner = object()

    active_count = 0
    max_concurrent = 0
    sid_counter = 0
    connect_calls = 0

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal active_count, max_concurrent, sid_counter, connect_calls
        if url.endswith("/api/ssh/connect"):
            connect_calls += 1
            sid_counter += 1
            active_count += 1
            max_concurrent = max(max_concurrent, active_count)
            return _Response({"session_id": f"sid-{sid_counter:03d}"})
        if url.endswith("/api/ssh/disconnect"):
            active_count -= 1
            return _Response({"status": "disconnected"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    # Create 3 scoped clients for same owner
    scoped_clients = []
    for i in range(3):
        ms = _McpSession(f"count-{i}")
        c = pool.get(base, ms, owner)
        scoped_clients.append(c)

    # Each scoped client connects → 3 sessions
    for c in scoped_clients:
        c.connect()

    assert connect_calls == 3
    # At most 3 active (no reconnect yet, so 1 per client)
    assert max_concurrent == 3

    # Reconnect each scoped client once → during reconnect window, max is 3+1=4
    # (new created before old disconnected, but bounded by pool size)
    for c in scoped_clients:
        c.connect()

    # Total connects = 3 (initial) + 3 (reconnect) = 6
    assert connect_calls == 6
    # Max concurrent: at most 3 + 1 = 4 (one reconnect window at a time per client,
    # but serialized by lock, so at most pool_size + 1)
    assert max_concurrent <= len(scoped_clients) + 1

    # Release all
    for c in scoped_clients:
        c.release()
    assert active_count == 0


def test_l_dual_pool_persistent_sid_bounded(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """L: Both pool and agent_pool scoped clients can coexist without
    unbounded SID accumulation.  Total persistent SIDs are bounded by
    (pool_size + agent_pool_size) * 2."""
    pool = live_server.GatewayClientSessionPool()
    agent_pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    owner = object()

    active_count = 0
    max_concurrent = 0
    sid_counter = 0

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal active_count, max_concurrent, sid_counter
        if url.endswith("/api/ssh/connect"):
            sid_counter += 1
            active_count += 1
            max_concurrent = max(max_concurrent, active_count)
            return _Response({"session_id": f"sid-{sid_counter:03d}"})
        if url.endswith("/api/ssh/disconnect"):
            active_count -= 1
            return _Response({"status": "disconnected"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)

    # 2 from pool + 1 from agent_pool = 3 total scoped clients
    scoped_main = []
    for i in range(2):
        ms = _McpSession(f"dual-main-{i}")
        scoped_main.append(pool.get(base, ms, owner))

    ms_agent = _McpSession("dual-agent-0")
    scoped_agent = agent_pool.get(base, ms_agent, owner)

    # All connect
    for c in scoped_main:
        c.connect()
    scoped_agent.connect()

    # 3 sessions active
    assert max_concurrent == 3

    # Reconnect all
    for c in scoped_main:
        c.connect()
    scoped_agent.connect()

    # Max concurrent bounded by total + 1 (reconnect window)
    assert max_concurrent <= 3 + 1

    # Release all
    for c in scoped_main:
        c.release()
    scoped_agent.release()
    assert active_count == 0


def test_m_failed_disconnect_no_sid_accumulation(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """M: Persistent disconnect failure with N=100 reconnect cycles.

    Proves:
    1. Borrowed seed NEVER enters disconnect_calls or retired set.
    2. After first failed cleanup, reconnect is blocked (fail-closed).
       No /connect, current SID unchanged, retired debt unchanged.
    3. connect_calls stays constant (2: initial + first reconnect) for N=100.
    4. Remote active SID count <= constant (2) independent of N.
    5. After fake cleanup switches to success: retired debt clears, reconnect
       unblocked, next reconnect creates one new session.
    """
    N = 100

    base = _base_client(live_server)
    assert base.session_id == "seed-session"
    scoped = base.fork_session()
    assert scoped.session_id == "seed-session"
    assert scoped._owns_session is False

    active_count = 0
    connect_calls = 0
    max_concurrent = 0
    disconnect_calls: list[str] = []
    disconnect_fails_persistent = False

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal active_count, connect_calls, max_concurrent
        if url.endswith("/api/ssh/connect"):
            connect_calls += 1
            active_count += 1
            max_concurrent = max(max_concurrent, active_count)
            return _Response({"session_id": f"conn-{connect_calls:03d}"})
        if url.endswith("/api/ssh/disconnect"):
            payload = kwargs.get("json", {})
            sid = payload.get("session_id", "unknown")
            disconnect_calls.append(sid)
            if disconnect_fails_persistent and sid != "seed-session":
                raise OSError("gateway unreachable")
            active_count -= 1
            return _Response({"status": "disconnected"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)
    monkeypatch.setattr("gateway_client.validate_readonly_command", lambda cmd: cmd)

    # --- Phase 0: borrowed seed is never touched ---
    # First connect does NOT disconnect the borrowed seed (old_sid == new_sid).
    scoped.connect()
    assert connect_calls == 1
    assert active_count == 1
    assert "seed-session" not in disconnect_calls
    assert len(scoped._retired) == 0

    # --- Phase 1: persistent disconnect failure, N cycles ---
    disconnect_fails_persistent = True

    # Cycle 2: connect conn-002, disconnect conn-001 FAILS → retired={"conn-001"}
    scoped.connect()
    assert connect_calls == 2
    assert active_count == 2
    assert "conn-001" in scoped._retired

    # Cycles 3..N: drain fails (persistent), reconnect blocked
    blocked_count = 0
    for _i in range(3, N + 1):
        with pytest.raises(live_server.GatewayClientError, match="reconnect blocked"):
            scoped.connect()
        blocked_count += 1

    assert blocked_count == N - 2

    # Invariant: connect_calls frozen at 2 — no new sessions created
    assert connect_calls == 2
    # Invariant: current SID unchanged
    assert scoped.session_id == "conn-002"
    # Invariant: retired debt unchanged
    assert "conn-001" in scoped._retired
    # Invariant: remote active SID count <= constant (2)
    assert max_concurrent <= 2
    # Borrowed seed was never in disconnect_calls
    assert "seed-session" not in disconnect_calls

    # --- Phase 2: recovery after fake cleanup succeeds ---
    disconnect_fails_persistent = False

    # Next connect: drain conn-001 succeeds → retired cleared → reconnect unblocked
    scoped.connect()
    assert connect_calls == 3
    assert active_count == 1  # conn-003 live; conn-001 (drained) + conn-002 (old) disconnected
    assert len(scoped._retired) == 0  # retired cleared
    assert scoped.session_id == "conn-003"

    # One more cycle to prove normal flow restored
    scoped.connect()
    assert connect_calls == 4
    assert active_count == 1
    assert max_concurrent <= 2

    # Final: cleanup
    scoped.release()
    assert active_count == 0


def test_n_teardown_disconnects_both_current_and_retired_sids(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """N: Teardown (pool detach + release) disconnects BOTH the current owned
    SID and every retired owned SID exactly once.  Borrowed seed is never
    attempted.  After release the client cannot reconnect.

    Also verifies BLOCKER 1: no network I/O while pool._lock is held.
    """
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    owner = object()
    mcp_session = _McpSession("teardown-both")

    # Simulate: reconnect conn-2, disconnect conn-1 fails → retired={"conn-1"}
    disconnect_calls: list[str] = []
    disconnect_fails_for: set[str] = set()
    connect_count = 0

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal connect_count
        if url.endswith("/api/ssh/connect"):
            connect_count += 1
            return _Response({"session_id": f"conn-{connect_count}"})
        if url.endswith("/api/ssh/disconnect"):
            payload = kwargs.get("json", {})
            sid = payload.get("session_id", "unknown")
            disconnect_calls.append(sid)
            if sid in disconnect_fails_for:
                raise OSError("gateway unreachable")
            return _Response({"status": "disconnected"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)
    monkeypatch.setattr("gateway_client.validate_readonly_command", lambda cmd: cmd)

    scoped = pool.get(base, mcp_session, owner)
    # Initial connect: gets conn-1, borrowed seed not disconnected
    scoped.connect()
    assert scoped.session_id == "conn-1"
    assert scoped._owns_session is True

    disconnect_fails_for.add("conn-1")
    scoped.connect()
    assert scoped.session_id == "conn-2"
    assert "conn-1" in scoped._retired

    # --- Pool teardown: detach_owner proves no network I/O (BLOCKER 1) ---
    disconnect_calls.clear()
    detached = pool.detach_owner(owner)
    # No disconnect happened during detach_owner (under pool lock)
    assert disconnect_calls == []
    assert len(detached) == 1
    (scoped_detached, targets) = detached[0]
    assert "conn-2" in targets.all_sids
    assert "conn-1" in targets.all_sids
    # Borrowed seed never in targets
    assert "seed-session" not in targets.all_sids
    assert scoped_detached._released is True

    # Manual cleanup: iterate all_sids (this is what release_owner does AFTER lock)
    for sid in targets.all_sids:
        try:
            scoped_detached._post(
                "/api/ssh/disconnect",
                {"session_id": sid},
                timeout=2.0,
            )
        except Exception:
            pass

    # BOTH current (conn-2) and retired (conn-1) were disconnected
    assert "conn-2" in disconnect_calls
    assert "conn-1" in disconnect_calls
    # Borrowed seed was never touched
    assert "seed-session" not in disconnect_calls

    # Client is released — reconnect rejected
    with pytest.raises(live_server.GatewayClientError, match="MCP session is closed"):
        scoped_detached.connect()


def test_j_pool_detach_then_reconnect_is_rejected(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """J: After pool detach_owner(), any reconnect attempt on the scoped client
    must be rejected — the scoped client has been logically destroyed."""
    pool = live_server.GatewayClientSessionPool()
    base = _base_client(live_server)
    owner = object()
    mcp_session = _McpSession("detach-reconnect")

    scoped = pool.get(base, mcp_session, owner)
    scoped.session_id = "detach-me"
    scoped._owns_session = True

    # detach sets _released = True
    pool.detach_owner(owner)
    assert scoped._released is True

    with pytest.raises(live_server.GatewayClientError, match="MCP session is closed"):
        scoped.connect()


@pytest.mark.asyncio
async def test_09_lifespan_retired_sid_cleanup_via_production_path(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    """TEST-09 GREEN: Production _mcp_lifespan teardown cleans up BOTH current
    owned SID and retired owned SID through the real production code path:

        _mcp_lifespan
          -> detach_owner (pool._lock, no network I/O)
          -> targets.all_sids iteration
          -> release_sid_async (async network, per-SID)

    Setup:
      - base.session_id == "seed-session" (real borrowed seed)
      - scoped = base.fork_session() inherits borrowed seed
      - scoped registered in pool via pool.get()
      - scoped then gets current owned SID + one retired SID debt
    """
    gateway_pool = live_server.GatewayClientSessionPool()
    agent_pool = live_server.GatewayClientSessionPool()
    async_disconnects: list[str] = []

    async def tracking_post_async(
        _client: Any, _path: str, payload: dict[str, Any], *, timeout: float | int
    ) -> dict[str, Any]:
        sid = payload.get("session_id", "")
        async_disconnects.append(sid)
        return {"status": "disconnected"}

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(live_server, "_gateway_client_sessions", gateway_pool)
    monkeypatch.setattr(live_server, "_agent_client_sessions", agent_pool)
    monkeypatch.setattr(live_server, "_MCP_SESSION_RELEASE_DEADLINE_SECONDS", 10.0)
    monkeypatch.setattr(live_server, "close_fleet_runtime", _noop_close)
    monkeypatch.setattr(live_server.GatewayClient, "_post_async", tracking_post_async)

    scoped: Any = None

    async with live_server._mcp_lifespan(live_server.mcp) as owner:
        base = live_server.GatewayClient(
            base_url="http://gateway.invalid",
            api_key="test-key",
            session_id="seed-session",
            ssh_host="executor.invalid",
            ssh_user="tester",
        )
        mcp_session = _McpSession("lifespan-retired")
        scoped = gateway_pool.get(base, mcp_session, owner)

        # Verify real borrowed seed starting state
        assert scoped.session_id == "seed-session"
        assert scoped._owns_session is False

        # Simulate: scoped gets its own owned SID (like a successful connect)
        scoped.session_id = "current-owned"
        scoped._owns_session = True
        scoped._release_http_timeout = 0.5

        # Simulate: a reconnect created a retired SID debt
        scoped._retired.add("retired-owned")

    # --- After lifespan exit: production teardown has run ---

    # 1. current owned SID disconnect attempted
    assert "current-owned" in async_disconnects
    # 2. retired owned SID disconnect attempted
    assert "retired-owned" in async_disconnects
    # 3. borrowed seed NEVER attempted
    assert "seed-session" not in async_disconnects
    # 7. no cleanup target silently lost — exactly 2 disconnects
    assert len(async_disconnects) == 2
    # 4. both cleanups went through production path
    assert len(gateway_pool._clients) == 0
    # 5. scoped._released is True
    assert scoped._released is True
    # 6. subsequent connect/reconnect raises closed error
    with pytest.raises(live_server.GatewayClientError, match="MCP session is closed"):
        scoped.connect()
def test_global_reconnect_governor_suppresses_followup_connects_after_429(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    first = _base_client(live_server).fork_session()
    second = _base_client(live_server).fork_session()
    connect_attempts = 0

    def fake_post(url: str, **kwargs: Any) -> _Response:
        nonlocal connect_attempts
        if url.endswith("/api/ssh/connect"):
            connect_attempts += 1
            return _Response({"detail": "rate limited"}, 429, {"Retry-After": "60"})
        raise AssertionError(url)

    monkeypatch.setattr("gateway_client.httpx.post", fake_post)
    monkeypatch.setattr(live_server.GatewayClient, "_connect_retry_not_before", 0.0)

    with pytest.raises(live_server.GatewayClientError, match="429"):
        first.connect()
    with pytest.raises(live_server.GatewayClientError, match="cooldown"):
        second.connect()

    assert connect_attempts == 1


def test_repo_status_project_honors_explicit_session_id(
    monkeypatch: pytest.MonkeyPatch, live_server: Any
) -> None:
    client = _base_client(live_server)
    client.session_id = "stale-default"
    seen_sids: list[str] = []

    def fake_post(path: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        assert path == "/api/ssh/execute-argv"
        seen_sids.append(payload["session_id"])
        return {"exit_code": 0, "stdout": "ok\n", "stderr": ""}

    class _Registry:
        def project_info(self, _project: str) -> dict[str, str]:
            return {"root": "/workspace/project"}

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr("app.workspace.registry.get_registry", lambda: _Registry())

    result = client.repo_status(session_id="explicit-healthy", project="project")

    assert result["status"]["exit_code"] == 0
    assert seen_sids == ["explicit-healthy", "explicit-healthy", "explicit-healthy"]
