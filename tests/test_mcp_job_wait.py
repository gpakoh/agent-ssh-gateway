"""Tests for MCP job_wait tool."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))

from gateway_client import GatewayClient, GatewayClientError  # noqa: E402


class TestMCPJobWait:
    def _make_client(self):
        c = GatewayClient.__new__(GatewayClient)
        c.base_url = "http://localhost:8085"
        c.api_key = "test-key"
        c.session_id = "s1"
        c.command_timeout = 30
        c.job_timeout = 5
        c._reconnect_lock = __import__("threading").Lock()
        c._ssh_host = ""
        c._ssh_port = 22
        c._ssh_user = ""
        c._ssh_password = ""
        c._ssh_private_key = ""
        return c

    def _import_server_module(self):
        import importlib

        import examples.mcp_server.server as srv

        importlib.reload(srv)
        return srv

    def test_job_wait_returns_completed_result(self):
        srv = self._import_server_module()

        client = self._make_client()
        completed = {
            "job_id": "j1",
            "status": "completed",
            "exit_code": 0,
            "stdout": "hello\n",
        }
        with patch("examples.mcp_server.server.client", client):
            with patch.object(client, "wait_job", return_value=completed):
                result = srv.gateway_job_wait(job_id="j1", timeout_sec=30)
                assert result["ok"] is True
                assert result["result"]["status"] == "completed"

    def test_job_wait_timeout_returns_error(self):
        srv = self._import_server_module()

        client = self._make_client()
        with patch("examples.mcp_server.server.client", client):
            with patch.object(
                client,
                "wait_job",
                return_value={"job_id": "j1", "status": "running", "wait_timed_out": True},
            ):
                result = srv.gateway_job_wait(job_id="j1", timeout_sec=30)
                assert result["ok"] is False
                assert result["error"]["code"] == "WAIT_TIMEOUT"

    def test_job_wait_handles_gateway_error(self):
        srv = self._import_server_module()

        client = self._make_client()
        with patch("examples.mcp_server.server.client", client):
            with patch.object(
                client,
                "wait_job",
                side_effect=GatewayClientError("Job j1 not found", status_code=404),
            ):
                result = srv.gateway_job_wait(job_id="j1", timeout_sec=30)
                assert result["ok"] is False
