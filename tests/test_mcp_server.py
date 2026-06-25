"""Tests for MCP server AuthSettings configuration."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# server.py needs sys.path set up for its internal imports
EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))


@pytest.fixture(autouse=True)
def reset_env():
    """Ensure clean MCP_AUTH_MODE for each test that doesn't override it."""
    with patch.dict(os.environ, {"MCP_AUTH_MODE": "token"}, clear=False):
        import importlib

        import examples.mcp_server.server as srv

        importlib.reload(srv)
        yield


def test_auth_disabled_by_default():
    """Default MCP_AUTH_MODE=token should not configure auth."""
    from examples.mcp_server.server import mcp

    assert mcp.settings.auth is None


@patch.dict(os.environ, {"MCP_AUTH_MODE": "mixed"})
def test_oauth_provider_initialized_in_mixed_mode():
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv._auth_provider is not None


@patch.dict(os.environ, {"MCP_AUTH_MODE": "token"})
def test_oauth_provider_not_initialized_in_token_mode():
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv._auth_provider is None


@patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"})
def test_oauth_provider_initialized_in_oauth_mode():
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv._auth_provider is not None


@patch.dict(os.environ, {"MCP_AUTH_MODE": "mixed"})
def test_mixed_mode_uses_proxy_auth():
    """In mixed mode, FastMCP auth is disabled; proxy handles auth."""
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv.mcp.settings.auth is None


@patch.dict(os.environ, {"MCP_AUTH_MODE": "mixed", "MCP_PUBLIC_TOKEN": "test-token"})
def test_mixed_mode_registers_mcp_token():
    """In mixed mode, MCP_PUBLIC_TOKEN is pre-registered as a valid access token."""
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    token = srv._auth_provider.verify_access_token("test-token")
    assert token is not None
    assert token.client_id == "mcp_token_client"


@patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"})
def test_oauth_mode_uses_fastmcp_auth():
    """In oauth mode, FastMCP auth is configured with provider and settings."""
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv.mcp.settings.auth is not None
    assert srv.mcp.settings.auth.client_registration_options.enabled is True
    assert "mcp:read" in srv.mcp.settings.auth.client_registration_options.valid_scopes
