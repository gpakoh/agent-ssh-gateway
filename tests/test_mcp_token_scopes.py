"""Tests for examples.mcp_server.server._get_token_scopes.

Regression coverage for a real bug: the docker admin-scope checks
(docker_prune type=volume/system, docker_compose_down volumes=True)
read scopes from a MCP_TOKEN_SCOPES env var that the running service
never actually sets, so the check always saw an empty scope list and
always denied admin actions regardless of the caller's real profile.
The fix reads the authenticated request's AccessToken via FastMCP's
per-request auth contextvar instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
for _p in (str(MCP_SERVER_DIR), str(EXAMPLES_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _set_auth_mode():
    with patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"}, clear=False):
        yield


def test_returns_scopes_from_current_access_token():
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    from examples.mcp_server.server import _get_token_scopes

    token = AccessToken(
        token="abc",
        client_id="test-client",
        scopes=["mcp:docker", "mcp:docker:admin"],
    )
    user = AuthenticatedUser(auth_info=token)
    reset = auth_context_var.set(user)
    try:
        assert _get_token_scopes() == ["mcp:docker", "mcp:docker:admin"]
    finally:
        auth_context_var.reset(reset)


def test_no_access_token_falls_back_to_env_var():
    from examples.mcp_server.server import _get_token_scopes

    with patch.dict(os.environ, {"MCP_TOKEN_SCOPES": "mcp:read, mcp:project"}, clear=False):
        assert _get_token_scopes() == ["mcp:read", "mcp:project"]


def test_no_access_token_no_env_var_returns_empty():
    from examples.mcp_server.server import _get_token_scopes

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MCP_TOKEN_SCOPES", None)
        assert _get_token_scopes() == []


def test_supervisor_register_project_requires_admin_scope():
    from examples.mcp_server.tool_scopes import get_required_scopes, has_required_scope

    assert get_required_scopes("supervisor_register_project") == ["mcp:admin"]
    assert not has_required_scope(["mcp:read"], "supervisor_register_project")
    assert not has_required_scope(["mcp:project"], "supervisor_register_project")
    assert not has_required_scope(["mcp:read", "mcp:project"], "supervisor_register_project")
    assert has_required_scope(["mcp:admin"], "supervisor_register_project")
    assert has_required_scope(["mcp:read", "mcp:admin"], "supervisor_register_project")


def test_supervisor_register_project_fail_closed_for_unknown_tool():
    from examples.mcp_server.tool_scopes import FAIL_CLOSED_SCOPE, get_required_scopes

    assert get_required_scopes("nonexistent_tool_xyz") == [FAIL_CLOSED_SCOPE]
