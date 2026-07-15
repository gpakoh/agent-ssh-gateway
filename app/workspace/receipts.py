"""Change receipts for workspace write/edit/patch operations.

Records before/after state, computes hashes, and provides read-back verification.
All receipts are immutable dataclasses returned from write operations.
Receipts are metadata-only — no file content is stored in the receipt.
Linking: receipt_id ↔ snapshot_id for traceability between receipt and snapshot.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _generate_receipt_id() -> str:
    """Generate a unique receipt ID."""
    return f"rcpt_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class ChangeReceipt:
    """Immutable metadata record of a file mutation.

    Receipt is metadata-only: hashes, sizes, and verification status.
    No file content is stored in the receipt.

    Attributes:
        receipt_id: Unique identifier for this receipt.
        snapshot_id: Links to a Snapshot for rollback (None until linked).
        project_id: Project that was modified.
        relative_path: Project-relative path of the file.
        operation: "write", "edit", or "patch".
        file_exists_before: True if file existed before mutation.
        before_hash: SHA-256 of file bytes before mutation (None if new file).
        after_hash: SHA-256 of file bytes after mutation.
        size_before: Byte size before mutation (0 if new file).
        size_after: Byte size after mutation.
        changed: True if content actually changed.
        verified: True if read-back matches after_hash.
        diff_summary: Brief description of what changed.
        error: Error message if verification failed, None otherwise.
    """

    receipt_id: str = field(default_factory=_generate_receipt_id)
    snapshot_id: str | None = None
    project_id: str = ""
    relative_path: str = ""
    operation: str = ""
    file_exists_before: bool = False
    before_hash: str | None = None
    after_hash: str = ""
    size_before: int = 0
    size_after: int = 0
    changed: bool = False
    verified: bool = False
    diff_summary: str = ""
    error: str | None = None

    def with_snapshot_id(self, snapshot_id: str) -> ChangeReceipt:
        """Return a new receipt with snapshot_id set (immutable update)."""
        return ChangeReceipt(
            receipt_id=self.receipt_id,
            snapshot_id=snapshot_id,
            project_id=self.project_id,
            relative_path=self.relative_path,
            operation=self.operation,
            file_exists_before=self.file_exists_before,
            before_hash=self.before_hash,
            after_hash=self.after_hash,
            size_before=self.size_before,
            size_after=self.size_after,
            changed=self.changed,
            verified=self.verified,
            diff_summary=self.diff_summary,
            error=self.error,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict (receipt fields only)."""
        d: dict[str, Any] = {
            "receipt_id": self.receipt_id,
            "snapshot_id": self.snapshot_id,
            "operation": self.operation,
            "file_exists_before": self.file_exists_before,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "size_before": self.size_before,
            "size_after": self.size_after,
            "changed": self.changed,
            "verified": self.verified,
            "diff_summary": self.diff_summary,
        }
        if self.error:
            d["error"] = self.error
        return d


def compute_hash(content: str | bytes) -> str:
    """Compute SHA-256 hash of content.

    For strings, hashes the UTF-8 encoded bytes.
    For bytes, hashes directly.
    """
    if isinstance(content, str):
        content = content.encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def compute_file_hash(path: Path, max_bytes: int = 10_000_000) -> str | None:
    """Compute SHA-256 hash of file bytes without decoding.

    Returns hash string or None if file unreadable.
    """
    try:
        size = path.stat().st_size
        if size > max_bytes:
            return None
        raw = path.read_bytes()
        return "sha256:" + hashlib.sha256(raw).hexdigest()
    except (OSError, PermissionError):
        return None


def read_file_bytes(path: Path, max_bytes: int = 10_000_000) -> tuple[bytes | None, int]:
    """Read file as raw bytes for receipt computation.

    Returns (bytes, size) or (None, 0) if file doesn't exist or is too large.
    """
    if not path.exists():
        return None, 0
    try:
        size = path.stat().st_size
        if size > max_bytes:
            return None, size
        raw = path.read_bytes()
        return raw, size
    except (OSError, PermissionError):
        return None, 0


def verify_readback(path: Path, expected_hash: str, max_bytes: int = 10_000_000) -> tuple[bool, str | None]:
    """Read file bytes and verify hash matches expected.

    Returns (ok, error_message).
    """
    actual_hash = compute_file_hash(path, max_bytes)
    if actual_hash is None:
        return False, "File not readable after write"
    if actual_hash != expected_hash:
        return False, f"Hash mismatch: expected {expected_hash}, got {actual_hash}"
    return True, None


def make_diff_summary(old_content: str | None, new_content: str, operation: str) -> str:
    """Create a brief diff summary."""
    if old_content is None:
        lines = new_content.count("\n") + (1 if new_content and not new_content.endswith("\n") else 0)
        return f"created ({lines} lines)"
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    added = max(0, len(new_lines) - len(old_lines))
    removed = max(0, len(old_lines) - len(new_lines))
    return f"{operation}: +{added}/-{removed} lines"


def make_receipt(
    project_id: str,
    relative_path: str,
    operation: str,
    file_path: Path,
    before_content: str | None,
    after_content: str,
    verify: bool = True,
) -> ChangeReceipt:
    """Create a ChangeReceipt after a write operation.

    Args:
        project_id: Project identifier.
        relative_path: Project-relative path.
        operation: "write", "edit", or "patch".
        file_path: Absolute path to the file.
        before_content: Content before mutation (None if new file).
        after_content: Content after mutation.
        verify: Whether to read-back and verify hash.
    """
    file_exists_before = before_content is not None
    before_hash = compute_hash(before_content) if before_content is not None else None
    before_size = len(before_content.encode("utf-8")) if before_content is not None else 0

    after_hash = compute_hash(after_content)
    after_size = len(after_content.encode("utf-8"))

    changed = before_content != after_content

    diff_summary = make_diff_summary(before_content, after_content, operation)

    verified = True
    error = None
    if verify and changed:
        verified, error = verify_readback(file_path, after_hash)

    return ChangeReceipt(
        receipt_id=_generate_receipt_id(),
        project_id=project_id,
        relative_path=relative_path,
        operation=operation,
        file_exists_before=file_exists_before,
        before_hash=before_hash,
        after_hash=after_hash,
        size_before=before_size,
        size_after=after_size,
        changed=changed,
        verified=verified,
        diff_summary=diff_summary,
        error=error,
    )
