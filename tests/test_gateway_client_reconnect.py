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

    def test_raises_session_not_found_when_empty_but_ssh_creds_present(self):
        """P0-adjacent audit finding: a real MCP client hit "GATEWAY_
        SESSION_ID is required" on git_status against a deployment with
        valid GATEWAY_SSH_HOST/USER/KEY already configured -- nothing
        was ever attempting to use them for a first connection, even
        though every caller of _require_session_id() is already wrapped
        with _retry_on_session_not_found. Raising with the same
        _SESSION_NOT_FOUND sentinel that decorator already looks for
        means the auto-reconnect path built for a session that went
        STALE now also covers a session that never existed yet.
        """
        client = _client(GATEWAY_SESSION_ID="")
        with pytest.raises(GatewayClientError, match=GatewayClient._SESSION_NOT_FOUND):
            client._require_session_id()

    def test_raises_helpful_error_when_empty_and_no_ssh_creds(self):
        """No SSH creds to auto-connect with -- the SESSION_NOT_FOUND
        sentinel would just trigger a doomed retry (_reconnect_session()
        immediately fails with its own "GATEWAY_SSH_HOST and
        GATEWAY_SSH_USER are required" error), so fail with one clear,
        actionable message right away instead."""
        client = _client(GATEWAY_SESSION_ID="", GATEWAY_SSH_HOST="", GATEWAY_SSH_USER="")
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

    def test_auto_connects_when_no_session_was_ever_set(self):
        """End-to-end reproduction of the live finding: a client with no
        GATEWAY_SESSION_ID configured at all (never even attempted a
        connection yet) but valid SSH creds must auto-connect on first
        use of any session-based tool -- git_status, execute_restricted,
        etc. -- not fail outright with "GATEWAY_SESSION_ID is required".
        """
        client = _client(GATEWAY_SESSION_ID="")
        assert client.session_id == ""

        def _reconnect_side_effect():
            client.session_id = "freshly-connected-session"

        with patch.object(client, "_post") as mock_post:
            mock_post.return_value = {"job_id": "job-1"}
            with patch.object(
                client, "_reconnect_session", side_effect=_reconnect_side_effect
            ) as mock_reconnect:
                result = client.execute_restricted("pwd")
        mock_reconnect.assert_called_once()
        assert result == {"job_id": "job-1"}
        assert mock_post.call_args.args[1]["session_id"] == "freshly-connected-session"


# ── Decorator: execute_project_command ──────────────────────────


class TestExecuteProjectCommandReconnect:
    def test_reconnects_and_retries(self):
        """Regression: WorkspaceRegistry.project_info() returns a plain
        dict (see app/workspace/registry.py), never an object with a
        `.root` attribute. This test used to mock an object with `.root`,
        which matched the (buggy) production code instead of the real
        contract — masking a live 'dict' object has no attribute 'root'
        failure in execute_project_command.
        """
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
                with patch("app.workspace.registry.get_registry") as mock_get_reg:
                    mock_reg = mock_get_reg.return_value
                    mock_reg.project_info.return_value = {"project_id": "myapp", "root": "/projects"}
                    result = client.execute_project_command("myapp", "pwd")
        assert result == {"job_id": "job-1"}
        assert call_count == 2
        mock_reconnect.assert_called_once()


class TestExecuteProjectScriptResolvesRealDictContract:
    """Regression: execute_project_script resolves the project root the
    same way execute_project_command does (get_registry().project_info()
    returns a plain dict), and used to crash with
    "'dict' object has no attribute 'root'" on every call — this is the
    method run_pytest/run_mypy's read-only-filesystem fallback path uses.
    """

    def test_writes_script_under_resolved_dict_root(self, tmp_path):
        client = _client()

        with patch.object(client, "execute_argv") as mock_execute_argv:
            mock_execute_argv.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
            with patch("app.workspace.registry.get_registry") as mock_get_reg:
                mock_reg = mock_get_reg.return_value
                mock_reg.project_info.return_value = {"project_id": "myapp", "root": str(tmp_path)}
                result = client.execute_project_script("myapp", "echo hi")

        assert result == {"exit_code": 0, "stdout": "ok", "stderr": ""}
        mock_execute_argv.assert_called_once()
        call_args, call_kwargs = mock_execute_argv.call_args
        assert call_args[0] == ["sh"]
        assert "echo hi" in call_kwargs["stdin"]
        assert "__AGENT_SCRIPT_BODY__" in call_kwargs["stdin"]
        assert call_kwargs["cwd"] == str(tmp_path)
        # No local temp file is ever written -- the script is piped via
        # stdin (audit BLOCKER: the old temp-file approach always failed
        # with EROFS against the real, read-only-mounted project root).
        assert not (tmp_path / ".ai-bridge").exists()


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
