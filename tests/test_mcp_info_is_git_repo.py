"""Regression tests for info()'s is_git_repo false positive.

Before this fix, info() reported is_git_repo=True for any project whose
root contained a path named ".git", regardless of whether it was an
actual git repository. Found live: quart-platform/.git on the deploy
host is a scratch metadata directory left by an unrelated tool (only
"info/" and "mimocode-project-id" inside, no HEAD/objects/refs) -- info()
reported is_git_repo=True for it while git itself, correctly, refuses
every git command there with "fatal: not a git repository".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
for _p in (str(MCP_SERVER_DIR), str(EXAMPLES_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mcp_client_tools import _is_real_git_repo, info  # noqa: E402

import app.workspace.registry as registry_module  # noqa: E402
from app.workspace.registry import WorkspaceRegistry, reset_registry  # noqa: E402


class TestIsRealGitRepoUnit:
    def test_real_repo_with_head_file(self, tmp_path):
        project = tmp_path / "real-repo"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        assert _is_real_git_repo(project) is True

    def test_fake_git_named_directory_without_head(self, tmp_path):
        """Exact shape found live: a ".git" directory that isn't actually
        a git repo (no HEAD/objects/refs -- just unrelated tool scratch
        state)."""
        project = tmp_path / "quart-platform"
        project.mkdir()
        fake_git = project / ".git"
        fake_git.mkdir()
        (fake_git / "info").mkdir()
        (fake_git / "mimocode-project-id").write_text("abc123\n")
        assert _is_real_git_repo(project) is False

    def test_worktree_or_submodule_git_file(self, tmp_path):
        """A worktree/submodule's ".git" is a FILE containing "gitdir: ..." --
        a real, valid marker even though it's not a directory."""
        project = tmp_path / "submodule-repo"
        project.mkdir()
        (project / ".git").write_text("gitdir: ../.git/modules/submodule-repo\n")
        assert _is_real_git_repo(project) is True

    def test_no_git_path_at_all(self, tmp_path):
        project = tmp_path / "plain-dir"
        project.mkdir()
        assert _is_real_git_repo(project) is False


@pytest.fixture
def registry_with_fake_git_project(tmp_path):
    project_root = tmp_path / "quart-platform"
    project_root.mkdir()
    fake_git = project_root / ".git"
    fake_git.mkdir()
    (fake_git / "info").mkdir()
    (fake_git / "mimocode-project-id").write_text("abc123\n")

    real_git_root = tmp_path / "real-project"
    real_git_root.mkdir()
    (real_git_root / ".git").mkdir()
    (real_git_root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text(
        f"""
registry_root: {tmp_path}
projects:
  quart-platform:
    root: quart-platform
    type: platform
    description: umbrella with a stray non-git ".git" directory
    tags: []
  real-project:
    root: real-project
    type: python
    description: actual git repo
    tags: []
"""
    )
    reset_registry()
    registry = WorkspaceRegistry.load(yaml_path)
    registry_module._registry = registry
    yield tmp_path
    reset_registry()


class TestInfoIsGitRepoEndToEnd:
    def test_fake_git_directory_reports_not_a_git_repo(
        self, registry_with_fake_git_project
    ):
        result = info(None, "quart-platform")
        assert result["exists"] is True
        assert result["is_dir"] is True
        assert result["is_git_repo"] is False

    def test_real_git_repo_still_reports_true(self, registry_with_fake_git_project):
        result = info(None, "real-project")
        assert result["is_git_repo"] is True
