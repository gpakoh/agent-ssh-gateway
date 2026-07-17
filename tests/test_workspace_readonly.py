"""Tests for workspace readonly mode."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.audit import AuditEventLogger, AuditEventType
from app.config import settings
from app.main import app
from app.workspace.models import ProjectInfo
from app.workspace.registry import WorkspaceRegistry


@pytest.fixture
def client():
    with patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1"):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


@pytest.fixture(autouse=True)
def _auth_bypass(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "test-api-key")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


@pytest.fixture(autouse=True)
def _workspace_test_registry(monkeypatch, tmp_path: Path):
    project_root = tmp_path / "web-ssh-gateway"
    project_root.mkdir(parents=True)
    (project_root / "README.md").write_text("test\n", encoding="utf-8")

    registry = WorkspaceRegistry(
        {
            "web-ssh-gateway": ProjectInfo(
                project_id="web-ssh-gateway",
                root=project_root,
                type="app",
                description="readonly test fixture",
                tags=["test"],
            )
        },
        [tmp_path],
        granted_scopes={"project:read", "project:write", "workspace:read"},
    )

    def fake_get_registry(*_args: Any, **_kwargs: Any) -> WorkspaceRegistry:
        return registry

    monkeypatch.setattr("app.workspace.tools.get_registry", fake_get_registry)
    monkeypatch.setattr("app.workspace.files.get_registry", fake_get_registry)
    monkeypatch.setattr("app.workspace.preview.get_registry", fake_get_registry)
    monkeypatch.setattr("app.routers.workspace.get_registry", fake_get_registry)


class TestReadonlyBlocksWorkspaceWrites:
    """Verify workspace_readonly=true blocks workspace write/edit/patch endpoints."""

    def test_write_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/workspace/projects/web-ssh-gateway/files/write",
                json={"path": "_test.txt", "content": "hello"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_edit_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/workspace/projects/web-ssh-gateway/files/edit",
                json={"path": "README.md", "old_string": "test", "new_string": "test2"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_patch_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/workspace/projects/web-ssh-gateway/files/patch",
                json={"path": "README.md", "patch": "--- a/test\n+++ b/test\n"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403


class TestReadonlyBlocksFileWrites:
    """Verify workspace_readonly=true blocks file upload/AST endpoints."""

    def test_file_upload_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/file/upload",
                params={"session_id": "fake", "path": "/tmp/test", "content": "aGVsbG8="},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_file_upload_json_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/file/upload/json",
                json={"session_id": "fake", "path": "/tmp/test", "content": "aGVsbG8="},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_project_apply_patch_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/projects/web-ssh-gateway/apply-patch",
                json={
                    "session_id": "fake-session-id",
                    "project": "web-ssh-gateway",
                    "patch": "--- a/test\n+++ b/test\n@@ -1 +1 @@\n-old\n+new\n",
                    "expected_hashes": {"test.py": "sha256:abc"},
                },
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_ast_rename_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/ast/rename",
                json={"session_id": "fake", "path": "test.py", "old_name": "foo", "new_name": "bar"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_refactor_rename_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/refactor/rename",
                json={"session_id": "fake", "path": "test.py", "old_name": "foo", "new_name": "bar"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_ast_extract_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/ast/extract",
                json={"session_id": "fake", "path": "test.py", "start_line": 1, "end_line": 5, "func_name": "new_func"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403


class TestReadonlyAllowsReads:
    """Verify preview/verify still work when readonly."""

    def test_preview_write_allowed(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/workspace/projects/web-ssh-gateway/files/preview/write",
                json={"path": "_test.txt", "content": "hello"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200
            assert resp.json()["changed"] is True

    def test_preview_edit_allowed(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/workspace/projects/web-ssh-gateway/files/preview/edit",
                json={"path": "README.md", "old_string": "test", "new_string": "test2"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200

    def test_preview_patch_allowed(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/workspace/projects/web-ssh-gateway/files/preview/patch",
                json={
                    "path": "README.md",
                    "patch": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n",
                },
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code in (200, 400)

    def test_verify_allowed(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/workspace/projects/web-ssh-gateway/files/verify",
                json={"path": "README.md", "expected_hash": "sha256:wrong"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200


class TestReadonlyBlocksLegacyMutators:
    """Verify workspace_readonly=true blocks scaffold/code/replace/template endpoints."""

    def test_scaffold_python_class_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/scaffold/python-class",
                json={
                    "session_id": "fake",
                    "class_name": "TestClass",
                    "module_path": "/tmp/test",
                },
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_code_insert_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/code/insert",
                json={
                    "context_id": "fake",
                    "path": "test.py",
                    "instruction": "add a method",
                },
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_replace_global_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/replace/global",
                json={
                    "session_id": "fake",
                    "path": "/tmp",
                    "search": "old",
                    "replace": "new",
                    "dry_run": False,
                },
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_replace_global_dry_run_allowed(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/replace/global",
                json={
                    "session_id": "fake",
                    "path": "/tmp",
                    "search": "old",
                    "replace": "new",
                    "dry_run": True,
                },
                headers={"X-API-Key": settings.api_key},
            )
            # dry_run=true should NOT be blocked by readonly
            assert resp.status_code != 403

    def test_templates_render_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/templates/render",
                json={
                    "context_id": "fake",
                    "template_id": "test",
                    "target_path": "/tmp/test.py",
                    "params": {},
                },
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403


class TestReadonlyBlocksSnapshotOps:
    """Verify workspace_readonly=true blocks snapshot create/restore/delete."""

    def test_snapshot_create_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/snapshots",
                json={"context_id": "fake", "name": "test"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_snapshot_restore_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/snapshots/restore",
                json={"context_id": "fake", "snapshot_id": "snap-123"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_snapshot_delete_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.delete(
                "/api/snapshots/snap-123",
                params={"context_id": "fake"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403


class TestReadonlyBlocksGitOps:
    """Verify workspace_readonly=true blocks git init/commit/backup/restore."""

    def test_git_init_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/git/init",
                json={"context_id": "fake"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_git_commit_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/git/commit",
                json={"context_id": "fake", "message": "test"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_git_backup_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/git/backup",
                params={"context_id": "fake"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_git_restore_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/git/restore",
                params={"context_id": "fake"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403


class TestReadonlyBlocksRecoveryOps:
    """Verify workspace_readonly=true blocks recovery backup/restore."""

    def test_recovery_backup_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/recovery/backup",
                json={"context_id": "fake", "name": "test"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

    def test_recovery_restore_blocked(self, client):
        with patch.object(settings, "workspace_readonly", True):
            resp = client.post(
                "/api/recovery/restore",
                json={"context_id": "fake"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403


class TestReadonlyAuditAttribution:
    """WORKSPACE_READONLY audit events include actor identity when available."""

    def test_write_deny_includes_actor_fingerprint(self, client, monkeypatch):
        monkeypatch.setattr(settings, "workspace_readonly", True)
        from app import state as _app_state

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "audit.jsonl")
            _app_state.event_audit_logger = AuditEventLogger(
                log_path=log_path, recent_limit=100
            )

            resp = client.post(
                "/api/workspace/projects/web-ssh-gateway/files/write",
                json={"path": "_test.txt", "content": "hello"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

            events = _app_state.event_audit_logger.recent()
            readonly_events = [
                e for e in events
                if e.event_type == AuditEventType.WORKSPACE_READONLY_BLOCK
            ]
            assert len(readonly_events) >= 1
            evt = readonly_events[0]
            assert evt.actor_type == "master"
            assert evt.actor_fingerprint  # non-empty
            assert len(evt.actor_fingerprint) == 12
            assert evt.route == "POST /api/workspace/projects/*/files/write"

    def test_edit_deny_includes_actor_fingerprint(self, client, monkeypatch):
        monkeypatch.setattr(settings, "workspace_readonly", True)
        from app import state as _app_state

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "audit.jsonl")
            _app_state.event_audit_logger = AuditEventLogger(
                log_path=log_path, recent_limit=100
            )

            resp = client.post(
                "/api/workspace/projects/web-ssh-gateway/files/edit",
                json={"path": "README.md", "old_string": "test", "new_string": "test2"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 403

            events = _app_state.event_audit_logger.recent()
            readonly_events = [
                e for e in events
                if e.event_type == AuditEventType.WORKSPACE_READONLY_BLOCK
            ]
            assert len(readonly_events) >= 1
            evt = readonly_events[0]
            assert evt.actor_type == "master"
            assert evt.actor_fingerprint
            assert evt.route == "POST /api/workspace/projects/*/files/edit"

    def test_fingerprint_is_not_raw_key(self, client, monkeypatch):
        monkeypatch.setattr(settings, "workspace_readonly", True)
        from app import state as _app_state

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "audit.jsonl")
            _app_state.event_audit_logger = AuditEventLogger(
                log_path=log_path, recent_limit=100
            )

            client.post(
                "/api/workspace/projects/web-ssh-gateway/files/write",
                json={"path": "_test.txt", "content": "hello"},
                headers={"X-API-Key": settings.api_key},
            )

            events = _app_state.event_audit_logger.recent()
            readonly_events = [
                e for e in events
                if e.event_type == AuditEventType.WORKSPACE_READONLY_BLOCK
            ]
            assert len(readonly_events) >= 1
            evt = readonly_events[0]
            # Fingerprint must be hash-truncated, not raw API key
            assert settings.api_key not in evt.actor_fingerprint
            assert "-" not in evt.actor_fingerprint
