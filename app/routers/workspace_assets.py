"""Binary asset transport for website candidate workspaces.

One discoverable REST/OpenAPI group; zero new MCP tools. Clients upload raw
bytes rather than base64, with a digest and explicit previous revision lease.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.auth_middleware import AuthIdentity, require_scope
from app.routers.workspace import (
    _map_workspace_error,
    assert_workspace_writable,
    registry_for_identity,
)
from app.state import _err
from app.workspace.assets import (
    ASSET_TYPES,
    MAX_ASSET_BYTES,
    AssetConflict,
    AssetError,
    _candidate_path,
    inspect_asset,
    publish_asset,
)
from app.workspace.policy import WorkspacePolicyError

router = APIRouter(
    prefix="/api/workspace/projects/{project_id}/assets",
    tags=["workspace-assets"],
)


@router.get("/inspect")
def inspect_candidate_asset(
    project_id: str,
    path: str = Query(..., min_length=1),
    identity: AuthIdentity = Depends(require_scope("project:read")),
) -> dict[str, Any]:
    """Return file digest, bytes, MIME type; no file content or host paths."""
    try:
        return inspect_asset(registry_for_identity(identity), project_id, path)
    except (KeyError, WorkspacePolicyError) as exc:
        raise _asset_error(exc) from exc


def _asset_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AssetConflict):
        return HTTPException(409, detail=_err(409, str(exc), code="ASSET_CONFLICT"))
    if isinstance(exc, AssetError) and "exceeds maximum" in str(exc):
        return HTTPException(413, detail=_err(413, str(exc), code="ASSET_TOO_LARGE"))
    if isinstance(exc, WorkspacePolicyError) and "not found" in str(exc).lower():
        return HTTPException(404, detail=_err(404, "Asset not found", code="ASSET_NOT_FOUND"))
    return _map_workspace_error(exc)


@router.put("/upload")
async def upload_candidate_asset(
    project_id: str,
    request: Request,
    path: str = Query(..., min_length=1),
    sha256: str = Query(..., min_length=64, max_length=64),
    previous_sha256: str | None = Query(None, min_length=64, max_length=64),
    create_parents: bool = Query(False),
    identity: AuthIdentity = Depends(require_scope("project:write")),
) -> dict[str, Any]:
    """Upload <=8 MiB raw bytes; overwrite requires exact old-file SHA-256."""
    assert_workspace_writable(
        actor_type=identity.token_type,
        actor_name=identity.name or "",
        actor_fingerprint=identity.fingerprint[:12],
        route="PUT /api/workspace/projects/*/assets/upload",
        request_id=getattr(request.state, "request_id", ""),
    )
    registry = registry_for_identity(identity)
    try:
        full = _candidate_path(registry, project_id, path, write=True)
        media_type = ASSET_TYPES[full.suffix.lower()]
        request_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if request_type not in {media_type, "application/octet-stream"}:
            raise AssetError("Content-Type does not match the requested binary format")
    except (KeyError, WorkspacePolicyError) as exc:
        raise _asset_error(exc) from exc

    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isascii() or not declared.isdecimal():
            raise HTTPException(400, detail=_err(400, "Invalid Content-Length"))
        if int(declared) > MAX_ASSET_BYTES:
            raise HTTPException(413, detail=_err(413, "Asset exceeds maximum upload size"))

    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > MAX_ASSET_BYTES:
            raise HTTPException(413, detail=_err(413, "Asset exceeds maximum upload size"))
        chunks.extend(chunk)
    try:
        return publish_asset(
            registry, project_id, path, bytes(chunks), sha256,
            previous_sha256=previous_sha256, create_parents=create_parents,
        )
    except (KeyError, WorkspacePolicyError) as exc:
        raise _asset_error(exc) from exc


@router.get("/download")
def download_candidate_asset(
    project_id: str,
    path: str = Query(..., min_length=1),
    identity: AuthIdentity = Depends(require_scope("project:read")),
) -> Response:
    """Small bounded private preview download for visual comparison."""
    registry = registry_for_identity(identity)
    try:
        meta = inspect_asset(registry, project_id, path)
        target = _candidate_path(registry, project_id, path, write=False)
        content = target.read_bytes()
        if (len(content) != meta["size"] or hashlib.sha256(content).hexdigest() != meta["sha256"]):
            raise AssetConflict("Asset changed during download")
    except (KeyError, WorkspacePolicyError) as exc:
        raise _asset_error(exc) from exc
    return Response(
        content=content,
        media_type=meta["media_type"],
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "ETag": '"sha256-' + meta["sha256"] + '"',
        },
    )
