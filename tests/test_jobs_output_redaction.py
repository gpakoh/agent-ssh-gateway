"""Tests for optional output redaction in GET /api/jobs/{job_id}/result."""

from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from app.config import settings
from app.main import app

SECRET_STDOUT = "TOKEN=abc123\npassword=secret123\nAuthorization: Bearer test-token\n"
SECRET_STDERR = "error: password=admin123"

MOCK_JOB_RESULT = {
    "job_id": "job-1",
    "session_id": "s-1",
    "command": "env",
    "status": "completed",
    "stdout": SECRET_STDOUT,
    "stderr": SECRET_STDERR,
    "exit_code": 0,
    "created_at": 1000.0,
    "started_at": 1000.1,
    "completed_at": 1000.5,
    "duration": 0.4,
    "error_message": None,
    "progress": {},
}


class TestJobsOutputRedaction:
    """Verify job result output redaction respects settings and query override."""

    def _setup_mocks(self):
        from app import state as _app_state

        _app_state.job_manager = AsyncMock()
        _app_state.job_manager.get_job_result = AsyncMock(return_value=dict(MOCK_JOB_RESULT))
        _app_state.job_manager.get_job_status = AsyncMock(return_value={})
        _app_state.job_manager.list_jobs = AsyncMock(return_value={"jobs": [], "count": 0})
        _app_state.job_manager._jobs = {}
        _app_state.job_manager.stop_cleanup_task = AsyncMock()
        _app_state.job_manager.wait_for_all_jobs = AsyncMock()
        _app_state.audit_logger = MagicMock()
        _app_state.manager = AsyncMock()
        _app_state.manager.execute = AsyncMock()
        _app_state.manager.disconnect = AsyncMock()
        _app_state.manager.stop_cleanup_task = AsyncMock()
        _app_state.manager.start_cleanup_task = AsyncMock()
        _app_state.manager.list_sessions = AsyncMock(return_value=[])
        _app_state.manager.reconnect = AsyncMock(return_value=True)

    # ------------------------------------------------------------------
    # Test 1: default setting false + no query → raw
    # ------------------------------------------------------------------

    def test_default_false_no_query_returns_raw(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "command_output_redaction_enabled", False)

        with TestClient(app) as client:
            self._setup_mocks()
            resp = client.get(
                "/api/jobs/job-1/result",
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["stdout"] == SECRET_STDOUT
        assert "password=admin123" in data["stderr"]

    # ------------------------------------------------------------------
    # Test 2: ?redact_output=true → redacted
    # ------------------------------------------------------------------

    def test_redact_true_query_redacts_output(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "command_output_redaction_enabled", False)

        with TestClient(app) as client:
            self._setup_mocks()
            resp = client.get(
                "/api/jobs/job-1/result?redact_output=true",
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "abc123" not in data["stdout"]
        assert "secret123" not in data["stdout"]
        assert "test-token" not in data["stdout"]
        assert "admin123" not in data["stderr"]
        assert "[REDACTED]" in data["stdout"]
        assert "[REDACTED]" in data["stderr"]

    # ------------------------------------------------------------------
    # Test 3: setting true + no query → redacted
    # ------------------------------------------------------------------

    def test_setting_true_no_query_redacts(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "command_output_redaction_enabled", True)

        with TestClient(app) as client:
            self._setup_mocks()
            resp = client.get(
                "/api/jobs/job-1/result",
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "abc123" not in data["stdout"]
        assert "secret123" not in data["stdout"]
        assert "test-token" not in data["stdout"]
        assert "admin123" not in data["stderr"]
        assert "[REDACTED]" in data["stdout"]
        assert "[REDACTED]" in data["stderr"]

    # ------------------------------------------------------------------
    # Test 4: setting true + ?redact_output=false → raw
    # ------------------------------------------------------------------

    def test_setting_true_redact_false_override_returns_raw(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "command_output_redaction_enabled", True)

        with TestClient(app) as client:
            self._setup_mocks()
            resp = client.get(
                "/api/jobs/job-1/result?redact_output=false",
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["stdout"] == SECRET_STDOUT
        assert "password=admin123" in data["stderr"]
