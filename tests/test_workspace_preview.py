"""Tests for workspace preview and verify tools."""

from __future__ import annotations

import pytest

from app.workspace.models import ProjectInfo
from app.workspace.preview import (
    project_file_preview_edit,
    project_file_preview_patch,
    project_file_preview_write,
    project_file_verify,
)
from app.workspace.registry import WorkspaceRegistry


@pytest.fixture
def preview_workspace(tmp_path):
    """Create a workspace for preview tests."""
    project = tmp_path / "preview-project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("def main():\n    pass\n")
    (project / "README.md").write_text("# Preview Project\n")
    (project / ".env").write_text("SECRET=abc123\n")

    allowed_roots = [tmp_path]
    projects = {
        "preview-project": ProjectInfo(
            project_id="preview-project",
            root=project,
            type="python",
            description="Test project",
            tags=["test"],
        ),
    }
    registry = WorkspaceRegistry(
        projects=projects,
        allowed_roots=allowed_roots,
        granted_scopes={"project:read"},
    )

    return {"tmp_path": tmp_path, "project": project, "registry": registry}


# ── Preview Write tests ──────────────────────────────────────────


class TestPreviewWrite:
    def test_new_file_preview(self, preview_workspace):
        result = project_file_preview_write(
            "preview-project",
            "new_file.txt",
            "hello world",
            registry=preview_workspace["registry"],
        )
        assert result["file_exists_before"] is False
        assert result["before_hash"] is None
        assert result["after_hash"] is not None
        assert result["changed"] is True
        assert result["size_before"] == 0
        assert result["size_after"] == 11
        # Verify no file was created
        assert not (preview_workspace["project"] / "new_file.txt").exists()

    def test_overwrite_preview(self, preview_workspace):
        result = project_file_preview_write(
            "preview-project",
            "README.md",
            "# New README\n",
            registry=preview_workspace["registry"],
        )
        assert result["file_exists_before"] is True
        assert result["before_hash"] is not None
        assert result["changed"] is True
        # Verify file unchanged
        assert "# Preview Project" in (preview_workspace["project"] / "README.md").read_text()

    def test_content_too_large(self, preview_workspace):
        from app.workspace.policy import WorkspacePolicyError
        with pytest.raises(WorkspacePolicyError, match="exceeds maximum"):
            project_file_preview_write(
                "preview-project",
                "big.txt",
                "x" * 2_000_001,
                max_bytes=2_000_000,
                registry=preview_workspace["registry"],
            )

    def test_binary_rejected(self, preview_workspace):
        from app.workspace.policy import WorkspacePolicyError
        with pytest.raises(WorkspacePolicyError, match="Binary"):
            project_file_preview_write(
                "preview-project",
                "bin.txt",
                "hello\x00world",
                registry=preview_workspace["registry"],
            )

    def test_hidden_path_rejected(self, preview_workspace):
        from app.workspace.policy import WorkspacePolicyError
        with pytest.raises(WorkspacePolicyError):
            project_file_preview_write(
                "preview-project",
                ".env",
                "SECRET=xyz",
                registry=preview_workspace["registry"],
            )

    def test_directory_target_rejected(self, preview_workspace):
        from app.workspace.policy import WorkspacePolicyError
        with pytest.raises(WorkspacePolicyError, match="directory"):
            project_file_preview_write(
                "preview-project",
                "src",
                "content",
                registry=preview_workspace["registry"],
            )

    def test_missing_parent_rejected(self, preview_workspace):
        from app.workspace.policy import WorkspacePolicyError
        with pytest.raises(WorkspacePolicyError, match="Parent directory"):
            project_file_preview_write(
                "preview-project",
                "nonexistent/dir/file.txt",
                "content",
                registry=preview_workspace["registry"],
            )


# ── Preview Edit tests ───────────────────────────────────────────


class TestPreviewEdit:
    def test_edit_preview(self, preview_workspace):
        result = project_file_preview_edit(
            "preview-project",
            "src/main.py",
            "pass",
            "return 0",
            registry=preview_workspace["registry"],
        )
        assert result["file_exists_before"] is True
        assert result["changed"] is True
        assert result["replaced"] is True
        assert result["before_hash"] is not None
        assert result["after_hash"] is not None
        assert "return 0" in result["diff"]
        # Verify file unchanged
        assert "pass" in (preview_workspace["project"] / "src" / "main.py").read_text()

    def test_no_change_preview(self, preview_workspace):
        result = project_file_preview_edit(
            "preview-project",
            "src/main.py",
            "pass",
            "pass",
            registry=preview_workspace["registry"],
        )
        assert result["changed"] is False
        assert result["replaced"] is False

    def test_empty_old_string(self, preview_workspace):
        from app.workspace.policy import WorkspacePolicyError
        with pytest.raises(WorkspacePolicyError, match="must not be empty"):
            project_file_preview_edit(
                "preview-project",
                "src/main.py",
                "",
                "new",
                registry=preview_workspace["registry"],
            )

    def test_old_string_not_found(self, preview_workspace):
        from app.workspace.policy import WorkspacePolicyError
        with pytest.raises(WorkspacePolicyError, match="not found"):
            project_file_preview_edit(
                "preview-project",
                "src/main.py",
                "nonexistent",
                "new",
                registry=preview_workspace["registry"],
            )


# ── Preview Patch tests ──────────────────────────────────────────


class TestPreviewPatch:
    def test_patch_preview(self, preview_workspace):
        patch = """\
--- a/src/main.py
+++ b/src/main.py
@@ -1,2 +1,2 @@
-def main():
-    pass
+def entry():
+    return 0
"""
        result = project_file_preview_patch(
            "preview-project",
            "src/main.py",
            patch,
            registry=preview_workspace["registry"],
        )
        assert result["file_exists_before"] is True
        assert result["changed"] is True
        assert result["applied"] is True
        assert result["before_hash"] is not None
        assert result["after_hash"] is not None
        # Verify file unchanged
        assert "def main():" in (preview_workspace["project"] / "src" / "main.py").read_text()

    def test_patch_no_change(self, preview_workspace):
        from app.workspace.edit import PatchError
        patch = """\
--- a/src/main.py
+++ b/src/main.py
@@ -1,2 +1,2 @@
-def nonexistent():
-    pass
+def also_nonexistent():
+    return 0
"""
        with pytest.raises(PatchError, match="mismatch"):
            project_file_preview_patch(
                "preview-project",
                "src/main.py",
                patch,
                registry=preview_workspace["registry"],
            )


# ── Verify tests ─────────────────────────────────────────────────


class TestVerify:
    def test_verify_matching_hash(self, preview_workspace):
        path = preview_workspace["project"] / "README.md"
        import hashlib
        raw = path.read_bytes()
        expected_hash = "sha256:" + hashlib.sha256(raw).hexdigest()

        result = project_file_verify(
            "preview-project",
            "README.md",
            expected_hash,
            registry=preview_workspace["registry"],
        )
        assert result["matches"] is True
        assert result["current_hash"] == expected_hash
        assert result["file_exists"] is True

    def test_verify_mismatched_hash(self, preview_workspace):
        result = project_file_verify(
            "preview-project",
            "README.md",
            "sha256:wrong",
            registry=preview_workspace["registry"],
        )
        assert result["matches"] is False
        assert result["current_hash"] is not None
        assert result["file_exists"] is True

    def test_verify_nonexistent_file(self, preview_workspace):
        result = project_file_verify(
            "preview-project",
            "missing.txt",
            "sha256:anything",
            registry=preview_workspace["registry"],
        )
        assert result["matches"] is False
        assert result["current_hash"] is None
        assert result["file_exists"] is False

    def test_verify_hidden_path(self, preview_workspace):
        from app.workspace.policy import WorkspacePolicyError
        with pytest.raises(WorkspacePolicyError):
            project_file_verify(
                "preview-project",
                ".env",
                "sha256:anything",
                registry=preview_workspace["registry"],
            )
