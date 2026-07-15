"""Tests for workspace readonly mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

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
