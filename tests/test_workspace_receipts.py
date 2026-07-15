"""Tests for workspace change receipts."""

from __future__ import annotations

import pytest

from app.workspace.edit import (
    WriteError,
    project_apply_patch,
    project_file_edit,
    project_file_write,
)
from app.workspace.models import ProjectInfo
from app.workspace.receipts import (
    ChangeReceipt,
    compute_file_hash,
    compute_hash,
    make_diff_summary,
    make_receipt,
    read_file_bytes,
    verify_readback,
)
from app.workspace.registry import WorkspaceRegistry


@pytest.fixture
def receipt_workspace(tmp_path):
    """Create a workspace for receipt tests."""
    project = tmp_path / "receipt-project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("def main():\n    pass\n")
    (project / "README.md").write_text("# Receipt Project\n")

    allowed_roots = [tmp_path]
    projects = {
        "receipt-project": ProjectInfo(
            project_id="receipt-project",
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


# ── Receipt dataclass tests ──────────────────────────────────────


class TestChangeReceipt:
    def test_to_dict(self):
        receipt = ChangeReceipt(
            receipt_id="rcpt_test123",
            project_id="test",
            relative_path="file.txt",
            operation="write",
            file_exists_before=False,
            before_hash=None,
            after_hash="sha256:new",
            size_before=0,
            size_after=200,
            changed=True,
            verified=True,
            diff_summary="created (10 lines)",
        )
        d = receipt.to_dict()
        assert d["receipt_id"] == "rcpt_test123"
        assert d["snapshot_id"] is None  # not linked yet
        assert "project_id" not in d  # receipt is nested
        assert "path" not in d
        assert d["file_exists_before"] is False
        assert d["operation"] == "write"
        assert d["changed"] is True
        assert d["verified"] is True
        assert "error" not in d

    def test_to_dict_with_error(self):
        receipt = ChangeReceipt(
            receipt_id="rcpt_err",
            operation="write",
            file_exists_before=True,
            before_hash="sha256:old",
            after_hash="sha256:new",
            size_before=100,
            size_after=200,
            changed=True,
            verified=False,
            diff_summary="write: +5/-3 lines",
            error="Hash mismatch",
        )
        d = receipt.to_dict()
        assert d["error"] == "Hash mismatch"
        assert d["file_exists_before"] is True

    def test_with_snapshot_id(self):
        receipt = ChangeReceipt(
            receipt_id="rcpt_test",
            operation="write",
            file_exists_before=False,
            before_hash=None,
            after_hash="sha256:new",
            size_before=0,
            size_after=100,
            changed=True,
            verified=True,
            diff_summary="created",
        )
        linked = receipt.with_snapshot_id("snap_abc123")
        assert linked.receipt_id == "rcpt_test"
        assert linked.snapshot_id == "snap_abc123"
        # Original unchanged (frozen dataclass)
        assert receipt.snapshot_id is None

    def test_to_dict_with_snapshot_id(self):
        receipt = ChangeReceipt(
            receipt_id="rcpt_test",
            snapshot_id="snap_linked",
            operation="edit",
            file_exists_before=True,
            before_hash="sha256:old",
            after_hash="sha256:new",
            size_before=100,
            size_after=200,
            changed=True,
            verified=True,
            diff_summary="edit: +1/-1 lines",
        )
        d = receipt.to_dict()
        assert d["snapshot_id"] == "snap_linked"

    def test_content_not_in_receipt(self):
        receipt = ChangeReceipt(
            receipt_id="rcpt_test",
            operation="write",
            file_exists_before=False,
            before_hash=None,
            after_hash="sha256:new",
            size_before=0,
            size_after=100,
            changed=True,
            verified=True,
            diff_summary="created",
        )
        d = receipt.to_dict()
        assert "content" not in d
        assert "before_content" not in d
        assert "after_content" not in d


class TestComputeHash:
    def test_string_hash(self):
        h = compute_hash("hello")
        assert h.startswith("sha256:")
        assert len(h) == 71

    def test_bytes_hash(self):
        h = compute_hash(b"hello")
        assert h.startswith("sha256:")

    def test_string_matches_bytes(self):
        assert compute_hash("hello") == compute_hash(b"hello")

    def test_deterministic(self):
        assert compute_hash("test") == compute_hash("test")
        assert compute_hash("test") != compute_hash("other")


class TestComputeFileHash:
    def test_existing_file(self, receipt_workspace):
        path = receipt_workspace["project"] / "README.md"
        h = compute_file_hash(path)
        assert h is not None
        assert h.startswith("sha256:")

    def test_nonexistent_file(self, receipt_workspace):
        path = receipt_workspace["project"] / "missing.txt"
        h = compute_file_hash(path)
        assert h is None


class TestReadFileBytes:
    def test_existing_file(self, receipt_workspace):
        raw, size = read_file_bytes(receipt_workspace["project"] / "README.md")
        assert raw is not None
        assert b"Receipt Project" in raw
        assert size > 0

    def test_nonexistent_file(self, receipt_workspace):
        raw, size = read_file_bytes(receipt_workspace["project"] / "missing.txt")
        assert raw is None
        assert size == 0


class TestVerifyReadback:
    def test_matching_hash(self, receipt_workspace):
        path = receipt_workspace["project"] / "README.md"
        h = compute_file_hash(path)
        ok, err = verify_readback(path, h)
        assert ok is True
        assert err is None

    def test_mismatched_hash(self, receipt_workspace):
        path = receipt_workspace["project"] / "README.md"
        ok, err = verify_readback(path, "sha256:wrong")
        assert ok is False
        assert "Hash mismatch" in err


class TestMakeDiffSummary:
    def test_new_file(self):
        summary = make_diff_summary(None, "line1\nline2\n", "write")
        assert "created" in summary
        assert "2 lines" in summary

    def test_existing_file(self):
        summary = make_diff_summary("old\n", "new\nline2\n", "edit")
        assert "edit" in summary
        assert "+1" in summary
        assert "-0" in summary


class TestMakeReceipt:
    def test_write_new_file(self, receipt_workspace):
        path = receipt_workspace["project"] / "new_file.txt"
        receipt = make_receipt(
            project_id="receipt-project",
            relative_path="new_file.txt",
            operation="write",
            file_path=path,
            before_content=None,
            after_content="new content",
            verify=False,
        )
        assert receipt.receipt_id.startswith("rcpt_")
        assert receipt.file_exists_before is False
        assert receipt.before_hash is None
        assert receipt.operation == "write"
        assert receipt.size_before == 0
        assert receipt.changed is True

    def test_write_overwrite(self, receipt_workspace):
        path = receipt_workspace["project"] / "README.md"
        old = path.read_text()
        # Write new content so verification can succeed
        path.write_text("new content")
        receipt = make_receipt(
            project_id="receipt-project",
            relative_path="README.md",
            operation="write",
            file_path=path,
            before_content=old,
            after_content="new content",
            verify=True,
        )
        assert receipt.file_exists_before is True
        assert receipt.before_hash is not None
        assert receipt.verified is True

    def test_edit_receipt(self, receipt_workspace):
        path = receipt_workspace["project"] / "src" / "main.py"
        old = path.read_text()
        new = old.replace("pass", "return 0")
        path.write_text(new)
        receipt = make_receipt(
            project_id="receipt-project",
            relative_path="src/main.py",
            operation="edit",
            file_path=path,
            before_content=old,
            after_content=new,
            verify=True,
        )
        assert receipt.file_exists_before is True
        assert receipt.verified is True
        assert receipt.changed is True


# ── Integration tests: default (no safe) ─────────────────────────


class TestWriteDefault:
    def test_no_receipt_by_default(self, receipt_workspace):
        result = project_file_write(
            "receipt-project",
            "new_file.txt",
            "hello world",
            registry=receipt_workspace["registry"],
        )
        assert "receipt" not in result
        assert result["project_id"] == "receipt-project"
        assert result["size"] == 11


class TestEditDefault:
    def test_no_receipt_by_default(self, receipt_workspace):
        result = project_file_edit(
            "receipt-project",
            "src/main.py",
            "pass",
            "return 0",
            registry=receipt_workspace["registry"],
        )
        assert "receipt" not in result
        assert result["replaced"] is True


class TestPatchDefault:
    def test_no_receipt_by_default(self, receipt_workspace):
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
            "receipt-project",
            "src/main.py",
            patch,
            registry=receipt_workspace["registry"],
        )
        assert "receipt" not in result
        assert result["applied"] is True


# ── Integration tests: safe=True ─────────────────────────────────


class TestWriteSafe:
    def test_new_file_receipt(self, receipt_workspace):
        result = project_file_write(
            "receipt-project",
            "new_file.txt",
            "hello world",
            registry=receipt_workspace["registry"],
            safe=True,
        )
        assert "receipt" in result
        receipt = result["receipt"]
        assert receipt["receipt_id"].startswith("rcpt_")
        assert receipt["file_exists_before"] is False
        assert receipt["before_hash"] is None
        assert receipt["operation"] == "write"
        assert receipt["changed"] is True
        assert receipt["verified"] is True

    def test_overwrite_receipt(self, receipt_workspace):
        result = project_file_write(
            "receipt-project",
            "README.md",
            "# New README\n",
            registry=receipt_workspace["registry"],
            safe=True,
        )
        assert result["receipt"]["file_exists_before"] is True
        assert result["receipt"]["before_hash"] is not None


class TestEditSafe:
    def test_includes_receipt(self, receipt_workspace):
        result = project_file_edit(
            "receipt-project",
            "src/main.py",
            "pass",
            "return 0",
            registry=receipt_workspace["registry"],
            safe=True,
        )
        assert "receipt" in result
        receipt = result["receipt"]
        assert receipt["receipt_id"].startswith("rcpt_")
        assert receipt["file_exists_before"] is True
        assert receipt["operation"] == "edit"
        assert receipt["changed"] is True
        assert receipt["verified"] is True


class TestPatchSafe:
    def test_includes_receipt(self, receipt_workspace):
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
            "receipt-project",
            "src/main.py",
            patch,
            registry=receipt_workspace["registry"],
            safe=True,
        )
        assert "receipt" in result
        receipt = result["receipt"]
        assert receipt["receipt_id"].startswith("rcpt_")
        assert receipt["file_exists_before"] is True
        assert receipt["operation"] == "patch"
        assert receipt["changed"] is True
        assert receipt["verified"] is True


# ── Error type tests ─────────────────────────────────────────────


class TestErrorTypes:
    def test_write_content_too_large(self, receipt_workspace):
        with pytest.raises(WriteError, match="exceeds maximum"):
            project_file_write(
                "receipt-project",
                "big.txt",
                "x" * 2_000_001,
                max_bytes=2_000_000,
                registry=receipt_workspace["registry"],
            )

    def test_write_binary_rejected(self, receipt_workspace):
        with pytest.raises(WriteError, match="Binary"):
            project_file_write(
                "receipt-project",
                "bin.txt",
                "hello\x00world",
                registry=receipt_workspace["registry"],
            )

    def test_edit_empty_old_string(self, receipt_workspace):
        with pytest.raises(WriteError, match="must not be empty"):
            project_file_edit(
                "receipt-project",
                "src/main.py",
                "",
                "new",
                registry=receipt_workspace["registry"],
            )

    def test_edit_old_string_not_found(self, receipt_workspace):
        with pytest.raises(WriteError, match="not found"):
            project_file_edit(
                "receipt-project",
                "src/main.py",
                "nonexistent",
                "new",
                registry=receipt_workspace["registry"],
            )
