from __future__ import annotations

import inspect
import time
from itertools import count
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.auth_middleware import AuthIdentity
from app.config import Settings, settings
from app.routers import ssh as ssh_routes
from app.routers.system import get_config
from app.security import limiter, rate_limit_mutation
from app.ssh_manager import SessionLimitError, SSHSessionManager


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter._storage.reset()
    yield
    limiter._storage.reset()


def test_master_capacity_settings_default_fail_conservative(monkeypatch):
    for name in (
        "MASTER_MAX_SESSIONS_PER_IP",
        "MASTER_CONNECT_RATE_LIMIT_REQUESTS",
        "MASTER_EXECUTE_RATE_LIMIT_REQUESTS",
    ):
        monkeypatch.delenv(name, raising=False)

    cfg = Settings.model_validate({})
    assert cfg.master_max_sessions_per_ip == 0
    assert cfg.master_connect_rate_limit_requests == 0
    assert cfg.master_execute_rate_limit_requests == 0


@pytest.mark.asyncio
async def test_master_session_lane_supports_fourteenth_same_ip(monkeypatch):
    monkeypatch.setattr(settings, "max_sessions_per_ip", 10)
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "master_max_sessions_per_ip", 14)

    manager = SSHSessionManager(connection_pool_size=0)
    now = time.time()
    manager._sessions = {
        f"active-{index}": cast(
            Any,
            SimpleNamespace(
                source_ip="10.0.0.5",
                effective_idle_timeout=3600,
                last_activity=now,
            ),
        )
        for index in range(7)
    }
    manager._pending_sessions_by_ip["10.0.0.5"] = 6
    monkeypatch.setattr(manager, "_create_session_unreserved", AsyncMock(return_value="master-14"))

    sid = await manager.create_session(
        host="h",
        port=22,
        username="u",
        password="pw",
        source_ip="10.0.0.5",
        owner_type="master",
        privileged_capacity=True,
    )
    assert sid == "master-14"
    assert manager._pending_sessions_by_ip["10.0.0.5"] == 6

    manager._pending_sessions_by_ip["10.0.0.5"] = 7
    with pytest.raises(SessionLimitError):
        await manager.create_session(
            host="h",
            port=22,
            username="u",
            password="pw",
            source_ip="10.0.0.5",
            owner_type="master",
            privileged_capacity=True,
        )


@pytest.mark.asyncio
async def test_master_label_without_verified_capacity_flag_stays_at_legacy_ten(monkeypatch):
    monkeypatch.setattr(settings, "max_sessions_per_ip", 10)
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "master_max_sessions_per_ip", 14)

    manager = SSHSessionManager(connection_pool_size=0)
    manager._pending_sessions_by_ip["10.0.0.5"] = 10
    create = AsyncMock(return_value="should-not-run")
    monkeypatch.setattr(manager, "_create_session_unreserved", create)
    with pytest.raises(SessionLimitError):
        await manager.create_session(
            host="h",
            port=22,
            username="u",
            password="pw",
            source_ip="10.0.0.5",
            owner_type="master",
        )
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_session_lane_stays_at_legacy_ten(monkeypatch):
    monkeypatch.setattr(settings, "max_sessions_per_ip", 10)
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "master_max_sessions_per_ip", 14)

    manager = SSHSessionManager(connection_pool_size=0)
    manager._pending_sessions_by_ip["10.0.0.5"] = 10
    create = AsyncMock(return_value="should-not-run")
    monkeypatch.setattr(manager, "_create_session_unreserved", create)
    with pytest.raises(SessionLimitError):
        await manager.create_session(
            host="h",
            port=22,
            username="u",
            password="pw",
            source_ip="10.0.0.5",
            owner_type="agent",
            privileged_capacity=True,
        )
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_disabled_master_label_stays_at_legacy_ten(monkeypatch):
    monkeypatch.setattr(settings, "max_sessions_per_ip", 10)
    monkeypatch.setattr(settings, "api_auth_enabled", False)
    monkeypatch.setattr(settings, "master_max_sessions_per_ip", 14)

    manager = SSHSessionManager(connection_pool_size=0)
    manager._pending_sessions_by_ip["10.0.0.5"] = 10
    create = AsyncMock(return_value="should-not-run")
    monkeypatch.setattr(manager, "_create_session_unreserved", create)
    with pytest.raises(SessionLimitError):
        await manager.create_session(
            host="h",
            port=22,
            username="u",
            password="pw",
            source_ip="10.0.0.5",
            owner_type="master",
            privileged_capacity=True,
        )
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_master_session_cap_falls_back_to_legacy_ten(monkeypatch):
    monkeypatch.setattr(settings, "max_sessions_per_ip", 10)
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "master_max_sessions_per_ip", 0)

    manager = SSHSessionManager(connection_pool_size=0)
    manager._pending_sessions_by_ip["10.0.0.5"] = 10
    create = AsyncMock(return_value="should-not-run")
    monkeypatch.setattr(manager, "_create_session_unreserved", create)
    with pytest.raises(SessionLimitError):
        await manager.create_session(
            host="h",
            port=22,
            username="u",
            password="pw",
            source_ip="10.0.0.5",
            owner_type="master",
            privileged_capacity=True,
        )
    create.assert_not_awaited()


_RATE_APP_SEQUENCE = count()


def _rate_app(identity: AuthIdentity | None, *, ordinary: int, setting_name: str) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, cast(Any, _rate_limit_exceeded_handler))
    app.add_middleware(SlowAPIMiddleware)

    @app.middleware("http")
    async def _identity_middleware(request: Request, call_next):
        if identity is not None:
            request.state.auth_identity = identity
        return await call_next(request)

    async def _limited(request: Request):
        return {"ok": True}

    lane_name = identity.token_type if identity is not None else "none"
    _limited.__name__ = f"_limited_{lane_name}_{setting_name}_{next(_RATE_APP_SEQUENCE)}"
    limited = rate_limit_mutation(
        ordinary,
        "minute",
        master_requests_setting=setting_name,
    )(_limited)
    app.post("/limited")(limited)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting_name",
    ["master_connect_rate_limit_requests", "master_execute_rate_limit_requests"],
)
async def test_configured_verified_master_rate_lane_exceeds_ordinary_limit(
    monkeypatch, setting_name
):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, setting_name, 4)
    identity = AuthIdentity(token_type="master", token="verified", scopes=("*",))
    app = _rate_app(identity, ordinary=2, setting_name=setting_name)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(4):
            assert (await client.post("/limited")).status_code == 200
        assert (await client.post("/limited")).status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        AuthIdentity(token_type="agent", token="agent", scopes=("ssh:connect",)),
        AuthIdentity(token_type="web-ui", token="jwt", scopes=("ssh:connect",)),
        None,
    ],
)
async def test_non_master_rate_lanes_keep_ordinary_limit(monkeypatch, identity):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "master_connect_rate_limit_requests", 4)
    app = _rate_app(identity, ordinary=2, setting_name="master_connect_rate_limit_requests")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/limited")).status_code == 200
        assert (await client.post("/limited")).status_code == 200
        assert (await client.post("/limited")).status_code == 429


@pytest.mark.asyncio
async def test_auth_disabled_does_not_activate_master_rate_lane(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", False)
    monkeypatch.setattr(settings, "master_connect_rate_limit_requests", 4)
    identity = AuthIdentity(token_type="master", token="", name="auth-disabled", scopes=("*",))
    app = _rate_app(identity, ordinary=2, setting_name="master_connect_rate_limit_requests")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/limited")).status_code == 200
        assert (await client.post("/limited")).status_code == 200
        assert (await client.post("/limited")).status_code == 429


@pytest.mark.asyncio
async def test_zero_master_rate_falls_back_to_ordinary_bucket(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "master_connect_rate_limit_requests", 0)
    identity = AuthIdentity(token_type="master", token="verified", scopes=("*",))
    app = _rate_app(identity, ordinary=2, setting_name="master_connect_rate_limit_requests")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/limited")).status_code == 200
        assert (await client.post("/limited")).status_code == 200
        assert (await client.post("/limited")).status_code == 429


def test_ssh_routes_wire_master_rate_settings_only_to_intended_control_plane_routes():
    connect_source = inspect.getsource(ssh_routes.ssh_connect)
    execute_source = inspect.getsource(ssh_routes.ssh_execute)
    argv_source = inspect.getsource(ssh_routes.ssh_execute_argv)
    prewarm_source = inspect.getsource(ssh_routes.ssh_prewarm)
    check_port_source = inspect.getsource(ssh_routes.check_port)

    assert 'master_requests_setting="master_connect_rate_limit_requests"' in connect_source
    assert 'master_requests_setting="master_execute_rate_limit_requests"' in execute_source
    assert 'master_requests_setting="master_execute_rate_limit_requests"' in argv_source
    assert "master_requests_setting=" not in prewarm_source
    assert "master_requests_setting=" not in check_port_source


@pytest.mark.asyncio
async def test_master_config_exposes_capacity_lane_without_secrets(monkeypatch):
    monkeypatch.setattr(settings, "master_max_sessions_per_ip", 16)
    monkeypatch.setattr(settings, "master_connect_rate_limit_requests", 30)
    monkeypatch.setattr(settings, "master_execute_rate_limit_requests", 180)
    identity = AuthIdentity(token_type="master", token="verified", scopes=("*",))

    result = await get_config(identity)

    assert result["master_max_sessions_per_ip"] == 16
    assert result["master_connect_rate_limit_requests"] == 30
    assert result["master_execute_rate_limit_requests"] == 180
    assert "api_key" not in result
    assert "encryption_key" not in result
