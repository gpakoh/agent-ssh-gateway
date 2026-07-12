"""Tests for MCP execute_argv tool and GatewayClient method."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# server.py needs sys.path set up for its internal imports
EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))


@pytest.fixture(autouse=True)
def _set_auth_mode():
    with patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth"}, clear=False):
        yield


def test_gateway_client_execute_argv_calls_correct_endpoint():
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

    with patch.object(
        client,
        "_post",
        return_value={"exit_code": 0, "stdout": "hi", "stderr": "", "duration": 0.1},
    ) as mock_post:
        result = client.execute_argv(
            argv=["python3", "-c", "print('hi')"],
            stdin="",
            timeout_s=30,
        )

    mock_post.assert_called_once_with(
        "/api/ssh/execute-argv",
        {
            "session_id": "test-session",
            "argv": ["python3", "-c", "print('hi')"],
            "stdin": "",
            "timeout_s": 30,
        },
    )
    assert result["exit_code"] == 0


def test_gateway_client_execute_argv_uses_session_id_override():
    from examples.mcp_server.gateway_client import GatewayClient

    client = GatewayClient.__new__(GatewayClient)
    client.base_url = "http://test:8085"
    client.api_key = "test-key"
    client.session_id = "default-session"
    client.command_timeout = 30
    client.job_timeout = 180
    client._reconnect_lock = MagicMock()
    client._ssh_host = ""
    client._ssh_port = 22
    client._ssh_user = ""
    client._ssh_password = ""
    client._ssh_private_key = ""

    with patch.object(
        client,
        "_post",
        return_value={"exit_code": 0, "stdout": "", "stderr": "", "duration": 0.0},
    ) as mock_post:
        client.execute_argv(
            argv=["ls"],
            session_id="override-session",
        )

    mock_post.assert_called_once_with(
        "/api/ssh/execute-argv",
        {
            "session_id": "override-session",
            "argv": ["ls"],
            "stdin": "",
            "timeout_s": 30,
        },
    )


def test_gateway_client_execute_argv_requires_session():
    from examples.mcp_server.gateway_client import GatewayClient, GatewayClientError

    client = GatewayClient.__new__(GatewayClient)
    client.base_url = "http://test:8085"
    client.api_key = "test-key"
    client.session_id = ""
    client.command_timeout = 30
    client.job_timeout = 180
    client._reconnect_lock = MagicMock()
    client._ssh_host = ""
    client._ssh_port = 22
    client._ssh_user = ""
    client._ssh_password = ""
    client._ssh_private_key = ""

    with pytest.raises(GatewayClientError, match="GATEWAY_SESSION_ID is required"):
        client.execute_argv(argv=["ls"])


def test_mcp_execute_argv_tool_exists():
    import importlib

    import examples.mcp_server.server as srv

    importlib.reload(srv)
    tool_names = [t.name for t in srv.mcp._tool_manager._tools.values()]
    assert "execute_argv" in tool_names
