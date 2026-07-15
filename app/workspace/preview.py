"""Preview and verify tools for workspace projects.

Preview tools compute what a write/edit/patch would produce without
writing to disk. Verify reads a file and compares its SHA-256 hash.

All operations are read-only: no disk mutations, no side effects.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.workspace.edit import (
    PatchError,
    _apply_hunks,
    _compute_backup_hash,
    _exact_read,
    _make_diff,
    _parse_unified_diff,
    _symlink_safe_preflight,
)
from app.workspace.policy import WorkspacePolicyError
from app.workspace.registry import WorkspaceRegistry, get_registry

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BYTES = 1_000_000


# ── Preview tools ────────────────────────────────────────────────


def project_file_preview_write(
    project_id: str,
    relative_path: str,
    content: str,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Preview a file write without writing to disk.

    Returns diff, hashes, and size changes. No disk mutation.

    Args:
        project_id: registered project identifier.
        relative_path: project-relative file path.
        content: UTF-8 text content to write.
        max_bytes: maximum allowed content size (default 1 MB).
        registry: optional WorkspaceRegistry instance.

    Returns:
        Dict with before_hash, after_hash, size_before, size_after,
        diff, changed, file_exists_before, encoding.
    """
    r = registry or get_registry()
    full = r._policy.validate_read(project_id, relative_path)
    project_root = r._policy._resolve_project_root(project_id)

    _symlink_safe_preflight(full, project_root)

    # Reject if target is an existing directory
    if full.exists() and full.is_dir():
        raise WorkspacePolicyError(
            f"Path is a directory, not a file: {relative_path}"
        )

    # Check parent directory exists
    if not full.parent.exists():
        raise WorkspacePolicyError(
            f"Parent directory does not exist: {full.parent}"
        )

    # Read before state
    file_exists = full.exists() and full.is_file()
    if file_exists:
        old_content, old_size = _exact_read(full, max_bytes)
    else:
        old_content = None
        old_size = 0

    # Validate new content
    new_bytes = content.encode("utf-8")
    if len(new_bytes) > max_bytes:
        raise WorkspacePolicyError(
            f"Content exceeds maximum size of {max_bytes} bytes"
        )
    if b"\x00" in new_bytes:
        raise WorkspacePolicyError("Binary content is not allowed")

    new_size = len(new_bytes)
    after_hash = _compute_backup_hash(content)

    if old_content is None:
        before_hash = None
        diff = _make_diff("", content, relative_path)
    else:
        before_hash = _compute_backup_hash(old_content)
        diff = _make_diff(old_content, content, relative_path)

    changed = old_content != content

    return {
        "project_id": project_id,
        "path": relative_path,
        "file_exists_before": file_exists,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "size_before": old_size,
        "size_after": new_size,
        "diff": diff,
        "changed": changed,
        "encoding": "utf-8",
    }


def project_file_preview_edit(
    project_id: str,
    relative_path: str,
    old_string: str,
    new_string: str,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Preview a file edit without writing to disk.

    Returns diff, hashes, and size changes. No disk mutation.

    Args:
        project_id: registered project identifier.
        relative_path: project-relative file path.
        old_string: literal string to find and replace (must not be empty).
        new_string: replacement string.
        max_bytes: maximum file size in bytes (default 1 MB).
        registry: optional WorkspaceRegistry instance.

    Returns:
        Dict with before_hash, after_hash, size_before, size_after,
        diff, changed, replaced, encoding.
    """
    if not old_string:
        raise WorkspacePolicyError("old_string must not be empty")

    r = registry or get_registry()
    full = r._policy.validate_read(project_id, relative_path)
    project_root = r._policy._resolve_project_root(project_id)

    _symlink_safe_preflight(full, project_root)

    if not full.exists():
        raise WorkspacePolicyError(f"File not found: {relative_path}")
    if full.is_dir():
        raise WorkspacePolicyError(f"Path is a directory: {relative_path}")

    old_content, old_size = _exact_read(full, max_bytes)

    if old_string not in old_content:
        raise WorkspacePolicyError("old_string not found in file content")

    if old_string == new_string:
        after_hash = _compute_backup_hash(old_content)
        return {
            "project_id": project_id,
            "path": relative_path,
            "file_exists_before": True,
            "before_hash": _compute_backup_hash(old_content),
            "after_hash": after_hash,
            "size_before": old_size,
            "size_after": old_size,
            "diff": "",
            "changed": False,
            "replaced": False,
            "encoding": "utf-8",
        }

    new_content = old_content.replace(old_string, new_string, 1)
    new_bytes = new_content.encode("utf-8")

    if len(new_bytes) > max_bytes:
        raise WorkspacePolicyError(
            f"Content after edit exceeds maximum of {max_bytes} bytes"
        )

    diff = _make_diff(old_content, new_content, relative_path)
    after_hash = _compute_backup_hash(new_content)

    return {
        "project_id": project_id,
        "path": relative_path,
        "file_exists_before": True,
        "before_hash": _compute_backup_hash(old_content),
        "after_hash": after_hash,
        "size_before": old_size,
        "size_after": len(new_bytes),
        "diff": diff,
        "changed": True,
        "replaced": True,
        "encoding": "utf-8",
    }


def project_file_preview_patch(
    project_id: str,
    relative_path: str,
    patch: str,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Preview a patch application without writing to disk.

    Returns diff, hashes, and size changes. No disk mutation.

    Args:
        project_id: registered project identifier.
        relative_path: project-relative file path.
        patch: unified diff text (single file).
        max_bytes: maximum file size in bytes (default 1 MB).
        registry: optional WorkspaceRegistry instance.

    Returns:
        Dict with before_hash, after_hash, size_before, size_after,
        diff, changed, applied, encoding.
    """
    r = registry or get_registry()
    full = r._policy.validate_read(project_id, relative_path)
    project_root = r._policy._resolve_project_root(project_id)

    _symlink_safe_preflight(full, project_root)

    if not full.exists():
        raise WorkspacePolicyError(f"File not found: {relative_path}")
    if full.is_dir():
        raise WorkspacePolicyError(f"Path is a directory: {relative_path}")

    old_content, old_size = _exact_read(full, max_bytes)

    hunks = _parse_unified_diff(patch)
    if not hunks:
        raise PatchError("No hunks found in patch")

    old_lines = old_content.splitlines()
    try:
        new_lines = _apply_hunks(old_lines, hunks)
    except PatchError:
        raise
    except Exception as exc:
        raise PatchError(f"Failed to apply patch: {exc}") from exc

    new_content = "\n".join(new_lines)
    if old_content.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"

    new_bytes = new_content.encode("utf-8")
    if len(new_bytes) > max_bytes:
        raise WorkspacePolicyError(
            f"Patched content exceeds maximum of {max_bytes} bytes"
        )

    diff = _make_diff(old_content, new_content, relative_path)
    after_hash = _compute_backup_hash(new_content)
    changed = old_content != new_content

    return {
        "project_id": project_id,
        "path": relative_path,
        "file_exists_before": True,
        "before_hash": _compute_backup_hash(old_content),
        "after_hash": after_hash,
        "size_before": old_size,
        "size_after": len(new_bytes),
        "diff": diff,
        "changed": changed,
        "applied": changed,
        "encoding": "utf-8",
    }


# ── Verify tool ──────────────────────────────────────────────────


def project_file_verify(
    project_id: str,
    relative_path: str,
    expected_hash: str,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Verify a file's current hash matches expected hash.

    Args:
        project_id: registered project identifier.
        relative_path: project-relative file path.
        expected_hash: expected SHA-256 hash (e.g. "sha256:abc...").
        registry: optional WorkspaceRegistry instance.

    Returns:
        Dict with project_id, path, matches, current_hash, file_exists.
    """
    r = registry or get_registry()
    full = r._policy.validate_read(project_id, relative_path)

    file_exists = full.exists() and full.is_file()

    if not file_exists:
        return {
            "project_id": project_id,
            "path": relative_path,
            "matches": False,
            "current_hash": None,
            "file_exists": False,
        }

    # Read file and compute hash
    try:
        raw = full.read_bytes()
        current_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    except (OSError, PermissionError):
        return {
            "project_id": project_id,
            "path": relative_path,
            "matches": False,
            "current_hash": None,
            "file_exists": True,
        }

    matches = current_hash == expected_hash

    return {
        "project_id": project_id,
        "path": relative_path,
        "matches": matches,
        "current_hash": current_hash,
        "file_exists": True,
    }
