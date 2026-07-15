"""Tests for SDK workspace helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sdk.ssh_gateway import SSHGatewayClient


class TestWorkspacePreviewWrite:
    def test_calls_correct_url(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"changed": True}
        mock_session.post.return_value = mock_response

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            result = SSHGatewayClient.workspace_preview_write(
                project_id="test-proj",
                path="src/main.py",
                content="new content",
                base_url="http://localhost:8085",
                api_key="test-key",
            )

        assert result["changed"] is True
        call_args = mock_session.post.call_args
        assert "/api/workspace/projects/test-proj/files/preview/write" in call_args[0][0]
        assert call_args[1]["json"]["path"] == "src/main.py"
        assert call_args[1]["json"]["content"] == "new content"

    def test_sets_api_key_header(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_session.post.return_value = mock_response
        mock_session.headers = {}

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            SSHGatewayClient.workspace_preview_write(
                project_id="p",
                path="f.txt",
                content="c",
                base_url="http://localhost:8085",
                api_key="key-123",
            )

        assert mock_session.headers.get("X-API-Key") == "key-123"


class TestWorkspacePreviewEdit:
    def test_calls_correct_url(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"replaced": True}
        mock_session.post.return_value = mock_response

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            result = SSHGatewayClient.workspace_preview_edit(
                project_id="test-proj",
                path="src/main.py",
                old_string="def foo():",
                new_string="def bar():",
                base_url="http://localhost:8085",
            )

        assert result["replaced"] is True
        call_args = mock_session.post.call_args
        assert "/api/workspace/projects/test-proj/files/preview/edit" in call_args[0][0]
        assert call_args[1]["json"]["old_string"] == "def foo():"
        assert call_args[1]["json"]["new_string"] == "def bar():"


class TestWorkspacePreviewPatch:
    def test_calls_correct_url(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"applied": True}
        mock_session.post.return_value = mock_response

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            result = SSHGatewayClient.workspace_preview_patch(
                project_id="test-proj",
                path="src/main.py",
                patch="--- a/src/main.py\n+++ b/src/main.py\n",
                base_url="http://localhost:8085",
            )

        assert result["applied"] is True
        call_args = mock_session.post.call_args
        assert "/api/workspace/projects/test-proj/files/preview/patch" in call_args[0][0]


class TestWorkspaceVerify:
    def test_calls_correct_url(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"matches": True, "current_hash": "sha256:abc"}
        mock_session.post.return_value = mock_response

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            result = SSHGatewayClient.workspace_verify(
                project_id="test-proj",
                path="src/main.py",
                expected_hash="sha256:abc",
                base_url="http://localhost:8085",
            )

        assert result["matches"] is True
        call_args = mock_session.post.call_args
        assert "/api/workspace/projects/test-proj/files/verify" in call_args[0][0]
        assert call_args[1]["json"]["expected_hash"] == "sha256:abc"


class TestWorkspaceWrite:
    def test_safe_false_by_default(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"size": 100}
        mock_session.post.return_value = mock_response

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            result = SSHGatewayClient.workspace_write(
                project_id="test-proj",
                path="file.txt",
                content="hello",
                base_url="http://localhost:8085",
            )

        assert result["size"] == 100
        call_args = mock_session.post.call_args
        assert call_args[1]["json"]["safe"] is False

    def test_safe_true(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"size": 100, "receipt": {"verified": True}}
        mock_session.post.return_value = mock_response

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            result = SSHGatewayClient.workspace_write(
                project_id="test-proj",
                path="file.txt",
                content="hello",
                safe=True,
                base_url="http://localhost:8085",
            )

        assert "receipt" in result
        call_args = mock_session.post.call_args
        assert call_args[1]["json"]["safe"] is True


class TestWorkspaceEdit:
    def test_calls_correct_url(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"replaced": True}
        mock_session.post.return_value = mock_response

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            result = SSHGatewayClient.workspace_edit(
                project_id="test-proj",
                path="file.txt",
                old_string="old",
                new_string="new",
                safe=True,
                base_url="http://localhost:8085",
            )

        assert result["replaced"] is True
        call_args = mock_session.post.call_args
        assert "/api/workspace/projects/test-proj/files/edit" in call_args[0][0]
        assert call_args[1]["json"]["safe"] is True


class TestWorkspacePatch:
    def test_calls_correct_url(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"applied": True}
        mock_session.post.return_value = mock_response

        with patch("sdk.ssh_gateway.requests.Session", return_value=mock_session):
            result = SSHGatewayClient.workspace_patch(
                project_id="test-proj",
                path="file.txt",
                patch="--- a/file.txt\n+++ b/file.txt\n",
                safe=True,
                base_url="http://localhost:8085",
            )

        assert result["applied"] is True
        call_args = mock_session.post.call_args
        assert "/api/workspace/projects/test-proj/files/patch" in call_args[0][0]
        assert call_args[1]["json"]["safe"] is True
