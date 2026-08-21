"""Agent observability routes.

Read-only view over the in-memory agent lifecycle event store
(``app.agent_events``). Events are emitted by ``JobManager._run_job``
and survive only within the current process, so history is available
while the job record is still known to the gateway.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app import state as _state
from app.agent_events import list_agent_events
from app.auth_middleware import AuthIdentity, require_scope
from app.rbac import job_visible_to
from app.state import _err

router = APIRouter(tags=["agents"])


@router.get("/api/agents/{job_id}/events")
async def agent_events_history(
    job_id: str,
    _identity: AuthIdentity = Depends(require_scope("jobs:read")),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Lifecycle event timeline (started/heartbeat/progress/completed/failed)
    for a background job. Same ownership rules as the jobs endpoints."""
    job = await _state.job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=_err(404, f"Job {job_id} not found"))
    if not job_visible_to(job, _identity):
        raise HTTPException(status_code=403, detail=_err(403, "Job belongs to a different owner"))
    events = list_agent_events(job_id)[-limit:]
    return {
        "job_id": job_id,
        "count": len(events),
        "events": [event.to_dict() for event in events],
    }
