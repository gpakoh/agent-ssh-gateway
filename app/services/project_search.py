"""Pure-Python project file search — no shell, no grep, no BusyBox dependency."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

from app.workspace.policy import is_hidden_or_secret_path

_PRUNE_DIRS = frozenset(
    {
        ".git",
        ".ai-bridge",
        ".hypothesis",
        ".state",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".benchmarks",
        "dist",
        "build",
        ".coverage",
        "htmlcov",
    }
)

_MAX_FILES_DEFAULT = 5000
_MAX_MATCHES_DEFAULT = 200
_MAX_FILE_SIZE_BYTES_DEFAULT = 2_000_000


def _match_glob_parts(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return not path_parts
    head = pattern_parts[0]
    if head == "**":
        return _match_glob_parts(path_parts, pattern_parts[1:]) or (
            bool(path_parts)
            and _match_glob_parts(path_parts[1:], pattern_parts)
        )
    return bool(path_parts) and fnmatch.fnmatchcase(path_parts[0], head) and _match_glob_parts(
        path_parts[1:], pattern_parts[1:]
    )


def _matches_rglob_pattern(relative_path: Path, pattern: str) -> bool:
    pattern_parts = tuple(part for part in pattern.split("/") if part not in {"", "."})
    return _match_glob_parts(relative_path.parts, ("**", *pattern_parts))


def _walk_project_files(root_path: Path):
    """Yield files while pruning generated/runtime directories before descent."""
    for dirpath, dirnames, filenames in os.walk(root_path, topdown=True, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _PRUNE_DIRS and not name.endswith(".egg-info")
        )
        base = Path(dirpath)
        for name in sorted(filenames):
            yield base / name


def _is_binary(path: Path) -> bool:
    """Detect binary file by scanning first 4096 bytes for a null byte."""
    try:
        with path.open("rb") as f:
            return b"\0" in f.read(4096)
    except OSError:
        return True


def search_text(
    root: str | Path,
    query: str,
    *,
    glob: str | None = None,
    max_files: int = _MAX_FILES_DEFAULT,
    max_matches: int = _MAX_MATCHES_DEFAULT,
    max_file_size_bytes: int = _MAX_FILE_SIZE_BYTES_DEFAULT,
) -> dict[str, Any]:
    if not query:
        return _empty_result(query)

    root_path = Path(root).resolve()

    if not root_path.exists():
        raise ValueError(f"Root path does not exist: {root}")
    if not root_path.is_dir():
        raise ValueError(f"Root path is not a directory: {root}")

    matches: list[dict[str, Any]] = []
    files_read = 0
    truncated = False
    truncated_reason: str | None = None

    for p in _walk_project_files(root_path):
        if not p.is_file():
            continue

        rel = p.relative_to(root_path)
        if glob and not _matches_rglob_pattern(rel, glob):
            continue

        # Resolve the file before reading so a symlinked file cannot escape
        # the registered project root. Directory symlinks are never followed
        # by _walk_project_files().
        try:
            p.resolve().relative_to(root_path)
        except (OSError, ValueError):
            continue

        if any(part in _PRUNE_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue

        if is_hidden_or_secret_path(str(rel)):
            continue

        if p.stat().st_size > max_file_size_bytes:
            continue
        if _is_binary(p):
            continue

        if files_read >= max_files:
            truncated = True
            truncated_reason = "max_files"
            break

        files_read += 1

        try:
            text = p.read_text("utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue

        for i, line in enumerate(text.splitlines(), 1):
            if query not in line:
                continue
            if len(matches) >= max_matches:
                truncated = True
                truncated_reason = "max_matches"
                break

            matches.append(
                {
                    "path": str(rel),
                    "line_number": i,
                    "line": line,
                    "preview": line.strip(),
                }
            )

        if truncated:
            break

    result: dict[str, Any] = {
        "query": query,
        "count": len(matches),
        "matches": matches,
        "truncated": truncated,
    }
    if glob:
        result["glob"] = glob
    if truncated_reason:
        result["truncated_reason"] = truncated_reason

    return result


def _empty_result(query: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "query": query,
        "count": 0,
        "matches": [],
        "truncated": False,
    }
    return result
