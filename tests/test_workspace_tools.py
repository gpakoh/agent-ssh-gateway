"""Smoke tests for workspace tools — real projects.yaml, all 7 projects."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workspace.policy import WorkspacePolicyError
from app.workspace.registry import reset_registry
from app.workspace.tools import project_info, project_tree, workspace_list_projects


def _real_workspace_available() -> bool:
    """Return True only on the production/dev host with real projects mounted."""
    return Path("/media/1TB/Python/web_ssh/web-ssh-gateway").exists()


pytestmark = pytest.mark.skipif(
    not _real_workspace_available(),
    reason="real /media/1TB/Python workspace is not available",
)


@pytest.fixture(autouse=True)
def _reset():
    """Reset registry singleton before each test."""
    reset_registry()
    yield
    reset_registry()


class TestWorkspaceListProjects:
    def test_returns_all_projects(self):
        projects = workspace_list_projects()
        ids = [p["project_id"] for p in projects]
        assert "web-ssh-gateway" in ids
        assert "quart-platform" in ids
        assert "quart-core" in ids
        assert "kojo-bot-service" in ids
        assert "pricetuner-scraper" in ids
        assert "tg-audio-bot" in ids
        assert "nod-gateway" in ids
        assert len(projects) == 7

    def test_project_has_expected_fields(self):
        projects = workspace_list_projects()
        for p in projects:
            assert "project_id" in p
            assert "type" in p
            assert "description" in p
            assert "tags" in p


class TestProjectInfo:
    def test_info_returns_metadata(self):
        info = project_info("web-ssh-gateway")
        assert info["project_id"] == "web-ssh-gateway"
        assert "root" in info
        assert "type" in info

    def test_info_includes_parent(self):
        info = project_info("quart-core")
        assert info.get("parent") == "quart-platform"

    def test_info_no_parent_for_root(self):
        info = project_info("quart-platform")
        assert "parent" not in info

    def test_info_unknown_project(self):
        with pytest.raises((WorkspacePolicyError, KeyError)):
            project_info("nonexistent-project")


class TestProjectTree:
    def test_tree_root(self):
        tree = project_tree("web-ssh-gateway")
        assert tree["type"] == "directory"
        assert "children" in tree

    def test_tree_depth_limit(self):
        tree = project_tree("web-ssh-gateway", depth=1)
        for child in tree.get("children", []):
            if child["type"] == "directory":
                assert "children" not in child or child.get("children") is None

    def test_tree_all_projects(self):
        """Smoke: project_tree works for all 7 projects."""
        projects = workspace_list_projects()
        for p in projects:
            tree = project_tree(p["project_id"])
            assert tree["type"] == "directory"
