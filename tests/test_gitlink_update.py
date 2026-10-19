"""Regression tests for exact-SHA Git submodule staging without an agent runtime."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from mcp_client_tools import git_set_gitlink

OLD = "b596c0dc569fb3e0e8573675c11ff30dc54ec631"
NEW = "70d8adb5f8e91583ecd6bde40929aa4bf67d05cb"


def test_gitlink_mutation_requires_explicit_write_mode() -> None:
    from examples.mcp_server.tool_modes import TOOL_NAMES_BY_MODE
    from examples.mcp_server.tool_scopes import get_required_scopes

    assert "git_set_gitlink" in TOOL_NAMES_BY_MODE["mcp_client_write"]
    assert "git_set_gitlink" not in TOOL_NAMES_BY_MODE["mcp_client"]
    assert "mcp:project" in get_required_scopes("git_set_gitlink")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _index_sha(repo: Path) -> str:
    raw = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    return hashlib.sha256(raw).hexdigest()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "feature/pin")
    _git(repo, "config", "user.email", "gitlink-tests@example.invalid")
    _git(repo, "config", "user.name", "Gitlink Tests")
    (repo / "README.md").write_text("release root\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")
    # A gitlink is a pointer to a child repository, not a parent Git object.
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{OLD},gateway_client")
    _git(repo, "commit", "-q", "-m", "pin child")
    # Git treats a completely absent submodule directory as a deleted
    # gitlink in porcelain status; a present, empty uninitialized directory
    # models the actual managed NOD parent candidate.
    (repo / "gateway_client").mkdir()
    assert _git(repo, "status", "--porcelain=v1") == ""
    return repo


class LocalGateway:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.mutations = 0

    def execute_project_script(self, project: str, script: str, timeout_s: int = 30) -> dict:
        assert project == "demo"
        proc = subprocess.run(
            ["sh"], input=script, cwd=self.repo, capture_output=True,
            text=True, timeout=timeout_s, check=False,
        )
        return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}

    def execute_project_command(self, project: str, command: str) -> dict:
        assert project == "demo"
        self.mutations += 1
        proc = subprocess.run(
            ["sh"], input=command, cwd=self.repo, capture_output=True,
            text=True, timeout=30, check=False,
        )
        return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _stage(repo: Path, **overrides: str) -> tuple[dict, LocalGateway]:
    client = LocalGateway(repo)
    args = {
        "project": "demo",
        "path": "gateway_client",
        "old_sha": OLD,
        "new_sha": NEW,
        "expected_head": _git(repo, "rev-parse", "HEAD"),
        "expected_branch": "feature/pin",
        "expected_index_sha256": _index_sha(repo),
    }
    args.update(overrides)
    return git_set_gitlink(client, **args), client


def test_stages_exactly_one_uninitialized_submodule_gitlink(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    result, client = _stage(repo)
    assert result["exit_code"] == 0, result
    assert client.mutations == 1
    assert "GITLINK_STAGED=1" in result["stdout"]
    assert _git(repo, "ls-tree", "HEAD", "gateway_client") == (
        f"160000 commit {OLD}\tgateway_client"
    )
    assert _git(repo, "ls-files", "--stage", "--", "gateway_client") == (
        f"160000 {NEW} 0\tgateway_client"
    )
    assert _git(repo, "diff", "--cached", "--name-only") == "gateway_client"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert not (repo / ".git" / "index.lock").exists()


@pytest.mark.parametrize("override", [
    {"old_sha": "f" * 40},
    {"expected_head": "e" * 40},
    {"expected_branch": "feature/other"},
    {"expected_index_sha256": "0" * 64},
])
def test_stale_lease_never_modifies_index(tmp_path: Path, override: dict[str, str]) -> None:
    repo = _repo(tmp_path)
    before = _index_sha(repo)
    result, client = _stage(repo, **override)
    assert result["exit_code"] != 0, result
    assert client.mutations == 1
    assert _index_sha(repo) == before
    assert _git(repo, "status", "--porcelain=v1") == ""
    assert not (repo / ".git" / "index.lock").exists()


def test_dirty_worktree_prevents_staging(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("unsaved changes\n", encoding="utf-8")
    before = _index_sha(repo)
    result, _ = _stage(repo)
    assert result["exit_code"] != 0
    assert _index_sha(repo) == before


@pytest.mark.parametrize("bad_path", [
    "../gateway_client", "/gateway_client", "gateway_client;echo", ".git/config",
    "gateway_client\\extra", "gateway_client//extra", "-gateway_client",
])
def test_path_injection_and_traversal_rejected_before_execution(tmp_path: Path, bad_path: str) -> None:
    repo = _repo(tmp_path)
    client = LocalGateway(repo)
    with pytest.raises(ValueError):
        git_set_gitlink(
            client, "demo", bad_path, OLD, NEW, _git(repo, "rev-parse", "HEAD"),
            "feature/pin", _index_sha(repo),
        )
    assert client.mutations == 0


@pytest.mark.parametrize("protected", ["main", "master", "HEAD"])
def test_protected_branches_rejected_before_execution(tmp_path: Path, protected: str) -> None:
    repo = _repo(tmp_path)
    client = LocalGateway(repo)
    with pytest.raises(ValueError):
        git_set_gitlink(
            client, "demo", "gateway_client", OLD, NEW,
            _git(repo, "rev-parse", "HEAD"), protected, _index_sha(repo),
        )
    assert client.mutations == 0