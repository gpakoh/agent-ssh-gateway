"""Read-only file inspection and discovery for workspace projects.

Provides project_file_read (read file content) and project_find_files
(glob-based file discovery). All access validated through WorkspacePolicy.
Hidden/secret/vendor/cache paths are filtered. Binary files rejected.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any

from app.workspace.policy import (
    HIDDEN_DIR_PATTERNS,
    SECRET_FILE_PATTERNS,
    WorkspacePolicyError,
)
from app.workspace.registry import VENDOR_CACHE_PATTERNS, WorkspaceRegistry, get_registry

logger = logging.getLogger(__name__)

# Default cap for binary detection before decode attempt
_BINARY_CHECK_BYTES = 512


def _is_hidden_or_secret_name(name: str) -> bool:
    """Check if a single filename matches hidden/secret patterns."""
    for pattern in SECRET_FILE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def _is_hidden_or_secret_path(relative: str) -> bool:
    """Check if any path component matches hidden/secret patterns."""
    parts = Path(relative).parts
    for part in parts:
        for pattern in HIDDEN_DIR_PATTERNS:
            if fnmatch.fnmatch(part, pattern):
                return True
    name = Path(relative).name
    for pattern in SECRET_FILE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def _is_vendor_or_cache(name: str) -> bool:
    """Check if a name matches vendor/cache directory patterns."""
    for pattern in VENDOR_CACHE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def _is_binary_path(path: Path, check_bytes: int = _BINARY_CHECK_BYTES) -> bool:
    """Detect binary files by reading a small prefix and checking for null bytes."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(check_bytes)
        return b"\x00" in chunk
    except OSError:
        return False


# ── Public tools ─────────────────────────────────────────────────


def project_file_read(
    project_id: str,
    relative_path: str,
    start_line: int | None = None,
    max_lines: int | None = None,
    max_bytes: int = 200_000,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Read a UTF-8/text file inside a project.

    Args:
        project_id: registered project identifier.
        relative_path: project-relative file path.
        start_line: 1-based first line to include (optional).
        max_lines: maximum number of lines to return (optional).
        max_bytes: byte limit before decoding (default 200KB).
        registry: optional WorkspaceRegistry instance.

    Returns:
        Dict with project_id, path, type, size, encoding, content,
        start_line, end_line, truncated.

    Raises:
        WorkspacePolicyError: unknown project, traversal, symlink escape.
        HiddenPathError: path matches hidden/secret patterns.
        WorkspacePolicyError: binary file detected.
    """
    r = registry or get_registry()
    full = r._policy.validate_read(project_id, relative_path)

    if not full.exists():
        raise WorkspacePolicyError(f"File not found: {relative_path}")
    if full.is_dir():
        raise WorkspacePolicyError(f"Path is a directory, not a file: {relative_path}")

    # Binary detection before full read
    if _is_binary_path(full):
        raise WorkspacePolicyError("Binary content is not returned")

    file_size = full.stat().st_size

    # Read only a bounded prefix. Do not call read_bytes(): workspace files may
    # be arbitrarily large and max_bytes is a response cap, not an allocation cap.
    with open(full, "rb") as f:
        raw = f.read(max_bytes + 1)
    truncated = len(raw) > max_bytes or file_size > max_bytes
    raw = raw[:max_bytes]

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspacePolicyError(
            f"Cannot decode file as UTF-8: {relative_path}"
        ) from exc

    # Apply optional line slicing
    lines = content.splitlines()
    total_lines = len(lines)

    if start_line is not None and start_line < 1:
        start_line = 1

    effective_start = (start_line - 1) if start_line is not None else 0
    effective_start = max(0, min(effective_start, total_lines))

    if max_lines is not None:
        sliced = lines[effective_start : effective_start + max_lines]
    else:
        sliced = lines[effective_start:]

    end_line = effective_start + len(sliced)

    return {
        "project_id": project_id,
        "path": relative_path,
        "type": "file",
        "size": file_size,
        "encoding": "utf-8",
        "content": "\n".join(sliced),
        "start_line": effective_start + 1,
        "end_line": end_line,
        "truncated": truncated,
    }


def project_find_files(
    project_id: str,
    pattern: str = "*",
    relative_path: str = "",
    max_results: int = 500,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Find files/directories by glob-like pattern inside a project.

    Args:
        project_id: registered project identifier.
        pattern: filename glob pattern (default "*").
        relative_path: project-relative directory to search (default: project root).
        max_results: hard cap on number of results (default 500).
        registry: optional WorkspaceRegistry instance.

    Returns:
        Dict with project_id, root, pattern, results (list of path/type/size),
        truncated flag.
    """
    r = registry or get_registry()
    search_rel = relative_path or "."
    full = r._policy.validate_read(project_id, search_rel)

    if not full.exists():
        raise WorkspacePolicyError(f"Search path does not exist: {relative_path}")
    if not full.is_dir():
        raise WorkspacePolicyError(f"Search path is not a directory: {relative_path}")

    results: list[dict[str, Any]] = []
    truncated = False

    def _walk_search(dir_path: Path) -> None:
        nonlocal truncated
        if truncated:
            return

        try:
            entries = sorted(dir_path.iterdir(), key=lambda e: e.name.lower())
        except PermissionError:
            logger.warning("Permission denied: %s", dir_path)
            return
        except OSError as exc:
            logger.warning("Error reading directory %s: %s", dir_path, exc)
            return

        for entry in entries:
            if truncated:
                return

            entry_name = entry.name

            # Rebuild relative path from the project root
            try:
                project_root = r._policy._resolve_project_root(project_id)
                rel_to_project = str(entry.relative_to(project_root))
            except (ValueError, WorkspacePolicyError):
                rel_to_project = entry_name

            if _is_hidden_or_secret_path(rel_to_project):
                continue

            # Skip vendor/cache
            if _is_vendor_or_cache(entry_name):
                continue

            # Skip symlink escapes
            if entry.is_symlink():
                try:
                    resolved = entry.resolve()
                    resolved.relative_to(project_root)
                except ValueError:
                    logger.debug("Skipping symlink escape: %s", entry)
                    continue

            # Check glob pattern against name
            if not fnmatch.fnmatch(entry_name, pattern):
                # If it's a directory, still recurse
                if entry.is_dir() and not entry.is_symlink():
                    _walk_search(entry)
                continue

            if len(results) >= max_results:
                truncated = True
                return

            try:
                stat = entry.stat()
                size = stat.st_size
            except OSError:
                size = 0

            entry_type = "symlink" if entry.is_symlink() else ("directory" if entry.is_dir() else "file")
            results.append({
                "path": rel_to_project,
                "type": entry_type,
                "size": size,
            })

            # Recurse into directories
            if entry.is_dir() and not entry.is_symlink():
                _walk_search(entry)

    _walk_search(full)

    return {
        "project_id": project_id,
        "root": relative_path,
        "pattern": pattern,
        "results": results,
        "truncated": truncated,
    }
