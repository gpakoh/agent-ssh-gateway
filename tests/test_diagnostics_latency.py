"""Tests for GET /api/diagnostics/latency endpoint."""

from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from app.config import settings
from app.main import app


class TestDiagnosticsLatency:
    def _setup_mocks(self):
        from app import state as _app_state

        _app_state.job_manager = AsyncMock()
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
        monkeypatch.setattr(
            "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
        )
        return TestClient(app, raise_server_exceptions=False)

    def test_latency_endpoint_returns_json(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get(
            "/api/diagnostics/latency",
            headers={"X-API-Key": "secret-42"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "gateway" in data
        assert "jobs" in data["gateway"]
        assert "mcp" in data

    def test_latency_not_in_health(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "latency" not in data

    def test_latency_requires_auth(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/api/diagnostics/latency")
        assert resp.status_code == 401
