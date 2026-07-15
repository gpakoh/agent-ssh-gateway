"""In-memory snapshot store for workspace rollback + JSONL audit helper.

Provides session-scoped rollback for write/edit/patch operations.
Snapshots are stored in a bounded in-memory ring buffer per project.
Audit logs are metadata-only (no file content, no patch text).

Rollback is secure: runs through validate_write + _symlink_safe_preflight
+ _atomic_write, the same path validation as the original write.

Staleness detection: rollback verifies the file's current hash matches the
expected hash at capture time. If another agent/writer modified the file
after the snapshot was taken, rollback is refused with StaleSnapshotError.

Receipt linkage: Snapshot.receipt_id == ChangeReceipt.receipt_id.
Call capture(receipt_id=receipt.receipt_id) to link them. Shared fields:
project_id, relative_path, operation, before_hash.
Both auto-generate IDs when not supplied (snapshot: r_, receipt: rcpt_).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.workspace.edit import (
    _atomic_write,
    _exact_read,
    _symlink_safe_preflight,
)
from app.workspace.policy import WorkspacePolicyError
from app.workspace.registry import WorkspaceRegistry, get_registry

logger = logging.getLogger(__name__)

# ── Defaults ─────────────────────────────────────────────────────

_DEFAULT_MAX_PER_PROJECT = 10
_DEFAULT_MAX_TOTAL_BYTES = 10_000_000  # 10 MB
_DEFAULT_MAX_AUDIT_ENTRIES = 500  # in-memory audit cap (when no log_path)


# ── Errors ───────────────────────────────────────────────────────


class StaleSnapshotError(WorkspacePolicyError):
    """Raised when a snapshot's expected hash no longer matches the file.

    This means another writer modified the file after the snapshot was
    taken. Rollback is refused to prevent silent data loss.
    """


# ── Data structures ──────────────────────────────────────────────


@dataclass
class Snapshot:
    """A captured file state for rollback.

    For existing files: content=old_bytes, before_hash=hash(old_bytes),
        expected_current_hash=hash of file on disk at capture time (after mutation).
    For new files: content=None, before_hash=None,
        expected_current_hash=hash of created file.
    For empty existing files: content=b"", before_hash=real sha256 of "".

    Attributes:
        receipt_id: unique identifier — links snapshot to ChangeReceipt.
            Pass ChangeReceipt.receipt_id to capture() to establish the link.
            Auto-generated as r_{uuid} when not provided.
        project_id: registered project identifier.
        relative_path: project-relative file path (never absolute).
        content: original file content bytes, or None for new files.
        size: len(content) if content else 0.
        before_hash: SHA-256 of content, or None for new files.
        expected_current_hash: SHA-256 of file on disk at capture time.
            For existing files: hash of file after mutation was applied.
            For new files: hash of created file.
            None only if caller did not provide file_hash.
        file_exists_before: True if the file existed before the operation.
            False means this is a new-file creation snapshot.
        timestamp: capture time (time.time()).
        operation: the operation that triggered capture (write/edit/patch).
    """

    receipt_id: str
    project_id: str
    relative_path: str
    content: bytes | None
    size: int
    before_hash: str | None
    expected_current_hash: str | None
    file_exists_before: bool
    timestamp: float
    operation: str


@dataclass
class RollbackResult:
    """Result of a rollback operation."""

    project_id: str
    path: str
    size: int
    before_hash: str | None  # hash of file before rollback (None if missing)
    after_hash: str | None  # hash of file after rollback (None if deleted)
    rolled_back: bool
    receipt_id: str
    stale_detected: bool = False


@dataclass
class AuditEntry:
    """Metadata-only audit record (no file content)."""

    receipt_id: str
    project_id: str
    relative_path: str
    operation: str
    before_hash: str
    after_hash: str
    size: int
    timestamp: float
    identity: str  # caller fingerprint (opaque string)
    success: bool
    error: str = ""


# ── Helpers ──────────────────────────────────────────────────────


def _compute_hash(data: bytes) -> str:
    """SHA-256 hash of bytes, prefixed with 'sha256:'."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _compute_hash_str(content: str) -> str:
    """SHA-256 hash of string content."""
    return _compute_hash(content.encode("utf-8"))


def _file_hash(path: Path) -> str | None:
    """Compute SHA-256 of a file on disk. None if file is missing.

    Important: empty files get a real hash (sha256:e3b0...), not "".
    Missing files return None — distinct from empty.
    """
    if not path.exists():
        return None
    content, _ = _exact_read(path)
    return _compute_hash(content.encode("utf-8"))


# ── SnapshotStore ────────────────────────────────────────────────


class SnapshotStore:
    """Bounded in-memory snapshot store for workspace rollback.

    Storage model:
        - Per-project: OrderedDict mapping relative_path -> Snapshot
        - Per-file: only the latest snapshot is kept (overwritten on new capture)
        - Per-project cap: 10 snapshots (configurable)
        - Global cap: 10 MB across all snapshots (configurable)

    Eviction: oldest snapshots evicted first (FIFO) when caps are hit.

    Staleness detection: rollback() verifies the file's current hash
    matches the expected hash before writing. If the file was modified
    by another writer after the snapshot was taken, rollback is refused.

    Thread safety: NOT thread-safe. Caller must synchronize if needed.
    """

    def __init__(
        self,
        max_snapshots_per_project: int = _DEFAULT_MAX_PER_PROJECT,
        max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    ):
        self._max_per_project = max_snapshots_per_project
        self._max_total_bytes = max_total_bytes
        # project_id -> OrderedDict[(relative_path) -> Snapshot]
        self._stores: dict[str, OrderedDict[str, Snapshot]] = {}
        self._total_bytes = 0

    def _get_store(self, project_id: str) -> OrderedDict[str, Snapshot]:
        """Get or create the snapshot store for a project."""
        if project_id not in self._stores:
            self._stores[project_id] = OrderedDict()
        return self._stores[project_id]

    def _evict(self, project_id: str) -> None:
        """Evict oldest snapshots until within caps."""
        store = self._get_store(project_id)

        # Evict from this project until within per-project cap
        while len(store) > self._max_per_project:
            _key, evicted = store.popitem(last=False)
            self._total_bytes -= evicted.size
            logger.debug(
                "Evicted snapshot %s/%s (%d bytes)",
                evicted.project_id,
                evicted.relative_path,
                evicted.size,
            )

        # Evict globally oldest if over total cap (check all projects)
        while self._total_bytes > self._max_total_bytes and self._stores:
            # Find the globally oldest snapshot across all projects
            oldest_project: str | None = None
            oldest_key: str | None = None
            oldest_snapshot: Snapshot | None = None

            for pid, store in self._stores.items():
                if store:
                    key, snap = next(iter(store.items()))
                    if oldest_snapshot is None or snap.timestamp < oldest_snapshot.timestamp:
                        oldest_project = pid
                        oldest_key = key
                        oldest_snapshot = snap

            if oldest_project is None or oldest_key is None:
                break

            self._stores[oldest_project].pop(oldest_key)
            self._total_bytes -= oldest_snapshot.size
            logger.debug(
                "Global eviction: %s/%s (%d bytes)",
                oldest_project,
                oldest_key,
                oldest_snapshot.size,
            )

    def capture(
        self,
        project_id: str,
        relative_path: str,
        content: bytes | None,
        operation: str,
        *,
        file_exists_before: bool = True,
        receipt_id: str | None = None,
        file_hash: str | None = None,
    ) -> Snapshot:
        """Capture a file's state for rollback.

        Call this AFTER applying the mutation. file_hash is the hash of the
        file on disk at capture time (after mutation was applied).

        For existing files: content = old bytes, file_hash = hash of mutated file.
        For new files: content = None, file_hash = hash of created file.
        For empty existing files: content = b"", file_hash = real hash of "".

        Args:
            project_id: registered project identifier.
            relative_path: project-relative file path.
            content: original file content bytes, or None for new files.
            operation: the operation being performed (write/edit/patch).
            file_exists_before: True if the file existed before this operation.
            receipt_id: external receipt ID — pass ChangeReceipt.receipt_id
                to link snapshot ↔ receipt. Auto-generated as r_{uuid} if None.
            file_hash: hash of file on disk at capture time (after mutation).
                Used for staleness detection. If None, defaults to hash of
                content for backward compatibility with existing files.

        Returns:
            Snapshot with receipt_id, hashes, and metadata.
        """
        store = self._get_store(project_id)

        # Overwrite existing snapshot for same file (per-file latest only)
        existing = store.get(relative_path)
        if existing:
            self._total_bytes -= existing.size

        # Compute before_hash
        if content is not None:
            before_hash = _compute_hash(content)
        else:
            before_hash = None

        # expected_current_hash: hash of file on disk at capture time.
        if file_hash is not None:
            expected_current_hash = file_hash
        elif file_exists_before and content is not None:
            # Backward compat: no file_hash provided, compute from content
            expected_current_hash = before_hash
        else:
            expected_current_hash = None

        snapshot = Snapshot(
            receipt_id=receipt_id or f"r_{uuid.uuid4().hex[:16]}",
            project_id=project_id,
            relative_path=relative_path,
            content=content,
            size=len(content) if content else 0,
            before_hash=before_hash,
            expected_current_hash=expected_current_hash,
            file_exists_before=file_exists_before,
            timestamp=time.time(),
            operation=operation,
        )

        store[relative_path] = snapshot
        self._total_bytes += snapshot.size

        # Evict if over caps
        self._evict(project_id)

        return snapshot

    def rollback(
        self,
        project_id: str,
        relative_path: str,
        registry: WorkspaceRegistry | None = None,
    ) -> RollbackResult:
        """Rollback a file to its last captured state.

        Staleness model:
        - Existing file (file_exists_before=True):
            current_hash must == expected_current_hash (hash after mutation).
            If match → restore old content.
            If mismatch → StaleSnapshotError (another writer modified it).
            If file missing → StaleSnapshotError (unexpected deletion).
        - New file (file_exists_before=False):
            If file exists and current_hash == expected_current_hash → delete file.
            If file exists and current_hash != expected_current_hash → StaleSnapshotError.
            If file missing → no-op (not stale, just already absent).

        Runs the same security checks as the original write.

        Raises:
            WorkspacePolicyError: no snapshot found or security check failed.
            StaleSnapshotError: file was modified/deleted after snapshot.
        """
        store = self._get_store(project_id)
        if relative_path not in store:
            raise WorkspacePolicyError(
                f"No snapshot found for {project_id}/{relative_path}"
            )

        snapshot = store[relative_path]

        # Security: same validation as original write
        r = registry or get_registry()
        full = r._policy.validate_write(project_id, relative_path)
        project_root = r._policy._resolve_project_root(project_id)
        _symlink_safe_preflight(full, project_root)

        # ── Staleness check ──────────────────────────────────────
        current_hash = _file_hash(full)  # None if missing

        if not snapshot.file_exists_before:
            # ── New file rollback ────────────────────────────────
            if current_hash is None:
                # File already missing — no-op, not stale data loss
                self._total_bytes -= snapshot.size
                del store[relative_path]
                return RollbackResult(
                    project_id=project_id,
                    path=relative_path,
                    size=0,
                    before_hash=None,
                    after_hash=None,
                    rolled_back=True,
                    receipt_id=snapshot.receipt_id,
                    stale_detected=False,
                )

            if (
                snapshot.expected_current_hash is not None
                and current_hash != snapshot.expected_current_hash
            ):
                raise StaleSnapshotError(
                    f"Snapshot for {project_id}/{relative_path} is stale: "
                    f"expected {snapshot.expected_current_hash}, "
                    f"got {current_hash}. "
                    f"File was modified after creation — refusing rollback."
                )

            # File exists and matches — delete it
            full.unlink()
            before_hash = current_hash
            after_hash = None

        else:
            # ── Existing file rollback ───────────────────────────
            if current_hash is None:
                raise StaleSnapshotError(
                    f"Snapshot for {project_id}/{relative_path} is stale: "
                    f"expected {snapshot.expected_current_hash}, "
                    f"got <missing>. "
                    f"File was deleted after snapshot — refusing rollback."
                )

            if (
                snapshot.expected_current_hash is not None
                and current_hash != snapshot.expected_current_hash
            ):
                raise StaleSnapshotError(
                    f"Snapshot for {project_id}/{relative_path} is stale: "
                    f"expected {snapshot.expected_current_hash}, "
                    f"got {current_hash}. "
                    f"File was modified after snapshot — refusing rollback."
                )

            # File matches — restore old content
            if snapshot.content is not None:
                _atomic_write(full, snapshot.content)
            before_hash = current_hash
            after_hash = _file_hash(full)

        # Remove the snapshot after successful rollback
        self._total_bytes -= snapshot.size
        del store[relative_path]

        return RollbackResult(
            project_id=project_id,
            path=relative_path,
            size=snapshot.size,
            before_hash=before_hash,
            after_hash=after_hash,
            rolled_back=True,
            receipt_id=snapshot.receipt_id,
            stale_detected=False,
        )

    def has_snapshot(self, project_id: str, relative_path: str) -> bool:
        """Check if a snapshot exists for the given file."""
        store = self._get_store(project_id)
        return relative_path in store

    def get_snapshot(self, project_id: str, relative_path: str) -> Snapshot | None:
        """Get a snapshot without consuming it."""
        store = self._get_store(project_id)
        return store.get(relative_path)

    def list_snapshots(self, project_id: str) -> list[Snapshot]:
        """List all snapshots for a project (oldest first)."""
        store = self._get_store(project_id)
        return list(store.values())

    @property
    def total_bytes(self) -> int:
        """Total bytes stored across all projects."""
        return self._total_bytes

    @property
    def total_snapshots(self) -> int:
        """Total number of snapshots across all projects."""
        return sum(len(s) for s in self._stores.values())

    def clear(self, project_id: str | None = None) -> None:
        """Clear snapshots.

        Args:
            project_id: if provided, clear only this project's snapshots.
                        If None, clear all snapshots.
        """
        if project_id:
            if project_id in self._stores:
                for snap in self._stores[project_id].values():
                    self._total_bytes -= snap.size
                del self._stores[project_id]
        else:
            self._stores.clear()
            self._total_bytes = 0


# ── JSONL Audit Logger ───────────────────────────────────────────


class WorkspaceAuditLogger:
    """Append-only JSONL audit logger for workspace mutations.

    Writes metadata-only records (no file content, no patch text,
    no old_string/new_string, no absolute host paths).

    IMPORTANT: log_path must NOT be inside any project root.
    The caller is responsible for choosing a safe audit path
    (e.g., /var/log/web-ssh-gateway/audit.jsonl or a temp dir).

    Each line is a JSON object with fields:
        receipt_id, project_id, relative_path, operation,
        before_hash, after_hash, size, timestamp, identity, success, error

    When log_path is None (in-memory only), entries are capped at
    _DEFAULT_MAX_AUDIT_ENTRIES (500) to prevent unbounded growth.
    Oldest entries are dropped when the cap is hit.

    This is a helper — rollback does NOT depend on audit.
    """

    def __init__(
        self,
        log_path: str | Path | None = None,
        max_in_memory_entries: int = _DEFAULT_MAX_AUDIT_ENTRIES,
    ):
        """Initialize the audit logger.

        Args:
            log_path: path to the JSONL log file. If None, logging is disabled
                     (entries are in-memory only). Must NOT be inside any
                     project root to avoid polluting workspace directories.
            max_in_memory_entries: max entries to keep when log_path is None.
                Prevents unbounded memory growth. Default 500.
        """
        self._log_path = Path(log_path) if log_path else None
        self._entries: list[dict[str, Any]] = []  # in-memory buffer
        self._max_in_memory = max_in_memory_entries if not log_path else 0

    def log(
        self,
        receipt_id: str,
        project_id: str,
        relative_path: str,
        operation: str,
        before_hash: str,
        after_hash: str,
        size: int,
        identity: str = "",
        success: bool = True,
        error: str = "",
    ) -> AuditEntry:
        """Append an audit entry.

        Args:
            receipt_id: snapshot/receipt identifier.
            project_id: registered project identifier.
            relative_path: project-relative path (never absolute).
            operation: operation type (write/edit/patch/rollback).
            before_hash: SHA-256 of content before operation.
            after_hash: SHA-256 of content after operation.
            size: content size in bytes.
            identity: caller fingerprint (opaque string, no secrets).
            success: whether the operation succeeded.
            error: error message if failed.

        Returns:
            The created AuditEntry.
        """
        entry = AuditEntry(
            receipt_id=receipt_id,
            project_id=project_id,
            relative_path=relative_path,
            operation=operation,
            before_hash=before_hash,
            after_hash=after_hash,
            size=size,
            timestamp=time.time(),
            identity=identity,
            success=success,
            error=error,
        )

        self._entries.append(
            {
                "receipt_id": entry.receipt_id,
                "project_id": entry.project_id,
                "relative_path": entry.relative_path,
                "operation": entry.operation,
                "before_hash": entry.before_hash,
                "after_hash": entry.after_hash,
                "size": entry.size,
                "timestamp": entry.timestamp,
                "identity": entry.identity,
                "success": entry.success,
                "error": entry.error,
            }
        )

        # Enforce memory cap when no log_path
        if self._max_in_memory and len(self._entries) > self._max_in_memory:
            self._entries = self._entries[-self._max_in_memory :]

        # Write to file if configured
        if self._log_path:
            try:
                line = json.dumps(
                    self._entries[-1], ensure_ascii=False, separators=(",", ":")
                )
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as exc:
                logger.warning("Failed to write audit log: %s", exc)

        return entry

    @property
    def entries(self) -> list[dict[str, Any]]:
        """In-memory audit entries (read-only copy)."""
        return list(self._entries)

    def clear(self) -> None:
        """Clear in-memory entries."""
        self._entries.clear()


# Backward-compatible alias
AuditLogger = WorkspaceAuditLogger
