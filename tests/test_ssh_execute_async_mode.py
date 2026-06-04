"""Tests for async_mode in POST /api/ssh/execute."""

from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from app.auth_middleware import token_fingerprint
from app.config import settings
from app.main import app


class TestExecuteAsyncMode:
    """Verify async_mode creates background jobs without breaking sync mode."""

    @classmethod
    def _base_manager_mock(cls):
        mgr = MagicMock()
        mgr.execute = AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1})
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
        _app_state.job_manager.create_job = AsyncMock(return_value="mock-job-id")
        _app_state.job_manager.list_jobs = AsyncMock(return_value={"jobs": [], "count": 0})
        _app_state.job_manager.get_job = AsyncMock(return_value=None)
        _app_state.job_manager.stop_cleanup_task = AsyncMock()
        _app_state.job_manager.wait_for_all_jobs = AsyncMock()

    # ------------------------------------------------------------------
    # Default sync mode unchanged
    # ------------------------------------------------------------------

    def test_sync_mode_calls_execute_not_create_job(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        with TestClient(app) as client:
            self._setup_state()
            from app import state as _app_state
            resp = client.post(
                "/api/ssh/execute",
                headers={"X-API-Key": "secret-42"},
                json={"session_id": "s-1", "command": "ls -la"},
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "stdout" in data
        assert data["stdout"] == "ok"
        assert data["exit_code"] == 0
        assert "job_id" not in data
        _app_state.manager.execute.assert_awaited_once()
        _app_state.job_manager.create_job.assert_not_called()

    def test_sync_mode_explicit_false(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        with TestClient(app) as client:
            self._setup_state()
            from app import state as _app_state
            resp = client.post(
                "/api/ssh/execute",
                headers={"X-API-Key": "secret-42"},
                json={"session_id": "s-1", "command": "ls -la", "async_mode": False},
            )
        assert resp.status_code == 200
        _app_state.manager.execute.assert_awaited_once()
        _app_state.job_manager.create_job.assert_not_called()

    # ------------------------------------------------------------------
    # Async mode
    # ------------------------------------------------------------------

    def test_async_mode_creates_job(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        with TestClient(app) as client:
            self._setup_state()
            from app import state as _app_state
            resp = client.post(
                "/api/ssh/execute",
                headers={"X-API-Key": "secret-42"},
                json={"session_id": "s-1", "command": "sleep 60", "async_mode": True},
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["job_id"] == "mock-job-id"
        assert data["status"] == "running"
        assert "message" in data
        _app_state.job_manager.create_job.assert_awaited_once_with(
            session_id="s-1", command="sleep 60"
        )
        _app_state.manager.execute.assert_not_called()

    # ------------------------------------------------------------------
    # Policy — still enforced before job creation
    # ------------------------------------------------------------------

    def test_async_mode_still_blocks_forbidden_commands(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "default")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        with TestClient(app) as client:
            self._setup_state()
            resp = client.post(
                "/api/ssh/execute",
                headers={"X-API-Key": "secret-42"},
                json={"session_id": "s-1", "command": "shutdown -h now", "async_mode": True},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    # ------------------------------------------------------------------
    # Ownership — still enforced before job creation
    # ------------------------------------------------------------------

    def test_async_mode_still_blocks_cross_tenant(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr(settings, "agent_token_scopes",
                            ["ssh:connect", "ssh:execute", "ssh:disconnect", "ssh:files"])
        monkeypatch.setattr(settings, "agent_token_expires_at", None)
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        async def _fake_is_agent_token_valid(settings, provided: str, token_store=None):
            from app.auth_middleware import AuthIdentity
            if provided in ("agent-token-a", "agent-token-b"):
                return AuthIdentity(
                    token_type="agent",
                    token=provided,
                    name="agent",
                    scopes=("ssh:connect", "ssh:execute", "ssh:disconnect", "ssh:files"),
                )
            return None
        monkeypatch.setattr(
            "app.auth_middleware.is_agent_token_valid",
            _fake_is_agent_token_valid,
        )

        with TestClient(app) as client:
            self._setup_state()
            # Cross-tenant: session owned by bot-b, request from bot-a
            from app import state as _app_state
            _app_state.manager.get_session = AsyncMock(return_value=MagicMock(
                owner_type="agent",
                owner_name="bot-b",
                owner_token_fingerprint=token_fingerprint("agent-token-b"),
                is_connected=MagicMock(return_value=True),
            ))
            resp = client.post(
                "/api/ssh/execute",
                headers={"Authorization": "Bearer agent-token-a"},
                json={"session_id": "s-2", "command": "ls", "async_mode": True},
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    # ------------------------------------------------------------------
    # E2E: async execute → job_id → job status
    # ------------------------------------------------------------------

    def test_e2e_async_execute_then_job_status(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        with TestClient(app) as client:
            self._setup_state()
            from app import state as _app_state

            # Step 1: async execute → job_id
            resp = client.post(
                "/api/ssh/execute",
                headers={"X-API-Key": "secret-42"},
                json={"session_id": "s-1", "command": "docker compose build", "async_mode": True},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["job_id"] == "mock-job-id"
            assert data["status"] == "running"

            # Step 2: mock get_job_status for the next step
            mock_status_data = {
                "job_id": "mock-job-id",
                "status": "running",
                "progress": {},
                "duration": None,
            }
            _app_state.job_manager.get_job_status = AsyncMock(return_value=mock_status_data)

            # Step 3: GET job status
            resp2 = client.get(
                f"/api/jobs/{data['job_id']}/status",
                headers={"X-API-Key": "secret-42"},
            )
            assert resp2.status_code == 200
            status_data = resp2.json()
            assert status_data["job_id"] == "mock-job-id"
            assert status_data["status"] == "running"
            assert "progress" in status_data

            # Step 4: verify mocks
            _app_state.job_manager.create_job.assert_awaited_once_with(
                session_id="s-1", command="docker compose build"
            )
            _app_state.manager.execute.assert_not_called()
            _app_state.job_manager.get_job_status.assert_awaited_once_with("mock-job-id")
