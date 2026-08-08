"""Tests for MCP auth middleware (oauth mode)."""

import os
from unittest.mock import patch

import pytest
from starlette.responses import JSONResponse


@pytest.fixture
def valid_token():
    return "test-token-123"


async def _mock_proxy(request):
    """Stub upstream — the middleware is what we test, not the real MCP server."""
    return JSONResponse({"ok": True})


@pytest.fixture
def token_client(valid_token):
    from starlette.testclient import TestClient

    with patch.dict(os.environ, {"MCP_PUBLIC_TOKEN": valid_token, "MCP_AUTH_MODE": "token"}):
        import importlib

        import examples.mcp_client_remote.server as srv

        importlib.reload(srv)
        srv.proxy_request = _mock_proxy
        app = srv.create_proxy_app()
        yield TestClient(app)


@pytest.fixture
def oauth_client(valid_token):
    from starlette.testclient import TestClient

    with patch.dict(os.environ, {"MCP_PUBLIC_TOKEN": valid_token, "MCP_AUTH_MODE": "oauth"}):
        import importlib

        import examples.mcp_client_remote.server as srv

        importlib.reload(srv)
        srv.proxy_request = _mock_proxy
        app = srv.create_proxy_app()
        yield TestClient(app)


def test_oauth_public_paths():
    from examples.mcp_client_remote.server import _is_oauth_public_path

    assert _is_oauth_public_path("/.well-known/oauth-authorization-server")
    assert _is_oauth_public_path("/oauth/authorize")
    assert _is_oauth_public_path("/oauth/token")
    assert _is_oauth_public_path("/oauth/register")
    assert not _is_oauth_public_path("/mcp")
    assert _is_oauth_public_path("/health")
    # Bare top-level forms (advertised by openid_configuration()'s
    # authorization_endpoint/token_endpoint/registration_endpoint) must
    # also stay public, exactly and with nested subpaths.
    assert _is_oauth_public_path("/authorize")
    assert _is_oauth_public_path("/token")
    assert _is_oauth_public_path("/register")
    assert _is_oauth_public_path("/token/refresh")


def test_oauth_public_paths_require_a_boundary_not_a_bare_prefix():
    """Regression (R5): path.startswith(("/authorize", "/token",
    "/register", "/health")) treated ANY path merely starting with one of
    those substrings as public -- a future route named e.g.
    /authorized_keys, /tokens-export, /registered-hosts, or
    /health-debug-internal would silently skip Bearer/mcp_token auth. Each
    must require an exact match or a "/"-bounded nested path.
    """
    from examples.mcp_client_remote.server import _is_oauth_public_path

    assert not _is_oauth_public_path("/authorized_keys")
    assert not _is_oauth_public_path("/authorize-legacy")
    assert not _is_oauth_public_path("/tokens-export")
    assert not _is_oauth_public_path("/token_leak")
    assert not _is_oauth_public_path("/registered-hosts")
    assert not _is_oauth_public_path("/registration-bypass")
    assert not _is_oauth_public_path("/health-debug-internal")
    assert not _is_oauth_public_path("/healthcheck-secret")


def test_token_mode_no_auth(token_client):
    """Token mode rejects requests without auth."""
    resp = token_client.get("/")
    assert resp.status_code == 401


def test_token_mode_mcp_token_valid(token_client, valid_token):
    """Token mode accepts valid mcp_token query param."""
    resp = token_client.get(f"/?mcp_token={valid_token}")
    assert resp.status_code not in (401, 403)


def test_token_mode_mcp_token_invalid(token_client):
    """Token mode rejects invalid mcp_token query param."""
    resp = token_client.get("/?mcp_token=wrong")
    assert resp.status_code in (401, 403)


def test_token_mode_bearer_valid(token_client, valid_token):
    """Token mode accepts valid Bearer token."""
    resp = token_client.get("/", headers={"Authorization": f"Bearer {valid_token}"})
    assert resp.status_code not in (401, 403)


def test_token_mode_bearer_invalid(token_client):
    """Token mode rejects invalid Bearer token."""
    resp = token_client.get("/", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code in (401, 403)


def test_oauth_mode_bearer_passthrough(oauth_client):
    """Bearer token is passed through in oauth mode."""
    resp = oauth_client.get("/", headers={"Authorization": "Bearer some-token"})
    assert resp.status_code not in (401, 403)


def test_oauth_mode_rejects_mcp_token(oauth_client, valid_token):
    """mcp_token is rejected in oauth mode."""
    resp = oauth_client.get(f"/?mcp_token={valid_token}")
    assert resp.status_code == 401


def test_oauth_mode_no_auth(oauth_client):
    """Missing auth in oauth mode returns 401."""
    resp = oauth_client.get("/")
    assert resp.status_code == 401


def test_oauth_endpoints_public_without_token(token_client):
    """OAuth discovery endpoints must work without any auth."""
    resp = token_client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code not in (401, 403)
