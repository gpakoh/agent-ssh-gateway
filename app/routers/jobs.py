"""Job, bulk, and batch execute routes."""

import json
import asyncio
import time

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import state as _state
from app.state import _err
from app.config import settings
from app.security import rate_limit_mutation
from app.models import (
    JobRunRequest,
    JobRunResponse,
    JobStatusResponse,
    JobResultResponse,
    JobListResponse,
    BulkExecuteRequest,
    BulkExecuteResult,
    BulkExecuteResponse,
)

router = APIRouter()


@router.post("/api/jobs/run", response_model=JobRunResponse)
@rate_limit_mutation(20, "minute")
async def jobs_run(req: JobRunRequest, request: Request):
    """Start a background job on an SSH session."""
    session = await _state.manager.get_session(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail=_err(404, f"Session {req.session_id} not found"))
    job_id = await _state.job_manager.create_job(
        session_id=req.session_id,
        command=req.command,
    )
    return JobRunResponse(job_id=job_id)


@router.get("/api/jobs/{job_id}/status", response_model=JobStatusResponse)
async def jobs_status(job_id: str):
    """Get job status."""
    status = await _state.job_manager.get_job_status(job_id)
    return JobStatusResponse(**status)


@router.get("/api/jobs/{job_id}/result", response_model=JobResultResponse)
async def jobs_result(job_id: str):
    """Get full job result."""
    result = await _state.job_manager.get_job_result(job_id)
    return JobResultResponse(**result)


@router.get("/api/jobs/queue/stats")
async def jobs_queue_stats():
    """Get Redis job queue statistics."""
    if not _state.redis_queue or not _state.redis_queue._redis:
        return {"error": "Redis not available"}

    stats = await _state.redis_queue.get_queue_stats()
    return stats


@router.get("/api/jobs/queue/dead")
async def jobs_dead_letter(limit: int = 100):
    """Get dead letter queue jobs."""
    if not _state.redis_queue or not _state.redis_queue._redis:
        return {"error": "Redis not available"}

    jobs = await _state.redis_queue.get_dead_letter_jobs(limit)
    return {"jobs": jobs, "count": len(jobs)}


@router.post("/api/bulk/execute", response_model=BulkExecuteResponse)
@rate_limit_mutation(10, "minute")
async def bulk_execute(req: BulkExecuteRequest, request: Request):
    """Execute multiple commands concurrently."""
    start_time = time.time()
    results = await _state.bulk_ops.execute_batch_commands(
        req.session_id,
        req.commands,
        _state.manager,
        max_concurrency=10,
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

        response_results.append(BulkExecuteResult(
            command=result.get("item", ""),
            success=is_success,
            stdout=result.get("result", {}).get("stdout", "") if is_success else "",
            stderr=result.get("result", {}).get("stderr", "") if is_success else result.get("error", ""),
            exit_code=result.get("result", {}).get("exit_code", -1) if is_success else -1,
            duration=result.get("result", {}).get("duration", 0.0) if is_success else 0.0,
            error=result.get("error") if not is_success else None,
        ))

    return BulkExecuteResponse(
        results=response_results,
        total_commands=len(req.commands),
        successful=successful,
        failed=failed,
        total_duration=time.time() - start_time,
    )


@router.get("/api/jobs", response_model=JobListResponse)
async def jobs_list(
    session_id: Optional[str] = None,
    status: Optional[str] = None,
):
    """List background jobs."""
    jobs = await _state.job_manager.list_jobs(session_id=session_id, status=status)
    return JobListResponse(
        jobs=[JobResultResponse(**j.to_dict()) for j in jobs],
        count=len(jobs),
    )


@router.post("/api/jobs/{job_id}/cancel")
async def jobs_cancel(job_id: str):
    """Cancel a running job."""
    await _state.job_manager.cancel_job(job_id)
    return {"status": "cancelled", "job_id": job_id}


# ---------------------------------------------------------------------------
# Job Stream (SSE)
# ---------------------------------------------------------------------------


@router.get("/api/jobs/{job_id}/stream", response_class=StreamingResponse)
async def jobs_stream(job_id: str):
    """Stream job output via Server-Sent Events."""
    job = await _state.job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=_err(404, f"Job {job_id} not found"))

    queue: asyncio.Queue = asyncio.Queue()
    job.add_listener(queue)

    async def event_generator():
        try:
            # Send Initial Status
            yield f"data: {json.dumps({'type': 'status', 'status': job.status})}\n\n"

            # Send Buffered Output If Job Already Completed
            if job.stdout:
                yield f"data: {json.dumps({'type': 'stdout', 'data': job.stdout})}\n\n"
            if job.stderr:
                yield f"data: {json.dumps({'type': 'stderr', 'data': job.stderr})}\n\n"

            # Stream New Events
            while job.status in ("pending", "running"):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
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
async def jobs_events(job_id: str):
    """Alias for /api/jobs/{job_id}/stream — SSE job progress events."""
    return await jobs_stream(job_id)
