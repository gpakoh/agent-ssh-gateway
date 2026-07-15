"""Portable tests for workspace tools — temp registry, no host dependencies.

These tests run in GitHub CI without /media/1TB/Python.
They validate list/info/tree logic against a synthetic project tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workspace.models import ProjectInfo
from app.workspace.registry import WorkspaceRegistry
from app.workspace.tools import project_info, project_tree, workspace_list_projects


@pytest.fixture()
def _tmp_registry(monkeypatch, tmp_path: Path):
    """Build a temp registry with 3 synthetic projects."""
    project_ids = ["alpha", "beta", "gamma"]
    projects: dict[str, ProjectInfo] = {}
    for pid in project_ids:
        root = tmp_path / pid
        root.mkdir(parents=True)
        # seed a few files so tree has something to show
        (root / "README.md").write_text(f"# {pid}", encoding="utf-8")
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
        projects[pid] = ProjectInfo(
            project_id=pid,
            root=root,
            type="app",
            description=f"{pid} fixture",
            tags=["test"],
            parent="alpha" if pid in {"beta", "gamma"} else None,
        )

    registry = WorkspaceRegistry(
        projects,
        [tmp_path],
        granted_scopes={"project:read", "workspace:read"},
    )

    from app.workspace import tools as _tools_mod

    monkeypatch.setattr(_tools_mod, "get_registry", lambda: registry)
    yield registry


class TestListProjectsPortable:
    def test_returns_all(self, _tmp_registry: WorkspaceRegistry):
        projects = workspace_list_projects(_tmp_registry)
        ids = [p["project_id"] for p in projects]
        assert ids == ["alpha", "beta", "gamma"]
        assert len(projects) == 3

    def test_fields_present(self, _tmp_registry: WorkspaceRegistry):
        for p in workspace_list_projects(_tmp_registry):
            assert "project_id" in p
            assert "type" in p
            assert "description" in p
            assert "tags" in p


class TestProjectInfoPortable:
    def test_returns_metadata(self, _tmp_registry: WorkspaceRegistry):
        info = project_info("alpha", _tmp_registry)
        assert info["project_id"] == "alpha"
        assert "root" in info
        assert "type" in info

    def test_includes_parent(self, _tmp_registry: WorkspaceRegistry):
        info = project_info("beta", _tmp_registry)
        assert info.get("parent") == "alpha"

    def test_no_parent_for_root(self, _tmp_registry: WorkspaceRegistry):
        info = project_info("alpha", _tmp_registry)
        assert "parent" not in info

    def test_unknown_project(self, _tmp_registry: WorkspaceRegistry):
        from app.workspace.policy import WorkspacePolicyError

        with pytest.raises(WorkspacePolicyError):
            project_info("nonexistent", _tmp_registry)


class TestProjectTreePortable:
    def test_tree_root(self, _tmp_registry: WorkspaceRegistry):
        tree = project_tree("alpha", registry=_tmp_registry)
        assert tree["type"] == "directory"
        assert "children" in tree

    def test_depth_limit(self, _tmp_registry: WorkspaceRegistry):
        tree = project_tree("alpha", depth=1, registry=_tmp_registry)
        for child in tree.get("children", []):
            if child["type"] == "directory":
                assert "children" not in child or child.get("children") is None

    def test_all_projects(self, _tmp_registry: WorkspaceRegistry):
        for p in workspace_list_projects(_tmp_registry):
            tree = project_tree(p["project_id"], registry=_tmp_registry)
            assert tree["type"] == "directory"
