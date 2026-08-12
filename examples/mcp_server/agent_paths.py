"""Operator-controlled paths for agent coordination and managed workspaces.

The source checkout is an input, not a coordination database.  Production can
set ``MCP_AGENT_STATE_ROOT`` to a persistent writable directory visible on the
SSH executor; task plans, status, evidence and runner-local caches then live
there instead of under ``<source>/.ai-bridge``.

The legacy relative layout is retained only when the environment variable is
unset so local development/tests keep working during the migration.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import PurePosixPath

_STATE_ROOT_ENV = "MCP_AGENT_STATE_ROOT"
_WORKSPACE_ROOT_ENV = "MCP_AGENT_WORKSPACE_ROOT"
_PROJECT_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _configured_root(env_name: str) -> str | None:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    if "\x00" in raw:
        raise ValueError(f"{env_name} contains a NUL byte")
    path = PurePosixPath(raw)
    if not path.is_absolute():
        raise ValueError(f"{env_name} must be an absolute executor path")
    normalized = str(path)
    if normalized == "/":
        raise ValueError(f"{env_name} must not be filesystem root")
    return normalized.rstrip("/")


def project_state_key(project: str) -> str:
    """Return a stable, path-safe key without trusting the project name."""
    if not isinstance(project, str) or not project.strip():
        raise ValueError("project must be a non-empty string")
    raw = project.strip()
    slug = _PROJECT_SLUG_RE.sub("-", raw).strip("-._")[:48] or "project"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def task_tasks_dir(project: str) -> str:
    root = _configured_root(_STATE_ROOT_ENV)
    if root is None:
        return ".ai-bridge/tasks"
    return f"{root}/{project_state_key(project)}/tasks"


def task_archive_dir(project: str) -> str:
    root = _configured_root(_STATE_ROOT_ENV)
    if root is None:
        return ".ai-bridge/archive"
    return f"{root}/{project_state_key(project)}/archive"


def task_dir(project: str, task_id: str) -> str:
    return f"{task_tasks_dir(project)}/{task_id}"


def task_archive_path(project: str, task_id: str) -> str:
    return f"{task_archive_dir(project)}/{task_id}"


def managed_workspace_paths(project: str, task_id: str) -> tuple[str, str] | None:
    """Return (bare mirror, task worktree) for the executor-managed plane.

    No caller-provided path is accepted here.  Both paths are derived from an
    operator-controlled absolute root plus validated project/task identities.
    """
    root = _configured_root(_WORKSPACE_ROOT_ENV)
    if root is None:
        return None
    key = project_state_key(project)
    return (
        f"{root}/repos/{key}.git",
        f"{root}/tasks/{key}/{task_id}",
    )
