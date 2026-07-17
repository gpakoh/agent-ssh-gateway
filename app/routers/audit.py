"""Audit event query endpoint — read-only, master key only."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.state import _err

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"])


@router.get("/api/admin/audit/recent")
async def audit_recent(
    _identity: AuthIdentity = Depends(require_master_key),
    limit: int = Query(100, ge=1, le=1000, description="Max events to return"),
    event_type: str | None = Query(None, description="Filter by event type"),
    decision: str | None = Query(None, description="Filter by decision (allowed/denied/error)"),
    sort: str = Query("newest", description="Sort order: newest or oldest"),
) -> dict[str, Any]:
    """Return recent audit events from the in-memory ring buffer.

    Read-only. No mutation. Master key required.
    Returns newest-first by default.
    """
    event_logger = _state.event_audit_logger
    if event_logger is None:
        raise HTTPException(
            status_code=503,
            detail=_err(503, "Audit event logger not initialized"),
        )

    # Fetch all recent events
    events = event_logger.recent()

    # Apply filters
    if event_type:
        events = [e for e in events if e.event_type == event_type]
    if decision:
        events = [e for e in events if e.decision == decision]

    # Sort: deque is oldest-first naturally
    if sort == "newest":
        events = list(reversed(events))

    # Apply limit
    events = events[:limit]

    # Serialize (strip empty values)
    events_data = [e.to_dict() for e in events]

    return {
        "events": events_data,
        "total": len(events_data),
        "buffer_size": event_logger.recent_count,
    }
