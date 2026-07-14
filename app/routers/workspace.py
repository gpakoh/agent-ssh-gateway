"""Workspace Control Plane — read-only project inspection routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth_middleware import AuthIdentity, require_master_key
from app.state import _err
from app.workspace.policy import WorkspacePolicyError
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
