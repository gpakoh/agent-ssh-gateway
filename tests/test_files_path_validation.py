"""Verify that project tree, structure, and file-watch reject traversal paths."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app

TRAVERSAL_PATHS = [
    "../../etc",
    "../",
    "~/config",
]


def _patch_base(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "secret-42")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
    monkeypatch.setattr("app.auth_middleware.is_ip_allowed", lambda ip, nets: True)


def _make_manager_mock():
    mgr = MagicMock()
    mgr.execute = AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0})
    mgr.get_session = AsyncMock(
        return_value=MagicMock(
            owner_type="master",
            owner_name="admin",
            owner_token_fingerprint="fp",
            is_connected=MagicMock(return_value=True),
        )
    )
    mgr.disconnect = AsyncMock()
    mgr.stop_cleanup_task = AsyncMock()
    mgr.list_sessions = AsyncMock(return_value=[])
    mgr.start_cleanup_task = AsyncMock()
    mgr.reconnect = AsyncMock(return_value=True)
    return mgr


def _override_manager(client, mock_mgr):
    from app import state as _app_state

    _app_state.manager = mock_mgr


class TestProjectTreeTraversalRejected:
    """GET /api/project/tree must reject traversal paths with 400."""

    @pytest.mark.parametrize("bad_path", TRAVERSAL_PATHS)
    def test_project_tree_rejects_traversal(self, monkeypatch, bad_path):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            _override_manager(client, _make_manager_mock())
            resp = client.get(
                "/api/project/tree",
                params={"session_id": "s-1", "path": bad_path},
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code == 400, (
            f"Expected 400 for path={bad_path!r}, got {resp.status_code}: {resp.text}"
        )

    def test_project_tree_accepts_valid_path(self, monkeypatch):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            _override_manager(client, _make_manager_mock())
            resp = client.get(
                "/api/project/tree",
                params={"session_id": "s-1", "path": "/home/user/project"},
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code != 400


class TestProjectFilesStructureTraversalRejected:
    """POST /api/project/files/structure must reject traversal paths with 400."""

    @pytest.mark.parametrize("bad_path", TRAVERSAL_PATHS)
    def test_project_files_structure_rejects_traversal(self, monkeypatch, bad_path):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            _override_manager(client, _make_manager_mock())
            resp = client.post(
                "/api/project/files/structure",
                json={"session_id": "s-1", "path": bad_path, "max_depth": 2},
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code == 400, (
            f"Expected 400 for path={bad_path!r}, got {resp.status_code}: {resp.text}"
        )

    def test_project_files_structure_accepts_valid(self, monkeypatch):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            _override_manager(client, _make_manager_mock())
            resp = client.post(
                "/api/project/files/structure",
                json={"session_id": "s-1", "path": "/valid/path", "max_depth": 2},
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code != 400


class TestFileWatchTraversalRejected:
    """WS /api/file/watch must reject traversal paths with error JSON."""

    @pytest.mark.parametrize("bad_path", TRAVERSAL_PATHS)
    def test_file_watch_rejects_traversal(self, monkeypatch, bad_path):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            _override_manager(client, _make_manager_mock())
            with client.websocket_connect(
                "/api/file/watch",
                headers={"X-API-Key": "secret-42"},
            ) as ws:
                ws.send_json({"session_id": "s-1", "path": bad_path})
                resp = ws.receive_json()
                assert resp.get("type") == "error", (
                    f"Expected error for path={bad_path!r}, got {resp}"
                )

    def test_file_watch_accepts_valid_path(self, monkeypatch):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            _override_manager(client, _make_manager_mock())
            with client.websocket_connect(
                "/api/file/watch",
                headers={"X-API-Key": "secret-42"},
            ) as ws:
                ws.send_json({"session_id": "s-1", "path": "/var/log/app.log"})
                resp = ws.receive_json()
                assert resp.get("type") != "error"


def _make_file_editor_mock():
    fe = MagicMock()
    fe.read_file = AsyncMock(return_value="secret file contents")
    fe.edit_file = AsyncMock(return_value={"success": True, "operations_applied": 1, "changed": True})
    return fe


class TestBulkReadForbiddenPathsRejected:
    """POST /api/bulk/read must not bypass validate_path()'s FORBIDDEN_PATHS
    guardrail — unlike its singular sibling POST /api/file/read (and the
    batch sibling POST /api/batch/read), it delegated straight to
    BulkOperationsManager.read_files_bulk() which called
    file_editor.read_file() with no validation at all.
    """

    def test_bulk_read_rejects_forbidden_path(self, monkeypatch):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            _override_manager(client, _make_manager_mock())
            from app import state as _app_state

            fe = _make_file_editor_mock()
            _app_state.file_editor = fe
            resp = client.post(
                "/api/bulk/read",
                json={"session_id": "s-1", "paths": ["/etc/shadow"]},
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code == 200
        assert "/etc/shadow" not in resp.json()["files"], (
            "forbidden path content leaked through bulk read"
        )
        fe.read_file.assert_not_called()

    def test_bulk_read_accepts_valid_path(self, monkeypatch):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            _override_manager(client, _make_manager_mock())
            from app import state as _app_state

            _app_state.file_editor = _make_file_editor_mock()
            resp = client.post(
                "/api/bulk/read",
                json={"session_id": "s-1", "paths": ["/home/user/app.py"]},
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code == 200
        assert resp.json()["files"].get("/home/user/app.py") == "secret file contents"


class TestBulkEditForbiddenPathsRejected:
    """PATCH /api/bulk/edit must not bypass validate_path()'s FORBIDDEN_PATHS
    guardrail — unlike its near-identical sibling PATCH /api/batch/edit
    (same underlying edit_many() helper), it passed validate=False so no
    check ever ran.
    """

    def test_bulk_edit_rejects_forbidden_path(self, monkeypatch):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            _override_manager(client, _make_manager_mock())
            from app import state as _app_state

            fe = _make_file_editor_mock()
            _app_state.file_editor = fe
            resp = client.post(
                "/api/bulk/edit",
                json={
                    "session_id": "s-1",
                    "files": [
                        {
                            "path": "/root/.ssh/authorized_keys",
                            "operations": [{"type": "replace", "old": "a", "new": "b"}],
                        }
                    ],
                },
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["success"] is False, "forbidden path write was not rejected"
        fe.edit_file.assert_not_called()

    def test_bulk_edit_accepts_valid_path(self, monkeypatch):
        _patch_base(monkeypatch)
        with TestClient(app) as client:
            _override_manager(client, _make_manager_mock())
            from app import state as _app_state

            _app_state.file_editor = _make_file_editor_mock()
            resp = client.post(
                "/api/bulk/edit",
                json={
                    "session_id": "s-1",
                    "files": [
                        {
                            "path": "/home/user/app.py",
                            "operations": [{"type": "replace", "old": "a", "new": "b"}],
                        }
                    ],
                },
                headers={"X-API-Key": "secret-42"},
            )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["success"] is True
