"""Batch command execution routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.models import (
    BatchExecuteRequest,
    BatchExecuteResponse,
    BatchOperationResultResponse,
)
from app.state import _err

router = APIRouter()


@router.post("/api/batch/execute", tags=["files"], response_model=BatchExecuteResponse)
async def batch_execute(
    req: BatchExecuteRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Execute multiple file operations in a single transaction."""

    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))

    operations = []
    for op in req.operations:
        op_dict = {
            "type": op.type,
            "path": op.path,
            "continue_on_error": op.continue_on_error,
        }
        if op.operations:
            op_dict["operations"] = [o.model_dump() for o in op.operations]
        if op.content:
            op_dict["content"] = op.content
        if op.new_path:
            op_dict["new_path"] = op.new_path
        if op.dest_path:
            op_dict["dest_path"] = op.dest_path
        if op.command:
            op_dict["command"] = op.command
        operations.append(op_dict)

    result = await _state.batch_manager.execute_batch(
        session_id=ctx.session_id,
        context_id=req.context_id,
        operations=operations,
        auto_commit=req.auto_commit,
        commit_message=req.commit_message,
        run_validation=req.run_validation,
        transaction_id=str(uuid.uuid4())[:8],
    )

    return BatchExecuteResponse(
        transaction_id=result.transaction_id,
        overall_success=result.overall_success,
        summary=result.summary,
        total_duration=result.total_duration,
        operations=[
            BatchOperationResultResponse(
                operation=op.operation,
                path=op.path,
                success=op.success,
                output=op.output,
                error=op.error,
                duration=op.duration,
                lines_changed=op.lines_changed,
            )
            for op in result.operations
        ],
        git_commit=result.git_commit,
        validation_result=result.validation_result,
    )
