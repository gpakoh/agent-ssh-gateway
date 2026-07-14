"""Tests for SDK diagnostic helpers: auth_check, session_check, quick."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sdk.ssh_gateway import SSHGatewayClient, quick


class TestAuthCheck:
    """Tests for client.auth_check()."""

    def test_auth_check_returns_valid(self):
        client = SSHGatewayClient("http://localhost:8085")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"valid": True, "auth_mode": "api_key", "key_name": "default"}
        client.session.get = MagicMock(return_value=mock_response)

        result = client.auth_check()

        assert result["valid"] is True
        assert result["auth_mode"] == "api_key"
        client.session.get.assert_called_once_with("http://localhost:8085/api/auth/check")

    def test_auth_check_returns_invalid(self):
        client = SSHGatewayClient("http://localhost:8085")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"valid": False, "auth_mode": "api_key", "key_name": ""}
        client.session.get = MagicMock(return_value=mock_response)

        result = client.auth_check()

        assert result["valid"] is False

    def test_auth_check_raises_on_server_error(self):
        client = SSHGatewayClient("http://localhost:8085")
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = Exception("Server error")
        client.session.get = MagicMock(return_value=mock_response)

        with pytest.raises(Exception, match="Server error"):
            client.auth_check()


class TestSessionCheck:
    """Tests for client.session_check()."""

    def test_session_check_with_explicit_id(self):
        client = SSHGatewayClient("http://localhost:8085")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"valid": True, "session_id": "sid-123", "status": "connected"}
        client.session.post = MagicMock(return_value=mock_response)

        result = client.session_check("sid-123")

        assert result["valid"] is True
        assert result["session_id"] == "sid-123"
        client.session.post.assert_called_once_with(
            "http://localhost:8085/api/session/check",
            json={"session_id": "sid-123"},
        )

    def test_session_check_with_current_session(self):
        client = SSHGatewayClient("http://localhost:8085")
        client._ssh_session = MagicMock()
        client._ssh_session.session_id = "current-sid"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"valid": True, "session_id": "current-sid", "status": "connected"}
        client.session.post = MagicMock(return_value=mock_response)

        result = client.session_check()

        assert result["valid"] is True
        client.session.post.assert_called_once_with(
            "http://localhost:8085/api/session/check",
            json={"session_id": "current-sid"},
        )

    def test_session_check_no_session_raises(self):
        client = SSHGatewayClient("http://localhost:8085")
        with pytest.raises(RuntimeError, match="No session_id"):
            client.session_check()

    def test_session_check_not_found(self):
        client = SSHGatewayClient("http://localhost:8085")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "valid": False,
            "session_id": "expired-sid",
            "code": "SESSION_NOT_FOUND",
            "hint": "Create a session via POST /api/ssh/connect",
        }
        client.session.post = MagicMock(return_value=mock_response)

        result = client.session_check("expired-sid")

        assert result["valid"] is False
        assert result["code"] == "SESSION_NOT_FOUND"
        assert "hint" in result


class TestQuickRun:
    """Tests for quick.run()."""

    def test_quick_run_success(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"stdout": "hello\n", "stderr": "", "exit_code": 0}
        mock_session.post.return_value = mock_response

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            with patch.object(SSHGatewayClient, "ssh_connect") as mock_connect:
                with patch.object(SSHGatewayClient, "disconnect") as mock_disconnect:
                    session_mock = MagicMock()
                    session_mock.session_id = "test-sid"
                    mock_connect.return_value = session_mock
                    # Mock execute to avoid _ssh_session check
                    with patch.object(SSHGatewayClient, "execute") as mock_execute:
                        mock_execute.return_value = {"stdout": "hello\n", "stderr": "", "exit_code": 0}
                        result = quick.run(
                            host="192.168.1.100",
                            username="root",
                            command="echo hello",
                        )
                        assert result["exit_code"] == 0
                        mock_disconnect.assert_called_once()

    def test_quick_run_disconnects_on_error(self):
        with patch("sdk.ssh_gateway.requests.Session"):
            with patch.object(SSHGatewayClient, "ssh_connect") as mock_connect:
                with patch.object(SSHGatewayClient, "disconnect") as mock_disconnect:
                    mock_connect.side_effect = ConnectionError("refused")
                    try:
                        quick.run(
                            host="192.168.1.100",
                            username="root",
                            command="echo hello",
                        )
                    except ConnectionError:
                        pass
                    mock_disconnect.assert_called_once()

    def test_quick_run_sets_api_key(self):
        mock_session = MagicMock()
        mock_session.headers = {}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"stdout": "", "stderr": "", "exit_code": 0}
        mock_session.post.return_value = mock_response

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            with patch.object(SSHGatewayClient, "ssh_connect") as mock_connect:
                with patch.object(SSHGatewayClient, "disconnect"):
                    session_mock = MagicMock()
                    session_mock.session_id = "test-sid"
                    mock_connect.return_value = session_mock
                    with patch.object(SSHGatewayClient, "execute") as mock_execute:
                        mock_execute.return_value = {"stdout": "", "stderr": "", "exit_code": 0}
                        quick.run(
                            host="192.168.1.100",
                            username="root",
                            command="echo hi",
                            api_key="test-key-123",
                        )
                        assert mock_session.headers.get("X-API-Key") == "test-key-123"


class TestQuickRead:
    """Tests for quick.read()."""

    def test_quick_read_success(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "file content here"
        mock_session.get.return_value = mock_response

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            with patch.object(SSHGatewayClient, "ssh_connect") as mock_connect:
                with patch.object(SSHGatewayClient, "disconnect") as mock_disconnect:
                    session_mock = MagicMock()
                    session_mock.session_id = "test-sid"
                    mock_connect.return_value = session_mock
                    with patch.object(SSHGatewayClient, "read_file") as mock_read:
                        mock_read.return_value = "file content here"
                        result = quick.read(
                            host="192.168.1.100",
                            username="root",
                            path="/etc/hostname",
                        )
                        assert result == "file content here"
                        mock_disconnect.assert_called_once()

    def test_quick_read_disconnects_on_error(self):
        with patch("sdk.ssh_gateway.requests.Session"):
            with patch.object(SSHGatewayClient, "ssh_connect") as mock_connect:
                with patch.object(SSHGatewayClient, "disconnect") as mock_disconnect:
                    mock_connect.side_effect = ConnectionError("refused")
                    try:
                        quick.read(
                            host="192.168.1.100",
                            username="root",
                            path="/etc/hostname",
                        )
                    except ConnectionError:
                        pass
                    mock_disconnect.assert_called_once()


class TestUploadFileCompatibility:
    """Regression tests for existing SDK upload behavior."""

    def test_upload_file_uses_legacy_base64_query_endpoint(self, tmp_path):
        local_file = tmp_path / "hello.txt"
        local_file.write_text("hello world", encoding="utf-8")
        client = SSHGatewayClient("http://localhost:8085")
        client._ssh_session = MagicMock()
        client._ssh_session.session_id = "sid-123"
        mock_response = MagicMock()
        mock_response.json.return_value = {"success": True}
        client.session.post = MagicMock(return_value=mock_response)

        result = client.upload_file(str(local_file), "/tmp/hello.txt")

        assert result == {"success": True}
        client.session.post.assert_called_once_with(
            "http://localhost:8085/api/file/upload",
            params={
                "session_id": "sid-123",
                "path": "/tmp/hello.txt",
                "content": "aGVsbG8gd29ybGQ=",
            },
        )
