"""Workspace Control Plane — project inspection + file write/edit/patch."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.auth_middleware import AuthIdentity, require_master_key, require_scope
from app.state import _err
from app.workspace.edit import (
    PatchError,
    WriteError,
    project_apply_patch,
    project_file_edit,
    project_file_write,
)
from app.workspace.policy import (
    ALL_SCOPES,
    HiddenPathError,
    ScopeDeniedError,
    SymlinkEscapeError,
    TraversalError,
    WorkspacePolicyError,
)
from app.workspace.preview import (
    project_file_preview_edit,
    project_file_preview_patch,
    project_file_preview_write,
    project_file_verify,
)
from app.workspace.registry import WorkspaceRegistry, get_registry
from app.workspace.tools import (
    project_file_read,
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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workspace"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def registry_for_identity(identity: AuthIdentity) -> WorkspaceRegistry:
    """Return a WorkspaceRegistry scoped to the identity's granted scopes.

    Master tokens (``*``) receive ALL_SCOPES.
    Agent tokens receive exactly their token scopes — the scope hierarchy in
    ``WorkspacePolicy`` handles implication (``project:write`` implies
    ``project:read``, etc.).
    """
    base: WorkspaceRegistry = get_registry()
    if identity.token_type == "master" or "*" in identity.scopes:
        return WorkspaceRegistry(
            base._projects,
            base._allowed_roots,
            granted_scopes=ALL_SCOPES,
        )
    return WorkspaceRegistry(
        base._projects,
        base._allowed_roots,
        granted_scopes=set(identity.scopes),
    )


def _map_workspace_error(exc: Exception) -> HTTPException:
    """Map workspace tool exceptions to consistent HTTP errors (v3.1).

    +--------------------+------+
    | Case               | HTTP |
    +--------------------+------+
    | traversal          |  400 |
    | symlink            |  400 |
    | binary / invalid   |  400 |
    | hidden             |  403 |
    | scope denied       |  403 |
    | content too large  |  413 |
    | write failed (I/O) |  500 |
    | unknown / not found|  404 |
    +--------------------+------+
    """
    msg = str(exc)
    lower = msg.lower()

    # Content too large → 413 (regardless of exception type)
    if "exceeds maximum" in lower:
        return HTTPException(413, detail=_err(413, msg, code="CONTENT_TOO_LARGE"))

    # I/O write failure → 500 (server-side, not client error)
    if isinstance(exc, WriteError) and lower.startswith("write failed"):
        return HTTPException(500, detail=_err(500, msg, code="WRITE_FAILED"))

    # Not found → 404
    if isinstance(exc, (KeyError, WorkspacePolicyError)):
        if "unknown" in lower or "not found" in lower:
            return HTTPException(
                404, detail=_err(404, msg, code=type(exc).__name__.upper())
            )

    # Type-based mapping for remaining
    mapping: dict[type, int] = {
        SymlinkEscapeError: 400,
        TraversalError: 400,
        HiddenPathError: 403,
        ScopeDeniedError: 403,
        WriteError: 400,
        PatchError: 400,
        ValueError: 400,
        WorkspacePolicyError: 400,
    }
    for exc_type, status in mapping.items():
        if isinstance(exc, exc_type):
            return HTTPException(
                status, detail=_err(status, msg, code=type(exc).__name__.upper())
            )
    logger.exception("Unhandled workspace error")
    return HTTPException(500, detail=_err(500, "Internal server error"))


# ---------------------------------------------------------------------------
# Project listing and info
# ---------------------------------------------------------------------------


@router.get("/api/workspace/projects")
def list_projects(_identity: AuthIdentity = Depends(require_master_key)) -> dict[str, Any]:
    """List all registered workspace projects."""
    projects = workspace_list_projects()
    return {"projects": projects, "count": len(projects)}


@router.get("/api/workspace/projects/{project_id}")
def get_project_info(
    project_id: str, _identity: AuthIdentity = Depends(require_master_key)
) -> dict[str, Any]:
    """Get metadata for a single project."""
    try:
        return project_info(project_id)
    except (KeyError, WorkspacePolicyError):
        raise HTTPException(
            status_code=404, detail=_err(404, f"Project not found: {project_id}")
        ) from None


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------


@router.get("/api/workspace/projects/{project_id}/tree")
def get_project_tree(
    project_id: str,
    path: str = Query("", alias="path"),
    depth: int = Query(2, ge=0, le=10),
    max_nodes: int = Query(500, ge=1, le=5000),
    _identity: AuthIdentity = Depends(require_master_key),
) -> dict[str, Any]:
    """Get directory tree for a project."""
    try:
        return project_tree(
            project_id, relative_path=path, depth=depth, max_nodes=max_nodes
        )
    except (KeyError, WorkspacePolicyError):
        raise HTTPException(
            status_code=404, detail=_err(404, f"Project not found: {project_id}")
        ) from None


# ---------------------------------------------------------------------------
# File read
# ---------------------------------------------------------------------------


@router.get("/api/workspace/projects/{project_id}/files/read")
def read_file(
    project_id: str,
    path: str = Query(..., alias="path"),
    start_line: int | None = Query(None, ge=1),
    max_lines: int | None = Query(None, ge=1),
    max_bytes: int = Query(200_000, ge=1, le=1_000_000),
    _identity: AuthIdentity = Depends(require_master_key),
) -> dict[str, Any]:
    """Read a file inside a project."""
    try:
        return project_file_read(
            project_id,
            relative_path=path,
            start_line=start_line,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )
    except (KeyError, WorkspacePolicyError):
        raise HTTPException(
            status_code=404, detail=_err(404, f"Project not found: {project_id}")
        ) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=_err(400, str(exc))
        ) from exc


# ---------------------------------------------------------------------------
# File find
# ---------------------------------------------------------------------------


@router.get("/api/workspace/projects/{project_id}/files/find")
def find_files(
    project_id: str,
    pattern: str = Query("*"),
    path: str = Query("", alias="path"),
    max_results: int = Query(500, ge=1, le=5000),
    _identity: AuthIdentity = Depends(require_master_key),
) -> dict[str, Any]:
    """Find files by glob pattern inside a project."""
    try:
        return project_find_files(
            project_id,
            pattern=pattern,
            relative_path=path,
            max_results=max_results,
        )
    except (KeyError, WorkspacePolicyError):
        raise HTTPException(
            status_code=404, detail=_err(404, f"Project not found: {project_id}")
        ) from None


# ---------------------------------------------------------------------------
# Text search
# ---------------------------------------------------------------------------


@router.get("/api/workspace/projects/{project_id}/search")
def search_text(
    project_id: str,
    query: str = Query(..., min_length=1),
    path: str = Query("", alias="path"),
    file_glob: str = Query("**/*"),
    case_sensitive: bool = Query(False),
    context_lines: int = Query(2, ge=0, le=10),
    max_matches: int = Query(100, ge=1, le=1000),
    _identity: AuthIdentity = Depends(require_master_key),
) -> dict[str, Any]:
    """Search text content inside project files."""
    try:
        return project_search_text(
            project_id,
            query=query,
            relative_path=path,
            file_glob=file_glob,
            case_sensitive=case_sensitive,
            context_lines=context_lines,
            max_matches=max_matches,
        )
    except (KeyError, WorkspacePolicyError):
        raise HTTPException(
            status_code=404, detail=_err(404, f"Project not found: {project_id}")
        ) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=_err(400, str(exc))
        ) from exc


# ---------------------------------------------------------------------------
# Git read-only inspection
# ---------------------------------------------------------------------------


@router.get("/api/workspace/projects/{project_id}/git/status")
def git_status(
    project_id: str, _identity: AuthIdentity = Depends(require_master_key)
) -> dict[str, Any]:
    """Get git status for a project."""
    try:
        return project_git_status(project_id)
    except (KeyError, WorkspacePolicyError):
        raise HTTPException(
            status_code=404, detail=_err(404, f"Project not found: {project_id}")
        ) from None


@router.get("/api/workspace/projects/{project_id}/git/branch")
def git_branch(
    project_id: str, _identity: AuthIdentity = Depends(require_master_key)
) -> dict[str, Any]:
    """Get current git branch for a project."""
    try:
        return project_git_branch(project_id)
    except (KeyError, WorkspacePolicyError):
        raise HTTPException(
            status_code=404, detail=_err(404, f"Project not found: {project_id}")
        ) from None


@router.get("/api/workspace/projects/{project_id}/git/log")
def git_log(
    project_id: str,
    limit: int = Query(20, ge=1, le=100),
    path: str | None = Query(None, alias="path"),
    _identity: AuthIdentity = Depends(require_master_key),
) -> dict[str, Any]:
    """Get git log for a project."""
    try:
        return project_git_log(project_id, limit=limit, relative_path=path)
    except (KeyError, WorkspacePolicyError):
        raise HTTPException(
            status_code=404, detail=_err(404, f"Project not found: {project_id}")
        ) from None


@router.get("/api/workspace/projects/{project_id}/git/diff")
def git_diff(
    project_id: str,
    path: str | None = Query(None, alias="path"),
    staged: bool = Query(False),
    max_bytes: int = Query(200_000, ge=1, le=1_000_000),
    _identity: AuthIdentity = Depends(require_master_key),
) -> dict[str, Any]:
    """Get git diff for a project."""
    try:
        return project_git_diff(
            project_id, relative_path=path, staged=staged, max_bytes=max_bytes
        )
    except (KeyError, WorkspacePolicyError):
        raise HTTPException(
            status_code=404, detail=_err(404, f"Project not found: {project_id}")
        ) from None


# ---------------------------------------------------------------------------
# File write / edit / patch — require project:write scope
# ---------------------------------------------------------------------------


@router.post("/api/workspace/projects/{project_id}/files/write")
def write_file(
    project_id: str,
    path: str = Body(..., alias="path"),
    content: str = Body(...),
    _identity: AuthIdentity = Depends(require_scope("project:write")),
) -> dict[str, Any]:
    """Write (create or overwrite) a UTF-8 text file."""
    fp = _identity.fingerprint[:12]
    logger.info("write_file project=%s path=%s by=%s type=%s fp=%s",
                project_id, path, _identity.name, _identity.token_type, fp)
    try:
        registry = registry_for_identity(_identity)
        return project_file_write(
            project_id, path, content, registry=registry
        )
    except (KeyError, WorkspacePolicyError) as exc:
        raise _map_workspace_error(exc) from exc


@router.post("/api/workspace/projects/{project_id}/files/edit")
def edit_file(
    project_id: str,
    path: str = Body(..., alias="path"),
    old_string: str = Body(...),
    new_string: str = Body(...),
    _identity: AuthIdentity = Depends(require_scope("project:write")),
) -> dict[str, Any]:
    """Edit a file by replacing the first occurrence of old_string."""
    fp = _identity.fingerprint[:12]
    logger.info("edit_file project=%s path=%s by=%s type=%s fp=%s",
                project_id, path, _identity.name, _identity.token_type, fp)
    try:
        registry = registry_for_identity(_identity)
        return project_file_edit(
            project_id, path, old_string, new_string, registry=registry
        )
    except (KeyError, WorkspacePolicyError) as exc:
        raise _map_workspace_error(exc) from exc


@router.post("/api/workspace/projects/{project_id}/files/patch")
def patch_file(
    project_id: str,
    path: str = Body(..., alias="path"),
    patch: str = Body(...),
    _identity: AuthIdentity = Depends(require_scope("project:write")),
) -> dict[str, Any]:
    """Apply a unified diff patch to a file."""
    fp = _identity.fingerprint[:12]
    logger.info("patch_file project=%s path=%s by=%s type=%s fp=%s",
                project_id, path, _identity.name, _identity.token_type, fp)
    try:
        registry = registry_for_identity(_identity)
        return project_apply_patch(
            project_id, path, patch, registry=registry
        )
    except (KeyError, WorkspacePolicyError) as exc:
        raise _map_workspace_error(exc) from exc


# ---------------------------------------------------------------------------
# Preview / Verify — read-only, require master key
# ---------------------------------------------------------------------------


@router.post("/api/workspace/projects/{project_id}/files/preview/write")
def preview_write(
    project_id: str,
    path: str = Body(..., alias="path"),
    content: str = Body(...),
    max_bytes: int = Body(1_000_000, ge=1, le=2_000_000),
    _identity: AuthIdentity = Depends(require_scope("project:read")),
) -> dict[str, Any]:
    """Preview a file write without writing to disk."""
    try:
        registry = registry_for_identity(_identity)
        return project_file_preview_write(
            project_id, path, content, max_bytes=max_bytes, registry=registry
        )
    except (KeyError, WorkspacePolicyError) as exc:
        raise _map_workspace_error(exc) from exc


@router.post("/api/workspace/projects/{project_id}/files/preview/edit")
def preview_edit(
    project_id: str,
    path: str = Body(..., alias="path"),
    old_string: str = Body(...),
    new_string: str = Body(...),
    max_bytes: int = Body(1_000_000, ge=1, le=2_000_000),
    _identity: AuthIdentity = Depends(require_scope("project:read")),
) -> dict[str, Any]:
    """Preview a file edit without writing to disk."""
    try:
        registry = registry_for_identity(_identity)
        return project_file_preview_edit(
            project_id, path, old_string, new_string,
            max_bytes=max_bytes, registry=registry,
        )
    except (KeyError, WorkspacePolicyError) as exc:
        raise _map_workspace_error(exc) from exc


@router.post("/api/workspace/projects/{project_id}/files/preview/patch")
def preview_patch(
    project_id: str,
    path: str = Body(..., alias="path"),
    patch: str = Body(...),
    max_bytes: int = Body(1_000_000, ge=1, le=2_000_000),
    _identity: AuthIdentity = Depends(require_scope("project:read")),
) -> dict[str, Any]:
    """Preview a patch application without writing to disk."""
    try:
        registry = registry_for_identity(_identity)
        return project_file_preview_patch(
            project_id, path, patch, max_bytes=max_bytes, registry=registry
        )
    except (KeyError, WorkspacePolicyError) as exc:
        raise _map_workspace_error(exc) from exc


@router.post("/api/workspace/projects/{project_id}/files/verify")
def verify_file(
    project_id: str,
    path: str = Body(..., alias="path"),
    expected_hash: str = Body(...),
    _identity: AuthIdentity = Depends(require_scope("project:read")),
) -> dict[str, Any]:
    """Verify a file's current hash matches expected hash."""
    try:
        registry = registry_for_identity(_identity)
        return project_file_verify(
            project_id, path, expected_hash, registry=registry
        )
    except (KeyError, WorkspacePolicyError) as exc:
        raise _map_workspace_error(exc) from exc
