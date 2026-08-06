"""Regression tests: POST /api/context/file/read, PATCH /api/context/file/edit,
POST /api/analytics, and POST /api/tree must validate `path` the same way
every route in routers/files.py does.

None of these four routes called validate_path() before touching the
filesystem -- context_file_read/context_file_edit read/write through
FileEditor directly (edit_file_with_context() resolves a relative path
against ctx.path but never validates it, and an absolute path bypasses
ctx.path entirely, same pattern as batch_operations.py before its fix);
run_analytics/get_file_tree_v2 pass `path` straight into
Analytics.analyze_project()/FileTreeExplorer.get_tree(), which build their
own shell commands directly (bypassing command_policy). All four are
master-key gated, but validate_path()'s FORBIDDEN_PATHS denylist and
traversal check are supposed to apply uniformly regardless of privilege
level, matching every other file-editing/inspection route in this codebase.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


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


class TestContextFileReadPathTraversal:
    @pytest.mark.parametrize("path", ["/etc/passwd", "/proc", "/sys", "/dev", "/boot"])
    def test_forbidden_path_rejected(self, client, path):
        resp = client.post(
            "/api/context/file/read",
            json={"session_id": "fake", "path": path},
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400
        assert "forbidden" in resp.json()["detail"]["message"].lower()

    def test_traversal_rejected(self, client):
        resp = client.post(
            "/api/context/file/read",
            json={"session_id": "fake", "path": "/home/user/project/../../etc/passwd"},
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400


class TestContextFileEditPathTraversal:
    @pytest.mark.parametrize("path", ["/etc/passwd", "/proc", "/sys", "/dev", "/boot"])
    def test_forbidden_path_rejected(self, client, path):
        resp = client.patch(
            "/api/context/file/edit",
            json={
                "context_id": "fake",
                "path": path,
                "operations": [{"type": "append", "text": "pwned"}],
            },
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400
        assert "forbidden" in resp.json()["detail"]["message"].lower()

    def test_traversal_rejected(self, client):
        resp = client.patch(
            "/api/context/file/edit",
            json={
                "context_id": "fake",
                "path": "/home/user/project/../../root/.ssh",
                "operations": [{"type": "append", "text": "pwned"}],
            },
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400


class TestAnalyticsPathTraversal:
    @pytest.mark.parametrize("path", ["/etc/passwd", "/proc", "/sys", "/dev", "/boot"])
    def test_forbidden_path_rejected(self, client, path):
        resp = client.post(
            "/api/analytics",
            json={"session_id": "fake", "path": path},
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400
        assert "forbidden" in resp.json()["detail"]["message"].lower()

    def test_traversal_rejected(self, client):
        resp = client.post(
            "/api/analytics",
            json={"session_id": "fake", "path": "/home/user/project/../../etc"},
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400


class TestFileTreePathTraversal:
    @pytest.mark.parametrize("path", ["/etc/passwd", "/proc", "/sys", "/dev", "/boot"])
    def test_forbidden_path_rejected(self, client, path):
        resp = client.post(
            "/api/tree",
            json={"session_id": "fake", "path": path},
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400
        assert "forbidden" in resp.json()["detail"]["message"].lower()

    def test_traversal_rejected(self, client):
        resp = client.post(
            "/api/tree",
            json={"session_id": "fake", "path": "/home/user/project/../../etc"},
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 400
