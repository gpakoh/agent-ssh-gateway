"""Tests for optional output redaction in GET /api/jobs/{job_id}/stream (SSE)."""

import json
from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from app.config import settings
from app.job_manager import JobRecord
from app.main import app

SECRET_STDOUT = "TOKEN=abc123\npassword=secret123\nAuthorization: Bearer test-token\n"
SECRET_STDERR = "error: password=admin123"


class TestJobsStreamRedaction:
    """Verify SSE stream output redaction respects settings and query override."""

    def _setup_mocks(self, monkeypatch):
        from app import state as _app_state

        self.job = JobRecord(
            job_id="job-stream-1",
            session_id="s-1",
            command="env",
        )
        self.job.stdout = SECRET_STDOUT
        self.job.stderr = SECRET_STDERR
        self.job.exit_code = 0
        self.job.status = "completed"

        _app_state.job_manager = AsyncMock()
        _app_state.job_manager.get_job = AsyncMock(return_value=self.job)
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

    def _setup_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

    def _read_sse_events(self, resp):
        events = []
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        return events

    # ------------------------------------------------------------------
    # Test 1: default setting false + no query → raw SSE output
    # ------------------------------------------------------------------

    def test_default_false_no_query_returns_raw(self, monkeypatch):
        self._setup_settings(monkeypatch)
        monkeypatch.setattr(settings, "command_output_redaction_enabled", False)

        with TestClient(app) as client:
            self._setup_mocks(monkeypatch)
            with client.stream(
                "GET", "/api/jobs/job-stream-1/stream", headers={"X-API-Key": "secret-42"}
            ) as resp:
                assert resp.status_code == 200
                events = self._read_sse_events(resp)

        stdout_events = [e for e in events if e.get("type") == "stdout"]
        stderr_events = [e for e in events if e.get("type") == "stderr"]
        assert len(stdout_events) == 1
        assert stdout_events[0]["data"] == SECRET_STDOUT
        assert "password=admin123" in stderr_events[0]["data"]

    # ------------------------------------------------------------------
    # Test 2: ?redact_output=true → redacted SSE output
    # ------------------------------------------------------------------

    def test_redact_true_query_redacts_output(self, monkeypatch):
        self._setup_settings(monkeypatch)
        monkeypatch.setattr(settings, "command_output_redaction_enabled", False)

        with TestClient(app) as client:
            self._setup_mocks(monkeypatch)
            with client.stream(
                "GET",
                "/api/jobs/job-stream-1/stream?redact_output=true",
                headers={"X-API-Key": "secret-42"},
            ) as resp:
                assert resp.status_code == 200
                events = self._read_sse_events(resp)

        for e in events:
            if e.get("type") == "stdout":
                assert "abc123" not in e["data"]
                assert "secret123" not in e["data"]
                assert "test-token" not in e["data"]
                assert "[REDACTED]" in e["data"]
            if e.get("type") == "stderr":
                assert "admin123" not in e["data"]
                assert "[REDACTED]" in e["data"]

    # ------------------------------------------------------------------
    # Test 3: setting true + no query → redacted SSE output
    # ------------------------------------------------------------------

    def test_setting_true_no_query_redacts(self, monkeypatch):
        self._setup_settings(monkeypatch)
        monkeypatch.setattr(settings, "command_output_redaction_enabled", True)

        with TestClient(app) as client:
            self._setup_mocks(monkeypatch)
            with client.stream(
                "GET", "/api/jobs/job-stream-1/stream", headers={"X-API-Key": "secret-42"}
            ) as resp:
                assert resp.status_code == 200
                events = self._read_sse_events(resp)

        for e in events:
            if e.get("type") == "stdout":
                assert "abc123" not in e["data"]
                assert "secret123" not in e["data"]
                assert "test-token" not in e["data"]
                assert "[REDACTED]" in e["data"]
            if e.get("type") == "stderr":
                assert "admin123" not in e["data"]
                assert "[REDACTED]" in e["data"]

    # ------------------------------------------------------------------
    # Test 4: setting true + ?redact_output=false → raw SSE output
    # ------------------------------------------------------------------

    def test_setting_true_redact_false_override_returns_raw(self, monkeypatch):
        self._setup_settings(monkeypatch)
        monkeypatch.setattr(settings, "command_output_redaction_enabled", True)

        with TestClient(app) as client:
            self._setup_mocks(monkeypatch)
            with client.stream(
                "GET",
                "/api/jobs/job-stream-1/stream?redact_output=false",
                headers={"X-API-Key": "secret-42"},
            ) as resp:
                assert resp.status_code == 200
                events = self._read_sse_events(resp)

        stdout_events = [e for e in events if e.get("type") == "stdout"]
        stderr_events = [e for e in events if e.get("type") == "stderr"]
        assert len(stdout_events) == 1
        assert stdout_events[0]["data"] == SECRET_STDOUT
        assert "password=admin123" in stderr_events[0]["data"]

    # ------------------------------------------------------------------
    # Test 5: /events alias also respects redact_output
    # ------------------------------------------------------------------

    def test_events_alias_redacts_output(self, monkeypatch):
        self._setup_settings(monkeypatch)
        monkeypatch.setattr(settings, "command_output_redaction_enabled", False)

        with TestClient(app) as client:
            self._setup_mocks(monkeypatch)
            with client.stream(
                "GET",
                "/api/jobs/job-stream-1/events?redact_output=true",
                headers={"X-API-Key": "secret-42"},
            ) as resp:
                assert resp.status_code == 200
                events = self._read_sse_events(resp)

        for e in events:
            if e.get("type") == "stdout":
                assert "abc123" not in e["data"]
                assert "[REDACTED]" in e["data"]
