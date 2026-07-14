"""Integration smoke tests for workspace REST endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def _auth_bypass(monkeypatch):
    """Bypass IP allowlist and set API key for TestClient."""
    monkeypatch.setattr(settings, "api_key", "test-api-key")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


client = TestClient(app)

API_KEY = "test-api-key"

HEADERS = {"X-API-Key": API_KEY}


def _get(path: str, **kwargs):
    return client.get(path, headers=HEADERS, **kwargs)


# ---------------------------------------------------------------------------
# Project listing
# ---------------------------------------------------------------------------


class TestWorkspaceProjects:
    def test_list_projects(self):
        resp = _get("/api/workspace/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert "projects" in data
        assert data["count"] >= 7
        ids = [p["project_id"] for p in data["projects"]]
        assert "web-ssh-gateway" in ids

    def test_project_info(self):
        resp = _get("/api/workspace/projects/web-ssh-gateway")
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == "web-ssh-gateway"
        assert "type" in data

    def test_project_info_not_found(self):
        resp = _get("/api/workspace/projects/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------


class TestWorkspaceTree:
    def test_tree_root(self):
        resp = _get("/api/workspace/projects/web-ssh-gateway/tree")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "directory"
        assert "children" in data

    def test_tree_with_depth(self):
        resp = _get("/api/workspace/projects/web-ssh-gateway/tree?depth=1")
        assert resp.status_code == 200

    def test_tree_not_found(self):
        resp = _get("/api/workspace/projects/nonexistent/tree")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# File read
# ---------------------------------------------------------------------------


class TestWorkspaceFileRead:
    def test_read_file(self):
        resp = _get(
            "/api/workspace/projects/web-ssh-gateway/files/read",
            params={"path": "pyproject.toml"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "file"
        assert "content" in data
        assert data["truncated"] is False

    def test_read_file_not_found(self):
        resp = _get(
            "/api/workspace/projects/web-ssh-gateway/files/read",
            params={"path": "nonexistent.py"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# File find
# ---------------------------------------------------------------------------


class TestWorkspaceFindFiles:
    def test_find_files(self):
        resp = _get(
            "/api/workspace/projects/web-ssh-gateway/files/find",
            params={"pattern": "*.py", "max_results": 5},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) <= 5


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestWorkspaceSearch:
    def test_search(self):
        resp = _get(
            "/api/workspace/projects/web-ssh-gateway/search",
            params={"query": "WorkspacePolicy", "max_matches": 3},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["matches"]) <= 3

    def test_search_empty_query(self):
        resp = _get(
            "/api/workspace/projects/web-ssh-gateway/search",
            params={"query": ""},
        )
        assert resp.status_code == 422  # FastAPI validation


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


class TestWorkspaceGit:
    def test_git_status(self):
        resp = _get("/api/workspace/projects/web-ssh-gateway/git/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "is_git_repo" in data

    def test_git_branch(self):
        resp = _get("/api/workspace/projects/web-ssh-gateway/git/branch")
        assert resp.status_code == 200

    def test_git_log(self):
        resp = _get(
            "/api/workspace/projects/web-ssh-gateway/git/log",
            params={"limit": 5},
        )
        assert resp.status_code == 200

    def test_git_diff(self):
        resp = _get("/api/workspace/projects/web-ssh-gateway/git/diff")
        assert resp.status_code == 200

    def test_git_non_repo(self):
        resp = _get("/api/workspace/projects/nod-gateway/git/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("is_git_repo") is False
