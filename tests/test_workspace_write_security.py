"""Security test pack for Phase C1 write/edit tools.

Covers: symlink target/parent escape, hidden/secret paths, traversal,
size limits, binary rejection, atomic failure, TOCTOU, scope enforcement.

Write/edit tests activate once project_file_write and project_file_edit exist
(Agent 1). Apply-patch tests additionally require project_apply_patch (Agent 2).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.workspace.policy import (
    ALL_SCOPES,
    HiddenPathError,
    ScopeDeniedError,
    SymlinkEscapeError,
    TraversalError,
    WorkspacePolicyError,
)
from app.workspace.registry import WorkspaceRegistry

# ── Guards: split by availability of write/edit vs patch tools ──

try:
    from app.workspace.edit import project_file_edit, project_file_write

    _HAS_WRITE_EDIT = True
except ImportError:
    _HAS_WRITE_EDIT = False

try:
    from app.workspace.edit import project_apply_patch

    _HAS_PATCH = True
except ImportError:
    _HAS_PATCH = False

REASON_NO_WRITE_EDIT = "project_file_write/edit not implemented (Agent 1 pending)"
REASON_NO_PATCH = "project_apply_patch not implemented (Agent 2 pending)"


# ── Helpers ──────────────────────────────────────────────────────────────


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def write_workspace(tmp_path):
    """Create a temporary workspace for write security tests."""
    project = tmp_path / "write-project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("print('hello')\n")
    (project / "README.md").write_text("# Write Project\n")
    (project / ".env").write_text("SECRET=abc123\n")
    (project / ".env.local").write_text("LOCAL=dev\n")
    (project / "id_rsa").write_text("private-key-data\n")
    (project / "secret.pem").write_text("-----BEGIN CERTIFICATE-----\n")

    hidden_dir = project / ".ssh"
    hidden_dir.mkdir()
    (hidden_dir / "authorized_keys").write_text("ssh-rsa AAAA...\n")

    # Symlink that escapes to tmp_path (sibling)
    sibling = tmp_path / "other-project"
    sibling.mkdir()
    (sibling / "loot.txt").write_text("stolen\n")
    escape_link = project / "escape_link"
    escape_link.symlink_to(sibling)

    # Symlink inside project (valid for reads)
    safe_link = project / "safe_link"
    safe_link.symlink_to(project / "src")

    # Symlink to /etc/passwd
    system_link = project / "system_link"
    system_link.symlink_to(Path("/etc/passwd"))

    # Directory with symlink in parent chain
    deep = project / "deep"
    deep.mkdir()
    deep_link_parent = deep / "link_parent"
    deep_link_parent.symlink_to(sibling)
    (deep_link_parent / "nested.txt").mkdir(parents=True, exist_ok=True)
    (deep_link_parent / "nested.txt" / "file.txt").write_text("nested\n")

    allowed_roots = [tmp_path]
    project_roots = {"write-project": project}

    return {
        "tmp_path": tmp_path,
        "project": project,
        "sibling": sibling,
        "project_roots": project_roots,
        "allowed_roots": allowed_roots,
    }


@pytest.fixture
def write_registry(write_workspace):
    """Create a WorkspaceRegistry with write scope for the write-project."""
    from app.workspace.models import ProjectInfo

    projects = {
        "write-project": ProjectInfo(
            project_id="write-project",
            root=write_workspace["project"],
            type="python",
            description="test write security",
            tags=[],
        ),
    }
    return WorkspaceRegistry(
        projects=projects,
        allowed_roots=write_workspace["allowed_roots"],
        granted_scopes=ALL_SCOPES,
    )


@pytest.fixture
def read_only_registry(write_workspace):
    """Registry with read-only scope — no write allowed."""
    from app.workspace.models import ProjectInfo

    projects = {
        "write-project": ProjectInfo(
            project_id="write-project",
            root=write_workspace["project"],
            type="python",
            description="test read-only",
            tags=[],
        ),
    }
    return WorkspaceRegistry(
        projects=projects,
        allowed_roots=write_workspace["allowed_roots"],
        granted_scopes={"project:read"},
    )


# ══════════════════════════════════════════════════════════════════════════
#  project_file_write — security tests
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not _HAS_WRITE_EDIT, reason=REASON_NO_WRITE_EDIT)
class TestFileWriteSecurity:
    """Security tests for project_file_write."""

    # ── Symlink escape ────────────────────────────────────────────

    def test_symlink_target_rejected(self, write_registry):
        """Write through a symlink that escapes the project."""
        with pytest.raises(SymlinkEscapeError):
            project_file_write(
                "write-project",
                "escape_link/loot.txt",
                "evil",
                registry=write_registry,
            )

    def test_symlink_target_system_rejected(self, write_registry):
        """Write through a symlink pointing to /etc/passwd."""
        with pytest.raises(SymlinkEscapeError):
            project_file_write(
                "write-project",
                "system_link/evil.txt",
                "evil",
                registry=write_registry,
            )

    def test_symlink_parent_rejected(self, write_registry):
        """Write to a path whose parent is a symlink escaping the project."""
        with pytest.raises(SymlinkEscapeError):
            project_file_write(
                "write-project",
                "escape_link/nested/file.txt",
                "evil",
                registry=write_registry,
            )

    def test_symlink_in_parent_chain_rejected(self, write_registry):
        """Write to deep path where an intermediate component is a symlink."""
        with pytest.raises((SymlinkEscapeError, WorkspacePolicyError)):
            project_file_write(
                "write-project",
                "deep/link_parent/nested.txt/file.txt",
                "evil",
                registry=write_registry,
            )

    # ── Hidden / secret paths ─────────────────────────────────────

    def test_write_dotenv_rejected(self, write_registry):
        with pytest.raises(HiddenPathError):
            project_file_write(
                "write-project", ".env", "SECRET=new\n", registry=write_registry,
            )

    def test_write_dotenv_local_rejected(self, write_registry):
        with pytest.raises(HiddenPathError):
            project_file_write(
                "write-project",
                ".env.local",
                "LOCAL=val\n",
                registry=write_registry,
            )

    def test_write_id_rsa_rejected(self, write_registry):
        with pytest.raises(HiddenPathError):
            project_file_write(
                "write-project",
                "id_rsa",
                "stolen-key\n",
                registry=write_registry,
            )

    def test_write_pem_rejected(self, write_registry):
        with pytest.raises(HiddenPathError):
            project_file_write(
                "write-project",
                "secret.pem",
                "-----BEGIN PRIVATE KEY-----\n",
                registry=write_registry,
            )

    def test_write_to_hidden_dir_rejected(self, write_registry):
        with pytest.raises(HiddenPathError):
            project_file_write(
                "write-project",
                ".ssh/new_key",
                "ssh-rsa BBBB\n",
                registry=write_registry,
            )

    def test_write_nested_dotenv_rejected(self, write_registry):
        """Write to src/.env should be blocked by hidden dir pattern."""
        with pytest.raises(HiddenPathError):
            project_file_write(
                "write-project",
                "src/.env",
                "SECRET=nested\n",
                registry=write_registry,
            )

    # ── Traversal ─────────────────────────────────────────────────

    def test_traversal_dotdot_rejected(self, write_registry):
        with pytest.raises(TraversalError):
            project_file_write(
                "write-project",
                "../escape.txt",
                "evil",
                registry=write_registry,
            )

    def test_traversal_middle_dotdot_rejected(self, write_registry):
        with pytest.raises(TraversalError):
            project_file_write(
                "write-project",
                "src/../../etc/shadow",
                "evil",
                registry=write_registry,
            )

    def test_absolute_path_rejected(self, write_registry):
        with pytest.raises(TraversalError, match="Absolute path"):
            project_file_write(
                "write-project",
                "/etc/passwd",
                "evil",
                registry=write_registry,
            )

    def test_tilde_path_rejected(self, write_registry):
        with pytest.raises(TraversalError, match="Tilde"):
            project_file_write(
                "write-project",
                "~/.bashrc",
                "evil",
                registry=write_registry,
            )

    # ── Size limits ───────────────────────────────────────────────

    def test_content_exceeds_max_bytes_rejected(self, write_registry):
        huge = "x" * 2_000_000
        with pytest.raises(WorkspacePolicyError, match="exceeds maximum"):
            project_file_write(
                "write-project",
                "huge.txt",
                huge,
                max_bytes=1_000_000,
                registry=write_registry,
            )

    def test_content_at_exact_max_bytes_accepted(self, write_registry):
        exact = "x" * 999_999
        result = project_file_write(
            "write-project",
            "exact.txt",
            exact,
            max_bytes=1_000_000,
            registry=write_registry,
        )
        assert result["size"] == 999_999

    # ── Binary rejection ──────────────────────────────────────────

    def test_binary_content_rejected(self, write_registry):
        binary = "hello\x00world"
        with pytest.raises(WorkspacePolicyError, match="Binary"):
            project_file_write(
                "write-project", "binary.txt", binary, registry=write_registry,
            )

    # ── Directory target ──────────────────────────────────────────

    def test_write_to_directory_rejected(self, write_registry):
        with pytest.raises(WorkspacePolicyError, match="directory"):
            project_file_write(
                "write-project", "src", "content\n", registry=write_registry,
            )

    # ── Missing parent ────────────────────────────────────────────

    def test_parent_dir_missing_rejected(self, write_registry):
        with pytest.raises(WorkspacePolicyError, match="Parent directory"):
            project_file_write(
                "write-project",
                "nonexistent/deep/file.txt",
                "content\n",
                registry=write_registry,
            )

    # ── Scope enforcement ─────────────────────────────────────────

    def test_scope_denied(self, read_only_registry):
        with pytest.raises(ScopeDeniedError, match="project:write"):
            project_file_write(
                "write-project",
                "new_file.txt",
                "content\n",
                registry=read_only_registry,
            )

    # ── Atomicity ─────────────────────────────────────────────────

    def test_atomic_failure_preserves_original(self, write_registry, write_workspace):
        """If atomic replace fails, original file is untouched."""
        target = write_workspace["project"] / "atomic_test.txt"
        target.write_text("original content\n")
        original_hash = _sha256("original content\n")

        # Patch os.replace to simulate failure
        import app.workspace.edit as edit_mod


        def failing_replace(src: Any, dst: Any) -> None:
            raise OSError("Simulated disk failure")

        with patch.object(edit_mod.os, "replace", side_effect=failing_replace):
            with pytest.raises(OSError, match="Simulated"):
                project_file_write(
                    "write-project",
                    "atomic_test.txt",
                    "new content\n",
                    registry=write_registry,
                )

        # Original content must be intact
        assert target.exists()
        assert _sha256(target.read_text()) == original_hash

    def test_atomic_no_leftover_tmp(self, write_registry, write_workspace):
        """After successful write, no .tmp file remains."""
        result = project_file_write(
            "write-project",
            "cleanup_test.txt",
            "hello\n",
            registry=write_registry,
        )
        tmp_path = write_workspace["project"] / "cleanup_test.txt.tmp"
        assert not tmp_path.exists()
        assert result["size"] == 6

    # ── Unknown project ───────────────────────────────────────────

    def test_unknown_project_rejected(self, write_registry):
        with pytest.raises(WorkspacePolicyError, match="Unknown project"):
            project_file_write(
                "nonexistent", "file.txt", "content\n", registry=write_registry,
            )

    # ── Write then read back ──────────────────────────────────────

    def test_write_creates_file_readable(self, write_registry):
        from app.workspace.files import project_file_read

        result = project_file_write(
            "write-project",
            "readback.txt",
            "read me\n",
            registry=write_registry,
        )
        assert result["size"] == 8

        read_result = project_file_read(
            "write-project", "readback.txt", registry=write_registry,
        )
        assert read_result["content"] == "read me"

    # ── Overwrite existing ────────────────────────────────────────

    def test_overwrite_existing_file(self, write_registry, write_workspace):
        target = write_workspace["project"] / "overwrite.txt"
        target.write_text("before\n")

        result = project_file_write(
            "write-project",
            "overwrite.txt",
            "after\n",
            registry=write_registry,
        )
        assert result["size"] == 6
        assert target.read_text() == "after\n"


# ══════════════════════════════════════════════════════════════════════════
#  project_file_edit — security tests
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not _HAS_WRITE_EDIT, reason=REASON_NO_WRITE_EDIT)
class TestFileEditSecurity:
    """Security tests for project_file_edit."""

    # ── Empty old_string ──────────────────────────────────────────

    def test_empty_old_string_rejected(self, write_registry):
        with pytest.raises(WorkspacePolicyError, match="old_string must not be empty"):
            project_file_edit(
                "write-project",
                "src/main.py",
                "",
                "new",
                registry=write_registry,
            )

    # ── old_string not found ──────────────────────────────────────

    def test_old_string_not_found(self, write_registry):
        with pytest.raises(WorkspacePolicyError, match="not found"):
            project_file_edit(
                "write-project",
                "src/main.py",
                "DOES_NOT_EXIST",
                "replacement",
                registry=write_registry,
            )

    # ── Hidden path ───────────────────────────────────────────────

    def test_edit_hidden_path_rejected(self, write_registry):
        with pytest.raises(HiddenPathError):
            project_file_edit(
                "write-project",
                ".env",
                "SECRET=abc123",
                "SECRET=new",
                registry=write_registry,
            )

    # ── Traversal ─────────────────────────────────────────────────

    def test_edit_traversal_rejected(self, write_registry):
        with pytest.raises(TraversalError):
            project_file_edit(
                "write-project",
                "../escape.txt",
                "old",
                "new",
                registry=write_registry,
            )

    # ── Scope ─────────────────────────────────────────────────────

    def test_edit_scope_denied(self, read_only_registry):
        with pytest.raises(ScopeDeniedError, match="project:write"):
            project_file_edit(
                "write-project",
                "src/main.py",
                "print('hello')",
                "print('world')",
                registry=read_only_registry,
            )

    # ── Symlink target ────────────────────────────────────────────

    def test_edit_symlink_target_rejected(self, write_registry):
        with pytest.raises(SymlinkEscapeError):
            project_file_edit(
                "write-project",
                "escape_link/loot.txt",
                "old",
                "new",
                registry=write_registry,
            )

    # ── Size limit on result ──────────────────────────────────────

    def test_edit_result_exceeds_max_bytes(self, write_registry):
        """Editing to produce content over max_bytes must be rejected."""
        huge_new = "x" * 2_000_000
        with pytest.raises(WorkspacePolicyError, match="exceeds maximum"):
            project_file_edit(
                "write-project",
                "src/main.py",
                "print('hello')",
                huge_new,
                max_bytes=1_000_000,
                registry=write_registry,
            )

    # ── Large source rejected before partial edit ─────────────────

    def test_large_source_rejected(self, write_registry, write_workspace):
        """Source file exceeding max_bytes must be rejected, not partially edited."""
        big = write_workspace["project"] / "big.py"
        big.write_text("x" * 2_000_000)

        with pytest.raises(WorkspacePolicyError):
            project_file_edit(
                "write-project",
                "big.py",
                "x" * 100,
                "y" * 100,
                max_bytes=1_000_000,
                registry=write_registry,
            )

    # ── No change (old == new) ────────────────────────────────────

    def test_edit_no_change_returns_replaced_false(self, write_registry):
        result = project_file_edit(
            "write-project",
            "src/main.py",
            "print('hello')",
            "print('hello')",
            registry=write_registry,
        )
        assert result["replaced"] is False

    # ── Diff output ───────────────────────────────────────────────

    def test_edit_diff_contains_change(self, write_registry):
        result = project_file_edit(
            "write-project",
            "src/main.py",
            "print('hello')",
            "print('world')",
            registry=write_registry,
        )
        assert result["replaced"] is True
        assert "diff" in result
        assert "-" in result["diff"]
        assert "+" in result["diff"]

    # ── First occurrence only ─────────────────────────────────────

    def test_edit_replaces_first_occurrence_only(self, write_registry, write_workspace):
        target = write_workspace["project"] / "multi.txt"
        target.write_text("aaa\naaa\naaa\n")

        result = project_file_edit(
            "write-project",
            "multi.txt",
            "aaa",
            "bbb",
            registry=write_registry,
        )
        assert result["replaced"] is True
        content = target.read_text()
        lines = content.strip().splitlines()
        assert lines[0] == "bbb"
        assert lines[1] == "aaa"
        assert lines[2] == "aaa"


# ══════════════════════════════════════════════════════════════════════════
#  project_apply_patch — security tests
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not _HAS_PATCH, reason=REASON_NO_PATCH)
class TestApplyPatchSecurity:
    """Security tests for project_apply_patch."""

    def _make_patch(self, filename: str, old: str, new: str) -> str:
        """Build a minimal unified diff patch."""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        header = f"--- a/{filename}\n+++ b/{filename}\n"
        hunk = f"@@ -1,{len(old_lines)} +1,{len(new_lines)} @@\n"
        body = "".join(f"-{line}\n" if not line.endswith("\n") else f"-{line}" for line in old_lines)
        body += "".join(f"+{line}\n" if not line.endswith("\n") else f"+{line}" for line in new_lines)
        return header + hunk + body

    # ── Invalid patch format ──────────────────────────────────────

    def test_invalid_patch_format_rejected(self, write_registry):
        with pytest.raises(WorkspacePolicyError, match="[Pp]atch"):
            project_apply_patch(
                "write-project",
                "src/main.py",
                "this is not a patch",
                registry=write_registry,
            )

    # ── Hunk conflict ─────────────────────────────────────────────

    def test_hunk_context_mismatch_rejected(self, write_registry):
        patch = self._make_patch(
            "src/main.py",
            "print('DOES_NOT_MATCH')",
            "print('changed')",
        )
        with pytest.raises(WorkspacePolicyError, match="[Pp]atch|hunk|context"):
            project_apply_patch(
                "write-project",
                "src/main.py",
                patch,
                registry=write_registry,
            )

    # ── Hidden path ───────────────────────────────────────────────

    def test_patch_hidden_path_rejected(self, write_registry):
        patch = self._make_patch(".env", "SECRET=abc", "SECRET=new")
        with pytest.raises(HiddenPathError):
            project_apply_patch(
                "write-project", ".env", patch, registry=write_registry,
            )

    # ── Traversal ─────────────────────────────────────────────────

    def test_patch_traversal_rejected(self, write_registry):
        with pytest.raises(TraversalError):
            project_apply_patch(
                "write-project",
                "../escape.txt",
                "--- a/escape.txt\n+++ b/escape.txt\n",
                registry=write_registry,
            )

    # ── Scope ─────────────────────────────────────────────────────

    def test_patch_scope_denied(self, read_only_registry):
        with pytest.raises(ScopeDeniedError, match="project:write"):
            project_apply_patch(
                "write-project",
                "src/main.py",
                "--- a/src/main.py\n+++ b/src/main.py\n",
                registry=read_only_registry,
            )

    # ── Symlink target ────────────────────────────────────────────

    def test_patch_symlink_target_rejected(self, write_registry):
        with pytest.raises(SymlinkEscapeError):
            project_apply_patch(
                "write-project",
                "escape_link/loot.txt",
                "--- a/loot.txt\n+++ b/loot.txt\n",
                registry=write_registry,
            )

    # ── Size limit ────────────────────────────────────────────────

    def test_patch_content_exceeds_max_bytes(self, write_registry, write_workspace):
        """Patch that would produce oversized content must be rejected."""
        target = write_workspace["project"] / "src" / "main.py"
        target.write_text("aaa\n")
        old = "aaa"
        new = "b" * 2_000_000
        patch = self._make_patch("src/main.py", old, new)
        with pytest.raises(WorkspacePolicyError, match="exceeds maximum"):
            project_apply_patch(
                "write-project",
                "src/main.py",
                patch,
                max_bytes=1_000_000,
                registry=write_registry,
            )

    # ── Backup hash ───────────────────────────────────────────────

    def test_backup_hash_matches_original(self, write_registry, write_workspace):
        """backup_hash must equal SHA-256 of pre-patch content."""
        target = write_workspace["project"] / "hash_test.py"
        original = "print('original')\n"
        target.write_text(original)
        expected_hash = _sha256(original)

        patch = self._make_patch("hash_test.py", "print('original')", "print('modified')")
        result = project_apply_patch(
            "write-project",
            "hash_test.py",
            patch,
            registry=write_registry,
        )
        assert result["backup_hash"] == f"sha256:{expected_hash}"

    # ── Atomic failure preserves original ─────────────────────────

    def test_atomic_failure_preserves_original(self, write_registry, write_workspace):
        """Simulated write failure must leave original file intact."""
        target = write_workspace["project"] / "atomic_patch.txt"
        original = "original content\n"
        target.write_text(original)
        original_hash = _sha256(original)

        patch_text = self._make_patch("atomic_patch.txt", "original content", "modified content")

        import app.workspace.edit as edit_mod

        def failing_replace(src: Any, dst: Any) -> None:
            raise OSError("Simulated disk failure")

        with patch.object(edit_mod.os, "replace", side_effect=failing_replace):
            with pytest.raises(OSError, match="Simulated"):
                project_apply_patch(
                    "write-project",
                    "atomic_patch.txt",
                    patch_text,
                    registry=write_registry,
                )

        assert target.exists()
        assert _sha256(target.read_text()) == original_hash

    # ── Clean apply ───────────────────────────────────────────────

    def test_clean_apply(self, write_registry, write_workspace):
        target = write_workspace["project"] / "clean_apply.py"
        target.write_text("line1\nline2\nline3\n")

        patch = self._make_patch(
            "clean_apply.py",
            "line1\nline2\nline3",
            "line1\nmodified\nline3",
        )
        result = project_apply_patch(
            "write-project",
            "clean_apply.py",
            patch,
            registry=write_registry,
        )
        assert result["applied"] is True
        assert target.read_text() == "line1\nmodified\nline3\n"


# ══════════════════════════════════════════════════════════════════════════
#  Shared write security — cross-cutting concerns
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not _HAS_PATCH, reason=REASON_NO_PATCH)
class TestImportSecurity:
    """Import sanity — all three tools must be reachable from public API."""

    def test_import_from_tools(self):
        from app.workspace.tools import (
            project_apply_patch,
            project_file_edit,
            project_file_write,
        )

        assert callable(project_file_write)
        assert callable(project_file_edit)
        assert callable(project_apply_patch)

    def test_import_from_init(self):
        from app.workspace import (
            project_apply_patch,
            project_file_edit,
            project_file_write,
        )

        assert callable(project_file_write)
        assert callable(project_file_edit)
        assert callable(project_apply_patch)


@pytest.mark.skipif(not _HAS_WRITE_EDIT, reason=REASON_NO_WRITE_EDIT)
class TestWriteSharedSecurity:
    """Cross-cutting security tests that apply to all write tools."""

    def test_symlink_safe_preflight_walks_components(self, write_registry, write_workspace):
        """Write to a path where an intermediate dir is a symlink to outside project."""
        # deep/link_parent is a symlink to sibling (outside project)
        with pytest.raises(SymlinkEscapeError):
            project_file_write(
                "write-project",
                "deep/link_parent/nested.txt/file.txt",
                "evil\n",
                registry=write_registry,
            )

    def test_chmod_not_available(self, write_registry):
        """Verify that no permission-changing function is exposed."""
        import app.workspace.edit as edit_mod

        # The module should not expose chmod, chown, or similar
        assert not hasattr(edit_mod, "chmod")
        assert not hasattr(edit_mod, "chown")
        # os module is imported but we don't expose chmod/chown directly
        assert not hasattr(edit_mod, "_chmod")
        assert not hasattr(edit_mod, "_chown")

    def test_empty_content_accepted(self, write_registry):
        result = project_file_write(
            "write-project",
            "empty.txt",
            "",
            registry=write_registry,
        )
        assert result["size"] == 0

    def test_unicode_content_accepted(self, write_registry):
        content = "Привет мир! 🌍 日本語テスト\n"
        result = project_file_write(
            "write-project",
            "unicode.txt",
            content,
            registry=write_registry,
        )
        assert result["size"] > 0

        from app.workspace.files import project_file_read

        read = project_file_read(
            "write-project", "unicode.txt", registry=write_registry,
        )
        assert read["content"] == content.strip()
