"""Tests for MCP project_apply_patch tool and GatewayClient method."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))


@pytest.fixture(autouse=True)
def _set_auth_mode():
    with patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"}, clear=False):
        yield


def _make_client():
    from examples.mcp_server.gateway_client import GatewayClient

    client = GatewayClient.__new__(GatewayClient)
    client.base_url = "http://test:8085"
    client.api_key = "test-key"
    client.session_id = "test-session"
    client.command_timeout = 30
    client.job_timeout = 180
    client._reconnect_lock = MagicMock()
    client._ssh_host = ""
    client._ssh_port = 22
    client._ssh_user = ""
    client._ssh_password = ""
    client._ssh_private_key = ""
    return client


def test_gateway_client_apply_patch_calls_correct_endpoint():
    client = _make_client()

    patch_text = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n"
    hashes = {"f": "sha256:abc"}

    with patch.object(
        client,
        "_post",
        return_value={"success": True, "files_applied": 1},
    ) as mock_post:
        result = client.apply_patch(
            project="myproject",
            patch=patch_text,
            expected_hashes=hashes,
            strip=1,
            dry_run=False,
        )

    mock_post.assert_called_once_with(
        "/api/projects/myproject/apply-patch",
        {
            "session_id": "test-session",
            "patch": patch_text,
            "expected_hashes": hashes,
            "strip": 1,
            "dry_run": False,
        },
    )
    assert result["success"] is True


def test_gateway_client_apply_patch_uses_session_id_override():
    client = _make_client()

    with patch.object(
        client,
        "_post",
        return_value={"success": True, "files_applied": 0},
    ) as mock_post:
        client.apply_patch(
            project="myproject",
            patch="--- a/f\n+++ b/f\n",
            expected_hashes={},
            session_id="override-session",
        )

    mock_post.assert_called_once_with(
        "/api/projects/myproject/apply-patch",
        {
            "session_id": "override-session",
            "patch": "--- a/f\n+++ b/f\n",
            "expected_hashes": {},
            "strip": 1,
            "dry_run": False,
        },
    )


def test_gateway_client_apply_patch_requires_session():
    client = _make_client()
    client.session_id = ""

    from examples.mcp_server.gateway_client import GatewayClientError

    with pytest.raises(GatewayClientError, match="GATEWAY_SESSION_ID is required"):
        client.apply_patch(
            project="myproject",
            patch="--- a/f\n+++ b/f\n",
            expected_hashes={},
        )


def test_gateway_client_apply_patch_validates_project():
    client = _make_client()

    from examples.mcp_server.gateway_client import GatewayClientError

    with pytest.raises(GatewayClientError, match="project argument is required"):
        client.apply_patch(
            project="",
            patch="--- a/f\n+++ b/f\n",
            expected_hashes={},
        )


def test_mcp_validate_project_rejects_empty():
    from examples.mcp_server.server import _validate_project

    with pytest.raises(ValueError, match="project argument is required"):
        _validate_project("")


def test_mcp_validate_project_rejects_traversal():
    from examples.mcp_server.server import _validate_project

    with pytest.raises(ValueError, match="Invalid project name"):
        _validate_project("../../etc/passwd")


def test_mcp_validate_project_accepts_valid():
    from examples.mcp_server.server import _validate_project

    assert _validate_project("myproject") == "myproject"


def test_mcp_project_apply_patch_tool_exists():
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    tool_names = [t.name for t in srv.mcp._tool_manager._tools.values()]
    assert "project_apply_patch" in tool_names
