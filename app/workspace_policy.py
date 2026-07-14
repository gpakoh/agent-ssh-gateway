"""Compatibility shim — import from app.workspace.policy instead.

This file preserves backward compatibility for:
    from app.workspace_policy import WorkspacePolicy, WorkspacePolicyError, ...
"""

from app.workspace.policy import (  # noqa: F401
    ALL_SCOPES,
    HIDDEN_DIR_PATTERNS,
    SCOPE_IMPLIES,
    SECRET_FILE_PATTERNS,
    SYSTEM_FORBIDDEN,
    HiddenPathError,
    ScopeDeniedError,
    SymlinkEscapeError,
    TraversalError,
    WorkspacePolicy,
    WorkspacePolicyError,
)
