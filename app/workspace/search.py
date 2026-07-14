"""Literal text search for workspace projects.

All searches are scoped to project_id and validated by WorkspacePolicy.
Binary files, hidden/secret paths, and vendor/cache dirs are skipped.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any

from app.workspace.registry import VENDOR_CACHE_PATTERNS, get_registry

logger = logging.getLogger(__name__)

_DEFAULT_MAX_MATCHES = 100
_DEFAULT_MAX_BYTES_PER_FILE = 1_000_000
_DEFAULT_CONTEXT_LINES = 2


class SearchError(Exception):
    """Raised when a search operation fails."""


def project_search_text(
    project_id: str,
    query: str,
    relative_path: str = "",
    file_glob: str = "**/*",
    case_sensitive: bool = False,
    context_lines: int = _DEFAULT_CONTEXT_LINES,
    max_matches: int = _DEFAULT_MAX_MATCHES,
    max_bytes_per_file: int = _DEFAULT_MAX_BYTES_PER_FILE,
    registry: Any = None,
) -> dict[str, Any]:
    """Search for literal text in project files.

    Args:
        project_id: registered project identifier.
        query: literal text to search for (must not be empty).
        relative_path: subdirectory to search within (default: project root).
        file_glob: glob pattern for files to include (default: **/*).
        case_sensitive: whether search is case-sensitive.
        context_lines: lines of context before/after each match.
        max_matches: maximum total matches before truncation.
        max_bytes_per_file: skip files larger than this.
        registry: optional WorkspaceRegistry (uses singleton if None).

    Returns:
        dict with keys:
            - project_id: str
            - query: str
            - case_sensitive: bool
            - matches: list of match dicts
            - truncated: bool

    Raises:
        SearchError: empty query.
        WorkspacePolicyError: unknown project, traversal, symlink escape.
    """
    if not query:
        raise SearchError("Search query must not be empty")

    r = registry or get_registry()
    policy = r._policy

    project_root = policy._resolve_project_root(project_id)

    search_root = project_root
    if relative_path:
        search_root = policy.validate_search(project_id, relative_path)
        if not search_root.exists():
            raise SearchError(f"Search path does not exist: {relative_path}")
        if not search_root.is_dir():
            raise SearchError(f"Search path is not a directory: {relative_path}")

    matches: list[dict[str, Any]] = []
    truncated = False

    try:
        for file_path in search_root.glob(file_glob):
            if not file_path.is_file():
                continue

            try:
                rel = str(file_path.relative_to(project_root))
            except ValueError:
                continue

            if _is_excluded(rel, file_path, project_root, policy):
                continue

            try:
                if file_path.stat().st_size > max_bytes_per_file:
                    continue
            except OSError:
                continue

            if _is_binary(file_path):
                continue

            remaining = max_matches - len(matches)
            file_matches, file_truncated = _search_file(
                file_path=file_path,
                rel_path=rel,
                query=query,
                case_sensitive=case_sensitive,
                context_lines=context_lines,
                max_matches=remaining,
            )
            matches.extend(file_matches)

            if file_truncated or len(matches) >= max_matches:
                truncated = True
                break

    except PermissionError as exc:
        logger.warning("Permission denied during search: %s", exc)
    except OSError as exc:
        logger.warning("Error during search: %s", exc)

    return {
        "project_id": project_id,
        "query": query,
        "case_sensitive": case_sensitive,
        "matches": matches,
        "truncated": truncated,
    }


def _is_excluded(
    rel: str,
    file_path: Path,
    project_root: Path,
    policy: Any,
) -> bool:
    """Check if a file should be excluded from search."""
    if policy._is_hidden_or_secret(rel):
        return True
    if _is_vendor_or_cache(file_path.name):
        return True
    if _is_in_excluded_dir(file_path, project_root):
        return True
    return False


def _is_in_excluded_dir(file_path: Path, project_root: Path) -> bool:
    """Check if any parent component matches vendor/cache patterns."""
    try:
        rel = file_path.relative_to(project_root)
    except ValueError:
        return False
    for part in rel.parts[:-1]:
        if _is_vendor_or_cache(part):
            return True
    return False


def _search_file(
    file_path: Path,
    rel_path: str,
    query: str,
    case_sensitive: bool,
    context_lines: int,
    max_matches: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Search a single file for literal text matches.

    Returns (matches, truncated) where truncated means more matches
    existed in this file but were not returned.
    """
    matches: list[dict[str, Any]] = []

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return matches, False

    lines = content.splitlines()
    search_query = query if case_sensitive else query.lower()
    truncated = False

    for i, line in enumerate(lines):
        if len(matches) >= max_matches:
            truncated = True
            break

        search_line = line if case_sensitive else line.lower()
        col = search_line.find(search_query)
        if col == -1:
            continue

        start = max(0, i - context_lines)
        end = min(len(lines), i + context_lines + 1)

        context_before = lines[start:i]
        context_after = lines[i + 1 : end]

        matches.append({
            "path": rel_path,
            "line": i + 1,
            "column": col + 1,
            "preview": line.rstrip(),
            "before": [ln.rstrip() for ln in context_before],
            "after": [ln.rstrip() for ln in context_after],
        })

    return matches, truncated


def _is_binary(file_path: Path) -> bool:
    """Check if a file is binary by reading a small chunk."""
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except (OSError, PermissionError):
        return True


def _is_vendor_or_cache(name: str) -> bool:
    """Check if a name matches vendor/cache patterns."""
    for pattern in VENDOR_CACHE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False
