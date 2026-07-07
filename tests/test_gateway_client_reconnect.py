"""Tests for GatewayClient auto-reconnect on SESSION_NOT_FOUND."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))

from gateway_client import GatewayClient, GatewayClientError  # noqa: E402

_BASE_ENV = {
    "GATEWAY_BASE_URL": "http://gateway:8085",
    "GATEWAY_API_KEY": "test-api-key",
    "GATEWAY_SESSION_ID": "test-session-1",
    "GATEWAY_SSH_HOST": "sshd-host",
    "GATEWAY_SSH_USER": "root",
    "GATEWAY_SSH_PASSWORD": "secret",
}


def _client(**overrides: str) -> GatewayClient:
    env = {**_BASE_ENV, **overrides}
    with patch.dict(os.environ, env, clear=True):
        return GatewayClient()


# ── _require_session_id ────────────────────────────────────────


class TestRequireSessionId:
    def test_returns_session_id_when_set(self):
        client = _client()
        assert client._require_session_id() == "test-session-1"

    def test_raises_when_empty(self):
        client = _client(GATEWAY_SESSION_ID="")
        with pytest.raises(GatewayClientError, match="GATEWAY_SESSION_ID is required"):
            client._require_session_id()


# ── _reconnect_session ──────────────────────────────────────────


class TestReconnectSession:
    def test_requires_host_and_user(self):
        client = _client(GATEWAY_SSH_HOST="", GATEWAY_SSH_USER="")
        err = "GATEWAY_SSH_HOST and GATEWAY_SSH_USER are required for auto-reconnect"
        with pytest.raises(GatewayClientError, match=err):
            client._reconnect_session()

    def test_updates_session_id_on_success(self):
        client = _client()
        with patch("gateway_client.httpx.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"session_id": "new-session-42"}
            client._reconnect_session()
        assert client.session_id == "new-session-42"
        mock_post.assert_called_once_with(
            "http://gateway:8085/api/ssh/connect",
            json={
                "host": "sshd-host",
                "port": 22,
                "username": "root",
                "password": "secret",
            },
            headers={"X-API-Key": "test-api-key"},
            timeout=30,
        )

    def test_includes_private_key_when_set(self):
        client = _client(GATEWAY_SSH_PRIVATE_KEY="key-content")
        with patch("gateway_client.httpx.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"session_id": "s2"}
            client._reconnect_session()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["private_key"] == "key-content"

    def test_omits_password_when_not_set(self):
        client = _client(GATEWAY_SSH_PASSWORD="")
        with patch("gateway_client.httpx.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"session_id": "s2"}
            client._reconnect_session()
        call_kwargs = mock_post.call_args[1]
        assert "password" not in call_kwargs["json"]

    def test_raises_on_http_error(self):
        client = _client()
        with patch("gateway_client.httpx.post") as mock_post:
            mock_post.return_value.status_code = 403
            mock_post.return_value.text = "forbidden"
            with pytest.raises(GatewayClientError, match="auto-reconnect failed"):
                client._reconnect_session()

    def test_custom_port(self):
        client = _client(GATEWAY_SSH_PORT="2222")
        assert client._ssh_port == 2222

    def test_empty_private_key_omitted_from_payload(self):
        client = _client(GATEWAY_SSH_PRIVATE_KEY="")
        with patch("gateway_client.httpx.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"session_id": "s2"}
            client._reconnect_session()
        call_kwargs = mock_post.call_args[1]
        assert "private_key" not in call_kwargs["json"]


# ── Decorator: session health ────────────────────────────────────


class TestSessionHealthReconnect:
    def test_success_no_reconnect(self):
        client = _client()
        with patch.object(client, "_get") as mock_get:
            mock_get.return_value = {"status": "ok"}
            result = client.session_health()
        assert result == {"status": "ok"}
        mock_get.assert_called_once_with("/api/ssh/session/test-session-1/health")

    def test_reconnects_on_session_not_found(self):
        client = _client()
        call_count = 0

        def _get_side_effect(path, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GatewayClientError("SESSION_NOT_FOUND\nhint: Create a session first")
            return {"status": "ok"}

        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = _get_side_effect
            with patch.object(client, "_reconnect_session") as mock_reconnect:
                result = client.session_health()
        assert result == {"status": "ok"}
        assert call_count == 2
        mock_reconnect.assert_called_once()

    def test_session_id_updated_after_reconnect(self):
        client = _client()
        call_count = 0

        def _get_side_effect(path, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GatewayClientError("SESSION_NOT_FOUND\nhint: Create a session first")
            return {"status": "ok"}

        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = _get_side_effect
            client._reconnect_session = lambda: setattr(client, "session_id", "reconnected-session")
            client.session_health()
        mock_get.assert_any_call("/api/ssh/session/reconnected-session/health")

    def test_raises_on_reconnect_failure(self):
        client = _client()
        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = GatewayClientError(
                "SESSION_NOT_FOUND\nhint: Create a session first"
            )
            with patch.object(client, "_reconnect_session") as mock_reconnect:
                mock_reconnect.side_effect = GatewayClientError("auto-reconnect failed: 403")
                with pytest.raises(GatewayClientError, match="auto-reconnect failed"):
                    client.session_health()
        mock_reconnect.assert_called_once()

    def test_raises_on_non_session_error(self):
        client = _client()
        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = GatewayClientError(
                "POST /api/ssh/execute failed: 400 some error"
            )
            with pytest.raises(GatewayClientError, match="some error"):
                client.session_health()

    def test_reconnects_only_once(self):
        client = _client()
        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = GatewayClientError(
                "SESSION_NOT_FOUND\nhint: Create a session first"
            )
            with patch.object(client, "_reconnect_session") as mock_reconnect:
                with pytest.raises(GatewayClientError):
                    client.session_health()
        mock_reconnect.assert_called_once()


# ── Decorator: execute_restricted ───────────────────────────────


class TestExecuteRestrictedReconnect:
    def test_reconnects_and_retries(self):
        client = _client()
        call_count = 0

        def _post_side_effect(path, payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GatewayClientError("SESSION_NOT_FOUND\nhint: Create a session first")
            return {"job_id": "job-1"}

        with patch.object(client, "_post") as mock_post:
            mock_post.side_effect = _post_side_effect
            with patch.object(client, "_reconnect_session") as mock_reconnect:
                result = client.execute_restricted("pwd")
        assert result == {"job_id": "job-1"}
        assert call_count == 2
        mock_reconnect.assert_called_once()


# ── Decorator: execute_project_command ──────────────────────────


class TestExecuteProjectCommandReconnect:
    def test_reconnects_and_retries(self):
        client = _client(MCP_GATEWAY_PROJECT_ROOT="/projects")
        call_count = 0

        def _post_side_effect(path, payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GatewayClientError("SESSION_NOT_FOUND\nhint: Create a session first")
            return {"job_id": "job-1"}

        with patch.object(client, "_post") as mock_post:
            mock_post.side_effect = _post_side_effect
            with patch.object(client, "_reconnect_session") as mock_reconnect:
                with patch.dict(
                    os.environ,
                    {"MCP_GATEWAY_PROJECT_ROOT": "/projects"},
                ):
                    result = client.execute_project_command("myapp", "pwd")
        assert result == {"job_id": "job-1"}
        assert call_count == 2
        mock_reconnect.assert_called_once()


# ── Decorator: read_file / write_file ──────────────────────────


class TestFileReconnect:
    def test_read_file_reconnects(self):
        client = _client()
        call_count = 0

        def _post_side_effect(path, payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GatewayClientError("SESSION_NOT_FOUND\nhint: Create a session first")
            return {"content": "file content"}

        with patch.object(client, "_post") as mock_post:
            mock_post.side_effect = _post_side_effect
            with patch.object(client, "_reconnect_session") as mock_reconnect:
                result = client.read_file("/some/path")
        assert result == {"content": "file content"}
        mock_reconnect.assert_called_once()

    def test_write_file_reconnects(self):
        client = _client()
        call_count = 0

        def _post_side_effect(path, payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GatewayClientError("SESSION_NOT_FOUND\nhint: Create a session first")
            return {"status": "written"}

        with patch.object(client, "_post") as mock_post:
            mock_post.side_effect = _post_side_effect
            with patch.object(client, "_reconnect_session") as mock_reconnect:
                result = client.write_file("/some/path", "content")
        assert result == {"status": "written"}
        mock_reconnect.assert_called_once()


# ── Without SSH env vars (should still fail gracefully) ────────


class TestReconnectWithoutSshConfig:
    def test_missing_host_raises_helpful_error(self):
        client = _client(GATEWAY_SSH_HOST="", GATEWAY_SSH_USER="root")
        with patch.object(client, "_post") as mock_post:
            mock_post.side_effect = GatewayClientError(
                "SESSION_NOT_FOUND\nhint: Create a session first"
            )
            with pytest.raises(
                GatewayClientError,
                match="GATEWAY_SSH_HOST and GATEWAY_SSH_USER are required",
            ):
                client.execute_restricted("pwd")

    def test_missing_user_raises_helpful_error(self):
        client = _client(GATEWAY_SSH_HOST="host", GATEWAY_SSH_USER="")
        with patch.object(client, "_post") as mock_post:
            mock_post.side_effect = GatewayClientError(
                "SESSION_NOT_FOUND\nhint: Create a session first"
            )
            with pytest.raises(
                GatewayClientError,
                match="GATEWAY_SSH_HOST and GATEWAY_SSH_USER are required",
            ):
                client.execute_restricted("pwd")


# ── Thread safety ─────────────────────────────────────────────


class TestReconnectThreadSafety:
    def test_lock_attribute_exists(self):
        client = _client()
        assert hasattr(client._reconnect_lock, "acquire")
        assert hasattr(client._reconnect_lock, "release")

    def test_reconnect_only_once_per_session_stale(self):
        client = _client()
        reconnect_calls: list[int] = []

        def counting_reconnect():
            reconnect_calls.append(1)

        client._reconnect_session = counting_reconnect
        session_not_found = GatewayClientError("SESSION_NOT_FOUND\nhint: Create a session first")

        with patch.object(client, "_post") as mock_post:
            mock_post.side_effect = session_not_found
            with pytest.raises(GatewayClientError):
                client.execute_restricted("pwd")

        assert len(reconnect_calls) == 1


# ── Non-SSH methods not affected ───────────────────────────────


class TestNonSessionMethods:
    def test_health_not_affected(self):
        client = _client()
        with patch.object(client, "_get") as mock_get:
            mock_get.return_value = {"status": "ok"}
            assert client.health() == {"status": "ok"}

    def test_list_sessions_not_affected(self):
        client = _client()
        with patch.object(client, "_get") as mock_get:
            mock_get.return_value = {"sessions": []}
            assert client.list_sessions() == {"sessions": []}
