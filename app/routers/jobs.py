"""Job, bulk, and batch execute routes."""

import asyncio
import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import state as _state
from app.access_control import AccessDeniedError, AccessPendingApprovalError
from app.auth_middleware import AuthIdentity, get_client_ip, parse_cidrs, require_scope
from app.command_policy import evaluate_command_policy, parse_key_profiles, profile_for_identity
from app.config import settings
from app.exceptions import JobNotFoundError, PermissionDeniedError
from app.metrics import metrics
from app.models import (
    BulkExecuteRequest,
    BulkExecuteResponse,
    BulkExecuteResult,
    JobListResponse,
    JobResultResponse,
    JobRunRequest,
    JobRunResponse,
    JobStatusResponse,
)
from app.output_redaction import should_redact_command_output
from app.security import rate_limit_mutation, redact_secrets
from app.state import _err

router = APIRouter(tags=["jobs"])

_GATEWAY_WORKERS = os.environ.get("GATEWAY_WORKERS", "1")


@router.post("/api/jobs/run", response_model=JobRunResponse)
@rate_limit_mutation(20, "minute")
async def jobs_run(
    req: JobRunRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("jobs:run")),
):
    """Start a background job on an SSH session."""
    session = await _state.manager.get_session(req.session_id)
    if not session:
        raise HTTPException(
            status_code=404, detail=_err(404, f"Session {req.session_id} not found")
        )

    # Access control gate
    access_effective_profile = None
    if settings.access_control_enabled and _state.access_control_store is not None:
        source_ip = get_client_ip(request, parse_cidrs(settings.trusted_proxy_cidrs))
        try:
            access = _state.access_control_store.resolve_access_policy(
                actor_fingerprint=_identity.fingerprint,
                token_type=_identity.token_type,
                source_ip=source_ip,
                requested_profile=settings.command_policy_profile,
                enforce_master=settings.access_control_enforce_master,
            )
            access_effective_profile = access.effective_profile
        except AccessDeniedError:
            raise HTTPException(status_code=403, detail=_err(403, "ACCESS_DENIED")) from None
        except AccessPendingApprovalError:
            pass

    # Command Policy Evaluation — server-owned profile resolution
    key_profiles = parse_key_profiles(settings.command_policy_key_profiles)
    effective_profile = (
        access_effective_profile
        if access_effective_profile is not None
        else profile_for_identity(
            _identity.fingerprint[:12] if _identity else None,
            key_profiles=key_profiles,
            default_profile=settings.command_policy_profile,
        )
    )
    decision = evaluate_command_policy(
        req.command,
        mode=settings.command_policy_mode,
        profile=effective_profile,
    )
    source_ip = request.client.host if request.client else "unknown"
    _state.audit_logger.log_security_event(
        "COMMAND_POLICY_DECISION",
        f"session_id={req.session_id}; command_root={decision.command_root}; "
        f"allowed={decision.allowed}; reason={decision.reason}; "
        f"profile={decision.profile}; mode={decision.mode}",
        source_ip,
    )

    # Structured audit event
    from app.audit import emit_command_policy_decision as _emit_jobs
    _emit_jobs(
        event_logger=_state.event_audit_logger,
        command=req.command,
        session_id=req.session_id,
        effective_profile=effective_profile,
        decision_allowed=decision.allowed,
        decision_reason=decision.reason,
        command_root=decision.command_root,
        source_ip=source_ip,
        route="POST /api/jobs/run",
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

    job_id = await _state.job_manager.create_job(
        session_id=req.session_id,
        command=req.command,
    )
    metrics.record_ssh_command(
        status="allowed",
        profile=decision.profile,
        command_root=decision.command_root,
    )
    return JobRunResponse(job_id=job_id)


@router.get("/api/jobs/{job_id}/status", response_model=JobStatusResponse)
async def jobs_status(job_id: str, _identity: AuthIdentity = Depends(require_scope("jobs:read"))):
    """Get job status."""
    status = await _state.job_manager.get_job_status(job_id)
    return JobStatusResponse(**status)


@router.get("/api/jobs/{job_id}/result", response_model=JobResultResponse)
async def jobs_result(
    job_id: str,
    redact_output: bool | None = Query(
        default=None,
        description="Override command output redaction for this response.",
    ),
    _identity: AuthIdentity = Depends(require_scope("jobs:read")),
):
    """Get full job result."""
    result = await _state.job_manager.get_job_result(job_id)
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    if should_redact_command_output(redact_output):
        stdout = redact_secrets(stdout)
        stderr = redact_secrets(stderr)
    return JobResultResponse(
        **{**result, "stdout": stdout, "stderr": stderr},
    )


@router.get("/api/jobs/queue/stats")
async def jobs_queue_stats(_identity: AuthIdentity = Depends(require_scope("jobs:read"))):
    """Get Redis job queue statistics."""
    if not _state.redis_queue or not _state.redis_queue._redis:
        return {"error": "Redis not available"}

    stats = await _state.redis_queue.get_queue_stats()
    metrics.update_queue_depth(
        pending=stats.get("pending", 0),
        processing=stats.get("processing", 0),
        dead=stats.get("dead_letter", 0),
    )
    return stats


@router.get("/api/jobs/queue/dead")
async def jobs_dead_letter(
    limit: int = 100, _identity: AuthIdentity = Depends(require_scope("jobs:read"))
):
    """Get dead letter queue jobs."""
    if not _state.redis_queue or not _state.redis_queue._redis:
        return {"error": "Redis not available"}

    jobs = await _state.redis_queue.get_dead_letter_jobs(limit)
    return {"jobs": jobs, "count": len(jobs)}


@router.post("/api/bulk/execute", response_model=BulkExecuteResponse)
@rate_limit_mutation(10, "minute")
async def bulk_execute(
    req: BulkExecuteRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("jobs:run")),
):
    """Execute multiple commands concurrently."""
    # Access control gate
    access_effective_profile = None
    if settings.access_control_enabled and _state.access_control_store is not None:
        source_ip = get_client_ip(request, parse_cidrs(settings.trusted_proxy_cidrs))
        try:
            access = _state.access_control_store.resolve_access_policy(
                actor_fingerprint=_identity.fingerprint,
                token_type=_identity.token_type,
                source_ip=source_ip,
                requested_profile=settings.command_policy_profile,
                enforce_master=settings.access_control_enforce_master,
            )
            access_effective_profile = access.effective_profile
        except AccessDeniedError:
            raise HTTPException(status_code=403, detail=_err(403, "ACCESS_DENIED")) from None
        except AccessPendingApprovalError:
            pass

    # Command Policy Evaluation — check all commands before execution
    source_ip = request.client.host if request.client else "unknown"
    key_profiles = parse_key_profiles(settings.command_policy_key_profiles)
    effective_profile = (
        access_effective_profile
        if access_effective_profile is not None
        else profile_for_identity(
            _identity.fingerprint[:12] if _identity else None,
            key_profiles=key_profiles,
            default_profile=settings.command_policy_profile,
        )
    )
    for cmd in req.commands:
        decision = evaluate_command_policy(
            cmd,
            mode=settings.command_policy_mode,
            profile=effective_profile,
        )
        _state.audit_logger.log_security_event(
            "COMMAND_POLICY_DECISION",
            f"bulk_execute; command_root={decision.command_root}; "
            f"allowed={decision.allowed}; reason={decision.reason}; "
            f"profile={decision.profile}; mode={decision.mode}",
            source_ip,
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

    start_time = time.time()
    results = await _state.bulk_ops.execute_batch_commands(
        req.session_id,
        req.commands,
        _state.manager,
        max_concurrency=10,
    )
    for _cmd in req.commands:
        metrics.record_ssh_command(
            status="allowed",
            profile=effective_profile,
            command_root=None,
        )

    # Convert To Response Format
    response_results = []
    successful = 0
    failed = 0

    for result in results:
        is_success = result.get("success", False)
        if is_success:
            successful += 1
        else:
            failed += 1

        response_results.append(
            BulkExecuteResult(
                command=result.get("item", ""),
                success=is_success,
                stdout=result.get("result", {}).get("stdout", "") if is_success else "",
                stderr=result.get("result", {}).get("stderr", "")
                if is_success
                else result.get("error", ""),
                exit_code=result.get("result", {}).get("exit_code", -1) if is_success else -1,
                duration=result.get("result", {}).get("duration", 0.0) if is_success else 0.0,
                error=result.get("error") if not is_success else None,
            )
        )

    return BulkExecuteResponse(
        results=response_results,
        total_commands=len(req.commands),
        successful=successful,
        failed=failed,
        total_duration=time.time() - start_time,
    )


@router.get("/api/jobs", response_model=JobListResponse)
async def jobs_list(
    session_id: str | None = None,
    status: str | None = None,
    _identity: AuthIdentity = Depends(require_scope("jobs:read")),
):
    """List background jobs."""
    jobs = await _state.job_manager.list_jobs(session_id=session_id, status=status)
    return JobListResponse(
        jobs=[JobResultResponse(**j.to_dict()) for j in jobs],
        count=len(jobs),
    )


@router.post("/api/jobs/{job_id}/cancel")
async def jobs_cancel(job_id: str, _identity: AuthIdentity = Depends(require_scope("jobs:run"))):
    """Cancel a running job."""
    await _state.job_manager.cancel_job(job_id)
    return {"status": "cancelled", "job_id": job_id}


@router.get("/api/jobs/{job_id}/wait")
async def jobs_wait(
    job_id: str,
    request: Request,
    timeout: float = Query(default=30.0, ge=0.1, le=300.0),
    _identity: AuthIdentity = Depends(require_scope("jobs:read")),
):
    """Long-poll: wait for job completion or timeout."""
    if _GATEWAY_WORKERS != "1":
        return JSONResponse(
            status_code=200,
            content={
                "error": "NOT_SUPPORTED",
                "detail": "Long-poll requires GATEWAY_WORKERS=1",
            },
        )

    try:
        result = await _state.job_manager.wait_for_completion(
            job_id=job_id,
            identity_sub=_identity.fingerprint,
            timeout_s=timeout,
        )
    except JobNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=_err(404, f"Job {job_id} not found"),
        ) from None
    except PermissionDeniedError:
        raise HTTPException(
            status_code=403,
            detail=_err(403, "Job belongs to a different owner"),
        ) from None

    return result


# ---------------------------------------------------------------------------
# Job Stream (SSE)
# ---------------------------------------------------------------------------


@router.get("/api/jobs/{job_id}/stream", response_class=StreamingResponse)
@rate_limit_mutation(20, "minute")
async def jobs_stream(
    job_id: str,
    request: Request,
    redact_output: bool | None = Query(
        default=None,
        description="Override command output redaction for this stream.",
    ),
    _identity: AuthIdentity = Depends(require_scope("jobs:read")),
):
    """Stream job output via Server-Sent Events."""
    job = await _state.job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=_err(404, f"Job {job_id} not found"))

    queue: asyncio.Queue = asyncio.Queue()
    job.add_listener(queue)

    async def event_generator():
        try:
            stream_start = time.time()
            MAX_SSE_DURATION = 3600

            # Send Initial Status
            yield f"data: {json.dumps({'type': 'status', 'status': job.status})}\n\n"

            # Send Buffered Output If Job Already Completed
            if job.stdout:
                data = (
                    redact_secrets(job.stdout)
                    if should_redact_command_output(redact_output)
                    else job.stdout
                )
                yield f"data: {json.dumps({'type': 'stdout', 'data': data})}\n\n"
            if job.stderr:
                data = (
                    redact_secrets(job.stderr)
                    if should_redact_command_output(redact_output)
                    else job.stderr
                )
                yield f"data: {json.dumps({'type': 'stderr', 'data': data})}\n\n"

            # Stream New Events
            while job.status in ("pending", "running"):
                if time.time() - stream_start > MAX_SSE_DURATION:
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Stream timeout'})}\n\n"
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    if (
                        should_redact_command_output(redact_output)
                        and event.get("type") in ("stdout", "stderr")
                        and isinstance(event.get("data"), str)
                    ):
                        event["data"] = redact_secrets(event["data"])
                    yield f"data: {json.dumps(event)}\n\n"
                except TimeoutError:
                    # Send Keepalive Comment
                    yield ":keepalive\n\n"
                    continue

            # Send Final Status
            yield f"data: {json.dumps({'type': 'status', 'status': job.status, 'exit_code': job.exit_code})}\n\n"
        finally:
            job.remove_listener(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.get("/api/jobs/{job_id}/events", response_class=StreamingResponse)
@rate_limit_mutation(20, "minute")
async def jobs_events(
    job_id: str,
    request: Request,
    redact_output: bool | None = Query(
        default=None,
        description="Override command output redaction for this stream.",
    ),
    _identity: AuthIdentity = Depends(require_scope("jobs:read")),
):
    """Alias for /api/jobs/{job_id}/stream — SSE job progress events."""
    return await jobs_stream(job_id, request, redact_output)
