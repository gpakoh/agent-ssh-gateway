"""Tests for app.services.project_patch — path-traversal safety.

No existing test coverage existed for this module at all before this file.
"""

from __future__ import annotations

import pytest

from app.services.project_patch import ProjectNotFoundError, apply_project_patch
from app.workspace.models import ProjectInfo
from app.workspace.registry import WorkspaceRegistry


def _make_registry(tmp_path, project_root):
    return WorkspaceRegistry(
        {
            "myproj": ProjectInfo(
                project_id="myproj",
                root=project_root,
                type="app",
                description="test fixture",
                tags=["test"],
            )
        },
        [tmp_path],
        granted_scopes={"project:read", "project:write", "project:patch"},
    )


def _traversal_patch(rel_path: str, old: str, new: str) -> str:
    return (
        f"--- a/{rel_path}\n"
        f"+++ b/{rel_path}\n"
        f"@@ -1 +1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


@pytest.mark.asyncio
async def test_traversal_path_is_rejected_not_written(tmp_path, monkeypatch):
    """Regression: f["path"] comes straight from the unified diff's
    source-file header with zero traversal filtering anywhere in
    patch_apply.py. project_root / "../outside.txt" resolved outside the
    project — and this endpoint only requires the "project:patch" scope,
    not the master key.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()

    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("original content\n", encoding="utf-8")

    registry = _make_registry(tmp_path, project_root)
    monkeypatch.setattr("app.workspace.registry.get_registry", lambda *a, **k: registry)

    patch = _traversal_patch("../outside.txt", "original content", "pwned content")

    with pytest.raises(Exception) as excinfo:
        await apply_project_patch(
            "myproj",
            patch=patch,
            strip=0,
            dry_run=False,
        )
    assert "escapes project root" in str(excinfo.value)

    assert outside_file.read_text(encoding="utf-8") == "original content\n", (
        "traversal patch must never modify a file outside the project root"
    )


@pytest.mark.asyncio
async def test_traversal_path_rejected_in_dry_run_too(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("original content\n", encoding="utf-8")

    registry = _make_registry(tmp_path, project_root)
    monkeypatch.setattr("app.workspace.registry.get_registry", lambda *a, **k: registry)

    patch = _traversal_patch("../outside.txt", "original content", "pwned content")

    with pytest.raises(Exception) as excinfo:
        await apply_project_patch(
            "myproj",
            patch=patch,
            strip=0,
            dry_run=True,
        )
    assert "escapes project root" in str(excinfo.value)


@pytest.mark.asyncio
async def test_normal_patch_within_project_still_applies(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "inside.txt"
    target.write_text("original content\n", encoding="utf-8")

    registry = _make_registry(tmp_path, project_root)
    monkeypatch.setattr("app.workspace.registry.get_registry", lambda *a, **k: registry)

    patch = _traversal_patch("inside.txt", "original content", "updated content")

    result = await apply_project_patch(
        "myproj",
        patch=patch,
        strip=0,
        dry_run=False,
    )

    assert result.success is True
    assert target.read_text(encoding="utf-8") == "updated content\n"


def _new_file_patch(rel_path: str, lines: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"--- a/{rel_path}\n"
        f"+++ b/{rel_path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}"
    )


def _delete_patch(rel_path: str, lines: list[str]) -> str:
    body = "".join(f"-{line}\n" for line in lines)
    return (
        f"--- a/{rel_path}\n"
        f"+++ /dev/null\n"
        f"@@ -1,{len(lines)} +0,0 @@\n"
        f"{body}"
    )


@pytest.mark.asyncio
async def test_rollback_removes_newly_created_file_not_leaves_it_empty(tmp_path, monkeypatch):
    """Regression: when a patch creates a brand-new file (didn't exist
    before) and a LATER file in the same batch fails to write, rollback
    is supposed to restore every already-completed file to its pre-patch
    state. For a new file, "backup" was just an empty placeholder written
    so the same rename-based rollback code path could be reused -- but
    rollback unconditionally renamed that empty placeholder back over the
    target, leaving an empty file behind where, before the patch, no file
    existed at all. A partially-failed multi-file patch must not leave
    new junk files around.
    """
    import os as os_module
    from pathlib import Path as PathCls

    project_root = tmp_path / "project"
    project_root.mkdir()

    registry = _make_registry(tmp_path, project_root)
    monkeypatch.setattr("app.workspace.registry.get_registry", lambda *a, **k: registry)

    patch = "\n".join(
        [
            _new_file_patch("first.txt", ["hello"]).rstrip("\n"),
            _new_file_patch("second.txt", ["world"]).rstrip("\n"),
        ]
    ) + "\n"

    real_rename = os_module.rename

    def _flaky_rename(src, dst):
        if str(src).endswith(".tmp") and PathCls(dst).name == "second.txt":
            raise OSError("simulated write failure for second.txt")
        return real_rename(src, dst)

    monkeypatch.setattr("app.services.project_patch.os.rename", _flaky_rename)

    result = await apply_project_patch(
        "myproj",
        patch=patch,
        strip=0,
        dry_run=False,
    )

    assert result.success is False
    first_path = project_root / "first.txt"
    assert not first_path.exists(), (
        f"rollback must remove a newly-created file, not leave it as an empty file "
        f"(exists={first_path.exists()})"
    )
    assert not (project_root / "second.txt").exists()


@pytest.mark.asyncio
async def test_unknown_project_raises(monkeypatch):
    registry = WorkspaceRegistry({}, [], granted_scopes={"project:patch"})
    monkeypatch.setattr("app.workspace.registry.get_registry", lambda *a, **k: registry)

    with pytest.raises(ProjectNotFoundError):
        await apply_project_patch(
            "nonexistent",
            patch=_traversal_patch("x.txt", "a", "b"),
            strip=0,
            dry_run=True,
        )


@pytest.mark.asyncio
async def test_dev_null_deletes_file(tmp_path, monkeypatch):
    """Regression: a unified diff whose target is `/dev/null` (git's
    convention for "file deleted") must DELETE the file. Before the fix the
    REST surface applied the patch in memory to an empty string and wrote a
    0-byte file instead of deleting anything.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "victim.txt"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    registry = _make_registry(tmp_path, project_root)
    monkeypatch.setattr("app.workspace.registry.get_registry", lambda *a, **k: registry)

    patch = _delete_patch("victim.txt", ["line1", "line2", "line3"])

    result = await apply_project_patch(
        "myproj",
        patch=patch,
        strip=0,
        dry_run=False,
    )

    assert result.success is True
    assert not target.exists(), (
        f"deletion patch must remove the file, not leave a 0-byte one "
        f"(exists={target.exists()})"
    )


@pytest.mark.asyncio
async def test_dev_null_delete_rollback_restores_deleted_file(tmp_path, monkeypatch):
    """Regression: when a patch deletes file A and a LATER file B in the
    same batch fails to write, rollback must restore A from its backup.
    Before the fix, deletion was not supported at all, so this scenario
    (delete + failing sibling) never produced a restored file.
    """
    import os as os_module
    from pathlib import Path as PathCls

    project_root = tmp_path / "project"
    project_root.mkdir()
    victim = project_root / "victim.txt"
    victim.write_text("line1\nline2\nline3\n", encoding="utf-8")

    registry = _make_registry(tmp_path, project_root)
    monkeypatch.setattr("app.workspace.registry.get_registry", lambda *a, **k: registry)

    patch = "\n".join(
        [
            _delete_patch("victim.txt", ["line1", "line2", "line3"]).rstrip("\n"),
            _new_file_patch("second.txt", ["world"]).rstrip("\n"),
        ]
    ) + "\n"

    real_rename = os_module.rename

    def _flaky_rename(src, dst):
        if str(src).endswith(".tmp") and PathCls(dst).name == "second.txt":
            raise OSError("simulated write failure for second.txt")
        return real_rename(src, dst)

    monkeypatch.setattr("app.services.project_patch.os.rename", _flaky_rename)

    result = await apply_project_patch(
        "myproj",
        patch=patch,
        strip=0,
        dry_run=False,
    )

    assert result.success is False
    assert victim.read_text(encoding="utf-8") == "line1\nline2\nline3\n", (
        "rollback must restore a deleted file from its backup"
    )
    assert not (project_root / "second.txt").exists()
