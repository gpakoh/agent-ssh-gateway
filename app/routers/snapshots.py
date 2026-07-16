"""Snapshot management routes."""

from fastapi import APIRouter, Depends, HTTPException

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.config import settings
from app.models import (
    CreateSnapshotRequest,
    RestoreSnapshotRequest,
    SnapshotActionResponse,
    SnapshotInfo,
    SnapshotListResponse,
)
from app.state import _err

router = APIRouter()


def _assert_rw() -> None:
    """Raise 403 if workspace is in readonly mode."""
    if settings.workspace_readonly:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail=_err(403, "WORKSPACE_READONLY: write operations are disabled"),
        )


@router.post("/api/snapshots", tags=["snapshots"], response_model=SnapshotActionResponse)
async def create_snapshot(
    req: CreateSnapshotRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Create a snapshot of current project state."""
    _assert_rw()
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))

    try:
        snapshot = await _state.snapshot_manager.create_snapshot(
            session_id=ctx.session_id,
            context_id=req.context_id,
            name=req.name,
            description=req.description,
        )

        return SnapshotActionResponse(
            success=True,
            message=f"Snapshot '{snapshot.name}' created with {len(snapshot.files)} files",
            snapshot_id=snapshot.id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=_err(500, f"Snapshot creation failed: {exc}")
        ) from exc


@router.get("/api/snapshots", tags=["snapshots"])
async def list_snapshots(context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """List all snapshots for context."""
    ctx = await _state.context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))

    snapshots = await _state.snapshot_manager.list_snapshots(ctx.session_id, context_id)

    return SnapshotListResponse(
        snapshots=[
            SnapshotInfo(
                id=s.id,
                name=s.name,
                context_id=s.context_id,
                created_at=s.created_at,
                files=s.files,
                description=s.description,
                git_commit_before=s.git_commit_before,
                size_bytes=s.size_bytes,
            )
            for s in snapshots
        ],
        count=len(snapshots),
    )


@router.post("/api/snapshots/restore", tags=["snapshots"], response_model=SnapshotActionResponse)
async def restore_snapshot(
    req: RestoreSnapshotRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Restore project from snapshot."""
    _assert_rw()
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))

    try:
        result = await _state.snapshot_manager.restore_snapshot(
            session_id=ctx.session_id,
            context_id=req.context_id,
            snapshot_id=req.snapshot_id,
        )

        return SnapshotActionResponse(
            success=result["success"],
            message=f"Restored {result['restored_files']} of {result['total_files']} files",
            snapshot_id=req.snapshot_id,
            restored_files=result["restored_files"],
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_err(500, f"Restore failed: {exc}")) from exc


@router.delete("/api/snapshots/{snapshot_id}", tags=["snapshots"])
async def delete_snapshot(
    snapshot_id: str, context_id: str, _identity: AuthIdentity = Depends(require_master_key)
):
    """Delete a snapshot."""
    _assert_rw()
    ctx = await _state.context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))

    success = await _state.snapshot_manager.delete_snapshot(
        session_id=ctx.session_id,
        context_id=context_id,
        snapshot_id=snapshot_id,
    )

    return {"status": "deleted" if success else "not_found", "snapshot_id": snapshot_id}
