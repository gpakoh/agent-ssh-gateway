"""Agent observability routes.

Read-only view over agent lifecycle events. With persistent sessions the
timeline is served from PostgreSQL (``app.agent_event_store``) — history
survives restarts and authorization is resolved from persisted ownership.
Without it, falls back to the in-memory store (process lifetime only).
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from starlette.responses import StreamingResponse

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


_TERMINAL_EVENT_TYPES = {"completed", "failed", "cancelled"}


def _sse(event_type: str, data: dict, sequence: int | None = None) -> str:
    lines = [f"event: {event_type}", f"data: {json.dumps(data)}"]
    if sequence is not None:
        lines.append(f"id: {sequence}")
    return "\n".join(lines) + "\n\n"


@router.get("/api/agents/{job_id}/events/stream")
async def agent_events_stream(
    job_id: str,
    _identity: AuthIdentity = Depends(require_scope("jobs:read")),
    response: Response = None,  # type: ignore[assignment]
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """SSE timeline with high-water-mark replay.

    Replay semantics: events after ``Last-Event-ID`` up to the subscription
    watermark are replayed from PG before live events flow — no gap, no
    duplication. Overflow is signaled out-of-band via an ``error`` event.
    """
    emitter = getattr(_state, "agent_event_emitter", None)
    pg_store = getattr(_state, "agent_event_store", None)
    if emitter is None or pg_store is None:
        raise HTTPException(
            status_code=503,
            detail=_err(503, "Event streaming requires persistent sessions"),
        )

    cursor = 0
    if last_event_id is not None:
        try:
            cursor = int(last_event_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_err(400, "Invalid Last-Event-ID")) from exc
        if cursor < 0:
            raise HTTPException(status_code=400, detail=_err(400, "Invalid Last-Event-ID"))

    owner_id = await pg_store.get_owner_id(job_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail=_err(404, f"Job {job_id} not found"))
    if (
        _identity.token_type != "master"
        and _identity.role != "admin"
        and owner_id != _identity.fingerprint
    ):
        raise HTTPException(status_code=403, detail=_err(403, "Job belongs to a different owner"))

    watermark_now = await pg_store.get_latest_sequence(job_id) or 0
    min_seq = await pg_store.get_min_sequence(job_id)
    if last_event_id is not None:
        if min_seq is not None and cursor < min_seq:
            raise HTTPException(
                status_code=409,
                detail=_err(409, "Replay window expired; reconnect without Last-Event-ID"),
            )
        if cursor > watermark_now:
            raise HTTPException(status_code=400, detail=_err(400, "Last-Event-ID is in the future"))

    subscription = await emitter.subscribe(job_id)

    async def event_stream():
        last_sent = cursor
        try:
            replay = await pg_store.get_events(
                job_id,
                after_sequence=cursor or None,
                up_to_sequence=subscription.watermark or None,
            )
            for record in replay:
                yield _sse(record.event_type, _record_to_dict(record), record.sequence)
                last_sent = record.sequence
                if record.event_type in _TERMINAL_EVENT_TYPES:
                    return

            while True:
                if subscription.overflow:
                    yield _sse("error", {"last_sequence": last_sent})
                    return
                try:
                    event = await asyncio.wait_for(subscription.queue.get(), timeout=1.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                sequence = event.get("sequence")
                event_type = event.get("type")
                if sequence is not None and sequence <= last_sent:
                    continue  # already replayed
                yield _sse(event_type, event, sequence)
                last_sent = sequence or last_sent
                if event_type in _TERMINAL_EVENT_TYPES:
                    return
        finally:
            emitter.remove_subscriber(job_id, subscription.queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
