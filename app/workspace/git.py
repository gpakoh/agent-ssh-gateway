"""Read-only git inspection tools for workspace projects.

All git commands use fixed argv, shell=False, timeout=10, and GIT_TERMINAL_PROMPT=0.
Network-capable git operations (fetch, pull, push, remote) are excluded.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from app.workspace.policy import WorkspacePolicy
from app.workspace.registry import WorkspaceRegistry, get_registry

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────

_GIT_TIMEOUT = 10
_SAFE_ENV: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/usr/local/bin",
}


# ── Internal helpers ─────────────────────────────────────────────


def _git_run(
    argv: list[str],
    cwd: Path,
    *,
    timeout: int = _GIT_TIMEOUT,
    max_bytes: int = 200_000,
) -> dict[str, Any]:
    """Run a fixed git command. Returns {stdout, stderr, returncode}."""
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            timeout=timeout,
            env=_SAFE_ENV,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        truncated = len(stdout.encode("utf-8")) > max_bytes
        if truncated:
            stdout = stdout[:max_bytes]
        return {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": result.returncode,
            "truncated": truncated,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"git command timed out after {timeout}s",
            "returncode": -1,
            "truncated": False,
        }
    except FileNotFoundError:
        return {
            "stdout": "",
            "stderr": "git binary not found",
            "returncode": -1,
            "truncated": False,
        }
    except OSError as exc:
        return {
            "stdout": "",
            "stderr": f"os error: {exc}",
            "returncode": -1,
            "truncated": False,
        }


def _is_git_repo(project_root: Path) -> bool:
    """Check if the project root is inside a git repository."""
    result = _git_run(["git", "rev-parse", "--git-dir"], project_root)
    return result["returncode"] == 0


def _validate_relative_path(
    policy: WorkspacePolicy,
    project_id: str,
    relative_path: str | None,
) -> str | None:
    """Validate an optional relative path for git commands.

    Returns the validated path, or None if input was None.
    Raises WorkspacePolicyError on traversal/escape.
    """
    if relative_path is None or relative_path == "":
        return None
    # Use validate_read to check traversal, symlinks, and allowed roots
    policy.validate_read(project_id, relative_path)
    return relative_path


# ── Public tools ─────────────────────────────────────────────────


def project_git_status(
    project_id: str,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Return git status (porcelain + branch) for a project.

    Returns:
        {
            "project_id": "...",
            "is_git_repo": True,
            "branch": "main",
            "ahead": 0,
            "behind": 0,
            "staged": [...],
            "unstaged": [...],
            "untracked": [...],
            "truncated": False,
        }

    Non-git projects return {"project_id": "...", "is_git_repo": False}.
    """
    r = registry or get_registry()
    project_root = r._policy._resolve_project_root(project_id)

    if not _is_git_repo(project_root):
        return {"project_id": project_id, "is_git_repo": False}

    result = _git_run(
        ["git", "status", "--porcelain=v1", "--branch"],
        project_root,
    )

    if result["returncode"] != 0:
        return {
            "project_id": project_id,
            "is_git_repo": True,
            "error": result["stderr"].strip(),
        }

    lines = result["stdout"].strip().splitlines()
    branch_line = lines[0] if lines else ""

    # Parse branch info: "## main...origin/main [ahead 1, behind 2]"
    branch = ""
    ahead = 0
    behind = 0
    if branch_line.startswith("## "):
        info = branch_line[3:]
        branch = info.split("...")[0] if "..." in info else info.split(" ")[0]
        if "ahead " in info:
            try:
                ahead = int(info.split("ahead ")[1].split(",")[0].split("]")[0])
            except (ValueError, IndexError):
                pass
        if "behind " in info:
            try:
                behind = int(info.split("behind ")[1].split(",")[0].split("]")[0])
            except (ValueError, IndexError):
                pass

    staged: list[dict[str, str]] = []
    unstaged: list[dict[str, str]] = []
    untracked: list[str] = []

    for line in lines[1:]:
        if len(line) < 4:
            continue
        index_status = line[0]
        work_status = line[1]
        filename = line[3:]

        if index_status == "?" and work_status == "?":
            untracked.append(filename)
        else:
            if index_status != " " and index_status != "?":
                staged.append({"status": index_status, "path": filename})
            if work_status != " " and work_status != "?":
                unstaged.append({"status": work_status, "path": filename})

    return {
        "project_id": project_id,
        "is_git_repo": True,
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "truncated": result["truncated"],
    }


def project_git_branch(
    project_id: str,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Return current branch name for a project.

    Returns:
        {"project_id": "...", "is_git_repo": True, "branch": "main"}
        or {"project_id": "...", "is_git_repo": False}
    """
    r = registry or get_registry()
    project_root = r._policy._resolve_project_root(project_id)

    if not _is_git_repo(project_root):
        return {"project_id": project_id, "is_git_repo": False}

    result = _git_run(["git", "branch", "--show-current"], project_root)

    if result["returncode"] != 0:
        return {
            "project_id": project_id,
            "is_git_repo": True,
            "error": result["stderr"].strip(),
        }

    branch = result["stdout"].strip()
    return {
        "project_id": project_id,
        "is_git_repo": True,
        "branch": branch,
    }


def project_git_log(
    project_id: str,
    limit: int = 20,
    relative_path: str | None = None,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Return recent git log entries for a project.

    Args:
        limit: max commits (capped at 100).
        relative_path: optional path filter (validated, appended after --).

    Returns:
        {"project_id": "...", "is_git_repo": True, "commits": [...], "truncated": False}
    """
    r = registry or get_registry()
    project_root = r._policy._resolve_project_root(project_id)

    if not _is_git_repo(project_root):
        return {"project_id": project_id, "is_git_repo": False}

    limit = max(1, min(limit, 100))

    argv = ["git", "log", f"--max-count={limit}", "--format=%H|%s|%an|%ai"]
    validated_path = _validate_relative_path(r._policy, project_id, relative_path)
    if validated_path:
        argv.extend(["--", validated_path])

    result = _git_run(argv, project_root)

    if result["returncode"] != 0:
        return {
            "project_id": project_id,
            "is_git_repo": True,
            "error": result["stderr"].strip(),
        }

    commits: list[dict[str, str]] = []
    for line in result["stdout"].strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "sha": parts[0],
                "subject": parts[1],
                "author": parts[2],
                "date": parts[3],
            })

    return {
        "project_id": project_id,
        "is_git_repo": True,
        "commits": commits,
        "truncated": result["truncated"],
    }


def project_git_diff(
    project_id: str,
    relative_path: str | None = None,
    staged: bool = False,
    max_bytes: int = 200_000,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Return git diff for a project.

    Args:
        relative_path: optional path filter (validated, appended after --).
        staged: if True, diff staged changes (--staged).
        max_bytes: output cap.

    Returns:
        {"project_id": "...", "is_git_repo": True, "diff": "...", "truncated": False}
    """
    r = registry or get_registry()
    project_root = r._policy._resolve_project_root(project_id)

    if not _is_git_repo(project_root):
        return {"project_id": project_id, "is_git_repo": False}

    argv = ["git", "diff"]
    if staged:
        argv.append("--staged")

    validated_path = _validate_relative_path(r._policy, project_id, relative_path)
    if validated_path:
        argv.extend(["--", validated_path])

    result = _git_run(argv, project_root, max_bytes=max_bytes)

    if result["returncode"] != 0:
        return {
            "project_id": project_id,
            "is_git_repo": True,
            "error": result["stderr"].strip(),
        }

    return {
        "project_id": project_id,
        "is_git_repo": True,
        "diff": result["stdout"],
        "truncated": result["truncated"],
    }
