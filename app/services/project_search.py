"""Pure-Python project file search — no shell, no grep, no BusyBox dependency."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_PRUNE_DIRS = frozenset(
    {
        ".git",
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
        return _empty_result(query, root)

    root_path = Path(root).resolve()

    if not root_path.exists():
        raise ValueError(f"Root path does not exist: {root}")
    if not root_path.is_dir():
        raise ValueError(f"Root path is not a directory: {root}")

    matches: list[dict[str, Any]] = []
    files_read = 0
    truncated = False
    truncated_reason: str | None = None

    iter_pattern = glob if glob else "**/*"

    for p in root_path.rglob(iter_pattern):
        if not p.is_file():
            continue

        rel = p.relative_to(root_path)
        if any(part in _PRUNE_DIRS for part in rel.parts):
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
        "root": str(root_path),
        "count": len(matches),
        "matches": matches,
        "truncated": truncated,
    }
    if glob:
        result["glob"] = glob
    if truncated_reason:
        result["truncated_reason"] = truncated_reason

    return result


def _empty_result(query: str, root: str | Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "query": query,
        "root": str(Path(root).resolve()) if root else "",
        "count": 0,
        "matches": [],
        "truncated": False,
    }
    return result
