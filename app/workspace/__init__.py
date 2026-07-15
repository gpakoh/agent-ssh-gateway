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
from app.workspace.preview import (
    project_file_preview_edit,
    project_file_preview_patch,
    project_file_preview_write,
    project_file_verify,
)
from app.workspace.receipts import ChangeReceipt, compute_hash, make_receipt
from app.workspace.registry import (
    WorkspaceRegistry,
    load_registry,
    reset_registry,
)
from app.workspace.tools import (
    project_apply_patch,
    project_file_edit,
    project_file_read,
    project_file_write,
    project_find_files,
    project_git_branch,
    project_git_diff,
    project_git_log,
    project_git_status,
    project_info,
    project_search_text,
    project_tree,
    workspace_list_projects,
)

__all__ = [
    "ALL_SCOPES",
    "ChangeReceipt",
    "HiddenPathError",
    "ProjectInfo",
    "ScopeDeniedError",
    "SymlinkEscapeError",
    "TreeNode",
    "TraversalError",
    "WorkspacePolicy",
    "WorkspacePolicyError",
    "WorkspaceRegistry",
    "compute_hash",
    "load_registry",
    "make_receipt",
    "project_apply_patch",
    "project_file_edit",
    "project_file_preview_edit",
    "project_file_preview_patch",
    "project_file_preview_write",
    "project_file_read",
    "project_file_verify",
    "project_file_write",
    "project_find_files",
    "project_git_branch",
    "project_git_diff",
    "project_git_log",
    "project_git_status",
    "project_info",
    "project_search_text",
    "project_tree",
    "reset_registry",
    "workspace_list_projects",
]
