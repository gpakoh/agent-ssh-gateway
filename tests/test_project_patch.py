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
