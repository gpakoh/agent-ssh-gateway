"""Tests for auth middleware: IP allowlist + API key + scope enforcement."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

from app.main import app
from app.config import Settings, settings
from app.auth_middleware import (
    get_client_ip,
    is_ip_allowed,
    verify_api_key,
    parse_cidrs,
    ws_auth_check,
    require_scope,
    AuthIdentity,
    CLOSE_POLICY_VIOLATION,
    VALID_AGENT_SCOPES,
)


@contextmanager
def _client(**kw):
    """TestClient with patched client IP 127.0.0.1 so IP
    allowlist does not reject 'testclient' hostname from httpx."""
    with TestClient(app, **kw) as c:
        yield c


def _mock_request(client_host="127.0.0.1", xff=None):
    req = MagicMock()
    req.client.host = client_host
    headers = {}
    if xff:
        headers["X-Forwarded-For"] = xff
    req.headers = headers
    return req


# ---------------------------------------------------------------------------
# Unit Tests For Individual Middleware Functions
# ---------------------------------------------------------------------------


class TestGetClientIp:
    def test_direct_ip(self):
        req = _mock_request(client_host="10.0.0.5")
        assert get_client_ip(req, []) == "10.0.0.5"

    def test_trusted_proxy_xff(self):
        req = _mock_request(client_host="10.0.0.1", xff="192.168.1.50")
        trusted = parse_cidrs("10.0.0.0/8")
        assert get_client_ip(req, trusted) == "192.168.1.50"

    def test_untrusted_proxy_ignores_xff(self):
        req = _mock_request(client_host="192.0.2.10", xff="10.0.0.99")
        trusted = parse_cidrs("10.0.0.0/8")
        assert get_client_ip(req, trusted) == "192.0.2.10"


class TestIsIpAllowed:
    def test_allowed_ip(self):
        nets = parse_cidrs("10.0.0.0/8")
        assert is_ip_allowed("10.0.0.5", nets) is True

    def test_forbidden_ip(self):
        nets = parse_cidrs("10.0.0.0/8")
        assert is_ip_allowed("192.0.2.10", nets) is False

    def test_invalid_ip_returns_false(self):
        nets = parse_cidrs("0.0.0.0/0")
        assert is_ip_allowed("not-an-ip", nets) is False


class TestVerifyApiKey:
    @pytest.mark.asyncio
    async def test_header_match(self):
        req = _mock_request()
        req.headers = {"X-API-Key": "secret-42"}
        identity = await verify_api_key(req, "secret-42")
        assert identity is not None
        assert identity.token_type == "master"

    @pytest.mark.asyncio
    async def test_header_mismatch(self):
        req = _mock_request()
        req.headers = {"X-API-Key": "wrong"}
        assert await verify_api_key(req, "secret-42") is None

    @pytest.mark.asyncio
    async def test_bearer_token(self):
        req = _mock_request()
        req.headers = {"Authorization": "Bearer my-token"}
        identity = await verify_api_key(req, "my-token")
        assert identity is not None
        assert identity.token_type == "master"

    @pytest.mark.asyncio
    async def test_no_key(self):
        req = _mock_request()
        req.headers = {}
        assert await verify_api_key(req, "secret-42") is None


# ---------------------------------------------------------------------------
# Integration Tests — Auth Disabled
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_disabled(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", False)
    monkeypatch.setattr(settings, "api_key", "")


class TestAuthDisabled:
    def test_health_without_key(self, auth_disabled):
        with _client() as client:
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_api_servers_without_key(self, auth_disabled):
        with _client() as client:
            resp = client.get("/api/servers")
        assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Integration Tests — Auth Enabled, API Key
# ---------------------------------------------------------------------------


@pytest.fixture
def api_key_auth(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "secret-42")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


class TestApiKey:
    def test_health_is_public(self, api_key_auth):
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_api_servers_without_key_returns_401(self, api_key_auth):
        with TestClient(app) as client:
            resp = client.get("/api/servers")
        assert resp.status_code == 401

    def test_api_servers_with_wrong_key_returns_401(self, api_key_auth):
        with TestClient(app) as client:
            resp = client.get("/api/servers", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    def test_api_servers_with_correct_key_not_401(self, api_key_auth):
        with TestClient(app) as client:
            resp = client.get("/api/servers", headers={"X-API-Key": "secret-42"})
        assert resp.status_code not in (401, 403)

    def test_check_port_without_key_returns_401(self, api_key_auth):
        with TestClient(app) as client:
            resp = client.get("/api/ssh/check-port?host=127.0.0.1&port=22")
        assert resp.status_code == 401

    def test_check_port_with_correct_key_not_401(self, api_key_auth):
        with TestClient(app) as client:
            resp = client.get(
                "/api/ssh/check-port?host=10.0.0.1&port=22",
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code not in (401, 403)


# ---------------------------------------------------------------------------
# Integration Tests — IP Allowlist
# ---------------------------------------------------------------------------


@pytest.fixture
def ip_allowlist_auth(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "secret-42")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "10.0.0.0/8")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")


class TestIpAllowlist:
    def test_forbidden_ip_returns_403(self, ip_allowlist_auth, monkeypatch):
        monkeypatch.setattr(
            "app.auth_middleware.get_client_ip", lambda req, trusted: "192.0.2.10"
        )
        with TestClient(app) as client:
            resp = client.get("/api/servers", headers={"X-API-Key": "secret-42"})
        assert resp.status_code == 403

    def test_allowed_ip_with_correct_key_succeeds(self, ip_allowlist_auth, monkeypatch):
        monkeypatch.setattr(
            "app.auth_middleware.get_client_ip", lambda req, trusted: "10.0.0.5"
        )
        with TestClient(app) as client:
            resp = client.get("/api/servers", headers={"X-API-Key": "secret-42"})
        assert resp.status_code not in (401, 403)


# ---------------------------------------------------------------------------
# Fail-closed Tests
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_auth_enabled_no_key_returns_503(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "")
        with _client() as client:
            resp = client.get("/api/servers")
        assert resp.status_code == 503

    def test_invalid_allowed_cidr_returns_503(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "not-a-cidr,also-bogus")
        with _client() as client:
            resp = client.get("/api/servers", headers={"X-API-Key": "secret-42"})
        assert resp.status_code == 503

    def test_invalid_trusted_cidr_returns_503(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "bad,,,crap")
        with _client() as client:
            resp = client.get("/api/servers", headers={"X-API-Key": "secret-42"})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Websocket Auth — Ws_auth_check Unit Tests + Integration
# ---------------------------------------------------------------------------


@pytest.fixture
def ws_settings(monkeypatch):
    """Enable API auth with a known key and allow everything."""
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "ws-secret-99")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")


@pytest.fixture
def ws_ip_deny_settings(monkeypatch):
    """Auth enabled with IP allowlist that blocks 'testclient'."""
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "ws-secret-99")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "10.0.0.0/8")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")


class TestWsAuthCheckUnit:
    """Direct unit tests for ws_auth_check() — no TestClient needed."""

    def _mock_ws(self, host="127.0.0.1", headers=None, query=None):
        ws = MagicMock()
        ws.client.host = host
        ws.headers = headers or {}
        ws.query_params = query or {}
        return ws

    def test_no_key_returns_1008(self, ws_settings):
        ws = self._mock_ws()
        result = asyncio.run(ws_auth_check(ws, settings))
        assert result is not None
        assert result[0] == CLOSE_POLICY_VIOLATION

    def test_wrong_key_returns_1008(self, ws_settings):
        ws = self._mock_ws(headers={"X-API-Key": "wrong"})
        result = asyncio.run(ws_auth_check(ws, settings))
        assert result is not None
        assert result[0] == CLOSE_POLICY_VIOLATION

    def test_correct_key_returns_none(self, ws_settings):
        ws = self._mock_ws(headers={"X-API-Key": "ws-secret-99"})
        result = asyncio.run(ws_auth_check(ws, settings))
        assert result is None

    def test_ip_denied_returns_1008(self, ws_ip_deny_settings):
        ws = self._mock_ws(host="192.0.2.10", headers={"X-API-Key": "ws-secret-99"})
        result = asyncio.run(ws_auth_check(ws, settings))
        assert result is not None
        assert result[0] == CLOSE_POLICY_VIOLATION

    def test_bearer_token_accepted(self, ws_settings):
        ws = self._mock_ws(headers={"Authorization": "Bearer ws-secret-99"})
        result = asyncio.run(ws_auth_check(ws, settings))
        assert result is None

    def test_auth_disabled_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", False)
        ws = self._mock_ws()
        result = asyncio.run(ws_auth_check(ws, settings))
        assert result is None

    def test_query_string_key_not_accepted(self, ws_settings):
        """Query string ?api_key=... must NOT work — only header auth."""
        ws = self._mock_ws(query={"api_key": "ws-secret-99"})
        result = asyncio.run(ws_auth_check(ws, settings))
        assert result is not None
        assert result[0] == CLOSE_POLICY_VIOLATION


WS_STREAM = "/api/ssh/execute/stream"
WS_FILE_WATCH = "/api/file/watch"


class TestWebSocketAuthIntegration:
    """Integration tests against the real /api/ssh/execute/stream and
    /api/file/watch endpoints.  Auth is tested directly; after auth passes
    we send empty session_id/command so the handler returns a business error
    without reaching external dependencies (manager)."""

    @pytest.fixture
    def ws_auth(self, monkeypatch):
        """Enable API auth, patch is_ip_allowed so TestClient's
        'testclient' hostname is not rejected."""
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "ws-secret-99")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.is_ip_allowed", lambda ip, nets: True)

    @pytest.fixture
    def ws_ip_deny(self, monkeypatch):
        """Enable API auth with a restrictive IP allowlist that
        blocks 'testclient'.  is_ip_allowed is NOT patched."""
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "ws-secret-99")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "10.0.0.0/8")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")

    # -- Ssh/execute/stream ------------------------------------------------

    def test_stream_without_key_denied(self, ws_auth):
        self._expect_reject(WS_STREAM, 1008)

    def test_stream_wrong_key_denied(self, ws_auth):
        self._expect_reject(WS_STREAM, 1008, headers={"X-API-Key": "wrong"})

    def test_stream_correct_key_passes_auth(self, ws_auth):
        """Valid key + IP → auth OK, then empty session → business error."""
        self._expect_business_error(
            WS_STREAM,
            headers={"X-API-Key": "ws-secret-99"},
            request={"session_id": "", "command": ""},
        )

    def test_stream_ip_deny_even_with_correct_key(self, ws_ip_deny):
        """IP allowlist blocks → 1008 even with a valid key."""
        self._expect_reject(
            WS_STREAM,
            1008,
            headers={"X-API-Key": "ws-secret-99"},
        )

    # -- File/watch --------------------------------------------------------

    def test_file_watch_without_key_denied(self, ws_auth):
        self._expect_reject(WS_FILE_WATCH, 1008)

    def test_file_watch_correct_key_passes_auth(self, ws_auth):
        """Valid key + IP → auth OK, then empty session → business error."""
        self._expect_business_error(
            WS_FILE_WATCH,
            headers={"X-API-Key": "ws-secret-99"},
            request={"session_id": "", "path": ""},
        )

    # -- Helpers -----------------------------------------------------------

    def _expect_reject(self, url, code, headers=None):
        from starlette.websockets import WebSocketDisconnect

        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(url, headers=headers or {}):
                    pass
            assert exc.value.code == code

    def _expect_business_error(self, url, headers, request):
        with TestClient(app) as client:
            with client.websocket_connect(url, headers=headers) as ws:
                ws.send_json(request)
                resp = ws.receive_json()
                assert resp.get("type") == "error"


# ---------------------------------------------------------------------------
# SDK Download Auth Tests
# ---------------------------------------------------------------------------

SDK_URL = "/api/sdk/download"


@pytest.fixture
def sdk_auth(monkeypatch):
    """Auth enabled with known key, IP patched so "testclient" passes."""
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "sdk-key-77")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


class TestSdkAuth:
    """/api/sdk/download auth — header and Bearer accepted, query string rejected."""

    def test_header_valid_passes_auth(self, sdk_auth):
        """Valid X-API-Key header → auth passes (404 from missing file is OK)."""
        with _client() as client:
            resp = client.get(SDK_URL, headers={"X-API-Key": "sdk-key-77"})
        assert resp.status_code not in (401, 403)

    def test_bearer_valid_passes_auth(self, sdk_auth):
        """Authorization: Bearer → middleware accepts, passes to endpoint."""
        with _client() as client:
            resp = client.get(SDK_URL, headers={"Authorization": "Bearer sdk-key-77"})
        assert resp.status_code not in (401, 403)

    def test_query_only_returns_401(self, sdk_auth):
        """?api_key=... without header → middleware denies at key
        check (verify_api_key does not inspect query params)."""
        with _client() as client:
            resp = client.get(f"{SDK_URL}?api_key=sdk-key-77")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Agent Token TTL
# ---------------------------------------------------------------------------


def test_settings_env_agent_token_gets_startup_expiry():
    cfg = Settings(agent_token="boot-agent", agent_token_ttl=60)
    assert cfg.agent_token_expires_at is not None
    assert cfg.agent_token_expires_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_verify_api_key_accepts_non_expired_agent_token(monkeypatch):
    req = _mock_request()
    req.headers = {"X-API-Key": "agent-live"}
    monkeypatch.setattr(settings, "agent_token", "agent-live")
    monkeypatch.setattr(
        settings,
        "agent_token_expires_at",
        datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    identity = await verify_api_key(req, "main-key", settings=settings)
    assert identity is not None
    assert identity.token_type == "agent"


@pytest.mark.asyncio
async def test_verify_api_key_rejects_expired_agent_token(monkeypatch):
    req = _mock_request()
    req.headers = {"X-API-Key": "agent-expired"}
    monkeypatch.setattr(settings, "agent_token", "agent-expired")
    monkeypatch.setattr(
        settings,
        "agent_token_expires_at",
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    assert await verify_api_key(req, "main-key", settings=settings) is None


def test_ws_auth_rejects_expired_agent_token(ws_settings, monkeypatch):
    monkeypatch.setattr(settings, "agent_token", "ws-agent-expired")
    monkeypatch.setattr(
        settings,
        "agent_token_expires_at",
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    ws = TestWsAuthCheckUnit()._mock_ws(headers={"X-API-Key": "ws-agent-expired"})
    result = asyncio.run(ws_auth_check(ws, settings))
    assert result is not None
    assert result[0] == CLOSE_POLICY_VIOLATION


# ---------------------------------------------------------------------------
# Agent Token Management — Master API Key Required
# ---------------------------------------------------------------------------


class TestAgentTokenManagement:
    """Agent token create/refresh endpoints require master API key, not agent tokens."""

    def test_master_key_can_create_agent_token(self, api_key_auth):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/agent/token",
                headers={"X-API-Key": "secret-42"},
                json={"name": "test-agent", "ttl_seconds": 3600},
            )
        assert resp.status_code not in (401, 403)

    def test_agent_token_cannot_create_another_token(self, api_key_auth, monkeypatch):
        monkeypatch.setattr(settings, "agent_token", "agent-live-token")
        monkeypatch.setattr(
            settings, "agent_token_expires_at",
            datetime.now(timezone.utc) + timedelta(hours=1),
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/token",
                headers={"X-API-Key": "agent-live-token"},
                json={"name": "second-agent", "ttl_seconds": 3600},
            )
        assert resp.status_code == 401

    def test_agent_token_cannot_refresh_another_token(self, api_key_auth, monkeypatch):
        monkeypatch.setattr(settings, "agent_token", "agent-live-token")
        monkeypatch.setattr(
            settings, "agent_token_expires_at",
            datetime.now(timezone.utc) + timedelta(hours=1),
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/token/refresh",
                headers={"X-API-Key": "agent-live-token"},
                json={"token": "agent-live-token", "ttl_seconds": 3600},
            )
        assert resp.status_code == 401

    def test_no_key_returns_401_on_create(self, api_key_auth):
        with TestClient(app) as client:
            resp = client.post("/api/agent/token", json={"name": "test"})
        assert resp.status_code == 401

    def test_no_key_returns_401_on_refresh(self, api_key_auth):
        with TestClient(app) as client:
            resp = client.post("/api/agent/token/refresh", json={"token": "x"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# SSH Key Upload — Disabled by Default
# ---------------------------------------------------------------------------


class TestSshKeyUpload:
    """SSH key upload endpoint respects ssh_key_upload_enabled flag."""

    def test_upload_disabled_by_default(self, api_key_auth):
        with TestClient(app) as client:
            resp = client.post(
                "/api/ssh/keys",
                headers={"X-API-Key": "secret-42"},
                files={
                    "file": (
                        "id_test",
                        b"-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----",
                        "text/plain",
                    )
                },
            )
        assert resp.status_code == 403
        assert "disabled" in resp.text.lower()

    def test_upload_can_be_enabled(self, api_key_auth, monkeypatch):
        monkeypatch.setattr(settings, "ssh_key_upload_enabled", True)
        with TestClient(app) as client:
            resp = client.post(
                "/api/ssh/keys",
                headers={"X-API-Key": "secret-42"},
                files={
                    "file": (
                        "id_test",
                        b"-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----",
                        "text/plain",
                    )
                },
            )
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Scope Enforcement Tests
# ---------------------------------------------------------------------------


class TestScopeEnforcement:
    """require_scope() — master key bypasses, agent token needs matching scope."""

    def _make_identity(self, token_type="master", scopes=("*",)):
        return AuthIdentity(token_type=token_type, name="test", token="x", scopes=scopes)

    async def _make_scope_req(self, identity):
        req = _mock_request()
        req.state.auth_identity = identity
        return req

    @pytest.mark.asyncio
    async def test_master_key_bypasses_any_scope(self):
        ident = self._make_identity(token_type="master")
        dep = require_scope("ssh:connect")
        req = await self._make_scope_req(ident)
        result = await dep(req)
        assert result is ident

    @pytest.mark.asyncio
    async def test_wildcard_scope_grants_all(self):
        ident = self._make_identity(token_type="agent", scopes=("*",))
        dep = require_scope("ssh:execute")
        req = await self._make_scope_req(ident)
        result = await dep(req)
        assert result is ident

    @pytest.mark.asyncio
    async def test_matching_scope_grants_access(self):
        ident = self._make_identity(token_type="agent", scopes=("ssh:connect",))
        dep = require_scope("ssh:connect")
        req = await self._make_scope_req(ident)
        result = await dep(req)
        assert result is ident

    @pytest.mark.asyncio
    async def test_missing_scope_raises_403(self):
        ident = self._make_identity(token_type="agent", scopes=("ssh:connect",))
        dep = require_scope("ssh:execute")
        req = await self._make_scope_req(ident)
        with pytest.raises(HTTPException) as exc:
            await dep(req)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_identity_raises_401(self):
        dep = require_scope("ssh:connect")
        req = await self._make_scope_req(None)
        with pytest.raises(HTTPException) as exc:
            await dep(req)
        assert exc.value.status_code == 401

    def test_valid_agent_scopes_defined(self):
        assert "ssh:connect" in VALID_AGENT_SCOPES
        assert "ssh:execute" in VALID_AGENT_SCOPES
        assert "ssh:port-check" in VALID_AGENT_SCOPES
        assert "ssh:disconnect" in VALID_AGENT_SCOPES
        assert "ssh:files" in VALID_AGENT_SCOPES
        assert "jobs:read" in VALID_AGENT_SCOPES
        assert "jobs:run" in VALID_AGENT_SCOPES


# ---------------------------------------------------------------------------
# Agent Token with Scopes — Integration Tests
# ---------------------------------------------------------------------------


class TestAgentTokenWithScopes:
    """Agent token requests with scopes are validated and stored."""

    @staticmethod
    def _mock_store():
        store = MagicMock()
        store.connected = True
        store.set_token = AsyncMock()
        store.validate_token = AsyncMock(return_value=(True, ["ssh:port-check"]))
        store.disconnect = AsyncMock()
        return store

    def test_create_token_with_default_scopes(self, api_key_auth, monkeypatch):
        with TestClient(app) as client:
            monkeypatch.setattr("app.state.agent_token_store", self._mock_store())
            resp = client.post(
                "/api/agent/token",
                headers={"X-API-Key": "secret-42"},
                json={"name": "agent", "ttl_seconds": 3600},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "scopes" in data
        assert data["scopes"] == ["ssh:connect", "ssh:execute"]

    def test_create_token_with_custom_scopes(self, api_key_auth, monkeypatch):
        with TestClient(app) as client:
            monkeypatch.setattr("app.state.agent_token_store", self._mock_store())
            resp = client.post(
                "/api/agent/token",
                headers={"X-API-Key": "secret-42"},
                json={
                    "name": "agent",
                    "ttl_seconds": 3600,
                    "scopes": ["ssh:port-check"],
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["scopes"] == ["ssh:port-check"]

    def test_create_token_with_invalid_scope_returns_400(self, api_key_auth, monkeypatch):
        with TestClient(app) as client:
            monkeypatch.setattr("app.state.agent_token_store", self._mock_store())
            resp = client.post(
                "/api/agent/token",
                headers={"X-API-Key": "secret-42"},
                json={
                    "name": "agent",
                    "ttl_seconds": 3600,
                    "scopes": ["invalid:scope"],
                },
            )
        assert resp.status_code == 400
