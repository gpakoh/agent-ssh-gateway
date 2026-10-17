"""Regression coverage for guarded canonical Git metadata recovery."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import project_git_metadata_repair as repair_module
import pytest
from project_git_metadata_repair import (
    ProjectGitMetadataRepairError,
    repair_project_git_metadata,
)
from tool_modes import should_register_tool
from tool_scopes import get_required_scopes, has_required_scope


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    repo = tmp_path / "demo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "old")
    old_head = _git(repo, "rev-parse", "HEAD")

    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "new")
    new_head = _git(repo, "rev-parse", "HEAD")

    replacement = repo / ".git.repair-current"
    shutil.copytree(repo / ".git", replacement)

    # Make canonical metadata stale without touching the new tracked worktree.
    _git(repo, "update-ref", "refs/heads/main", old_head)
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert subprocess.run(
        [
            "git",
            f"--git-dir={replacement}",
            f"--work-tree={repo}",
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout == ""
    return repo, tmp_path / "journal", old_head, new_head


def _repair(repo: Path, journal: Path, old_head: str, new_head: str):
    return repair_project_git_metadata(
        project_root=repo,
        journal_root=journal,
        replacement_dir=".git.repair-current",
        expected_branch="main",
        expected_current_head=old_head,
        expected_replacement_head=new_head,
    )


def test_repairs_exact_stale_git_metadata_and_preserves_quarantine(tmp_path: Path) -> None:
    repo, journal, old_head, new_head = _fixture(tmp_path)

    result = _repair(repo, journal, old_head, new_head)

    assert result.already_repaired is False
    assert result.previous_head == old_head
    assert result.replacement_head == new_head
    assert result.tracked_worktree_clean is True
    assert not (repo / ".git.repair-current").exists()
    assert _git(repo, "rev-parse", "HEAD") == new_head
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=no") == ""

    quarantine = repo.parent / result.quarantine_id
    assert quarantine.is_dir()
    assert subprocess.run(
        ["git", f"--git-dir={quarantine}", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip() == old_head


def test_replay_reconciles_completed_exact_swap_without_second_mutation(tmp_path: Path) -> None:
    repo, journal, old_head, new_head = _fixture(tmp_path)
    first = _repair(repo, journal, old_head, new_head)

    second = _repair(repo, journal, old_head, new_head)

    assert second.already_repaired is True
    assert second.quarantine_id == first.quarantine_id
    assert _git(repo, "rev-parse", "HEAD") == new_head
    assert (repo.parent / first.quarantine_id).is_dir()


def test_wrong_current_head_lease_fails_before_rename(tmp_path: Path) -> None:
    repo, journal, old_head, new_head = _fixture(tmp_path)

    with pytest.raises(ProjectGitMetadataRepairError) as raised:
        repair_project_git_metadata(
            project_root=repo,
            journal_root=journal,
            replacement_dir=".git.repair-current",
            expected_branch="main",
            expected_current_head="0" * 40,
            expected_replacement_head=new_head,
        )

    assert raised.value.code == "WORKSPACE_CONTENDED"
    assert raised.value.details["mutation_occurred"] is False
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert (repo / ".git.repair-current").is_dir()


def test_dirty_tracked_worktree_against_replacement_fails_closed(tmp_path: Path) -> None:
    repo, journal, old_head, new_head = _fixture(tmp_path)
    (repo / "tracked.txt").write_text("unexpected\n", encoding="utf-8")

    with pytest.raises(ProjectGitMetadataRepairError) as raised:
        _repair(repo, journal, old_head, new_head)

    assert raised.value.code == "WORKSPACE_CONTENDED"
    assert raised.value.details["mutation_occurred"] is False
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert (repo / ".git.repair-current").is_dir()


def test_symlink_replacement_is_rejected(tmp_path: Path) -> None:
    repo, journal, old_head, new_head = _fixture(tmp_path)
    replacement = repo / ".git.repair-current"
    shutil.rmtree(replacement)
    replacement.symlink_to(repo / ".git", target_is_directory=True)

    with pytest.raises(ProjectGitMetadataRepairError) as raised:
        _repair(repo, journal, old_head, new_head)

    assert raised.value.code == "POLICY_DENIED"
    assert _git(repo, "rev-parse", "HEAD") == old_head


def test_second_rename_failure_rolls_back_original_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo, journal, old_head, new_head = _fixture(tmp_path)
    real_rename = os.rename
    calls = 0

    def flaky_rename(src, dst, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second rename failure")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(repair_module.os, "rename", flaky_rename)

    with pytest.raises(ProjectGitMetadataRepairError) as raised:
        _repair(repo, journal, old_head, new_head)

    assert raised.value.code == "TOOL_EXECUTION_FAILED"
    assert raised.value.details["mutation_occurred"] is False
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert (repo / ".git.repair-current").is_dir()
    quarantine_names = list(repo.parent.glob(".demo.git-quarantine-*"))
    assert quarantine_names == []


def test_tool_is_write_mode_only_and_requires_project_plus_admin() -> None:
    name = "supervisor_repair_project_git_metadata"
    assert not should_register_tool(name, "mcp_client")
    assert should_register_tool(name, "mcp_client_write")
    assert get_required_scopes(name) == ["mcp:project", "mcp:admin"]
    assert not has_required_scope(["mcp:admin"], name)
    assert not has_required_scope(["mcp:project"], name)
    assert has_required_scope(["mcp:project", "mcp:admin"], name)
