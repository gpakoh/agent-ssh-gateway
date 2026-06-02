"""Tests for WebSocket scope enforcement and command policy."""


import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from app.main import app

WS_EXECUTE = "/api/ssh/execute/stream"
WS_PTY = "/api/ssh/pty/test-session/stream"
WS_FILE_WATCH = "/api/file/watch"


class TestWebSocketScopeEnforcement:
    """Agent token without required scope must be rejected before accept()."""

    @pytest.fixture
    def agent_no_scope(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "master-key-99")
        monkeypatch.setattr(settings, "agent_token", "agent-no-scope")
        monkeypatch.setattr(settings, "agent_token_scopes", [])
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.is_ip_allowed", lambda ip, nets: True)

    @pytest.fixture
    def agent_with_execute(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "master-key-99")
        monkeypatch.setattr(settings, "agent_token", "agent-exec")
        monkeypatch.setattr(settings, "agent_token_scopes", ["ssh:execute"])
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.is_ip_allowed", lambda ip, nets: True)

    def _expect_reject(self, url, headers):
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(url, headers=headers):
                    pass
            assert exc.value.code == 1008

    def _expect_business_error(self, url, headers, request):
        with TestClient(app) as client:
            with client.websocket_connect(url, headers=headers) as ws:
                ws.send_json(request)
                resp = ws.receive_json()
                assert resp.get("type") == "error"

    # -- Agent without scope cannot use execute stream -----------------------

    def test_execute_stream_denied_without_scope(self, agent_no_scope):
        self._expect_reject(
            WS_EXECUTE,
            headers={"X-API-Key": "agent-no-scope"},
        )

    # -- Agent without scope cannot use PTY ---------------------------------

    def test_pty_stream_denied_without_scope(self, agent_no_scope):
        self._expect_reject(
            WS_PTY,
            headers={"X-API-Key": "agent-no-scope"},
        )

    # -- Agent without scope cannot use file watch --------------------------

    def test_file_watch_denied_without_scope(self, agent_no_scope):
        self._expect_reject(
            WS_FILE_WATCH,
            headers={"X-API-Key": "agent-no-scope"},
        )

    # -- Agent with correct scope passes execute stream auth -----------------

    def test_execute_stream_allowed_with_correct_scope(self, agent_with_execute):
        self._expect_business_error(
            WS_EXECUTE,
            headers={"X-API-Key": "agent-exec"},
            request={"session_id": "", "command": ""},
        )

    # -- Master token bypasses scope checks ---------------------------------

    def test_master_key_bypasses_scope(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "master-key-99")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.is_ip_allowed", lambda ip, nets: True)
        self._expect_business_error(
            WS_EXECUTE,
            headers={"X-API-Key": "master-key-99"},
            request={"session_id": "", "command": ""},
        )


class TestWebSocketCommandPolicy:
    """Command policy must be enforced in WebSocket execute stream."""

    @pytest.fixture
    def agent_with_execute(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "master-key-99")
        monkeypatch.setattr(settings, "agent_token", "agent-exec")
        monkeypatch.setattr(settings, "agent_token_scopes", ["ssh:execute"])
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")
        monkeypatch.setattr("app.auth_middleware.is_ip_allowed", lambda ip, nets: True)

    def test_readonly_policy_denies_systemctl(self, agent_with_execute):
        """readonly profile in enforce mode must deny systemctl restart."""
        with TestClient(app) as client:
            with client.websocket_connect(
                WS_EXECUTE, headers={"X-API-Key": "agent-exec"}
            ) as ws:
                ws.send_json({
                    "session_id": "test-session",
                    "command": "systemctl restart nginx",
                })
                resp = ws.receive_json()
                assert resp.get("type") == "error"
                assert resp.get("code") == "COMMAND_POLICY_DENIED"
