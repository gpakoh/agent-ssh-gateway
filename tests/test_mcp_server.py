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
    with patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"}, clear=False):
        import importlib

        import examples.mcp_server.server as srv

        importlib.reload(srv)
        yield


def test_auth_enabled_by_default():
    """Default MCP_AUTH_MODE=oauth should configure auth."""
    from examples.mcp_server.server import mcp

    assert mcp.settings.auth is not None
    assert mcp.settings.auth.client_registration_options.enabled is True


@patch.dict(os.environ, {"MCP_AUTH_MODE": "token", "MCP_PUBLIC_TOKEN": "test-token"})
def test_token_mode_initializes_provider():
    """Token mode initializes GatewayOAuthProvider with MCP_PUBLIC_TOKEN."""
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv._auth_provider is not None
    token = srv._auth_provider.verify_access_token("test-token")
    assert token is not None
    assert token.client_id == "mcp_static_client"


@patch.dict(os.environ, {"MCP_AUTH_MODE": "token", "MCP_PUBLIC_TOKEN": ""})
def test_token_mode_requires_token():
    """Token mode raises ValueError if MCP_PUBLIC_TOKEN is empty."""
    import importlib

    import examples.mcp_server.server as srv

    with pytest.raises(ValueError, match="MCP_PUBLIC_TOKEN is required"):
        importlib.reload(srv)


@patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"})
def test_oauth_provider_initialized_in_oauth_mode():
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv._auth_provider is not None


@patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"})
def test_oauth_mode_configures_auth():
    """In oauth mode, FastMCP auth is configured with provider and settings."""
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv.mcp.settings.auth is not None
    assert srv.mcp.settings.auth.client_registration_options.enabled is True
    assert "mcp:read" in srv.mcp.settings.auth.client_registration_options.valid_scopes


@patch.dict(
    os.environ,
    {
        "MCP_AUTH_MODE": "oauth",
        "MCP_EXTRA_TOKENS_JSON": '{"extra-token-1": "mcp_client_sfae"}',
    },
)
def test_extra_token_unknown_profile_fails_closed():
    """Regression: a typo'd extra-token profile must NOT resolve to the
    full SUPPORTED_SCOPES set (fail-open). It must fail closed to the
    operator profile, so the token cannot reach admin/docker/execute."""
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    assert srv._auth_provider is not None
    token = srv._auth_provider.verify_access_token("extra-token-1")
    assert token is not None
    assert "mcp:admin" not in token.scopes
    assert "mcp:execute" not in token.scopes
    assert "mcp:docker" not in token.scopes
    assert "mcp:docker:admin" not in token.scopes
    assert "mcp:agent-run" not in token.scopes
    assert "mcp:read" in token.scopes
    assert "mcp:project" in token.scopes
