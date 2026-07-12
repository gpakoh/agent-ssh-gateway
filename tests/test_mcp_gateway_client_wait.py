"""Tests for GatewayClient.wait_job long-poll with fallback."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))

from gateway_client import GatewayClient, GatewayClientError  # noqa: E402


class TestGatewayClientWaitJob:
    def _client(self):
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

    def test_long_poll_success(self):
        client = self._client()
        completed_response = {
            "job_id": "j1",
            "status": "completed",
            "stdout": "hi\n",
            "exit_code": 0,
        }
        with patch.object(client, "_get", return_value=completed_response) as mock_get:
            result = client.wait_job("j1", timeout=10)
            assert result["status"] == "completed"
            mock_get.assert_called_once_with(
                "/api/jobs/j1/wait",
                params={"timeout": 10},
                timeout=15,
            )

    def test_long_poll_timeout_returns_dict(self):
        client = self._client()
        timeout_response = {
            "job_id": "j1",
            "status": "running",
            "wait_timed_out": True,
        }
        with patch.object(client, "_get", return_value=timeout_response):
            result = client.wait_job("j1", timeout=0.5)
            assert result.get("wait_timed_out") is True

    def test_fallback_on_not_supported(self):
        client = self._client()

        call_count = 0

        def _mock_get(path, params=None, timeout=30):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GatewayClientError(
                    "NOT_SUPPORTED",
                    status_code=200,
                    body={"error": "NOT_SUPPORTED"},
                )
            return {
                "job_id": "j1",
                "status": "completed",
                "exit_code": 0,
            }

        with patch.object(client, "_get", side_effect=_mock_get):
            result = client.wait_job("j1", timeout=10)
            assert result["status"] == "completed"
            assert call_count >= 2

    def test_fallback_on_404(self):
        client = self._client()

        call_count = 0

        def _mock_get(path, params=None, timeout=30):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GatewayClientError("Not Found", status_code=404)
            return {
                "job_id": "j1",
                "status": "completed",
                "exit_code": 0,
            }

        with patch.object(client, "_get", side_effect=_mock_get):
            result = client.wait_job("j1", timeout=10)
            assert result["status"] == "completed"
            assert call_count >= 2

    def test_no_fallback_on_permission_denied(self):
        client = self._client()

        with patch.object(
            client,
            "_get",
            side_effect=GatewayClientError(
                "Permission denied",
                status_code=403,
                body={"error": "PERMISSION_DENIED"},
            ),
        ):
            try:
                client.wait_job("j1", timeout=10)
                assert False, "Should have raised"
            except GatewayClientError as e:
                assert e.status_code == 403
