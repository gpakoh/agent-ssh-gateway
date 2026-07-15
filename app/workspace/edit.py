"""Write/edit tools for workspace projects.

Provides project_file_write (create/overwrite), project_file_edit (search/replace),
and project_apply_patch (unified diff). All writes are validated through
WorkspacePolicy.validate_write, use symlink-safe preflight, and atomically
write via os.replace().
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from app.workspace.files import _is_binary_path
from app.workspace.policy import SymlinkEscapeError, WorkspacePolicyError
from app.workspace.registry import WorkspaceRegistry, get_registry

logger = logging.getLogger(__name__)

# Default cap for write operations
_WRITE_MAX_BYTES = 1_000_000


# ── Exceptions ──────────────────────────────────────────────────


class WriteError(WorkspacePolicyError):
    """Raised when a write operation fails due to content or policy."""


# ── Shared helpers ───────────────────────────────────────────────


def _exact_read(
    file_path: Path, max_bytes: int = _WRITE_MAX_BYTES
) -> tuple[str, int]:
    """Read an entire text file, rejecting files that exceed *max_bytes*.

    Unlike ``project_file_read`` (which truncates), this helper rejects
    files larger than *max_bytes*.  Used by edit/patch tools that need
    full content for safe mutation.

    Returns:
        Tuple of (content: str, size: int).

    Raises:
        WorkspacePolicyError: file not found, is a directory, binary,
            exceeds max_bytes, or not valid UTF-8.
    """
    if not file_path.exists():
        raise WorkspacePolicyError(f"File not found: {file_path}")
    if file_path.is_dir():
        raise WorkspacePolicyError(f"Path is a directory, not a file: {file_path}")

    if _is_binary_path(file_path):
        raise WorkspacePolicyError("Binary content is not returned")

    file_size = file_path.stat().st_size
    if file_size > max_bytes:
        raise WorkspacePolicyError(
            f"File size {file_size} exceeds maximum of {max_bytes} bytes"
        )

    raw = file_path.read_bytes()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspacePolicyError(
            f"Cannot decode file as UTF-8: {file_path}"
        ) from exc

    return content, file_size


def _symlink_safe_preflight(target_path: Path, project_root: Path) -> None:
    """Check that no component in *target_path* is a symlink.

    Walks every path component from *project_root* to the final target.
    If any existing component is a symlink the write is rejected.

    This is stricter than ``validate_write``'s resolve-time check: it
    catches intermediate symlinks that resolve within the project root.

    Raises:
        SymlinkEscapeError: any path component is a symlink.
        WorkspacePolicyError: target is outside project_root.
    """
    try:
        relative = target_path.relative_to(project_root)
    except ValueError as exc:
        raise WorkspacePolicyError(
            f"Path {target_path} is not under project root {project_root}"
        ) from exc

    partial = project_root
    for part in relative.parts:
        partial = partial / part
        if partial.exists() and partial.is_symlink():
            raise SymlinkEscapeError(
                f"Symlink component rejected: {partial} is a symlink"
            )


def _atomic_write(target_path: Path, content_bytes: bytes) -> None:
    """Write *content_bytes* to *target_path* atomically.

    Uses a unique temp file with exclusive creation (``O_EXCL``) to prevent
    symlink-follow attacks on the temp path, then ``os.replace()`` on the
    same filesystem.  If the write fails the temp file is cleaned up and the
    original target is never touched.

    Raises:
        WriteError: parent directory missing, write failed.
    """
    if not target_path.parent.exists():
        raise WriteError(
            f"Parent directory does not exist: {target_path.parent}"
        )

    # Unique, unpredictable name in the same directory
    unique_suffix = f".{uuid.uuid4().hex}.tmp"
    tmp_path = target_path.parent / (target_path.name + unique_suffix)

    created_tmp = False
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        created_tmp = True
        with os.fdopen(fd, "wb") as f:
            f.write(content_bytes)

        os.replace(tmp_path, target_path)
    except WriteError:
        raise
    except Exception as exc:
        if created_tmp and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise WriteError(f"Write failed: {exc}") from exc


def _make_diff(old_content: str, new_content: str, file_label: str = "") -> str:
    """Compute a unified diff string between two text contents.

    Uses ``a/`` and ``b/`` prefixes (git-style) on the file labels.

    Returns empty string when there is no difference.
    """
    old_label = f"a/{file_label}" if file_label else ""
    new_label = f"b/{file_label}" if file_label else ""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(old_lines, new_lines, old_label, new_label, n=3)
    )
    return "".join(diff_lines)


# ── Public tools ─────────────────────────────────────────────────


def project_file_write(
    project_id: str,
    relative_path: str,
    content: str,
    max_bytes: int = _WRITE_MAX_BYTES,
    registry: WorkspaceRegistry | None = None,
    safe: bool = False,
) -> dict[str, Any]:
    """Write (create or overwrite) a UTF-8 text file inside a project.

    Args:
        project_id: registered project identifier.
        relative_path: project-relative file path.
        content: UTF-8 text content to write.
        max_bytes: maximum allowed content size (default 1 MB).
        registry: optional WorkspaceRegistry instance.
        safe: if True, include change receipt in response.

    Returns:
        Dict with project_id, path, size, encoding.
        If safe=True, includes nested receipt dict.

    Raises:
        WriteError: content exceeds max_bytes, binary content, or write failed.
        HiddenPathError: write to hidden/secret path denied.
        SymlinkEscapeError: any path component is a symlink.
        ScopeDeniedError: project:write scope required.
        TraversalError: path traversal detected.
        WorkspacePolicyError: path is a directory or parent dir missing.
    """
    r = registry or get_registry()
    full = r._policy.validate_write(project_id, relative_path)
    project_root = r._policy._resolve_project_root(project_id)

    if full.exists() and full.is_dir():
        raise WorkspacePolicyError(
            f"Path is a directory, not a file: {relative_path}"
        )

    _symlink_safe_preflight(full, project_root)

    utf8_bytes = content.encode("utf-8")

    if len(utf8_bytes) > max_bytes:
        raise WriteError(
            f"Content exceeds maximum size of {max_bytes} bytes"
        )

    if b"\x00" in utf8_bytes:
        raise WriteError("Binary content is not allowed")

    before_content = None
    if safe:
        from app.workspace.receipts import read_file_bytes
        raw, _ = read_file_bytes(full)
        if raw is not None:
            before_content = raw.decode("utf-8", errors="replace")

    _atomic_write(full, utf8_bytes)

    result: dict[str, Any] = {
        "project_id": project_id,
        "path": relative_path,
        "size": len(utf8_bytes),
        "encoding": "utf-8",
    }

    if safe:
        from app.workspace.receipts import make_receipt
        receipt = make_receipt(
            project_id=project_id,
            relative_path=relative_path,
            operation="write",
            file_path=full,
            before_content=before_content,
            after_content=content,
        )
        result["receipt"] = receipt.to_dict()

    return result


def project_file_edit(
    project_id: str,
    relative_path: str,
    old_string: str,
    new_string: str,
    max_bytes: int = _WRITE_MAX_BYTES,
    registry: WorkspaceRegistry | None = None,
    safe: bool = False,
) -> dict[str, Any]:
    """Edit a file by replacing the first occurrence of old_string with new_string.

    Args:
        project_id: registered project identifier.
        relative_path: project-relative file path.
        old_string: literal string to find and replace (must not be empty).
        new_string: replacement string.
        max_bytes: maximum file size in bytes (default 1 MB).
        registry: optional WorkspaceRegistry instance.
        safe: if True, include change receipt in response.

    Returns:
        Dict with project_id, path, size, encoding, old_string, new_string,
        diff, replaced. If safe=True, includes nested receipt dict.

    Raises:
        WriteError: old_string empty, not found, content exceeds max.
        HiddenPathError: write to hidden/secret path denied.
        SymlinkEscapeError: any path component is a symlink.
        ScopeDeniedError: project:write scope required.
        TraversalError: path traversal detected.
    """
    if not old_string:
        raise WriteError("old_string must not be empty")

    r = registry or get_registry()
    full = r._policy.validate_write(project_id, relative_path)
    project_root = r._policy._resolve_project_root(project_id)

    _symlink_safe_preflight(full, project_root)

    old_content, file_size = _exact_read(full, max_bytes)

    if old_string not in old_content:
        raise WriteError("old_string not found in file content")

    if old_string == new_string:
        from app.workspace.receipts import compute_hash

        result: dict[str, Any] = {
            "project_id": project_id,
            "path": relative_path,
            "size": file_size,
            "encoding": "utf-8",
            "old_string": old_string,
            "new_string": new_string,
            "diff": "",
            "replaced": False,
        }
        if safe:
            result["receipt"] = {
                "before_hash": compute_hash(old_content),
                "after_hash": compute_hash(old_content),
                "size_before": file_size,
                "size_after": file_size,
                "changed": False,
                "verified": True,
                "diff_summary": "edit: no change",
            }
        return result

    new_content = old_content.replace(old_string, new_string, 1)
    new_bytes = new_content.encode("utf-8")

    if len(new_bytes) > max_bytes:
        raise WriteError(
            f"Content after edit exceeds maximum of {max_bytes} bytes"
        )

    diff = _make_diff(old_content, new_content, relative_path)

    _atomic_write(full, new_bytes)

    result = {
        "project_id": project_id,
        "path": relative_path,
        "size": len(new_bytes),
        "encoding": "utf-8",
        "old_string": old_string,
        "new_string": new_string,
        "diff": diff,
        "replaced": True,
    }

    if safe:
        from app.workspace.receipts import make_receipt

        receipt = make_receipt(
            project_id=project_id,
            relative_path=relative_path,
            operation="edit",
            file_path=full,
            before_content=old_content,
            after_content=new_content,
        )
        result["receipt"] = receipt.to_dict()

    return result


# ── Patch helpers ────────────────────────────────────────────────


class PatchError(WorkspacePolicyError):
    """Raised when a patch cannot be applied."""


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")


def _parse_unified_diff(patch_text: str) -> list[dict[str, Any]]:
    """Parse a unified diff into a list of hunks.

    Each hunk dict has:
        - old_start: int
        - old_lines: list[str]  (context + removed, without +/- prefix)
        - new_lines: list[str]  (context + added, without +/- prefix)
        - removed: list[str]    (lines with - prefix)
        - added: list[str]      (lines with + prefix)
        - context: list[str]    (lines with no prefix)
    """
    lines = patch_text.splitlines()
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in lines:
        m = _HUNK_HEADER_RE.match(line)
        if m:
            if current is not None:
                hunks.append(current)
            current = {
                "old_start": int(m.group(1)),
                "old_lines": [],
                "new_lines": [],
                "removed": [],
                "added": [],
                "context": [],
            }
            continue

        if current is None:
            continue

        if line.startswith("-"):
            content = line[1:]
            current["old_lines"].append(content)
            current["removed"].append(content)
        elif line.startswith("+"):
            content = line[1:]
            current["new_lines"].append(content)
            current["added"].append(content)
        elif line.startswith(" ") or line == "":
            content = line[1:] if line.startswith(" ") else ""
            current["old_lines"].append(content)
            current["new_lines"].append(content)
            current["context"].append(content)

    if current is not None:
        hunks.append(current)

    return hunks


def _apply_hunks(old_lines: list[str], hunks: list[dict[str, Any]]) -> list[str]:
    """Apply hunks to old_lines and return new_lines.

    Raises PatchError if any hunk context doesn't match.
    """
    result = list(old_lines)
    offset = 0

    for i, hunk in enumerate(hunks):
        old_start = hunk["old_start"] - 1 + offset  # 0-based
        old_lines_hunk = hunk["old_lines"]
        new_lines_hunk = hunk["new_lines"]

        # Validate context lines match
        for j, expected in enumerate(old_lines_hunk):
            idx = old_start + j
            if idx < 0 or idx >= len(result):
                raise PatchError(
                    f"Hunk {i + 1}: context line {j + 1} out of range"
                )
            if result[idx] != expected:
                raise PatchError(
                    f"Hunk {i + 1}: context line {j + 1} mismatch: "
                    f"expected {expected!r}, got {result[idx]!r}"
                )

        # Replace the old lines with new lines
        result[old_start : old_start + len(old_lines_hunk)] = new_lines_hunk
        offset += len(new_lines_hunk) - len(old_lines_hunk)

    return result


def _compute_backup_hash(content: str) -> str:
    """Compute SHA-256 hash of content for audit."""
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ── Public tool ──────────────────────────────────────────────────


def project_apply_patch(
    project_id: str,
    relative_path: str,
    patch: str,
    max_bytes: int = _WRITE_MAX_BYTES,
    registry: WorkspaceRegistry | None = None,
    safe: bool = False,
) -> dict[str, Any]:
    """Apply a unified diff patch to a file inside a project.

    Args:
        project_id: registered project identifier.
        relative_path: project-relative file path.
        patch: unified diff text (single file).
        max_bytes: maximum file size in bytes (default 1 MB).
        registry: optional WorkspaceRegistry instance.
        safe: if True, include change receipt in response.

    Returns:
        Dict with project_id, path, size, encoding, applied, backup_hash.
        If safe=True, includes nested receipt dict.

    Raises:
        PatchError: patch format invalid, hunk conflict, file not found.
        WorkspacePolicyError: unknown project, traversal, symlink escape.
        HiddenPathError: write to hidden/secret path denied.
        ScopeDeniedError: project:write scope required.
    """
    r = registry or get_registry()
    full = r._policy.validate_write(project_id, relative_path)
    project_root = r._policy._resolve_project_root(project_id)

    _symlink_safe_preflight(full, project_root)

    old_content, file_size = _exact_read(full, max_bytes)

    # Compute backup hash before any mutation
    backup_hash = _compute_backup_hash(old_content)

    # Parse the patch
    hunks = _parse_unified_diff(patch)
    if not hunks:
        raise PatchError("No hunks found in patch")

    # Apply hunks
    old_lines = old_content.splitlines()
    try:
        new_lines = _apply_hunks(old_lines, hunks)
    except PatchError:
        raise
    except Exception as exc:
        raise PatchError(f"Failed to apply patch: {exc}") from exc

    # Preserve trailing newline if original had one
    new_content = "\n".join(new_lines)
    if old_content.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"

    new_bytes = new_content.encode("utf-8")

    if len(new_bytes) > max_bytes:
        raise WriteError(
            f"Patched content exceeds maximum of {max_bytes} bytes"
        )

    _atomic_write(full, new_bytes)

    result: dict[str, Any] = {
        "project_id": project_id,
        "path": relative_path,
        "size": len(new_bytes),
        "encoding": "utf-8",
        "applied": True,
        "backup_hash": backup_hash,
    }

    if safe:
        from app.workspace.receipts import make_receipt

        receipt = make_receipt(
            project_id=project_id,
            relative_path=relative_path,
            operation="patch",
            file_path=full,
            before_content=old_content,
            after_content=new_content,
        )
        result["receipt"] = receipt.to_dict()

    return result
