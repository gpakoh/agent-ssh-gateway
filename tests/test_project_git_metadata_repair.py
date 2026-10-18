"""Regression coverage for guarded canonical Git metadata recovery."""

from __future__ import annotations

import errno
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




def test_cross_device_sibling_quarantine_uses_atomic_in_worktree_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, journal, old_head, new_head = _fixture(tmp_path)
    real_rename = os.rename
    cross_device_attempts = 0

    def cross_device_sibling(src, dst, *args, **kwargs):
        nonlocal cross_device_attempts
        if Path(src) == repo / ".git" and Path(dst).parent == repo.parent:
            cross_device_attempts += 1
            raise OSError(errno.EXDEV, "injected cross-device quarantine")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(repair_module.os, "rename", cross_device_sibling)
    first = _repair(repo, journal, old_head, new_head)
    assert first.quarantine_location == "gitdir"
    assert first.as_dict()["quarantine_location"] == "gitdir"
    assert cross_device_attempts == 1
    assert (repo / ".git" / first.quarantine_id).is_dir()
    assert not (repo / first.quarantine_id).exists()
    assert not (repo.parent / first.quarantine_id).exists()
    assert _git(repo, "rev-parse", "HEAD") == new_head
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""

    again = _repair(repo, journal, old_head, new_head)
    assert again.already_repaired is True
    assert again.quarantine_location == "gitdir"
    assert cross_device_attempts == 1


def test_existing_nested_metadata_quarantine_blocks_without_mutation(
    tmp_path: Path,
) -> None:
    repo, journal, old_head, new_head = _fixture(tmp_path)
    quarantine = repo / ".git.repair-current" / repair_module._quarantine_name(
        repo, old_head, new_head
    )
    quarantine.mkdir()
    (quarantine / "do-not-delete.txt").write_text("unrelated preserved data", encoding="utf-8")

    with pytest.raises(ProjectGitMetadataRepairError) as raised:
        _repair(repo, journal, old_head, new_head)

    assert raised.value.code == "WORKSPACE_CONTENDED"
    assert raised.value.details["mutation_occurred"] is False
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert (repo / ".git.repair-current").is_dir()
    assert (quarantine / "do-not-delete.txt").read_text(encoding="utf-8") == "unrelated preserved data"


def test_non_exdev_quarantine_error_rolls_back_and_exposes_safe_errno(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, journal, old_head, new_head = _fixture(tmp_path)
    real_rename = os.rename

    def permission_denied(src, dst, *args, **kwargs):
        if Path(src) == repo / ".git" and Path(dst).parent == repo.parent:
            raise OSError(errno.EPERM, "injected metadata permissions")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(repair_module.os, "rename", permission_denied)
    with pytest.raises(ProjectGitMetadataRepairError) as raised:
        _repair(repo, journal, old_head, new_head)
    assert raised.value.code == "TOOL_EXECUTION_FAILED"
    assert raised.value.details == {
        "mutation_occurred": False,
        "failed_stage": "quarantine_sibling",
        "os_errno": "EPERM",
    }
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert (repo / ".git.repair-current").is_dir()
    assert not list(repo.parent.glob(".demo.git-quarantine-*"))
    assert not list(repo.glob(".demo.git-quarantine-*"))


def test_cross_device_fallback_failure_rolls_back_original_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, journal, old_head, new_head = _fixture(tmp_path)
    real_rename = os.rename

    def fallback_denied(src, dst, *args, **kwargs):
        if Path(src) == repo / ".git":
            if Path(dst).parent == repo.parent:
                raise OSError(errno.EXDEV, "injected cross-device boundary")
            if Path(dst).parent == repo / ".git.repair-current":
                raise OSError(errno.EBUSY, "injected busy mount")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(repair_module.os, "rename", fallback_denied)
    with pytest.raises(ProjectGitMetadataRepairError) as raised:
        _repair(repo, journal, old_head, new_head)
    assert raised.value.code == "TOOL_EXECUTION_FAILED"
    assert raised.value.details["failed_stage"] == "quarantine_gitdir"
    assert raised.value.details["os_errno"] == "EBUSY"
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert (repo / ".git.repair-current").is_dir()
    assert not list(repo.parent.glob(".demo.git-quarantine-*"))
    assert not list(repo.glob(".demo.git-quarantine-*"))

@pytest.mark.parametrize("fail_stage", ["promotion", "fsync"])
def test_cross_device_nested_quarantine_rolls_back_without_losing_old_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail_stage: str
) -> None:
    repo, journal, old_head, new_head = _fixture(tmp_path)
    real_rename = os.rename
    real_fsync = repair_module._fsync_directory
    promoted = False

    def injected_rename(src, dst, *args, **kwargs):
        nonlocal promoted
        if Path(src) == repo / ".git" and Path(dst).parent == repo.parent:
            raise OSError(errno.EXDEV, "injected cross-device sibling rename")
        if Path(src) == repo / ".git.repair-current" and Path(dst) == repo / ".git":
            if fail_stage == "promotion":
                raise OSError(errno.EBUSY, "injected promotion failure")
            promoted = True
        return real_rename(src, dst, *args, **kwargs)

    fsync_injected = False

    def injected_fsync(path):
        nonlocal fsync_injected
        if promoted and fail_stage == "fsync" and not fsync_injected:
            # Fail only once; the rollback must be able to sync its result.
            fsync_injected = True
            raise OSError(errno.EIO, "injected post-promotion fsync failure")
        return real_fsync(path)

    monkeypatch.setattr(repair_module.os, "rename", injected_rename)
    monkeypatch.setattr(repair_module, "_fsync_directory", injected_fsync)
    with pytest.raises(ProjectGitMetadataRepairError) as raised:
        _repair(repo, journal, old_head, new_head)

    assert raised.value.code == "TOOL_EXECUTION_FAILED"
    assert raised.value.details["mutation_occurred"] is False
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert (repo / ".git.repair-current").is_dir()
    assert not list(repo.glob(".demo.git-quarantine-*"))
    assert not list((repo / ".git").glob(".demo.git-quarantine-*"))
    assert not list((repo / ".git.repair-current").glob(".demo.git-quarantine-*"))
    assert not list(repo.parent.glob(".demo.git-quarantine-*"))


def test_tool_is_write_mode_only_and_requires_project_plus_admin() -> None:
    name = "supervisor_repair_project_git_metadata"
    assert not should_register_tool(name, "mcp_client")
    assert should_register_tool(name, "mcp_client_write")
    assert get_required_scopes(name) == ["mcp:project", "mcp:admin"]
    assert not has_required_scope(["mcp:admin"], name)
    assert not has_required_scope(["mcp:project"], name)
    assert has_required_scope(["mcp:project", "mcp:admin"], name)