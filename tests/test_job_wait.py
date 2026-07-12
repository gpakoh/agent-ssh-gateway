"""Tests for GET /api/jobs/{job_id}/wait long-poll endpoint."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

from app.config import settings
from app.main import app


MOCK_JOB = {
    "job_id": "job-1",
    "session_id": "s-1",
    "command": "echo hi",
    "status": "completed",
    "stdout": "hi\n",
    "stderr": "",
    "exit_code": 0,
    "created_at": 1000.0,
    "started_at": 1000.1,
    "completed_at": 1000.5,
    "duration": 0.4,
    "error_message": None,
    "progress": {},
    "owner_id": "user:admin",
    "queued_at_mono": None,
    "completed_at_mono": None,
}


class TestJobWaitEndpoint:
    def _setup_mocks(self):
        from app import state as _app_state

        _app_state.job_manager = AsyncMock()
        _app_state.job_manager.wait_for_completion = AsyncMock(return_value=dict(MOCK_JOB))
        _app_state.job_manager.get_job = AsyncMock(return_value=MagicMock(status="completed"))
        _app_state.job_manager.get_job_status = AsyncMock(return_value={})
        _app_state.job_manager.list_jobs = AsyncMock(return_value=[])
        _app_state.job_manager._jobs = {}
        _app_state.job_manager.stop_cleanup_task = AsyncMock()
        _app_state.job_manager.wait_for_all_jobs = AsyncMock()
        _app_state.audit_logger = MagicMock()
        _app_state.manager = AsyncMock()
        _app_state.manager.stop_cleanup_task = AsyncMock()
        _app_state.manager.start_cleanup_task = AsyncMock()
        _app_state.manager.list_sessions = AsyncMock(return_value=[])
        _app_state.event_hook_store = None
        _app_state.delivery_service = None

    def _client(self, monkeypatch):
        self._setup_mocks()
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        return TestClient(app, raise_server_exceptions=False)

    def test_wait_returns_job_result(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get(
            "/api/jobs/job-1/wait?timeout=30",
            headers={"X-API-Key": "secret-42"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "job-1"
        assert data["status"] == "completed"

    def test_wait_timeout_param_validated(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get(
            "/api/jobs/job-1/wait?timeout=0.01",
            headers={"X-API-Key": "secret-42"},
        )
        assert resp.status_code == 422

    def test_wait_timeout_too_large(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get(
            "/api/jobs/job-1/wait?timeout=999",
            headers={"X-API-Key": "secret-42"},
        )
        assert resp.status_code == 422

    def test_wait_no_auth_returns_401(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/api/jobs/job-1/wait?timeout=30")
        assert resp.status_code == 401
