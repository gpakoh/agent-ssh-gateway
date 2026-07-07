"""Tests for session ownership on file analysis endpoints (AST + bulk)."""

from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from app.auth_middleware import token_fingerprint
from app.config import settings
from app.main import app


class TestFileAnalysisOwnership:
    """Cross-tenant agent must be blocked from AST and bulk endpoints."""

    @classmethod
    def _base_mock(cls):
        mgr = MagicMock()
        mgr.execute = AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0})
        mgr.disconnect = AsyncMock()
        mgr.stop_cleanup_task = AsyncMock()
        mgr.list_sessions = AsyncMock(return_value=[])
        mgr.start_cleanup_task = AsyncMock()
        mgr.reconnect = AsyncMock(return_value=True)
        return mgr

    @classmethod
    def _make_cross_tenant_session_mock(cls):
        mgr = cls._base_mock()
        mgr.get_session = AsyncMock(
            return_value=MagicMock(
                owner_type="agent",
                owner_name="bot-b",
                owner_token_fingerprint=token_fingerprint("agent-token-b"),
                is_connected=MagicMock(return_value=True),
            )
        )
        return mgr

    @classmethod
    def _patch_base(cls, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr(
            settings,
            "agent_token_scopes",
            ["ssh:connect", "ssh:execute", "ssh:disconnect", "ssh:files"],
        )
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

    def _override_manager(self, client, mock_mgr):
        from app import state as _app_state

        _app_state.manager = mock_mgr

    def _make_file_editor_mock(self):
        fe = MagicMock()
        fe.read_file = AsyncMock(return_value="mock content")
        fe.edit_file = AsyncMock(return_value={"success": True, "operations_applied": 1})
        fe.write_file = AsyncMock(return_value=None)
        return fe

    def _make_bulk_ops_mock(self):
        bo = MagicMock()
        bo.read_files_bulk = AsyncMock(return_value={"test.py": "content"})
        return bo

    # ------------------------------------------------------------------
    # AST rename — agent blocked
    # ------------------------------------------------------------------

    def test_ast_rename_ownership_agent_blocked(self, monkeypatch):
        self._patch_base(monkeypatch)
        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            from app import state as _app_state

            _app_state.file_editor = self._make_file_editor_mock()
            resp = client.post(
                "/api/ast/rename",
                headers={"Authorization": "Bearer agent-token-a"},
                json={
                    "session_id": "s-2",
                    "old_name": "foo",
                    "new_name": "bar",
                    "path": "/tmp/test.py",
                },
            )
        assert resp.status_code == 403

    def test_ast_rename_ownership_master_bypasses(self, monkeypatch):
        self._patch_base(monkeypatch)
        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            from app import state as _app_state

            _app_state.file_editor = self._make_file_editor_mock()
            resp = client.post(
                "/api/ast/rename",
                headers={"X-API-Key": "secret-42"},
                json={
                    "session_id": "s-2",
                    "old_name": "foo",
                    "new_name": "bar",
                    "path": "/tmp/test.py",
                },
            )
        assert resp.status_code in (200, 500), (
            f"Expected 200 or 500, got {resp.status_code}: {resp.text}"
        )

    # ------------------------------------------------------------------
    # AST extract — agent blocked
    # ------------------------------------------------------------------

    def test_ast_extract_ownership_agent_blocked(self, monkeypatch):
        self._patch_base(monkeypatch)
        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            from app import state as _app_state

            _app_state.file_editor = self._make_file_editor_mock()
            resp = client.post(
                "/api/ast/extract",
                headers={"Authorization": "Bearer agent-token-a"},
                json={
                    "session_id": "s-2",
                    "path": "/tmp/test.py",
                    "start_line": 1,
                    "end_line": 5,
                    "func_name": "new_func",
                },
            )
        assert resp.status_code == 403

    def test_ast_extract_ownership_master_bypasses(self, monkeypatch):
        self._patch_base(monkeypatch)
        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            from app import state as _app_state

            _app_state.file_editor = self._make_file_editor_mock()
            resp = client.post(
                "/api/ast/extract",
                headers={"X-API-Key": "secret-42"},
                json={
                    "session_id": "s-2",
                    "path": "/tmp/test.py",
                    "start_line": 1,
                    "end_line": 5,
                    "func_name": "new_func",
                },
            )
        assert resp.status_code in (200, 500), (
            f"Expected 200 or 500, got {resp.status_code}: {resp.text}"
        )

    # ------------------------------------------------------------------
    # AST analyze — agent blocked
    # ------------------------------------------------------------------

    def test_ast_analyze_ownership_agent_blocked(self, monkeypatch):
        self._patch_base(monkeypatch)
        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            from app import state as _app_state

            _app_state.file_editor = self._make_file_editor_mock()
            resp = client.post(
                "/api/ast/analyze",
                headers={"Authorization": "Bearer agent-token-a"},
                json={
                    "session_id": "s-2",
                    "path": "/tmp/test.py",
                },
            )
        assert resp.status_code == 403

    def test_ast_analyze_ownership_master_bypasses(self, monkeypatch):
        self._patch_base(monkeypatch)
        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            from app import state as _app_state

            _app_state.file_editor = self._make_file_editor_mock()
            resp = client.post(
                "/api/ast/analyze",
                headers={"X-API-Key": "secret-42"},
                json={
                    "session_id": "s-2",
                    "path": "/tmp/test.py",
                },
            )
        assert resp.status_code in (200, 500), (
            f"Expected 200 or 500, got {resp.status_code}: {resp.text}"
        )

    # ------------------------------------------------------------------
    # Bulk read — agent blocked
    # ------------------------------------------------------------------

    def test_bulk_read_ownership_agent_blocked(self, monkeypatch):
        self._patch_base(monkeypatch)
        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            from app import state as _app_state

            _app_state.file_editor = self._make_file_editor_mock()
            _app_state.bulk_ops = self._make_bulk_ops_mock()
            resp = client.post(
                "/api/bulk/read",
                headers={"Authorization": "Bearer agent-token-a"},
                json={
                    "session_id": "s-2",
                    "paths": ["/tmp/test.py"],
                },
            )
        assert resp.status_code == 403

    def test_bulk_read_ownership_master_bypasses(self, monkeypatch):
        self._patch_base(monkeypatch)
        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            from app import state as _app_state

            _app_state.file_editor = self._make_file_editor_mock()
            _app_state.bulk_ops = self._make_bulk_ops_mock()
            resp = client.post(
                "/api/bulk/read",
                headers={"X-API-Key": "secret-42"},
                json={
                    "session_id": "s-2",
                    "paths": ["/tmp/test.py"],
                },
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    # ------------------------------------------------------------------
    # Bulk edit — agent blocked
    # ------------------------------------------------------------------

    def test_bulk_edit_ownership_agent_blocked(self, monkeypatch):
        self._patch_base(monkeypatch)
        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            from app import state as _app_state

            _app_state.file_editor = self._make_file_editor_mock()
            resp = client.post(
                "/api/bulk/edit",
                headers={"Authorization": "Bearer agent-token-a"},
                json={
                    "session_id": "s-2",
                    "files": [
                        {
                            "path": "/tmp/test.py",
                            "operations": [{"type": "replace", "old": "foo", "new": "bar"}],
                        }
                    ],
                },
            )
        assert resp.status_code == 403

    def test_bulk_edit_ownership_master_bypasses(self, monkeypatch):
        self._patch_base(monkeypatch)
        with TestClient(app) as client:
            self._override_manager(client, self._make_cross_tenant_session_mock())
            from app import state as _app_state

            _app_state.file_editor = self._make_file_editor_mock()
            resp = client.post(
                "/api/bulk/edit",
                headers={"X-API-Key": "secret-42"},
                json={
                    "session_id": "s-2",
                    "files": [
                        {
                            "path": "/tmp/test.py",
                            "operations": [{"type": "replace", "old": "foo", "new": "bar"}],
                        }
                    ],
                },
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
