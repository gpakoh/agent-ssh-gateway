"""Functional tests for file read/write/edit business logic (mocked SSH)."""

from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from app.auth_middleware import token_fingerprint
from app.config import settings
from app.main import app


class TestFileReadWriteEdit:
    """Verify that file read/write/edit endpoints flow correctly through mocked services."""

    @classmethod
    def _base_manager_mock(cls):
        mgr = MagicMock()
        mgr.execute = AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0})
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

    @classmethod
    def _make_file_editor_mock(cls):
        fe = MagicMock()
        fe.read_file = AsyncMock(return_value="file content here")
        fe.write_file = AsyncMock(return_value=None)
        fe.edit_file = AsyncMock(return_value={
            "success": True,
            "operations_applied": 1,
            "changed": True,
            "path": "/tmp/test.py",
        })
        return fe

    @classmethod
    def _make_audit_logger_mock(cls):
        al = MagicMock()
        al.log_file_access = MagicMock()
        return al

    def _setup_state(self):
        from app import state as _app_state
        _app_state.manager = self._make_session_mock()
        _app_state.file_editor = self._make_file_editor_mock()
        _app_state.audit_logger = self._make_audit_logger_mock()

    # ------------------------------------------------------------------
    # Happy path: file read
    # ------------------------------------------------------------------

    def test_file_read_returns_content(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        with TestClient(app) as client:
            self._setup_state()
            resp = client.post(
                "/api/file/read",
                headers={"X-API-Key": "secret-42"},
                json={"session_id": "s-1", "path": "/etc/hostname"},
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["path"] == "/etc/hostname"
        assert data["content"] == "file content here"
        # Verify the mock was called with the expected path
        from app import state as _app_state
        _app_state.file_editor.read_file.assert_awaited_once_with("s-1", "/etc/hostname")
        _app_state.audit_logger.log_file_access.assert_called_once()

    # ------------------------------------------------------------------
    # Happy path: file write
    # ------------------------------------------------------------------

    def test_file_write_returns_success(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        with TestClient(app) as client:
            self._setup_state()
            resp = client.post(
                "/api/file/write",
                headers={"X-API-Key": "secret-42"},
                json={
                    "session_id": "s-1",
                    "path": "/tmp/test.py",
                    "content": "print('hello')",
                },
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["path"] == "/tmp/test.py"
        assert data["size"] == len("print('hello')")
        assert data["mode"] == "write"
        from app import state as _app_state
        _app_state.file_editor.write_file.assert_awaited_once_with(
            "s-1", "/tmp/test.py", "print('hello')"
        )

    def test_file_write_append_mode(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        with TestClient(app) as client:
            self._setup_state()
            fe = self._make_file_editor_mock()
            fe.read_file = AsyncMock(return_value="existing content\n")
            from app import state as _app_state
            _app_state.file_editor = fe
            resp = client.post(
                "/api/file/write",
                headers={"X-API-Key": "secret-42"},
                json={
                    "session_id": "s-1",
                    "path": "/tmp/test.py",
                    "content": "new line",
                    "mode": "append",
                },
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["mode"] == "append"
        fe.write_file.assert_awaited_once_with(
            "s-1", "/tmp/test.py", "existing content\nnew line"
        )

    # ------------------------------------------------------------------
    # Happy path: file edit
    # ------------------------------------------------------------------

    def test_file_edit_returns_result(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")

        with TestClient(app) as client:
            self._setup_state()
            resp = client.patch(
                "/api/file/edit",
                headers={"X-API-Key": "secret-42"},
                json={
                    "session_id": "s-1",
                    "path": "/tmp/test.py",
                    "operations": [{"type": "replace", "old": "foo", "new": "bar"}],
                },
            )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["success"] is True
        assert data["operations_applied"] == 1
        from app import state as _app_state
        _app_state.file_editor.edit_file.assert_awaited_once()
        call_args = _app_state.file_editor.edit_file.await_args
        assert call_args is not None
        assert call_args[0][0] == "s-1"
        assert call_args[0][1] == "/tmp/test.py"
