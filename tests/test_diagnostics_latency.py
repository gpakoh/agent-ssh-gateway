"""Tests for GET /api/diagnostics/latency endpoint."""

from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from app.auth_middleware import AuthIdentity, token_fingerprint
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

    def _make_job(self, job_id: str, owner_id: str):
        job = MagicMock()
        job.job_id = job_id
        job.owner_id = owner_id
        job.status = "completed"
        job.queued_at_mono = 1.0
        job.completed_at_mono = 2.0
        job.acquired_at_mono = None
        job.command_started_at_mono = None
        job.command_finished_at_mono = None
        job.ssh_connect_started_at_mono = None
        job.ssh_connected_at_mono = None
        return job

    def test_latency_hides_other_tenants_jobs(self, monkeypatch):
        """Regression: _compute_job_latency_breakdown() iterated every job
        in job_manager._jobs unconditionally — unlike GET /api/jobs (which
        filters via job_visible_to()) — so any diagnostics:read-scoped
        agent token could see job_id/timing for every job across every
        tenant, not just its own.
        """
        client = self._client(monkeypatch)
        monkeypatch.setattr(
            settings,
            "agent_token_scopes",
            ["ssh:connect", "ssh:execute", "diagnostics:read"],
        )
        monkeypatch.setattr(settings, "agent_token_expires_at", None)

        async def _fake_is_agent_token_valid(settings, provided: str, token_store=None):
            if provided == "agent-token-a":
                return AuthIdentity(
                    token_type="agent",
                    token=provided,
                    name="agent-a",
                    scopes=("ssh:connect", "ssh:execute", "diagnostics:read"),
                )
            return None

        monkeypatch.setattr(
            "app.auth_middleware.is_agent_token_valid", _fake_is_agent_token_valid
        )

        from app import state as _app_state

        fp_a = token_fingerprint("agent-token-a")
        fp_b = token_fingerprint("agent-token-b")
        own_job = self._make_job("job-own", fp_a)
        other_job = self._make_job("job-other-tenant", fp_b)
        _app_state.job_manager._jobs = {"job-own": own_job, "job-other-tenant": other_job}

        resp = client.get(
            "/api/diagnostics/latency",
            headers={"Authorization": "Bearer agent-token-a"},
        )
        assert resp.status_code == 200
        job_ids = {j["job_id"] for j in resp.json()["gateway"]["jobs"]}
        assert job_ids == {"job-own"}
        assert resp.json()["gateway"]["total"] == 1

    def test_latency_master_sees_all_jobs(self, monkeypatch):
        client = self._client(monkeypatch)
        from app import state as _app_state

        fp_a = token_fingerprint("agent-token-a")
        fp_b = token_fingerprint("agent-token-b")
        _app_state.job_manager._jobs = {
            "job-a": self._make_job("job-a", fp_a),
            "job-b": self._make_job("job-b", fp_b),
        }

        resp = client.get(
            "/api/diagnostics/latency",
            headers={"X-API-Key": "secret-42"},
        )
        assert resp.status_code == 200
        job_ids = {j["job_id"] for j in resp.json()["gateway"]["jobs"]}
        assert job_ids == {"job-a", "job-b"}
