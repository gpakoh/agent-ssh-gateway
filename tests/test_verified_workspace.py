from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from examples.mcp_server.verified_workspace import (
    VerifiedWorkspaceError,
    verify_registered_delivery_workspace,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "allowed.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "allowed.txt")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "allowed.txt").write_text("candidate\n", encoding="utf-8")
    _git(root, "add", "allowed.txt")
    _git(root, "commit", "-q", "-m", "candidate")
    head = _git(root, "rev-parse", "HEAD")
    return root, base, head


def test_verified_workspace_accepts_exact_clean_scoped_commit(tmp_path: Path) -> None:
    root, base, head = _repo(tmp_path)
    proof = verify_registered_delivery_workspace(
        project_root=root,
        expected_base_sha=base,
        expected_head_sha=head,
        allowed_files=["allowed.txt"],
    )
    assert proof["base_sha"] == base
    assert proof["head_sha"] == head
    assert proof["clean"] is True
    assert proof["changed_files"] == ["allowed.txt"]
    assert proof["scope_verified"] is True


def test_verified_workspace_rejects_dirty_tree(tmp_path: Path) -> None:
    root, base, head = _repo(tmp_path)
    (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(VerifiedWorkspaceError) as exc_info:
        verify_registered_delivery_workspace(
            project_root=root,
            expected_base_sha=base,
            expected_head_sha=head,
            allowed_files=["allowed.txt"],
        )
    assert exc_info.value.code == "WORKSPACE_DIRTY"


def test_verified_workspace_rejects_head_mismatch(tmp_path: Path) -> None:
    root, base, _head = _repo(tmp_path)
    with pytest.raises(VerifiedWorkspaceError) as exc_info:
        verify_registered_delivery_workspace(
            project_root=root,
            expected_base_sha=base,
            expected_head_sha=base,
            allowed_files=["allowed.txt"],
        )
    assert exc_info.value.code == "HEAD_MISMATCH"


def test_verified_workspace_rejects_out_of_scope_commit(tmp_path: Path) -> None:
    root, base, _head = _repo(tmp_path)
    (root / "other.txt").write_text("other\n", encoding="utf-8")
    _git(root, "add", "other.txt")
    _git(root, "commit", "-q", "-m", "other")
    head = _git(root, "rev-parse", "HEAD")
    with pytest.raises(VerifiedWorkspaceError) as exc_info:
        verify_registered_delivery_workspace(
            project_root=root,
            expected_base_sha=base,
            expected_head_sha=head,
            allowed_files=["allowed.txt"],
        )
    assert exc_info.value.code == "CANDIDATE_SCOPE_VIOLATION"
    assert exc_info.value.details == {"path": "other.txt"}


def test_verified_workspace_rejects_unrelated_base(tmp_path: Path) -> None:
    root, _base, head = _repo(tmp_path)
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    _git(orphan, "init", "-q")
    _git(orphan, "config", "user.name", "Test")
    _git(orphan, "config", "user.email", "test@example.invalid")
    (orphan / "x.txt").write_text("x\n", encoding="utf-8")
    _git(orphan, "add", "x.txt")
    _git(orphan, "commit", "-q", "-m", "orphan")
    orphan_sha = _git(orphan, "rev-parse", "HEAD")
    _git(root, "fetch", str(orphan), orphan_sha)
    with pytest.raises(VerifiedWorkspaceError) as exc_info:
        verify_registered_delivery_workspace(
            project_root=root,
            expected_base_sha=orphan_sha,
            expected_head_sha=head,
            allowed_files=["allowed.txt"],
        )
    assert exc_info.value.code == "BASE_MISMATCH"
