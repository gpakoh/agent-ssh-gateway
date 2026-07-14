"""Workspace package — multi-project workspace security, registry, and tools.

Public API re-exported here for convenience:
    from app.workspace import WorkspacePolicy, WorkspaceRegistry, project_tree
"""

from app.workspace.models import ProjectInfo, TreeNode
from app.workspace.policy import (
    ALL_SCOPES,
    HiddenPathError,
    ScopeDeniedError,
    SymlinkEscapeError,
    TraversalError,
    WorkspacePolicy,
    WorkspacePolicyError,
)
from app.workspace.registry import (
    WorkspaceRegistry,
    load_registry,
    reset_registry,
)
from app.workspace.tools import (
    project_info,
    project_tree,
    workspace_list_projects,
)

__all__ = [
    "ALL_SCOPES",
    "HiddenPathError",
    "ProjectInfo",
    "ScopeDeniedError",
    "SymlinkEscapeError",
    "TreeNode",
    "TraversalError",
    "WorkspacePolicy",
    "WorkspacePolicyError",
    "WorkspaceRegistry",
    "load_registry",
    "project_info",
    "project_tree",
    "reset_registry",
    "workspace_list_projects",
]
