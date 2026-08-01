"""Audit event query endpoint — read-only, master key only."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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


@router.get("/api/audit")
async def audit_query(
    _identity: AuthIdentity = Depends(require_master_key),
    session_id: str | None = Query(None, description="Filter by session id"),
    event_type: str | None = Query(None, description="Filter by event type"),
    decision: str | None = Query(None, description="Filter by decision (allowed/denied/error)"),
    since: datetime | None = Query(None, description="Only events at or after this timestamp"),
    until: datetime | None = Query(None, description="Only events at or before this timestamp"),
    limit: int = Query(100, ge=1, le=1000, description="Max events to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
) -> dict[str, Any]:
    """Query the persistent audit log stored in PostgreSQL.

    Read-only. Master key required. Newest-first with pagination.
    Returns 503 when the persistent audit store is not configured.
    """
    store = _state.audit_log_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=_err(
                503,
                "Persistent audit log not configured "
                "(set AUDIT_LOG_PERSIST_ENABLED=true and DATABASE_URL)",
            ),
        )

    since_aware = since if since is None or since.tzinfo else since.replace(tzinfo=UTC)
    until_aware = until if until is None or until.tzinfo else until.replace(tzinfo=UTC)

    events = await store.query(
        session_id=session_id,
        event_type=event_type,
        decision=decision,
        since=since_aware,
        until=until_aware,
        limit=limit,
        offset=offset,
    )
    total = await store.count(
        session_id=session_id,
        event_type=event_type,
        decision=decision,
        since=since_aware,
        until=until_aware,
    )

    return {
        "events": [e.to_dict() for e in events],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
