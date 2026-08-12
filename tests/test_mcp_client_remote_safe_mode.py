"""Tests for mcp_client_remote safe registration mode (runtime config).

Regression: the documented safe-mode env vars (MCP_CLIENT_SAFE_MODE,
MCP_ACCESS_PROFILE) were not applied at runtime — MCP_DEFAULT_ACCESS_PROFILE
was read but never used, and MCP_ACCESS_PROFILE was not read at all, so a
deployment following mcp_client.*.env.example ran with the full default
profile (or "operator") and unsafe tools reachable. Safe mode must force
the mcp_client_safe profile and the proxy must resolve the shared default
token to the configured profile instead of failing open to the full scope
set or closed to nothing.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from starlette.requests import Request

from examples.mcp_server.tool_scopes import ACCESS_PROFILES, get_profile_scopes


@pytest.fixture
def _reload_srv():
    import importlib

    import examples.mcp_client_remote.server as srv

    importlib.reload(srv)
    return srv


class TestDefaultAccessProfile:
    def test_defaults_to_operator(self, _reload_srv):
        assert _reload_srv._default_access_profile() == "operator"

    def test_mcp_access_profile_env_respected(self, monkeypatch, _reload_srv):
        with patch.dict(
            os.environ, {"MCP_ACCESS_PROFILE": "viewer"}, clear=False
        ):
            import importlib

            importlib.reload(_reload_srv)
            assert _reload_srv._default_access_profile() == "viewer"

    def test_safe_mode_forces_mcp_client_safe(self, _reload_srv):
        """Safe mode wins over any configured profile: a misconfigured
        MCP_ACCESS_PROFILE=MCP_DEFAULT_ACCESS_PROFILE=full must still
        resolve to the safe bundle."""
        with patch.dict(
            os.environ,
            {
                "MCP_CLIENT_SAFE_MODE": "true",
                "MCP_ACCESS_PROFILE": "full",
                "MCP_DEFAULT_ACCESS_PROFILE": "full",
            },
            clear=False,
        ):
            import importlib

            importlib.reload(_reload_srv)
            assert _reload_srv.MCP_CLIENT_SAFE_MODE is True
            assert _reload_srv._default_access_profile() == "mcp_client_safe"

    def test_safe_mode_off_keeps_configured_profile(self, _reload_srv):
        with patch.dict(
            os.environ,
            {"MCP_CLIENT_SAFE_MODE": "false", "MCP_ACCESS_PROFILE": "operator"},
            clear=False,
        ):
            import importlib

            importlib.reload(_reload_srv)
            assert _reload_srv.MCP_CLIENT_SAFE_MODE is False
            assert _reload_srv._default_access_profile() == "operator"

    def test_safe_profile_scopes_never_include_admin_or_docker(self, _reload_srv):
        safe = set(get_profile_scopes("mcp_client_safe"))
        assert safe == set(ACCESS_PROFILES["mcp_client_safe"])
        assert "mcp:admin" not in safe
        assert "mcp:docker" not in safe
        assert "mcp:docker:admin" not in safe
        assert "mcp:execute" not in safe
        assert "mcp:handoff" not in safe


class TestDefaultTokenScopeFallback:
    async def _scopes_for(self, srv, token: str | None, public_token: str = "shared-tok"):
        with patch.dict(
            os.environ,
            {"MCP_PUBLIC_TOKEN": public_token, "MCP_AUTH_MODE": "token"},
            clear=False,
        ):
            import importlib

            importlib.reload(srv)
            with patch.object(
                srv._mcp_mod._auth_provider, "load_access_token"
            ) as mock_load:
                mock_load.return_value = None
                return await srv._get_token_scopes(token)

    @pytest.mark.asyncio
    async def test_unknown_token_fails_closed(self, _reload_srv):
        scopes = await self._scopes_for(_reload_srv, "not-the-shared-token")
        assert scopes == []

    @pytest.mark.asyncio
    async def test_no_token_fails_closed(self, _reload_srv):
        scopes = await self._scopes_for(_reload_srv, None)
        assert scopes == []

    @pytest.mark.asyncio
    async def test_shared_token_resolves_to_operator_profile(self, _reload_srv):
        scopes = await self._scopes_for(_reload_srv, "shared-tok")
        assert scopes == get_profile_scopes("operator")

    @pytest.mark.asyncio
    async def test_shared_token_in_safe_mode_resolves_to_safe_profile(
        self, _reload_srv
    ):
        with patch.dict(
            os.environ,
            {
                "MCP_PUBLIC_TOKEN": "shared-tok",
                "MCP_AUTH_MODE": "token",
                "MCP_CLIENT_SAFE_MODE": "true",
                "MCP_ACCESS_PROFILE": "full",
            },
            clear=False,
        ):
            import importlib

            importlib.reload(_reload_srv)
            with patch.object(
                _reload_srv._mcp_mod._auth_provider, "load_access_token"
            ) as mock_load:
                mock_load.return_value = None
                scopes = await _reload_srv._get_token_scopes("shared-tok")
        assert scopes == get_profile_scopes("mcp_client_safe")


class TestExecuteArgvScopeEnforcement:
    @pytest.mark.asyncio
    async def test_execute_argv_denied_without_execute_scope(self, _reload_srv):
        body = (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            b'"params":{"name":"execute_argv","arguments":{"argv":["git","rev-parse","HEAD"]}}}'
        )
        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
        request = Request(scope)
        request.state.auth_token = "tok"

        with patch.object(_reload_srv, "MCP_SCOPE_ENFORCEMENT", "enforce"):
            with patch.object(_reload_srv, "_get_token_scopes", return_value=["mcp:read", "mcp:project"]):
                resp = await _reload_srv._check_tool_scope(request, "/mcp", body)

        assert resp is not None
        assert resp.status_code == 403
        assert b"insufficient_scope" in resp.body

    @pytest.mark.asyncio
    async def test_execute_argv_allowed_with_execute_scope(self, _reload_srv):
        body = (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            b'"params":{"name":"execute_argv","arguments":{"argv":["git","rev-parse","HEAD"]}}}'
        )
        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
        request = Request(scope)
        request.state.auth_token = "tok"

        with patch.object(_reload_srv, "MCP_SCOPE_ENFORCEMENT", "enforce"):
            with patch.object(
                _reload_srv,
                "_get_token_scopes",
                return_value=["mcp:read", "mcp:project", "mcp:execute"],
            ):
                resp = await _reload_srv._check_tool_scope(request, "/mcp", body)

        assert resp is None
