"""Tests for MCP auth middleware (mixed mode)."""

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def valid_token():
    return "test-token-123"


@pytest.fixture
def token_client(valid_token):
    from starlette.testclient import TestClient

    with patch.dict(os.environ, {"MCP_PUBLIC_TOKEN": valid_token, "MCP_AUTH_MODE": "token"}):
        import importlib

        import examples.chatgpt_remote_mcp.server as srv
        importlib.reload(srv)
        app = srv.create_proxy_app()
        yield TestClient(app)


@pytest.fixture
def mixed_client(valid_token):
    from starlette.testclient import TestClient

    with patch.dict(os.environ, {"MCP_PUBLIC_TOKEN": valid_token, "MCP_AUTH_MODE": "mixed"}):
        import importlib

        import examples.chatgpt_remote_mcp.server as srv
        importlib.reload(srv)
        app = srv.create_proxy_app()
        yield TestClient(app)


def test_oauth_public_paths():
    from examples.chatgpt_remote_mcp.server import _is_oauth_public_path

    assert _is_oauth_public_path("/.well-known/oauth-authorization-server")
    assert _is_oauth_public_path("/oauth/authorize")
    assert _is_oauth_public_path("/oauth/token")
    assert _is_oauth_public_path("/oauth/register")
    assert not _is_oauth_public_path("/mcp")
    assert not _is_oauth_public_path("/health")


def test_token_mode_requires_token(token_client):
    resp = token_client.get("/")
    assert resp.status_code == 401


def test_token_mode_valid_token(token_client, valid_token):
    resp = token_client.get(f"/?mcp_token={valid_token}")
    assert resp.status_code not in (401, 403)  # backend may return 404/502, but auth passed


def test_token_mode_invalid_token(token_client):
    resp = token_client.get("/?mcp_token=wrong")
    assert resp.status_code in (401, 403)


def test_mixed_mode_no_auth(mixed_client):
    resp = mixed_client.get("/")
    assert resp.status_code == 401


def test_mixed_mode_mcp_token_valid(mixed_client, valid_token):
    resp = mixed_client.get(f"/?mcp_token={valid_token}")
    assert resp.status_code not in (401, 403)  # auth passed


def test_mixed_mode_mcp_token_invalid(mixed_client):
    resp = mixed_client.get("/?mcp_token=wrong")
    assert resp.status_code == 403


def test_mixed_mode_bearer_passthrough(mixed_client):
    """Bearer token is passed through to FastMCP (not validated by proxy)."""
    resp = mixed_client.get("/", headers={"Authorization": "Bearer some-token"})
    assert resp.status_code not in (401, 403)  # auth passed


def test_oauth_endpoints_public_without_token(token_client):
    """OAuth discovery endpoints must work without any auth."""
    resp = token_client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code not in (401, 403)  # passed middleware


def test_oauth_endpoints_public_in_mixed_mode(mixed_client):
    resp = mixed_client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code not in (401, 403)


def test_mixed_mode_bearer_preferred(mixed_client, valid_token):
    """When both Bearer and mcp_token are present, Bearer wins."""
    resp = mixed_client.get(
        f"/?mcp_token={valid_token}",
        headers={"Authorization": "Bearer some-token"},
    )
    assert resp.status_code not in (401, 403)


@pytest.fixture
def oauth_client(valid_token):
    from starlette.testclient import TestClient

    with patch.dict(os.environ, {"MCP_PUBLIC_TOKEN": valid_token, "MCP_AUTH_MODE": "oauth"}):
        import importlib

        import examples.chatgpt_remote_mcp.server as srv
        importlib.reload(srv)
        app = srv.create_proxy_app()
        yield TestClient(app)


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
