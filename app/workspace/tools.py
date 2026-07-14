"""Read-only workspace tools — list projects, project info, project tree.

These are the Phase A foundation tools. No file read, edit, search, or git.
All filesystem access goes through WorkspacePolicy for path containment
and secret filtering.
"""

from __future__ import annotations

from typing import Any

from app.workspace.registry import WorkspaceRegistry, get_registry


def workspace_list_projects(registry: WorkspaceRegistry | None = None) -> list[dict[str, Any]]:
    """List all registered projects with type, description, and tags."""
    r = registry or get_registry()
    return r.list_projects()


def project_info(project_id: str, registry: WorkspaceRegistry | None = None) -> dict[str, Any]:
    """Return detailed metadata for a single project.

    Raises WorkspacePolicyError if the project_id is unknown.
    """
    r = registry or get_registry()
    return r.project_info(project_id)


def project_tree(
    project_id: str,
    relative_path: str = "",
    depth: int = 3,
    max_nodes: int = 500,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Return a file tree for a project, with secret and vendor filtering.

    Args:
        project_id: registered project identifier.
        relative_path: path within the project (default: project root).
        depth: nesting depth (0 = current dir only).
        max_nodes: max entries per directory level.

    Raises:
        WorkspacePolicyError: unknown project, traversal, symlink escape.
    """
    r = registry or get_registry()
    return r.project_tree(project_id, relative_path, depth, max_nodes)
