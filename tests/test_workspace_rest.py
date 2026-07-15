"""Integration smoke tests for workspace REST endpoints."""

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


@pytest.fixture(autouse=True)
def _auth_bypass(monkeypatch):
    """Bypass IP allowlist and set API key for TestClient."""
    monkeypatch.setattr(settings, "api_key", "test-api-key")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


@pytest.fixture(autouse=True)
def _workspace_test_registry(monkeypatch, tmp_path: Path):
    """Use a temp project registry instead of host-specific /media/1TB paths."""
    project_ids = [
        "web-ssh-gateway",
        "nod-gateway",
        "quart-platform",
        "quart-core",
        "kojo-bot-service",
        "pricetuner-scraper",
        "tg-audio-bot",
    ]

    projects: dict[str, ProjectInfo] = {}
    for project_id in project_ids:
        root = tmp_path / project_id
        root.mkdir(parents=True)
        projects[project_id] = ProjectInfo(
            project_id=project_id,
            root=root,
            type="app",
            description=f"{project_id} test fixture",
            tags=["test"],
            parent="quart-platform" if project_id in {"quart-core", "kojo-bot-service"} else None,
        )

    web_root = tmp_path / "web-ssh-gateway"
    (web_root / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    policy_path = web_root / "app" / "workspace"
    policy_path.mkdir(parents=True)
    (policy_path / "policy.py").write_text(
        "class WorkspacePolicy:\n    pass\n",
        encoding="utf-8",
    )

    registry = WorkspaceRegistry(
        projects,
        [tmp_path],
        granted_scopes={
            "project:read",
            "project:write",
            "workspace:read",
            "workspace:write",
        },
    )

    def fake_get_registry(*_args: Any, **_kwargs: Any) -> WorkspaceRegistry:
        return registry

    monkeypatch.setattr("app.workspace.tools.get_registry", fake_get_registry)
    monkeypatch.setattr("app.workspace.files.get_registry", fake_get_registry)
    monkeypatch.setattr("app.workspace.search.get_registry", fake_get_registry)
    monkeypatch.setattr("app.workspace.git.get_registry", fake_get_registry)
    monkeypatch.setattr("app.routers.workspace.get_registry", fake_get_registry)


client = TestClient(app)

API_KEY = "test-api-key"

HEADERS = {"X-API-Key": API_KEY}


def _get(path: str, **kwargs):
    return client.get(path, headers=HEADERS, **kwargs)


def _post(path: str, json: dict[str, Any] | None = None, headers: dict | None = None, **kwargs):
    return client.post(path, json=json, headers=headers or HEADERS, **kwargs)


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


# ---------------------------------------------------------------------------
# File write — POST /api/workspace/projects/{project_id}/files/write
# ---------------------------------------------------------------------------


@pytest.fixture()
def write_ws(tmp_path: Path):
    """Create a temporary project tree for write endpoint tests."""
    project = tmp_path / "test-project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("print('hello')\n")
    (project / ".env").write_text("SECRET=abc\n")
    return {"tmp_path": tmp_path, "project": project}


def _mock_registry(project_root: Path):
    """Return a WorkspaceRegistry pointing at *project_root*."""
    from app.workspace.models import ProjectInfo

    projects = {
        "test-project": ProjectInfo(
            project_id="test-project",
            root=project_root,
            type="app",
            description="test",
            tags=[],
        )
    }
    return WorkspaceRegistry(
        projects,
        [project_root.parent],
        granted_scopes={
            "project:read",
            "project:write",
            "workspace:read",
            "workspace:write",
        },
    )


def _mock_master_identity():
    """Return a master AuthIdentity."""
    from app.auth_middleware import AuthIdentity

    return AuthIdentity(
        token_type="master", token=API_KEY, name="master", scopes=("*",)
    )


def _mock_agent_identity(scopes: tuple[str, ...] = ()):
    """Return an agent AuthIdentity with the given scopes."""
    from app.auth_middleware import AuthIdentity

    return AuthIdentity(
        token_type="agent", token="agent-token", name="agent", scopes=scopes
    )


class TestWriteFileEndpoint:
    def test_write_success(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/write",
                json={"path": "out.txt", "content": "new content"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == "test-project"
        assert data["size"] > 0
        assert (write_ws["project"] / "out.txt").read_text() == "new content"

    def test_scope_denied(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        identity = _mock_agent_identity(scopes=("ssh:connect",))
        from unittest.mock import AsyncMock

        with (
            patch(
                "app.auth_middleware.verify_api_key",
                AsyncMock(return_value=identity),
            ),
            patch(
                "app.routers.workspace.get_registry", return_value=registry
            ),
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/write",
                json={"path": "out.txt", "content": "data"},
                headers={"X-API-Key": "agent-token"},
            )
        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["code"] == "MISSING_SCOPE"

    def test_traversal_rejected(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/write",
                json={"path": "../escape.txt", "content": "data"},
            )
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"]["code"] == "TRAVERSALERROR"

    def test_symlink_rejected(self, write_ws):
        sibling = write_ws["tmp_path"] / "other"
        sibling.mkdir()
        (sibling / "loot.txt").write_text("stolen")
        link = write_ws["project"] / "escape_link"
        link.symlink_to(sibling)

        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/write",
                json={"path": "escape_link/loot.txt", "content": "hacked"},
            )
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"]["code"] == "SYMLINKESCAPEERROR"

    def test_hidden_path_rejected(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/write",
                json={"path": ".env", "content": "EVIL=true"},
            )
        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["code"] == "HIDDENPATHERROR" or "HIDDEN" in body["detail"]["code"]

    def test_unknown_project(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/nonexistent/files/write",
                json={"path": "x.txt", "content": "data"},
            )
        assert resp.status_code == 404

    def test_content_too_large(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        big = "x" * 2_000_001
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/write",
                json={"path": "big.txt", "content": big},
            )
        assert resp.status_code == 413
        body = resp.json()
        assert body["detail"]["code"] == "CONTENT_TOO_LARGE"

    def test_no_secret_in_error(self, write_ws):
        """Error responses must not leak absolute file system paths."""
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/write",
                json={"path": ".env", "content": "EVIL=true"},
            )
        assert resp.status_code == 403
        body_text = resp.text
        assert str(write_ws["tmp_path"]) not in body_text


# ---------------------------------------------------------------------------
# File edit — POST /api/workspace/projects/{project_id}/files/edit
# ---------------------------------------------------------------------------


class TestEditFileEndpoint:
    def test_edit_success(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/edit",
                json={
                    "path": "src/main.py",
                    "old_string": "hello",
                    "new_string": "world",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == "test-project"
        assert data["diff"]
        assert (write_ws["project"] / "src" / "main.py").read_text() == "print('world')\n"

    def test_scope_denied(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        identity = _mock_agent_identity(scopes=("ssh:connect",))
        from unittest.mock import AsyncMock

        with (
            patch(
                "app.auth_middleware.verify_api_key",
                AsyncMock(return_value=identity),
            ),
            patch(
                "app.routers.workspace.get_registry", return_value=registry
            ),
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/edit",
                json={
                    "path": "src/main.py",
                    "old_string": "hello",
                    "new_string": "world",
                },
                headers={"X-API-Key": "agent-token"},
            )
        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["code"] == "MISSING_SCOPE"

    def test_traversal_rejected(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/edit",
                json={
                    "path": "../escape.txt",
                    "old_string": "a",
                    "new_string": "b",
                },
            )
        assert resp.status_code == 400

    def test_symlink_rejected(self, write_ws):
        sibling = write_ws["tmp_path"] / "other"
        sibling.mkdir()
        (sibling / "loot.txt").write_text("stolen")
        link = write_ws["project"] / "escape_link"
        link.symlink_to(sibling)

        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/edit",
                json={
                    "path": "escape_link/loot.txt",
                    "old_string": "stolen",
                    "new_string": "hacked",
                },
            )
        assert resp.status_code == 400

    def test_hidden_path_rejected(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/edit",
                json={
                    "path": ".env",
                    "old_string": "SECRET",
                    "new_string": "EVIL",
                },
            )
        assert resp.status_code == 403

    def test_unknown_project(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/nonexistent/files/edit",
                json={
                    "path": "x.txt",
                    "old_string": "a",
                    "new_string": "b",
                },
            )
        assert resp.status_code == 404

    def test_content_too_large(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        big_new = "y" * 2_000_001
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/edit",
                json={
                    "path": "src/main.py",
                    "old_string": "hello",
                    "new_string": big_new,
                },
            )
        assert resp.status_code == 413
        body = resp.json()
        assert body["detail"]["code"] == "CONTENT_TOO_LARGE"

    def test_old_string_not_found(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/edit",
                json={
                    "path": "src/main.py",
                    "old_string": "DOES_NOT_EXIST",
                    "new_string": "b",
                },
            )
        assert resp.status_code == 404
        body = resp.json()
        assert "not found" in body["detail"].get("message", "").lower()

    def test_no_secret_in_error(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/edit",
                json={
                    "path": ".env",
                    "old_string": "SECRET",
                    "new_string": "EVIL",
                },
            )
        assert resp.status_code == 403
        body_text = resp.text
        assert str(write_ws["tmp_path"]) not in body_text


# ---------------------------------------------------------------------------
# Apply patch — POST /api/workspace/projects/{project_id}/files/patch
# ---------------------------------------------------------------------------


def _make_patch(filename: str, old: str, new: str) -> str:
    """Build a minimal unified diff patch."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    header = f"--- a/{filename}\n+++ b/{filename}\n"
    hunk = f"@@ -1,{len(old_lines)} +1,{len(new_lines)} @@\n"
    body = "".join(
        f"-{line}\n" if not line.endswith("\n") else f"-{line}"
        for line in old_lines
    )
    body += "".join(
        f"+{line}\n" if not line.endswith("\n") else f"+{line}"
        for line in new_lines
    )
    return header + hunk + body


class TestPatchFileEndpoint:
    def test_patch_success(self, write_ws):
        patch_text = _make_patch("src/main.py", "print('hello')", "print('world')")
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/patch",
                json={"path": "src/main.py", "patch": patch_text},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == "test-project"
        assert "backup_hash" in data
        # Response must NOT leak the patch text (v3.1 security)
        assert "patch" not in data or data["patch"] is None
        assert patch_text not in resp.text

    def test_scope_denied(self, write_ws):
        patch_text = _make_patch("src/main.py", "hello", "world")
        registry = _mock_registry(write_ws["project"])
        identity = _mock_agent_identity(scopes=("ssh:connect",))
        from unittest.mock import AsyncMock

        with (
            patch(
                "app.auth_middleware.verify_api_key",
                AsyncMock(return_value=identity),
            ),
            patch(
                "app.routers.workspace.get_registry", return_value=registry
            ),
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/patch",
                json={"path": "src/main.py", "patch": patch_text},
                headers={"X-API-Key": "agent-token"},
            )
        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["code"] == "MISSING_SCOPE"

    def test_traversal_rejected(self, write_ws):
        patch_text = _make_patch("escape.txt", "a", "b")
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/patch",
                json={"path": "../escape.txt", "patch": patch_text},
            )
        assert resp.status_code == 400

    def test_symlink_rejected(self, write_ws):
        sibling = write_ws["tmp_path"] / "other"
        sibling.mkdir()
        (sibling / "loot.txt").write_text("stolen")
        link = write_ws["project"] / "escape_link"
        link.symlink_to(sibling)

        patch_text = _make_patch("escape_link/loot.txt", "stolen", "hacked")
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/patch",
                json={"path": "escape_link/loot.txt", "patch": patch_text},
            )
        assert resp.status_code == 400

    def test_hidden_path_rejected(self, write_ws):
        patch_text = _make_patch(".env", "SECRET", "EVIL")
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/patch",
                json={"path": ".env", "patch": patch_text},
            )
        assert resp.status_code == 403

    def test_unknown_project(self, write_ws):
        patch_text = _make_patch("x.txt", "a", "b")
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/nonexistent/files/patch",
                json={"path": "x.txt", "patch": patch_text},
            )
        assert resp.status_code == 404

    def test_content_too_large(self, write_ws):
        old_content = "print('hello')"
        new_content = "y" * 2_000_001
        patch_text = _make_patch("src/main.py", old_content, new_content)
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/patch",
                json={"path": "src/main.py", "patch": patch_text},
            )
        assert resp.status_code == 413

    def test_invalid_patch_rejected(self, write_ws):
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/patch",
                json={"path": "src/main.py", "patch": "this is not a patch"},
            )
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"]["code"] == "PATCHERROR"

    def test_binary_content_rejected(self, write_ws):
        """Binary (non-UTF-8) content must be rejected."""
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = client.post(
                "/api/workspace/projects/test-project/files/write",
                json={"path": "bin.txt", "content": "data"},
                content=b'{"path":"bin.txt","content":"\xff\xfe"}',
                headers={**HEADERS, "Content-Type": "application/json"},
            )
        # FastAPI may reject malformed JSON or decode errors
        assert resp.status_code in (400, 422)

    def test_no_secret_in_error(self, write_ws):
        patch_text = _make_patch(".env", "SECRET", "EVIL")
        registry = _mock_registry(write_ws["project"])
        with patch(
            "app.routers.workspace.get_registry", return_value=registry
        ):
            resp = _post(
                "/api/workspace/projects/test-project/files/patch",
                json={"path": ".env", "patch": patch_text},
            )
        assert resp.status_code == 403
        body_text = resp.text
        assert str(write_ws["tmp_path"]) not in body_text
