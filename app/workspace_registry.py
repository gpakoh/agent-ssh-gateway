"""Compatibility shim — import from app.workspace.registry instead.

This file preserves backward compatibility for:
    from app.workspace_registry import WorkspaceRegistry, load_registry, ...
"""

from app.workspace.models import ProjectInfo, TreeNode  # noqa: F401
from app.workspace.registry import (  # noqa: F401
    VENDOR_CACHE_PATTERNS,
    WorkspaceRegistry,
    get_registry,
    get_registry_root,
    load_registry,
    reset_registry,
    set_registry_root,
)
from app.workspace.tools import (  # noqa: F401
    project_info,
    project_tree,
    workspace_list_projects,
)
