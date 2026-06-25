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
def test_auth_settings_configured_in_mixed_mode():
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv.mcp.settings.auth is not None
    assert srv.mcp.settings.auth.client_registration_options.enabled is True
    assert "mcp:read" in srv.mcp.settings.auth.client_registration_options.valid_scopes
