"""Tests for optional output redaction in POST /api/ssh/execute."""

from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from app.auth_middleware import token_fingerprint
from app.config import settings
from app.main import app

SECRET_OUTPUT = (
    "TOKEN=abc123\n"
    "password=secret123\n"
    "Authorization: Bearer test-token\n"
    "export API_KEY=supersecret\n"
)


class TestExecuteOutputRedaction:
    """Verify output redaction respects settings and request override."""

    @classmethod
    def _base_manager_mock(cls):
        mgr = MagicMock()
        mgr.execute = AsyncMock(return_value={
            "stdout": SECRET_OUTPUT,
            "stderr": "error: password=admin123",
            "exit_code": 0,
            "duration": 0.1,
        })
        mgr.disconnect = AsyncMock()
        mgr.stop_cleanup_task = AsyncMock()
        mgr.list_sessions = AsyncMock(return_value=[])
        mgr.start_cleanup_task = AsyncMock()
        mgr.reconnect = AsyncMock(return_value=True)
        return mgr

    @classmethod
    def _make_session_mock(cls):
        mgr = cls._base_manager_mock()
        mgr.get_session = AsyncMock(return_value=MagicMock(
            owner_type="master",
            owner_name="admin",
            owner_token_fingerprint=token_fingerprint("secret-42"),
            is_connected=MagicMock(return_value=True),
        ))
        return mgr

    def _setup_state(self):
        from app import state as _app_state
        _app_state.manager = self._make_session_mock()
        _app_state.audit_logger = MagicMock()
        _app_state.job_manager = AsyncMock()
        _app_state.job_manager._jobs = {}
        _app_state.job_manager.list_jobs = AsyncMock(return_value={"jobs": [], "count": 0})
        _app_state.job_manager.get_job = AsyncMock(return_value=None)
        _app_state.job_manager.stop_cleanup_task = AsyncMock()
        _app_state.job_manager.wait_for_all_jobs = AsyncMock()

    # ------------------------------------------------------------------
    # Test 1: default setting false + redact_output omitted → raw output
    # ------------------------------------------------------------------

    def test_default_false_no_override_returns_raw(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "command_output_redaction_enabled", False)

        with TestClient(app) as client:
            self._setup_state()
            resp = client.post(
                "/api/ssh/execute",
                headers={"X-API-Key": "secret-42"},
                json={"session_id": "s-1", "command": "env"},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["stdout"] == SECRET_OUTPUT
        assert "password=admin123" in data["stderr"]

    # ------------------------------------------------------------------
    # Test 2: request redact_output=true → redacted
    # ------------------------------------------------------------------

    def test_request_redact_true_redacts_output(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "command_output_redaction_enabled", False)

        with TestClient(app) as client:
            self._setup_state()
            resp = client.post(
                "/api/ssh/execute",
                headers={"X-API-Key": "secret-42"},
                json={
                    "session_id": "s-1",
                    "command": "env",
                    "redact_output": True,
                },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "abc123" not in data["stdout"]
        assert "secret123" not in data["stdout"]
        assert "test-token" not in data["stdout"]
        assert "supersecret" not in data["stdout"]
        assert "admin123" not in data["stderr"]
        assert "[REDACTED]" in data["stdout"]
        assert "[REDACTED]" in data["stderr"]

    # ------------------------------------------------------------------
    # Test 3: request redact_output=false overrides setting true → raw
    # ------------------------------------------------------------------

    def test_request_false_overrides_setting_true(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "command_output_redaction_enabled", True)

        with TestClient(app) as client:
            self._setup_state()
            resp = client.post(
                "/api/ssh/execute",
                headers={"X-API-Key": "secret-42"},
                json={
                    "session_id": "s-1",
                    "command": "env",
                    "redact_output": False,
                },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["stdout"] == SECRET_OUTPUT
        assert "password=admin123" in data["stderr"]

    # ------------------------------------------------------------------
    # Test 4: setting true + redact_output omitted → redacted
    # ------------------------------------------------------------------

    def test_setting_true_omitted_override_redacts(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "command_output_redaction_enabled", True)

        with TestClient(app) as client:
            self._setup_state()
            resp = client.post(
                "/api/ssh/execute",
                headers={"X-API-Key": "secret-42"},
                json={"session_id": "s-1", "command": "env"},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "abc123" not in data["stdout"]
        assert "secret123" not in data["stdout"]
        assert "test-token" not in data["stdout"]
        assert "supersecret" not in data["stdout"]
        assert "admin123" not in data["stderr"]
        assert "[REDACTED]" in data["stdout"]
        assert "[REDACTED]" in data["stderr"]
