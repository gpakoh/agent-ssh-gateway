"""Agent observability routes.

Read-only view over agent lifecycle events. With persistent sessions the
timeline is served from PostgreSQL (``app.agent_event_store``) — history
survives restarts and authorization is resolved from persisted ownership.
Without it, falls back to the in-memory store (process lifetime only).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app import state as _state
from app.agent_events import list_agent_events
from app.auth_middleware import AuthIdentity, require_scope
from app.rbac import job_visible_to
from app.state import _err

router = APIRouter(tags=["agents"])


def _record_to_dict(record) -> dict:
    created_at = getattr(record, "created_at", None)
    return {
        "sequence": record.sequence,
        "job_id": record.job_id,
        "attempt_id": record.attempt_id,
        "agent_id": record.agent_id,
        "type": record.event_type,
        "payload": record.payload or {},
        "created_at": created_at.isoformat() if created_at else None,
    }


@router.get("/api/agents/{job_id}/events")
async def agent_events_history(
    job_id: str,
    _identity: AuthIdentity = Depends(require_scope("jobs:read")),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Lifecycle event timeline (started/heartbeat/progress/completed/failed)
    for a background job. Same ownership rules as the jobs endpoints."""
    pg_store = getattr(_state, "agent_event_store", None)
    if pg_store is not None:
        owner_id = await pg_store.get_owner_id(job_id)
        if owner_id is None:
            raise HTTPException(status_code=404, detail=_err(404, f"Job {job_id} not found"))
        if (
            _identity.token_type != "master"
            and _identity.role != "admin"
            and owner_id != _identity.fingerprint
        ):
            raise HTTPException(
                status_code=403, detail=_err(403, "Job belongs to a different owner")
            )
        records = await pg_store.get_events(job_id)
        records = records[-limit:]
        return {
            "job_id": job_id,
            "count": len(records),
            "events": [_record_to_dict(r) for r in records],
        }

    # Legacy in-memory path (no persistent sessions wired).
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
