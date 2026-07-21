"""Batch command execution routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.command_policy import evaluate_command_policy, parse_key_profiles, profile_for_identity
from app.config import settings
from app.metrics import metrics
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

    # Command Policy Evaluation — check execute-type operations before execution
    source_ip = request.client.host if request.client else "unknown"
    key_profiles = parse_key_profiles(settings.command_policy_key_profiles)
    effective_profile = profile_for_identity(
        _identity.fingerprint[:12] if _identity else None,
        key_profiles=key_profiles,
        default_profile=settings.command_policy_profile,
    )
    for op in req.operations:
        if op.type == "execute" and op.command:
            decision = evaluate_command_policy(
                op.command,
                mode=settings.command_policy_mode,
                profile=effective_profile,
            )
            _state.audit_logger.log_security_event(
                "COMMAND_POLICY_DECISION",
                f"batch_execute; command_root={decision.command_root}; "
                f"allowed={decision.allowed}; reason={decision.reason}; "
                f"profile={decision.profile}; mode={decision.mode}",
                source_ip,
            )

            # Structured audit event
            from app.audit import emit_command_policy_decision as _emit_batch
            _emit_batch(
                event_logger=_state.event_audit_logger,
                command=op.command,
                session_id=getattr(req, "context_id", ""),
                effective_profile=effective_profile,
                decision_allowed=decision.allowed,
                decision_reason=decision.reason,
                command_root=decision.command_root,
                source_ip=source_ip,
                route="POST /api/batch/execute",
                actor_fingerprint=_identity.fingerprint[:12] if _identity else "",
                request_id=getattr(request.state, "request_id", ""),
            )

            if not decision.allowed:
                metrics.record_ssh_command(
                    status="denied",
                    profile=decision.profile,
                    command_root=decision.command_root,
                )
                raise HTTPException(
                    status_code=403,
                    detail=_err(403, f"Command denied by policy: {decision.reason}"),
                )

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

    # Record metrics for execute-type operations that passed policy
    for op in req.operations:
        if op.type == "execute" and op.command:
            metrics.record_ssh_command(
                status="allowed",
                profile=effective_profile,
                command_root=None,
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
