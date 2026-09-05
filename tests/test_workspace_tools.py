"""Smoke tests for workspace tools against an isolated registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workspace.policy import WorkspacePolicyError
from app.workspace.registry import WorkspaceRegistry
from app.workspace.tools import project_info, project_tree, workspace_list_projects


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep workspace-tool smoke tests independent of the live host registry."""
    workspace_root = tmp_path / "workspace-root"
    projects = {
        "web-ssh-gateway": {
            "root": "web-ssh-gateway",
            "type": "service",
            "description": "SSH gateway service",
            "tags": ["ssh", "gateway"],
        },
        "quart-platform": {
            "root": "quart-platform",
            "type": "platform",
            "description": "Quart platform root",
            "tags": ["quart"],
        },
        "kojo-bot-service": {
            "root": "quart-platform/kojo-bot-service",
            "type": "bot",
            "description": "Kojo bot service",
            "tags": ["bot"],
            "parent": "quart-platform",
        },
        "pricetuner-scraper": {
            "root": "pricetuner-scraper",
            "type": "worker",
            "description": "Pricetuner scraper",
            "tags": ["scraper"],
        },
        "tg-audio-bot": {
            "root": "tg-audio-bot",
            "type": "bot",
            "description": "Telegram audio bot",
            "tags": ["telegram"],
        },
        "nod-gateway": {
            "root": "nod-gateway",
            "type": "gateway",
            "description": "NOD gateway",
            "tags": ["nod"],
        },
    }
    for cfg in projects.values():
        project_root = workspace_root / cfg["root"]
        project_root.mkdir(parents=True, exist_ok=True)
        (project_root / "README.md").write_text("# fixture\n", encoding="utf-8")
    (workspace_root / "web-ssh-gateway" / "app").mkdir()
    (workspace_root / "web-ssh-gateway" / "app" / "main.py").write_text(
        "print('fixture')\n",
        encoding="utf-8",
    )

    registry_path = tmp_path / "projects.yaml"
    lines = ["version: 1", f"registry_root: {workspace_root}", "", "projects:"]
    for project_id, cfg in projects.items():
        lines.extend(
            [
                f"  {project_id}:",
                f"    root: {cfg['root']}",
                f"    type: {cfg['type']}",
                f"    description: {cfg['description']}",
                "    tags:",
                *[f"      - {tag}" for tag in cfg["tags"]],
            ]
        )
        if "parent" in cfg:
            lines.append(f"    parent: {cfg['parent']}")
    registry_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    registry = WorkspaceRegistry.load(registry_path)
    monkeypatch.setattr("app.workspace.tools.get_registry", lambda: registry)


class TestWorkspaceListProjects:
    def test_returns_all_projects(self):
        projects = workspace_list_projects()
        ids = [p["project_id"] for p in projects]
        assert "agent-ssh-gateway" in ids
        assert "quart-platform" in ids
        assert "kojo-bot-service" in ids
        assert "pricetuner-scraper" in ids
        assert "tg-audio-bot" in ids
        assert "nod-gateway" in ids
        assert len(projects) >= 6

    def test_project_has_expected_fields(self):
        projects = workspace_list_projects()
        for p in projects:
            assert "project_id" in p
            assert "type" in p
            assert "description" in p
            assert "tags" in p


class TestProjectInfo:
    def test_info_returns_metadata(self):
        info = project_info("agent-ssh-gateway")
        assert info["project_id"] == "agent-ssh-gateway"
        assert "root" in info
        assert "type" in info

    def test_info_includes_parent(self):
        info = project_info("kojo-bot-service")
        assert info.get("parent") == "quart-platform"

    def test_info_no_parent_for_root(self):
        info = project_info("quart-platform")
        assert "parent" not in info

    def test_info_unknown_project(self):
        with pytest.raises((WorkspacePolicyError, KeyError)):
            project_info("nonexistent-project")


class TestProjectTree:
    def test_tree_root(self):
        tree = project_tree("agent-ssh-gateway")
        assert tree["type"] == "directory"
        assert "children" in tree

    def test_tree_depth_limit(self):
        tree = project_tree("agent-ssh-gateway", depth=1)
        for child in tree.get("children", []):
            if child["type"] == "directory":
                assert "children" not in child or child.get("children") is None

    def test_tree_all_projects(self):
        """Smoke: project_tree works for every fixture project."""
        projects = workspace_list_projects()
        for p in projects:
            tree = project_tree(p["project_id"])
            assert tree["type"] == "directory"
