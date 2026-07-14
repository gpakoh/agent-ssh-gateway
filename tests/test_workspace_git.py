"""Tests for read-only git inspection tools."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.workspace.git import (
    project_git_branch,
    project_git_diff,
    project_git_log,
    project_git_status,
)
from app.workspace.policy import WorkspacePolicyError
from app.workspace.registry import WorkspaceRegistry


def _git_init(path: Path) -> None:
    """Initialize a git repo at path."""
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), capture_output=True, check=True,
    )


def _git_commit(path: Path, filename: str, content: str) -> None:
    """Create or update a file and commit."""
    (path / filename).parent.mkdir(parents=True, exist_ok=True)
    (path / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"update {filename}"],
        cwd=str(path), capture_output=True, check=True,
    )


@pytest.fixture
def git_project(tmp_path):
    """Create a temporary git project."""
    project_dir = tmp_path / "git-project"
    project_dir.mkdir()
    _git_init(project_dir)
    _git_commit(project_dir, "README.md", "# Hello\n")
    _git_commit(project_dir, "src/main.py", "print('hello')\n")

    # Create a non-git project
    non_git = tmp_path / "non-git-project"
    non_git.mkdir()
    (non_git / "file.txt").write_text("not a git repo")

    return {
        "tmp_path": tmp_path,
        "project_dir": project_dir,
        "non_git": non_git,
    }


@pytest.fixture
def git_registry(git_project):
    """Create a WorkspaceRegistry with a git project."""
    from app.workspace.models import ProjectInfo

    projects = {
        "git-project": ProjectInfo(
            project_id="git-project",
            root=git_project["project_dir"],
            type="python",
            description="test",
            tags=[],
        ),
        "non-git-project": ProjectInfo(
            project_id="non-git-project",
            root=git_project["non_git"],
            type="python",
            description="test",
            tags=[],
        ),
    }
    return WorkspaceRegistry(
        projects=projects,
        allowed_roots=[git_project["tmp_path"]],
    )


# ── Git status tests ────────────────────────────────────────────


class TestGitStatus:
    def test_clean_repo(self, git_registry):
        result = project_git_status("git-project", registry=git_registry)
        assert result["is_git_repo"] is True
        assert result["branch"] == "master"
        assert result["staged"] == []
        assert result["unstaged"] == []
        assert result["untracked"] == []

    def test_dirty_repo(self, git_project, git_registry):
        (git_project["project_dir"] / "dirty.txt").write_text("dirty")
        result = project_git_status("git-project", registry=git_registry)
        assert result["is_git_repo"] is True
        assert len(result["untracked"]) > 0

    def test_non_git_repo(self, git_registry):
        result = project_git_status("non-git-project", registry=git_registry)
        assert result["is_git_repo"] is False

    def test_staged_changes(self, git_project, git_registry):
        (git_project["project_dir"] / "staged.txt").write_text("staged")
        subprocess.run(
            ["git", "add", "staged.txt"],
            cwd=str(git_project["project_dir"]),
            capture_output=True, check=True,
        )
        result = project_git_status("git-project", registry=git_registry)
        assert result["is_git_repo"] is True
        assert len(result["staged"]) == 1
        assert result["staged"][0]["path"] == "staged.txt"

    def test_unstaged_changes(self, git_project, git_registry):
        (git_project["project_dir"] / "README.md").write_text("modified")
        result = project_git_status("git-project", registry=git_registry)
        assert result["is_git_repo"] is True
        assert len(result["unstaged"]) > 0


# ── Git branch tests ────────────────────────────────────────────


class TestGitBranch:
    def test_current_branch(self, git_registry):
        result = project_git_branch("git-project", registry=git_registry)
        assert result["is_git_repo"] is True
        assert result["branch"] == "master"

    def test_non_git_repo(self, git_registry):
        result = project_git_branch("non-git-project", registry=git_registry)
        assert result["is_git_repo"] is False

    def test_branch_after_create(self, git_project, git_registry):
        subprocess.run(
            ["git", "checkout", "-b", "feature/test"],
            cwd=str(git_project["project_dir"]),
            capture_output=True, check=True,
        )
        result = project_git_branch("git-project", registry=git_registry)
        assert result["branch"] == "feature/test"


# ── Git log tests ────────────────────────────────────────────────


class TestGitLog:
    def test_default_log(self, git_registry):
        result = project_git_log("git-project", registry=git_registry)
        assert result["is_git_repo"] is True
        assert len(result["commits"]) == 2
        assert result["commits"][0]["sha"]

    def test_limit_capped(self, git_registry):
        result = project_git_log("git-project", limit=1, registry=git_registry)
        assert result["is_git_repo"] is True
        assert len(result["commits"]) == 1

    def test_limit_max_100(self, git_project, git_registry):
        result = project_git_log("git-project", limit=200, registry=git_registry)
        assert result["is_git_repo"] is True
        # Should not error; limit is capped internally

    def test_non_git_repo(self, git_registry):
        result = project_git_log("non-git-project", registry=git_registry)
        assert result["is_git_repo"] is False

    def test_path_filter(self, git_project, git_registry):
        result = project_git_log(
            "git-project", relative_path="README.md", registry=git_registry,
        )
        assert result["is_git_repo"] is True
        # Only commits touching README.md
        for commit in result["commits"]:
            assert commit["sha"]

    def test_traversal_rejected(self, git_registry):
        with pytest.raises(WorkspacePolicyError):
            project_git_log("git-project", relative_path="../escape", registry=git_registry)


# ── Git diff tests ──────────────────────────────────────────────


class TestGitDiff:
    def test_clean_diff(self, git_registry):
        result = project_git_diff("git-project", registry=git_registry)
        assert result["is_git_repo"] is True
        assert result["diff"] == ""

    def test_unstaged_diff(self, git_project, git_registry):
        (git_project["project_dir"] / "README.md").write_text("modified content\n")
        result = project_git_diff("git-project", registry=git_registry)
        assert result["is_git_repo"] is True
        assert "modified content" in result["diff"]

    def test_staged_diff(self, git_project, git_registry):
        (git_project["project_dir"] / "staged.txt").write_text("staged content")
        subprocess.run(
            ["git", "add", "staged.txt"],
            cwd=str(git_project["project_dir"]),
            capture_output=True, check=True,
        )
        result = project_git_diff("git-project", staged=True, registry=git_registry)
        assert result["is_git_repo"] is True
        assert "staged content" in result["diff"]

    def test_non_git_repo(self, git_registry):
        result = project_git_diff("non-git-project", registry=git_registry)
        assert result["is_git_repo"] is False

    def test_path_validated(self, git_project, git_registry):
        (git_project["project_dir"] / "target.py").write_text("changed")
        result = project_git_diff(
            "git-project", relative_path="target.py", registry=git_registry,
        )
        assert result["is_git_repo"] is True
        # diff may be empty if unstaged, but should not error

    def test_traversal_rejected(self, git_registry):
        with pytest.raises(WorkspacePolicyError):
            project_git_diff("git-project", relative_path="../escape", registry=git_registry)


# ── Shared behavior tests ───────────────────────────────────────


class TestSharedBehavior:
    def test_unknown_project_raises(self, git_registry):
        with pytest.raises(WorkspacePolicyError, match="Unknown project"):
            project_git_status("nonexistent", registry=git_registry)

    def test_import_from_tools(self):
        from app.workspace.tools import (
            project_git_branch,
            project_git_diff,
            project_git_log,
            project_git_status,
        )
        assert callable(project_git_status)
        assert callable(project_git_branch)
        assert callable(project_git_log)
        assert callable(project_git_diff)

    def test_import_from_init(self):
        from app.workspace import (
            project_git_branch,
            project_git_diff,
            project_git_log,
            project_git_status,
        )
        assert callable(project_git_status)
        assert callable(project_git_branch)
        assert callable(project_git_log)
        assert callable(project_git_diff)

    def test_shims_still_import(self):
        from app.workspace_registry import (
            project_git_branch,
            project_git_diff,
            project_git_log,
            project_git_status,
        )
        assert callable(project_git_status)
        assert callable(project_git_branch)
        assert callable(project_git_log)
        assert callable(project_git_diff)
