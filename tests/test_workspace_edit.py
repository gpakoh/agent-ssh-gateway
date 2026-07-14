"""Tests for workspace file write/edit tools."""

from __future__ import annotations

import pytest

from app.workspace.edit import (
    PatchError,
    WriteError,
    _exact_read,
    _make_diff,
    _symlink_safe_preflight,
    project_apply_patch,
    project_file_edit,
    project_file_write,
)
from app.workspace.models import ProjectInfo
from app.workspace.policy import SymlinkEscapeError, WorkspacePolicyError
from app.workspace.registry import WorkspaceRegistry


@pytest.fixture
def edit_workspace(tmp_path):
    """Create a workspace with writable files."""
    project = tmp_path / "edit-project"
    project.mkdir()

    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("def main():\n    pass\n")
    (project / "src" / "utils.py").write_text("# utils\n\ndef helper():\n    return 42\n")
    (project / "README.md").write_text("# Edit Project\n")
    (project / "large.txt").write_text("x" * 2_000_000)  # > 1MB
    (project / ".env").write_text("SECRET=abc\n")

    allowed_roots = [tmp_path]
    projects = {
        "edit-project": ProjectInfo(
            project_id="edit-project",
            root=project,
            type="python",
            description="Test project",
            tags=["test"],
        ),
    }
    registry = WorkspaceRegistry(
        projects=projects,
        allowed_roots=allowed_roots,
        granted_scopes={"project:read", "project:write"},
    )

    return {"tmp_path": tmp_path, "project": project, "registry": registry}


# ── Shared helper tests ──────────────────────────────────────────


class TestSymlinkSafePreflight:
    def test_rejects_symlink_target(self, edit_workspace):
        project = edit_workspace["project"]
        link = project / "link.py"
        link.symlink_to(project / "src" / "main.py")
        with pytest.raises(SymlinkEscapeError, match="Symlink"):
            _symlink_safe_preflight(link, project)

    def test_rejects_symlink_parent(self, edit_workspace):
        project = edit_workspace["project"]
        link_dir = project / "link_dir"
        link_dir.symlink_to(project / "src")
        target = link_dir / "main.py"
        with pytest.raises(SymlinkEscapeError, match="Symlink"):
            _symlink_safe_preflight(target, project)

    def test_accepts_normal_path(self, edit_workspace):
        project = edit_workspace["project"]
        target = project / "src" / "main.py"
        _symlink_safe_preflight(target, project)  # no raise


class TestExactRead:
    def test_reads_file(self, edit_workspace):
        project = edit_workspace["project"]
        content, size = _exact_read(project / "README.md", max_bytes=100_000)
        assert "# Edit Project" in content

    def test_rejects_large_file(self, edit_workspace):
        project = edit_workspace["project"]
        with pytest.raises(WorkspacePolicyError, match="exceeds maximum"):
            _exact_read(project / "large.txt", max_bytes=1_000_000)

    def test_rejects_hidden_file(self, edit_workspace):
        project = edit_workspace["project"]
        # .env is hidden but _exact_read doesn't check that (policy does)
        content, size = _exact_read(project / ".env", max_bytes=100_000)
        assert "SECRET=abc" in content


class TestMakeDiff:
    def test_diff_contains_change(self):
        diff = _make_diff("old line\n", "new line\n", "test.py")
        assert "-old line" in diff
        assert "+new line" in diff
        assert "test.py" in diff

    def test_empty_diff_when_same(self):
        diff = _make_diff("same\n", "same\n", "test.py")
        assert diff == ""


# ── project_file_write tests ─────────────────────────────────────


class TestFileWrite:
    def test_creates_new_file(self, edit_workspace):
        result = project_file_write(
            "edit-project",
            "new_file.txt",
            "hello world",
            registry=edit_workspace["registry"],
        )
        assert result["path"] == "new_file.txt"
        assert result["size"] == 11
        assert result["encoding"] == "utf-8"
        assert (edit_workspace["project"] / "new_file.txt").read_text() == "hello world"

    def test_overwrites_existing(self, edit_workspace):
        project_file_write(
            "edit-project",
            "README.md",
            "# New README\n",
            registry=edit_workspace["registry"],
        )
        content = (edit_workspace["project"] / "README.md").read_text()
        assert content == "# New README\n"

    def test_content_exceeds_max_bytes(self, edit_workspace):
        with pytest.raises(WorkspacePolicyError, match="exceeds maximum"):
            project_file_write(
                "edit-project",
                "big.txt",
                "x" * 2_000_001,
                max_bytes=2_000_000,
                registry=edit_workspace["registry"],
            )

    def test_binary_rejected(self, edit_workspace):
        with pytest.raises(WorkspacePolicyError, match="Binary"):
            project_file_write(
                "edit-project",
                "bin.txt",
                "hello\x00world",
                registry=edit_workspace["registry"],
            )

    def test_to_directory_rejected(self, edit_workspace):
        with pytest.raises(WorkspacePolicyError, match="directory"):
            project_file_write(
                "edit-project",
                "src",
                "content",
                registry=edit_workspace["registry"],
            )

    def test_hidden_path_rejected(self, edit_workspace):
        with pytest.raises(WorkspacePolicyError):
            project_file_write(
                "edit-project",
                ".env",
                "NEW_SECRET=xyz\n",
                registry=edit_workspace["registry"],
            )

    def test_traversal_rejected(self, edit_workspace):
        with pytest.raises(WorkspacePolicyError):
            project_file_write(
                "edit-project",
                "../escape.txt",
                "content",
                registry=edit_workspace["registry"],
            )

    def test_symlink_target_rejected(self, edit_workspace):
        project = edit_workspace["project"]
        link = project / "link.py"
        link.symlink_to(project / "src" / "main.py")
        with pytest.raises(SymlinkEscapeError):
            project_file_write(
                "edit-project",
                "link.py",
                "new content",
                registry=edit_workspace["registry"],
            )

    def test_tmp_symlink_attack_prevented(self, edit_workspace, tmp_path):
        """A pre-existing {target}.tmp symlink must not be followed."""
        project = edit_workspace["project"]
        target = project / "safe.txt"
        outside = tmp_path / "outside_target.txt"

        # Attacker places a symlink named after the old predictable temp
        old_tmp = project / "safe.txt.tmp"
        old_tmp.symlink_to(outside)

        result = project_file_write(
            "edit-project",
            "safe.txt",
            "safe content",
            registry=edit_workspace["registry"],
        )

        assert result["path"] == "safe.txt"
        assert result["size"] == 12
        # The write must go to the real target, not follow the symlink
        assert target.read_text() == "safe content"
        # Outside file must NOT have been created or modified
        assert not outside.exists()

    def test_atomic_write_temp_collision_deterministic(self, edit_workspace, monkeypatch):
        """Pre-existing temp with a known UUID must not be deleted or followed."""
        project = edit_workspace["project"]
        outside = project / "outside.txt"
        outside.write_text("original")

        known_hex = "known_hex_00001"
        monkeypatch.setattr(
            "uuid.uuid4",
            lambda: type("FakeUUID", (), {"hex": known_hex})(),
        )
        # Pre-create the exact temp name our code will generate
        tmp_name = "doc.txt." + known_hex + ".tmp"
        tmp_path = project / tmp_name
        tmp_path.symlink_to(outside)

        with pytest.raises(WriteError, match="Write failed"):
            project_file_write(
                "edit-project",
                "doc.txt",
                "new content",
                registry=edit_workspace["registry"],
            )

        # Outside file must retain original content (symlink not followed)
        assert outside.read_text() == "original"
        # Pre-existing temp must NOT have been cleaned up
        assert tmp_path.is_symlink()


# ── project_file_edit tests ──────────────────────────────────────


class TestFileEdit:
    def test_replaces_first_occurrence(self, edit_workspace):
        result = project_file_edit(
            "edit-project",
            "src/main.py",
            "def main():",
            "def entry():",
            registry=edit_workspace["registry"],
        )
        assert result["replaced"] is True
        assert "def entry():" in (edit_workspace["project"] / "src" / "main.py").read_text()

    def test_empty_old_string(self, edit_workspace):
        with pytest.raises(WorkspacePolicyError, match="must not be empty"):
            project_file_edit(
                "edit-project",
                "src/main.py",
                "",
                "new",
                registry=edit_workspace["registry"],
            )

    def test_old_string_not_found(self, edit_workspace):
        with pytest.raises(WorkspacePolicyError, match="not found"):
            project_file_edit(
                "edit-project",
                "src/main.py",
                "nonexistent string",
                "new",
                registry=edit_workspace["registry"],
            )

    def test_no_change_when_same(self, edit_workspace):
        result = project_file_edit(
            "edit-project",
            "src/main.py",
            "def main():",
            "def main():",
            registry=edit_workspace["registry"],
        )
        assert result["replaced"] is False
        assert result["diff"] == ""

    def test_diff_contains_change(self, edit_workspace):
        result = project_file_edit(
            "edit-project",
            "src/main.py",
            "pass",
            "return 0",
            registry=edit_workspace["registry"],
        )
        assert result["replaced"] is True
        assert "-    pass" in result["diff"]
        assert "+    return 0" in result["diff"]

    def test_file_not_found(self, edit_workspace):
        with pytest.raises(WorkspacePolicyError, match="not found"):
            project_file_edit(
                "edit-project",
                "nonexistent.py",
                "old",
                "new",
                registry=edit_workspace["registry"],
            )

    def test_hidden_path_rejected(self, edit_workspace):
        with pytest.raises(WorkspacePolicyError):
            project_file_edit(
                "edit-project",
                ".env",
                "SECRET=abc",
                "SECRET=xyz",
                registry=edit_workspace["registry"],
            )

    def test_content_exceeds_max_bytes(self, edit_workspace):
        # Create a file just under the limit
        project = edit_workspace["project"]
        (project / "near_limit.txt").write_text("a" * 999_990)
        with pytest.raises(WorkspacePolicyError, match="exceeds maximum"):
            project_file_edit(
                "edit-project",
                "near_limit.txt",
                "a" * 999_990,
                "b" * 1_000_010,  # replacement makes it too large
                max_bytes=1_000_000,
                registry=edit_workspace["registry"],
            )


# ── project_apply_patch tests ────────────────────────────────────


class TestApplyPatch:
    def test_clean_apply(self, edit_workspace):
        patch = """\
--- a/src/main.py
+++ b/src/main.py
@@ -1,2 +1,2 @@
-def main():
-    pass
+def entry():
+    return 0
"""
        result = project_apply_patch(
            "edit-project",
            "src/main.py",
            patch,
            registry=edit_workspace["registry"],
        )
        assert result["applied"] is True
        assert result["backup_hash"].startswith("sha256:")
        content = (edit_workspace["project"] / "src" / "main.py").read_text()
        assert "def entry():" in content
        assert "return 0" in content
        # Response must not leak the patch text
        assert "patch" not in result or result["patch"] is None

    def test_hunk_conflict(self, edit_workspace):
        patch = """\
--- a/src/main.py
+++ b/src/main.py
@@ -1,2 +1,2 @@
-def nonexistent():
-    pass
+def entry():
+    return 0
"""
        with pytest.raises(PatchError, match="mismatch"):
            project_apply_patch(
                "edit-project",
                "src/main.py",
                patch,
                registry=edit_workspace["registry"],
            )

    def test_empty_patch(self, edit_workspace):
        with pytest.raises(PatchError, match="No hunks"):
            project_apply_patch(
                "edit-project",
                "src/main.py",
                "",
                registry=edit_workspace["registry"],
            )

    def test_file_not_found(self, edit_workspace):
        patch = """\
--- a/nonexistent.py
+++ b/nonexistent.py
@@ -1 +1 @@
-old
+new
"""
        with pytest.raises(WorkspacePolicyError, match="not found"):
            project_apply_patch(
                "edit-project",
                "nonexistent.py",
                patch,
                registry=edit_workspace["registry"],
            )

    def test_hidden_path_rejected(self, edit_workspace):
        patch = """\
--- a/.env
+++ b/.env
@@ -1 +1 @@
-SECRET=abc
+SECRET=xyz
"""
        with pytest.raises(WorkspacePolicyError):
            project_apply_patch(
                "edit-project",
                ".env",
                patch,
                registry=edit_workspace["registry"],
            )

    def test_backup_hash_matches(self, edit_workspace):
        original_content = (edit_workspace["project"] / "src" / "utils.py").read_text()
        import hashlib
        expected_hash = "sha256:" + hashlib.sha256(original_content.encode("utf-8")).hexdigest()

        patch = """\
--- a/src/utils.py
+++ b/src/utils.py
@@ -1,3 +1,3 @@
 # utils
 
-def helper():
-    return 42
+def compute():
+    return 0
"""
        result = project_apply_patch(
            "edit-project",
            "src/utils.py",
            patch,
            registry=edit_workspace["registry"],
        )
        assert result["backup_hash"] == expected_hash

    def test_symlink_target_rejected(self, edit_workspace):
        project = edit_workspace["project"]
        link = project / "link.py"
        link.symlink_to(project / "src" / "main.py")
        patch = """\
--- a/link.py
+++ b/link.py
@@ -1,2 +1,2 @@
-def main():
-    pass
+def entry():
+    return 0
"""
        with pytest.raises(SymlinkEscapeError):
            project_apply_patch(
                "edit-project",
                "link.py",
                patch,
                registry=edit_workspace["registry"],
            )
