"""Diagnostics routes — latency breakdown, system info."""

import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app import state as _state
from app.auth_middleware import AuthIdentity, require_scope
from app.models import SessionCheckRequest

router = APIRouter(tags=["diagnostics"])


def _compute_job_latency_breakdown() -> dict:
    """Compute latency breakdown from completed jobs with mono timestamps."""
    jobs_summary = []
    total_jobs = 0

    if _state.job_manager is None:
        return {"jobs": [], "total": 0}

    for job in _state.job_manager._jobs.values():
        total_jobs += 1
        if job.status not in ("completed", "failed", "cancelled"):
            continue
        if job.queued_at_mono is None or job.completed_at_mono is None:
            continue

        entry: dict = {
            "job_id": job.job_id,
            "status": job.status,
            "gateway_total_ms": round(
                (job.completed_at_mono - job.queued_at_mono) * 1000, 1
            ),
        }
        if job.acquired_at_mono is not None and job.queued_at_mono is not None:
            entry["queue_wait_ms"] = round(
                (job.acquired_at_mono - job.queued_at_mono) * 1000, 1
            )
        if (
            job.command_finished_at_mono is not None
            and job.command_started_at_mono is not None
        ):
            entry["command_execution_ms"] = round(
                (job.command_finished_at_mono - job.command_started_at_mono) * 1000, 1
            )
        if (
            job.ssh_connected_at_mono is not None
            and job.ssh_connect_started_at_mono is not None
        ):
            entry["ssh_connect_ms"] = round(
                (job.ssh_connected_at_mono - job.ssh_connect_started_at_mono) * 1000, 1
            )
        elif (
            job.ssh_connect_started_at_mono is not None
            and job.ssh_connected_at_mono is None
        ):
            entry["ssh_connect_ms"] = None  # session reused or still connecting

        jobs_summary.append(entry)

    return {"jobs": jobs_summary, "total": total_jobs}


@router.get("/api/diagnostics/latency")
async def diagnostics_latency(
    _identity: AuthIdentity = Depends(require_scope("diagnostics:read")),
):
    """Latency breakdown for completed jobs and MCP process metrics."""
    job_breakdown = _compute_job_latency_breakdown()

    return {
        "gateway": {
            "timestamp": time.time(),
            **job_breakdown,
        },
        "mcp": {
            "note": "MCP latency is process-local. Use the diagnostics_latency MCP tool.",
        },
    }


@router.post("/api/session/check", response_model=None)
async def session_check(
    body: SessionCheckRequest,
    _identity: AuthIdentity = Depends(require_scope("diagnostics:read")),
) -> JSONResponse | dict:
    """Check whether an SSH session is still alive.

    Does not execute any remote command — only inspects local session state.
    Scope: diagnostics:read (master key bypasses scope checks).
    """
    mgr = _state.manager
    if mgr is None:
        return JSONResponse(
            status_code=503,
            content={
                "message": "SSH manager not initialized",
                "code": "SERVICE_UNAVAILABLE",
                "retryable": True,
                "hint": "The gateway is still starting up. Retry in a few seconds.",
                "http_status": 503,
            },
        )

    record = await mgr.get_session(body.session_id)
    if record is None:
        return {
            "valid": False,
            "code": "SESSION_NOT_FOUND",
            "hint": "Create a session via POST /api/ssh/connect",
        }

    status = "connected" if record.is_connected() else "disconnected"
    return {
        "valid": True,
        "session_id": body.session_id,
        "status": status,
    }
