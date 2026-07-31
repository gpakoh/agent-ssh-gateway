"""Project structure scanning service (P19.2b).

Encapsulates the find-based directory scan, git-status overlay and tree
building shared by routers/files.py (project tree + structure) and
routers/context.py (project structure). One implementation so the two
routes cannot diverge.
"""

from __future__ import annotations

import shlex
from typing import Any

from app.models import FileMetadata

_STRUCTURE_CMD = (
    "cd {path} && find . -maxdepth {depth} -printf '%y|%p|%s|%m|%TY-%Tm-%Td %TH:%TM:%TS\\n' "
    "2>/dev/null || echo 'ERROR'"
)

_TREE_CMD = (
    "cd {path} && find . -maxdepth {depth} -not -path '*/\\.*' -not -path '*/node_modules/*' "
    "-not -path '*/__pycache__/*' -not -path '*/venv/*' -printf '%y|%p|%s\\n' "
    "2>/dev/null || echo 'ERROR'"
)

_GIT_STATUS_CMD = "cd {path} && git status --short 2>/dev/null || echo ''"


def _parse_file_lines(stdout: str) -> list[FileMetadata]:
    files = []
    for line in stdout.strip().split("\n"):
        if not line or line == "ERROR":
            continue

        parts = line.split("|", 4)
        if len(parts) < 5:
            continue

        file_type, path, size, permissions, mtime = parts
        path = path.lstrip("./")

        if not path:
            continue

        type_map = {"f": "file", "d": "directory", "l": "symlink"}
        file_type = type_map.get(file_type, "file")

        extension = None
        if "." in path and file_type == "file":
            extension = path.split(".")[-1]

        files.append(
            FileMetadata(
                name=path.split("/")[-1] if "/" in path else path,
                path=path,
                type=file_type,
                size=int(size) if size else 0,
                permissions=permissions,
                modified_at=mtime if mtime else None,
                extension=extension,
            )
        )
    return files


def _build_tree(files: list[FileMetadata]) -> dict[str, Any]:
    tree: dict[str, Any] = {"name": ".", "type": "directory", "children": {}}

    for file_meta in files:
        parts = file_meta.path.split("/")
        current = tree

        for i, part in enumerate(parts):
            if not part:
                continue

            if current.get("children") is None:
                current["children"] = {}

            if part not in current["children"]:
                current["children"][part] = {
                    "name": part,
                    "type": file_meta.type if i == len(parts) - 1 else "directory",
                    "children": {} if i < len(parts) - 1 else None,
                }

            current = current["children"][part]

    return tree


async def scan_project_structure(
    manager,
    session_id: str,
    path: str,
    max_depth: int,
    *,
    include_git_status: bool = False,
) -> tuple[list[FileMetadata], int, int, dict[str, Any]]:
    """Scan a directory into FileMetadata list, totals and a tree.

    Raises ValueError if the directory cannot be read.
    Returns (files, total_files, total_directories, tree).
    """
    cmd = _STRUCTURE_CMD.format(path=shlex.quote(path), depth=max_depth)
    result = await manager.execute(session_id, cmd, timeout=30)

    if result["exit_code"] != 0 or "ERROR" in result["stdout"]:
        raise ValueError(f"Cannot read directory: {result['stderr']}")

    files = _parse_file_lines(result["stdout"])

    total_files = sum(1 for f in files if f.type == "file")
    total_directories = sum(1 for f in files if f.type == "directory")

    if include_git_status:
        git_cmd = _GIT_STATUS_CMD.format(path=shlex.quote(path))
        git_result = await manager.execute(session_id, git_cmd, timeout=10)

        git_status_map = {}
        for line in git_result["stdout"].strip().split("\n"):
            if line and len(line) > 3:
                status = line[:2].strip()
                file_path = line[3:].strip()
                git_status_map[file_path] = status

        for file_meta in files:
            if file_meta.path in git_status_map:
                file_meta.git_status = git_status_map[file_meta.path]

    return files, total_files, total_directories, _build_tree(files)


async def scan_project_tree(
    manager,
    session_id: str,
    path: str,
    max_depth: int,
) -> list[dict[str, Any]]:
    """Scan a directory into a flat list of {type, path, size} items.

    Raises ValueError if the directory cannot be read.
    """
    cmd = _TREE_CMD.format(path=shlex.quote(path), depth=max_depth)
    result = await manager.execute(session_id, cmd, timeout=30)

    if result["exit_code"] != 0 or "ERROR" in result["stdout"]:
        raise ValueError(f"Cannot read directory: {result['stderr']}")

    items = []
    for line in result["stdout"].strip().split("\n"):
        if not line or line == "ERROR":
            continue
        parts = line.split("|", 3)
        if len(parts) < 3:
            continue

        ftype, fpath, fsize = parts
        fpath = fpath.lstrip("./")
        if not fpath:
            continue

        items.append(
            {
                "type": "directory" if ftype == "d" else "file",
                "path": fpath,
                "size": int(fsize) if fsize and ftype == "f" else None,
            }
        )

    return items
