"""Tests for workspace snapshot store and audit logger.

Covers:
    - Snapshot capture and retrieval (existing, new, empty files)
    - Rollback success (file unchanged since capture)
    - Rollback staleness detection (file modified after capture)
    - New file rollback (delete only if unchanged, reject if modified, no-op if missing)
    - Empty file vs missing file distinction
    - Rollback security (traversal, symlink, hidden path, scope denied)
    - Memory cap eviction (per-project and global)
    - Receipt ID can be supplied externally
    - Restart-loss model (documented behavior)
    - Audit logger metadata-only (no content leaks)
    - Audit logger in-memory cap
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.workspace.policy import (
    HiddenPathError,
    ScopeDeniedError,
    SymlinkEscapeError,
    TraversalError,
    WorkspacePolicyError,
)
from app.workspace.snapshot import (
    AuditLogger,
    SnapshotStore,
    StaleSnapshotError,
    WorkspaceAuditLogger,
    _compute_hash,
    _file_hash,
)

# ── Helpers ──────────────────────────────────────────────────────

_EMPTY_HASH = _compute_hash(b"")


def _make_project(tmp_path: Path, project_id: str = "test-project") -> Path:
    """Create a project directory with a test file."""
    project = tmp_path / project_id
    project.mkdir(parents=True, exist_ok=True)
    (project / "hello.txt").write_text("Hello, world!\n")
    (project / "src").mkdir(exist_ok=True)
    (project / "src" / "main.py").write_text("def main():\n    pass\n")
    return project


def _mock_registry(project_root: Path) -> MagicMock:
    """Create a mock WorkspaceRegistry with project:write scope."""
    policy = MagicMock()
    policy.validate_write.side_effect = lambda pid, path: project_root / path
    policy._resolve_project_root.return_value = project_root

    registry = MagicMock()
    registry._policy = policy
    return registry


def _mock_registry_hidden(project_root: Path) -> MagicMock:
    """Create a mock registry that rejects hidden paths."""
    policy = MagicMock()
    policy.validate_write.side_effect = HiddenPathError(
        "Write to hidden/secret path denied"
    )
    policy._resolve_project_root.return_value = project_root

    registry = MagicMock()
    registry._policy = policy
    return registry


def _mock_registry_scope_denied(project_root: Path) -> MagicMock:
    """Create a mock registry that denies write scope."""
    policy = MagicMock()
    policy.validate_write.side_effect = ScopeDeniedError(
        "Scope 'project:write' required"
    )
    policy._resolve_project_root.return_value = project_root

    registry = MagicMock()
    registry._policy = policy
    return registry


# ── SnapshotStore: capture ───────────────────────────────────────


class TestSnapshotCapture:
    """Test snapshot capture behavior."""

    def test_capture_stores_content(self, tmp_path):
        project = _make_project(tmp_path)
        store = SnapshotStore(max_snapshots_per_project=10, max_total_bytes=1_000_000)
        content = b"Hello, world!\n"
        file_hash = _compute_hash(content)

        snap = store.capture(
            project.name, "hello.txt", content, "write", file_hash=file_hash
        )

        assert snap.receipt_id.startswith("r_")
        assert snap.project_id == project.name
        assert snap.relative_path == "hello.txt"
        assert snap.content == content
        assert snap.size == len(content)
        assert snap.before_hash == _compute_hash(content)
        assert snap.operation == "write"
        assert snap.file_exists_before is True
        assert snap.expected_current_hash == file_hash

    def test_capture_replaces_same_file(self, tmp_path):
        project = _make_project(tmp_path)
        store = SnapshotStore()

        store.capture(project.name, "hello.txt", b"v1", "write")
        store.capture(project.name, "hello.txt", b"v2", "edit")

        assert store.total_snapshots == 1
        assert store.has_snapshot(project.name, "hello.txt")
        stored = store.get_snapshot(project.name, "hello.txt")
        assert stored.content == b"v2"

    def test_capture_multiple_files(self, tmp_path):
        project = _make_project(tmp_path)
        store = SnapshotStore()

        store.capture(project.name, "hello.txt", b"content1", "write")
        store.capture(project.name, "src/main.py", b"content2", "edit")

        assert store.total_snapshots == 2
        snaps = store.list_snapshots(project.name)
        assert len(snaps) == 2

    def test_capture_new_file(self, tmp_path):
        """New file: content=None, before_hash=None."""
        project = _make_project(tmp_path)
        store = SnapshotStore()

        # Create the file first so we can hash it
        (project / "new_file.txt").write_text("created content")
        file_hash = _file_hash(project / "new_file.txt")

        snap = store.capture(
            project.name,
            "new_file.txt",
            None,
            "write",
            file_exists_before=False,
            file_hash=file_hash,
        )

        assert snap.content is None
        assert snap.size == 0
        assert snap.before_hash is None
        assert snap.file_exists_before is False
        assert snap.expected_current_hash == file_hash

    def test_capture_empty_existing_file(self, tmp_path):
        """Empty existing file: content=b'', before_hash=real SHA-256 of ''."""
        project = _make_project(tmp_path)
        store = SnapshotStore()

        # Create an empty file
        (project / "empty.txt").write_bytes(b"")
        file_hash = _file_hash(project / "empty.txt")

        snap = store.capture(
            project.name, "empty.txt", b"", "write", file_hash=file_hash
        )

        assert snap.content == b""
        assert snap.size == 0
        assert snap.before_hash == _EMPTY_HASH
        assert snap.expected_current_hash == file_hash
        # Empty file hash is real SHA-256, not ""
        assert snap.before_hash.startswith("sha256:")
        assert file_hash is not None

    def test_total_bytes_tracked(self, tmp_path):
        project = _make_project(tmp_path)
        store = SnapshotStore()

        store.capture(project.name, "a.txt", b"12345", "write")
        store.capture(project.name, "b.txt", b"1234567890", "write")

        assert store.total_bytes == 15


# ── SnapshotStore: eviction ──────────────────────────────────────


class TestSnapshotEviction:
    """Test memory cap and eviction behavior."""

    def test_default_cap_is_10(self):
        """Default per-project cap is 10 snapshots."""
        store = SnapshotStore()
        assert store._max_per_project == 10

    def test_per_project_cap_eviction(self, tmp_path):
        project = _make_project(tmp_path)
        store = SnapshotStore(max_snapshots_per_project=3, max_total_bytes=10_000_000)

        for i in range(5):
            store.capture(project.name, f"f{i}.txt", f"content{i}".encode(), "write")

        assert store.total_snapshots == 3
        snaps = store.list_snapshots(project.name)
        # Oldest two evicted
        assert snaps[0].relative_path == "f2.txt"
        assert snaps[2].relative_path == "f4.txt"

    def test_global_bytes_cap_eviction(self, tmp_path):
        store = SnapshotStore(max_snapshots_per_project=100, max_total_bytes=25)

        # Project A gets snapshots first
        store.capture("projA", "a1.txt", b"x" * 10, "write")
        store.capture("projA", "a2.txt", b"y" * 10, "write")

        # Project B — total would be 30, triggers global eviction of oldest from A
        store.capture("projB", "b1.txt", b"z" * 10, "write")

        # Total should be <= 25
        assert store.total_bytes <= 25
        # Oldest from A should be evicted
        assert not store.has_snapshot("projA", "a1.txt")

    def test_clear_specific_project(self, tmp_path):
        store = SnapshotStore()
        store.capture("projA", "a.txt", b"aaa", "write")
        store.capture("projB", "b.txt", b"bbb", "write")

        store.clear("projA")

        assert store.total_snapshots == 1
        assert not store.has_snapshot("projA", "a.txt")
        assert store.has_snapshot("projB", "b.txt")

    def test_clear_all(self, tmp_path):
        store = SnapshotStore()
        store.capture("projA", "a.txt", b"aaa", "write")
        store.capture("projB", "b.txt", b"bbb", "write")

        store.clear()

        assert store.total_snapshots == 0
        assert store.total_bytes == 0


# ── SnapshotStore: rollback ──────────────────────────────────────


class TestSnapshotRollback:
    """Test rollback behavior — file unchanged since capture."""

    def test_rollback_restores_content(self, tmp_path):
        """Rollback succeeds when file is unmodified since capture."""
        project = _make_project(tmp_path)
        original = (project / "hello.txt").read_bytes()

        store = SnapshotStore()
        store.capture(
            project.name, "hello.txt", original, "write",
            file_hash=_compute_hash(original),
        )

        # File NOT modified — rollback should succeed
        registry = _mock_registry(project)
        result = store.rollback(project.name, "hello.txt", registry)

        assert result.rolled_back is True
        assert result.size == len(original)
        assert result.before_hash == _compute_hash(original)
        assert result.stale_detected is False
        # File should still have original content
        restored = (project / "hello.txt").read_bytes()
        assert restored == original

    def test_rollback_removes_snapshot(self, tmp_path):
        project = _make_project(tmp_path)
        original = (project / "hello.txt").read_bytes()

        store = SnapshotStore()
        store.capture(
            project.name, "hello.txt", original, "write",
            file_hash=_compute_hash(original),
        )

        registry = _mock_registry(project)
        store.rollback(project.name, "hello.txt", registry)

        assert not store.has_snapshot(project.name, "hello.txt")

    def test_rollback_no_snapshot_raises(self, tmp_path):
        project = _make_project(tmp_path)
        store = SnapshotStore()
        registry = _mock_registry(project)

        with pytest.raises(WorkspacePolicyError, match="No snapshot"):
            store.rollback(project.name, "hello.txt", registry)

    def test_rollback_validates_path(self, tmp_path):
        """Rollback must run through validate_write (traversal check)."""
        project = _make_project(tmp_path)
        store = SnapshotStore()
        store.capture(project.name, "hello.txt", b"original", "write")

        policy = MagicMock()
        policy.validate_write.side_effect = TraversalError("Path contains traversal")
        policy._resolve_project_root.return_value = project

        registry = MagicMock()
        registry._policy = policy

        with pytest.raises(TraversalError):
            store.rollback(project.name, "hello.txt", registry)

    def test_rollback_rejects_hidden_path(self, tmp_path):
        """Rollback must reject hidden/secret paths."""
        project = _make_project(tmp_path)
        store = SnapshotStore()
        store.capture(project.name, ".env", b"SECRET=1", "write")

        registry = _mock_registry_hidden(project)

        with pytest.raises(HiddenPathError):
            store.rollback(project.name, ".env", registry)

    def test_rollback_rejects_scope_denied(self, tmp_path):
        """Rollback must check scope."""
        project = _make_project(tmp_path)
        store = SnapshotStore()
        store.capture(project.name, "hello.txt", b"content", "write")

        registry = _mock_registry_scope_denied(project)

        with pytest.raises(ScopeDeniedError):
            store.rollback(project.name, "hello.txt", registry)

    def test_rollback_symlink_escape_rejected(self, tmp_path):
        """Rollback must reject symlinks."""
        project = _make_project(tmp_path)

        # Create a symlink
        sibling = tmp_path / "other"
        sibling.mkdir()
        (sibling / "loot.txt").write_text("stolen")
        link = project / "escape_link"
        link.symlink_to(sibling)

        store = SnapshotStore()
        # Store snapshot for the file that will be rolled back
        store.capture(project.name, "escape_link/loot.txt", b"content", "write")

        policy = MagicMock()
        policy.validate_write.return_value = project / "escape_link" / "loot.txt"
        policy._resolve_project_root.return_value = project

        registry = MagicMock()
        registry._policy = policy

        # The _symlink_safe_preflight should raise SymlinkEscapeError
        with pytest.raises(SymlinkEscapeError):
            store.rollback(project.name, "escape_link/loot.txt", registry)

    def test_rollback_verify_after_hash(self, tmp_path):
        """Rollback should compute after_hash of restored content."""
        project = _make_project(tmp_path)
        original = (project / "hello.txt").read_bytes()

        store = SnapshotStore()
        store.capture(
            project.name, "hello.txt", original, "write",
            file_hash=_compute_hash(original),
        )

        registry = _mock_registry(project)
        result = store.rollback(project.name, "hello.txt", registry)

        assert result.after_hash == _compute_hash(original)
        assert result.before_hash == result.after_hash


# ── SnapshotStore: staleness detection ───────────────────────────


class TestStalenessDetection:
    """Test that rollback refuses when file was modified after capture."""

    def test_stale_file_raises_error(self, tmp_path):
        """Modify file after capture → StaleSnapshotError on rollback."""
        project = _make_project(tmp_path)
        original = (project / "hello.txt").read_bytes()

        store = SnapshotStore()
        store.capture(
            project.name, "hello.txt", original, "write",
            file_hash=_compute_hash(original),
        )

        # Third-party modification
        (project / "hello.txt").write_bytes(b"MODIFIED BY OTHER!")

        registry = _mock_registry(project)
        with pytest.raises(StaleSnapshotError, match="stale"):
            store.rollback(project.name, "hello.txt", registry)

    def test_stale_file_not_removed_from_store(self, tmp_path):
        """Stale error does NOT consume the snapshot."""
        project = _make_project(tmp_path)
        original = (project / "hello.txt").read_bytes()

        store = SnapshotStore()
        store.capture(
            project.name, "hello.txt", original, "write",
            file_hash=_compute_hash(original),
        )

        (project / "hello.txt").write_bytes(b"CHANGED")

        registry = _mock_registry(project)
        with pytest.raises(StaleSnapshotError):
            store.rollback(project.name, "hello.txt", registry)

        # Snapshot still exists (not consumed on failure)
        assert store.has_snapshot(project.name, "hello.txt")

    def test_stale_existing_file_missing(self, tmp_path):
        """Existing file deleted after capture → StaleSnapshotError."""
        project = _make_project(tmp_path)
        original = (project / "hello.txt").read_bytes()

        store = SnapshotStore()
        store.capture(
            project.name, "hello.txt", original, "write",
            file_hash=_compute_hash(original),
        )

        # Third-party deletion
        (project / "hello.txt").unlink()

        registry = _mock_registry(project)
        with pytest.raises(StaleSnapshotError, match="missing"):
            store.rollback(project.name, "hello.txt", registry)

    def test_stale_error_is_workspace_policy_error(self):
        """StaleSnapshotError is a subclass of WorkspacePolicyError."""
        assert issubclass(StaleSnapshotError, WorkspacePolicyError)

    def test_stale_error_message_contains_path(self, tmp_path):
        """Error message includes project/path for debugging."""
        project = _make_project(tmp_path)
        original = (project / "hello.txt").read_bytes()

        store = SnapshotStore()
        store.capture(
            project.name, "hello.txt", original, "write",
            file_hash=_compute_hash(original),
        )
        (project / "hello.txt").write_bytes(b"X")

        registry = _mock_registry(project)
        with pytest.raises(StaleSnapshotError, match="test-project/hello.txt"):
            store.rollback(project.name, "hello.txt", registry)


# ── SnapshotStore: new file rollback ─────────────────────────────


class TestNewFileRollback:
    """Test new-file rollback model per spec."""

    def test_new_file_rollback_deletes_only_if_unchanged(self, tmp_path):
        """New file rollback succeeds only if file unchanged since capture."""
        project = _make_project(tmp_path)
        store = SnapshotStore()

        # Create the file
        (project / "new_file.txt").write_text("created content")
        file_hash = _file_hash(project / "new_file.txt")

        # Capture new-file snapshot
        store.capture(
            project.name,
            "new_file.txt",
            None,
            "write",
            file_exists_before=False,
            file_hash=file_hash,
        )

        # File NOT modified — rollback should delete it
        registry = _mock_registry(project)
        result = store.rollback(project.name, "new_file.txt", registry)

        assert result.rolled_back is True
        assert not (project / "new_file.txt").exists()

    def test_new_file_rollback_rejects_if_modified_after_create(self, tmp_path):
        """New file modified after capture → StaleSnapshotError."""
        project = _make_project(tmp_path)
        store = SnapshotStore()

        # Create the file
        (project / "new_file.txt").write_text("created content")
        file_hash = _file_hash(project / "new_file.txt")

        # Capture new-file snapshot
        store.capture(
            project.name,
            "new_file.txt",
            None,
            "write",
            file_exists_before=False,
            file_hash=file_hash,
        )

        # Third-party modification
        (project / "new_file.txt").write_text("TAMPERED!")

        registry = _mock_registry(project)
        with pytest.raises(StaleSnapshotError, match="modified after creation"):
            store.rollback(project.name, "new_file.txt", registry)

        # File still exists (rollback refused)
        assert (project / "new_file.txt").read_text() == "TAMPERED!"

    def test_new_file_rollback_noop_if_already_missing(self, tmp_path):
        """New file already missing at rollback → no-op, not stale."""
        project = _make_project(tmp_path)
        store = SnapshotStore()

        # Create the file temporarily to compute hash
        (project / "new_file.txt").write_text("created content")
        file_hash = _file_hash(project / "new_file.txt")

        # Capture new-file snapshot
        store.capture(
            project.name,
            "new_file.txt",
            None,
            "write",
            file_exists_before=False,
            file_hash=file_hash,
        )

        # File deleted before rollback (by someone else or user)
        (project / "new_file.txt").unlink()

        # Rollback should be a no-op, not stale
        registry = _mock_registry(project)
        result = store.rollback(project.name, "new_file.txt", registry)

        assert result.rolled_back is True
        assert not (project / "new_file.txt").exists()
        assert result.before_hash is None
        assert result.after_hash is None


# ── SnapshotStore: empty vs missing ──────────────────────────────


class TestEmptyVsMissing:
    """Empty file and missing file must be distinct states."""

    def test_empty_existing_file_distinct_from_missing_file(self, tmp_path):
        """Empty file hash (real SHA-256) is different from None (missing)."""
        project = _make_project(tmp_path)

        # Create an empty file
        empty_file = project / "empty.txt"
        empty_file.write_bytes(b"")
        empty_hash = _file_hash(empty_file)

        # Missing file
        missing_hash = _file_hash(project / "nonexistent.txt")

        # They must be different
        assert empty_hash is not None
        assert missing_hash is None
        assert empty_hash != missing_hash
        # Empty file has real SHA-256 hash
        assert empty_hash.startswith("sha256:")
        assert empty_hash == _EMPTY_HASH

    def test_rollback_empty_existing_file(self, tmp_path):
        """Rollback of empty file restores empty content (file unchanged)."""
        project = _make_project(tmp_path)

        # Create an empty file
        (project / "empty.txt").write_bytes(b"")
        file_hash = _file_hash(project / "empty.txt")

        store = SnapshotStore()
        store.capture(
            project.name, "empty.txt", b"", "write", file_hash=file_hash
        )

        # File NOT modified — rollback should succeed (no-op, same content)
        registry = _mock_registry(project)
        result = store.rollback(project.name, "empty.txt", registry)

        assert result.rolled_back is True
        assert (project / "empty.txt").read_bytes() == b""

    def test_stale_empty_file_modified(self, tmp_path):
        """Empty file modified after capture → StaleSnapshotError."""
        project = _make_project(tmp_path)

        # Create an empty file
        (project / "empty.txt").write_bytes(b"")
        file_hash = _file_hash(project / "empty.txt")

        store = SnapshotStore()
        store.capture(
            project.name, "empty.txt", b"", "write", file_hash=file_hash
        )

        # Third-party modification
        (project / "empty.txt").write_bytes(b"TAMPERED")

        registry = _mock_registry(project)
        with pytest.raises(StaleSnapshotError):
            store.rollback(project.name, "empty.txt", registry)


# ── SnapshotStore: receipt_id ────────────────────────────────────


class TestReceiptId:
    """Test receipt_id handling."""

    def test_snapshot_receipt_id_can_be_supplied(self, tmp_path):
        """Receipt ID can be provided externally."""
        project = _make_project(tmp_path)
        store = SnapshotStore()

        snap = store.capture(
            project.name, "hello.txt", b"content", "write",
            receipt_id="custom-receipt-123",
        )

        assert snap.receipt_id == "custom-receipt-123"

    def test_snapshot_receipt_id_auto_generated(self, tmp_path):
        """Receipt ID is auto-generated when not provided."""
        project = _make_project(tmp_path)
        store = SnapshotStore()

        snap = store.capture(project.name, "hello.txt", b"content", "write")

        assert snap.receipt_id.startswith("r_")
        assert len(snap.receipt_id) == 18  # "r_" + 16 hex chars

    def test_snapshot_receipt_id_links_to_change_receipt(self):
        """snapshot.receipt_id == receipt.receipt_id when supplied.

        This is the linkage pattern: capture(receipt_id=receipt.receipt_id)
        produces snapshot.receipt_id identical to the ChangeReceipt's ID.
        Both auto-generate IDs when not supplied (snapshot: r_, receipt: rcpt_).
        """
        from app.workspace.receipts import ChangeReceipt

        receipt = ChangeReceipt(
            project_id="proj",
            relative_path="src/main.py",
            operation="write",
        )

        store = SnapshotStore()
        snap = store.capture(
            "proj", "src/main.py", b"old content", "write",
            receipt_id=receipt.receipt_id,
        )

        # The linkage: snapshot.receipt_id == receipt.receipt_id
        assert snap.receipt_id == receipt.receipt_id
        assert receipt.receipt_id.startswith("rcpt_")

    def test_snapshot_auto_id_differs_from_receipt_auto_id(self):
        """Auto-generated IDs use different prefixes (r_ vs rcpt_)."""
        from app.workspace.receipts import ChangeReceipt

        receipt = ChangeReceipt()
        store = SnapshotStore()
        snap = store.capture("proj", "f.txt", b"data", "write")

        # Different prefixes when both auto-generated
        assert snap.receipt_id.startswith("r_")
        assert receipt.receipt_id.startswith("rcpt_")
        assert snap.receipt_id != receipt.receipt_id


# ── SnapshotStore: restart-loss model ────────────────────────────


class TestRestartLossModel:
    """Document that snapshots are lost on process restart.

    This is by design: session-scoped IDE workflow.
    The gateway restarts = new session = no rollback available.
    """

    def test_new_store_has_no_snapshots(self):
        """A fresh SnapshotStore is empty (simulates restart)."""
        store = SnapshotStore()
        assert store.total_snapshots == 0
        assert store.total_bytes == 0
        assert not store.has_snapshot("any-project", "any-file.txt")

    def test_clear_simulates_restart(self, tmp_path):
        """After clear(), no rollbacks available."""
        project = _make_project(tmp_path)
        store = SnapshotStore()
        store.capture(project.name, "hello.txt", b"content", "write")

        store.clear()

        registry = _mock_registry(project)
        with pytest.raises(WorkspacePolicyError, match="No snapshot"):
            store.rollback(project.name, "hello.txt", registry)


# ── WorkspaceAuditLogger ─────────────────────────────────────────


class TestAuditLogger:
    """Test audit logger metadata-only behavior."""

    def test_log_creates_entry(self):
        logger = WorkspaceAuditLogger()
        entry = logger.log(
            receipt_id="snap_abc123",
            project_id="proj",
            relative_path="src/main.py",
            operation="write",
            before_hash="sha256:aaa",
            after_hash="sha256:bbb",
            size=100,
            identity="agent-1",
            success=True,
        )
        assert entry.receipt_id == "snap_abc123"
        assert entry.operation == "write"
        assert len(logger.entries) == 1

    def test_log_failure_records_error(self):
        logger = WorkspaceAuditLogger()
        entry = logger.log(
            receipt_id="snap_fail",
            project_id="proj",
            relative_path="bad.txt",
            operation="write",
            before_hash="",
            after_hash="",
            size=0,
            success=False,
            error="TraversalError",
        )
        assert entry.success is False
        assert entry.error == "TraversalError"

    def test_log_to_jsonl_file(self, tmp_path):
        log_path = tmp_path / "audit.jsonl"
        logger = WorkspaceAuditLogger(log_path)

        logger.log(
            receipt_id="snap_1",
            project_id="proj",
            relative_path="a.txt",
            operation="write",
            before_hash="sha256:aaa",
            after_hash="sha256:bbb",
            size=50,
        )

        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["receipt_id"] == "snap_1"
        assert data["project_id"] == "proj"
        assert "content" not in data
        assert "patch" not in data
        assert "old_string" not in data
        assert "new_string" not in data

    def test_audit_no_content_leak(self, tmp_path):
        """Audit entries must never contain file content."""
        logger = WorkspaceAuditLogger(tmp_path / "audit.jsonl")
        logger.log(
            receipt_id="snap_x",
            project_id="proj",
            relative_path="secret.txt",
            operation="write",
            before_hash="sha256:aaa",
            after_hash="sha256:bbb",
            size=1024,
        )

        entry = logger.entries[0]
        # These keys must NEVER appear in audit entries
        forbidden_keys = {"content", "patch", "old_string", "new_string", "raw"}
        assert not forbidden_keys.intersection(entry.keys())

    def test_audit_no_absolute_paths(self, tmp_path):
        """Audit entries must not contain absolute host paths."""
        logger = WorkspaceAuditLogger()
        logger.log(
            receipt_id="snap_y",
            project_id="proj",
            relative_path="src/main.py",
            operation="write",
            before_hash="sha256:aaa",
            after_hash="sha256:bbb",
            size=100,
        )

        entry = logger.entries[0]
        assert entry["relative_path"] == "src/main.py"
        assert "/" not in entry["relative_path"] or entry["relative_path"].startswith("src/")

    def test_clear_resets_entries(self):
        logger = WorkspaceAuditLogger()
        logger.log(
            receipt_id="snap_z",
            project_id="proj",
            relative_path="a.txt",
            operation="write",
            before_hash="",
            after_hash="",
            size=0,
        )
        logger.clear()
        assert len(logger.entries) == 0

    def test_no_file_when_log_path_none(self):
        """If log_path is None, entries are in-memory only."""
        logger = WorkspaceAuditLogger(log_path=None)
        logger.log(
            receipt_id="snap_n",
            project_id="proj",
            relative_path="a.txt",
            operation="write",
            before_hash="",
            after_hash="",
            size=0,
        )
        assert len(logger.entries) == 1

    def test_in_memory_cap_enforced(self):
        """In-memory entries are capped at max_in_memory_entries."""
        logger = WorkspaceAuditLogger(log_path=None, max_in_memory_entries=5)
        for i in range(8):
            logger.log(
                receipt_id=f"snap_{i}",
                project_id="proj",
                relative_path=f"f{i}.txt",
                operation="write",
                before_hash="",
                after_hash="",
                size=0,
            )
        # Only the last 5 should remain
        assert len(logger.entries) == 5
        assert logger.entries[0]["receipt_id"] == "snap_3"
        assert logger.entries[4]["receipt_id"] == "snap_7"

    def test_no_cap_when_log_path_set(self):
        """When log_path is set, no in-memory cap is enforced."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            log_path = f.name
        try:
            logger = WorkspaceAuditLogger(log_path=log_path, max_in_memory_entries=3)
            for i in range(5):
                logger.log(
                    receipt_id=f"snap_{i}",
                    project_id="proj",
                    relative_path=f"f{i}.txt",
                    operation="write",
                    before_hash="",
                    after_hash="",
                    size=0,
                )
            # All entries kept in memory (cap disabled when log_path set)
            assert len(logger.entries) == 5
        finally:
            Path(log_path).unlink(missing_ok=True)

    def test_backward_compatible_alias(self):
        """AuditLogger is an alias for WorkspaceAuditLogger."""
        assert AuditLogger is WorkspaceAuditLogger


# ── Integration: capture + rollback cycle ────────────────────────


class TestSnapshotIntegration:
    """Integration tests for full capture → rollback cycle.

    With staleness detection, rollback succeeds only when the file is
    unmodified since capture. If the file was modified by a third party,
    StaleSnapshotError is raised.
    """

    def test_full_cycle_rollback_unchanged(self, tmp_path):
        """Capture → rollback (file unchanged) → content restored (no-op write)."""
        project = _make_project(tmp_path)
        original = (project / "hello.txt").read_bytes()

        store = SnapshotStore()
        store.capture(
            project.name, "hello.txt", original, "write",
            file_hash=_compute_hash(original),
        )

        # File NOT modified — rollback should succeed
        registry = _mock_registry(project)
        result = store.rollback(project.name, "hello.txt", registry)

        assert result.rolled_back is True
        assert (project / "hello.txt").read_bytes() == original
        assert not store.has_snapshot(project.name, "hello.txt")

    def test_full_cycle_stale_raises(self, tmp_path):
        """Capture → third-party modify → rollback → StaleSnapshotError."""
        project = _make_project(tmp_path)
        original = (project / "hello.txt").read_bytes()

        store = SnapshotStore()
        store.capture(
            project.name, "hello.txt", original, "write",
            file_hash=_compute_hash(original),
        )

        # Third-party modification
        (project / "hello.txt").write_bytes(b"THIRD PARTY CHANGED THIS")

        registry = _mock_registry(project)
        with pytest.raises(StaleSnapshotError):
            store.rollback(project.name, "hello.txt", registry)

        # File still has third-party content (rollback refused)
        assert (project / "hello.txt").read_bytes() == b"THIRD PARTY CHANGED THIS"

    def test_new_file_rollback_deletes_file(self, tmp_path):
        """Rollback of new-file snapshot should delete the file."""
        project = _make_project(tmp_path)
        store = SnapshotStore()

        # Create the file
        (project / "new_file.txt").write_text("created content")
        file_hash = _file_hash(project / "new_file.txt")

        # Capture new-file snapshot
        store.capture(
            project.name,
            "new_file.txt",
            None,
            "write",
            file_exists_before=False,
            file_hash=file_hash,
        )

        # Rollback should delete it
        registry = _mock_registry(project)
        result = store.rollback(project.name, "new_file.txt", registry)

        assert result.rolled_back is True
        assert not (project / "new_file.txt").exists()

    def test_multiple_files_independent_rollback(self, tmp_path):
        """Rollback of one file doesn't affect others."""
        project = _make_project(tmp_path)
        content_a = b"File A content"
        content_b = b"File B content"

        # Write files to disk
        (project / "a.txt").write_bytes(content_a)
        (project / "b.txt").write_bytes(content_b)

        store = SnapshotStore()
        store.capture(
            project.name, "a.txt", content_a, "write",
            file_hash=_compute_hash(content_a),
        )
        store.capture(
            project.name, "b.txt", content_b, "write",
            file_hash=_compute_hash(content_b),
        )

        # Rollback only A (file unchanged)
        registry = _mock_registry(project)
        store.rollback(project.name, "a.txt", registry)

        assert (project / "a.txt").read_bytes() == content_a
        # B still has original content (was never modified)
        assert (project / "b.txt").read_bytes() == content_b

    def test_rollback_with_audit_logging(self, tmp_path):
        """Audit logger works alongside rollback (independent)."""
        project = _make_project(tmp_path)
        original = (project / "hello.txt").read_bytes()

        store = SnapshotStore()
        audit = WorkspaceAuditLogger()

        # Capture
        snap = store.capture(
            project.name, "hello.txt", original, "write",
            file_hash=_compute_hash(original),
        )
        audit.log(
            receipt_id=snap.receipt_id,
            project_id=project.name,
            relative_path="hello.txt",
            operation="write",
            before_hash=snap.before_hash or "",
            after_hash="",
            size=snap.size,
        )

        # Rollback (file unchanged)
        registry = _mock_registry(project)
        result = store.rollback(project.name, "hello.txt", registry)

        # Log rollback
        audit.log(
            receipt_id=result.receipt_id,
            project_id=project.name,
            relative_path="hello.txt",
            operation="rollback",
            before_hash=result.before_hash or "",
            after_hash=result.after_hash or "",
            size=result.size,
        )

        assert len(audit.entries) == 2
        assert audit.entries[0]["operation"] == "write"
        assert audit.entries[1]["operation"] == "rollback"
        # No content in audit
        for e in audit.entries:
            assert "content" not in e
