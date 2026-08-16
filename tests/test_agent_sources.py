from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server.agent_paths import managed_source_bundle_path
from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    _run_git,
    ensure_managed_source_bundle,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "payload.txt").write_text("committed\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


class _Registry:
    def __init__(self, root: Path):
        self.root = root

    def project_info(self, project: str) -> dict[str, str]:
        return {"project_id": project, "root": str(self.root)}


def test_legacy_mode_without_source_root_is_noop(monkeypatch):
    monkeypatch.delenv("MCP_AGENT_SOURCE_ROOT", raising=False)
    assert ensure_managed_source_bundle("any-project", None) is None


def test_managed_mode_requires_exact_base_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "sources"))
    with pytest.raises(ValueError, match="exact base_ref"):
        ensure_managed_source_bundle("any-project", None)
    with pytest.raises(ValueError, match="Invalid base_ref"):
        ensure_managed_source_bundle("any-project", "main")


def test_publishes_exact_commit_for_arbitrary_project_ignoring_dirty_tree(
    tmp_path, monkeypatch
):
    repo, sha = _repo(tmp_path)
    (repo / "payload.txt").write_text("DIRTY WORKTREE\n", encoding="utf-8")
    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo)
    )

    published = ensure_managed_source_bundle("nod", sha)
    expected = managed_source_bundle_path("nod", sha)
    assert published == expected
    assert published is not None
    bundle = Path(published)
    assert bundle.is_file()
    heads = _git(repo, "bundle", "list-heads", str(bundle))
    assert heads.split()[0] == sha

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-b", "source", str(bundle), str(clone)],
        text=True,
        capture_output=True,
        check=True,
    )
    assert (clone / "payload.txt").read_text(encoding="utf-8") == "committed\n"


def test_missing_commit_fails_without_publishing(tmp_path, monkeypatch):
    repo, _sha = _repo(tmp_path)
    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo)
    )
    missing = "f" * 40
    with pytest.raises(ManagedSourceBundleError, match="cat-file"):
        ensure_managed_source_bundle("nod", missing)
    expected = managed_source_bundle_path("nod", missing)
    assert expected is not None
    assert not Path(expected).exists()


def test_atomic_replace_failure_propagates(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path)
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "sources"))
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo)
    )

    def fail_replace(src, dst):
        raise OSError("simulated atomic publication failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic publication failure"):
        ensure_managed_source_bundle("nod", sha)

    expected = managed_source_bundle_path("nod", sha)
    assert expected is not None
    assert not Path(expected).exists()


def test_git_timeout_fails_closed(monkeypatch):
    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=120)

    monkeypatch.setattr(subprocess, "run", timeout_run)

    with pytest.raises(ManagedSourceBundleError, match="timed out during git fetch"):
        _run_git(["fetch", "source"])
