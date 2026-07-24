"""Admin endpoint for access-control decisions."""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.config import settings

router = APIRouter(tags=["admin-access"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class DecisionRequest(BaseModel):
    actor_fingerprint: str
    source_ip: str
    decision: str  # "allow" | "deny"
    reason: str = ""
    ttl_seconds: float | None = None
    request_id: str | None = None


class DecisionResponse(BaseModel):
    decision_id: str
    key_hash: str
    decision: str
    expires_at: float
    effective_now: bool = True


class DecisionEntry(BaseModel):
    key_hash: str
    decision: str
    actor_fingerprint: str
    source_ip: str
    reason: str
    decided_by: str
    created_at: float
    expires_at: float
    ttl_seconds_remaining: float


class RecentResponse(BaseModel):
    decisions: list[DecisionEntry]
    total: int


class ClearRequest(BaseModel):
    actor_fingerprint: str
    source_ip: str
    reason: str = ""
    request_id: str | None = None


class ClearResponse(BaseModel):
    key_hash: str
    cleared: bool
    effective_now: bool = True


# ---------------------------------------------------------------------------
# POST /api/admin/access-control/decision
# ---------------------------------------------------------------------------


@router.post(
    "/api/admin/access-control/decision",
    response_model=DecisionResponse,
)
async def set_access_decision(
    req: DecisionRequest,
    _identity: AuthIdentity = Depends(require_master_key),
) -> DecisionResponse:
    if req.decision not in ("allow", "deny"):
        raise HTTPException(422, "decision must be 'allow' or 'deny'")

    store = _state.access_control_store

    # Normalize: API accepts "allow"/"deny", store uses "allowed"/"denied"
    internal_decision = "allowed" if req.decision == "allow" else "denied"

    if req.decision == "allow":
        ttl: int = int(req.ttl_seconds or settings.access_control_allow_ttl)
    else:
        ttl = int(req.ttl_seconds or settings.access_control_deny_ttl)

    entry = store.set(
        req.actor_fingerprint,
        req.source_ip,
        internal_decision,
        reason=req.reason,
        decided_by="operator",
        ttl_seconds=ttl,
    )

    if req.decision == "deny":
        from app.ssh_manager import disconnect_sessions_for_actor_source

        manager = _state.manager
        assert manager is not None
        await disconnect_sessions_for_actor_source(
            manager, req.actor_fingerprint, req.source_ip
        )

    _emit_decision_event(
        req.decision,
        req.actor_fingerprint,
        req.source_ip,
        request_id=req.request_id,
    )

    return DecisionResponse(
        decision_id=f"dec_{uuid.uuid4().hex[:12]}",
        key_hash=entry.key_hash,
        decision=req.decision,
        expires_at=entry.expires_at,
        effective_now=True,
    )


# ---------------------------------------------------------------------------
# GET /api/admin/access-control/recent
# ---------------------------------------------------------------------------


@router.get(
    "/api/admin/access-control/recent",
    response_model=RecentResponse,
)
async def list_recent_decisions(
    limit: int = Query(100, ge=1, le=1000),
    decision: str | None = Query(None, description="Filter: allowed|denied|pending"),
    sort: str = Query("newest", description="newest or oldest"),
    _identity: AuthIdentity = Depends(require_master_key),
) -> RecentResponse:
    store = _state.access_control_store
    entries = store.recent(limit=limit, decision=decision, sort=sort)
    now = time.time()
    return RecentResponse(
        decisions=[
            DecisionEntry(
                key_hash=e.key_hash,
                decision=e.decision,
                actor_fingerprint=e.actor_fingerprint,
                source_ip=e.source_ip,
                reason=e.reason,
                decided_by=e.decided_by,
                created_at=e.created_at,
                expires_at=e.expires_at,
                ttl_seconds_remaining=round(max(0.0, e.expires_at - now), 1),
            )
            for e in entries
        ],
        total=len(entries),
    )


# ---------------------------------------------------------------------------
# POST /api/admin/access-control/clear
# ---------------------------------------------------------------------------


@router.post(
    "/api/admin/access-control/clear",
    response_model=ClearResponse,
)
async def clear_access_decision(
    req: ClearRequest,
    _identity: AuthIdentity = Depends(require_master_key),
) -> ClearResponse:
    store = _state.access_control_store
    entry = store.clear(
        req.actor_fingerprint,
        req.source_ip,
        reason=req.reason,
        decided_by="operator",
    )

    key_hash = entry.key_hash if entry else store.make_key_hash(req.actor_fingerprint, req.source_ip) if hasattr(store, "make_key_hash") else ""

    from app.access_control import make_access_key_hash
    key_hash = entry.key_hash if entry else make_access_key_hash(req.actor_fingerprint, req.source_ip)

    _emit_clear_event(
        req.actor_fingerprint,
        req.source_ip,
        req.reason,
        request_id=req.request_id,
    )

    return ClearResponse(
        key_hash=key_hash,
        cleared=entry is not None,
    )


# ---------------------------------------------------------------------------
# Structured audit events
# ---------------------------------------------------------------------------


def _emit_decision_event(
    decision: str,
    actor_fingerprint: str,
    source_ip: str,
    request_id: str | None = None,
) -> None:
    """Emit a structured ACCESS_CONTROL_DECISION audit event."""
    from app.audit import AuditEvent, AuditEventLogger, AuditEventType, Decision

    event_logger: AuditEventLogger | None = getattr(_state, "event_audit_logger", None)
    if event_logger is None:
        return
    event_logger.append(AuditEvent(
        event_type=AuditEventType.ACCESS_CONTROL_DECISION,
        actor_type="operator",
        actor_fingerprint=actor_fingerprint[:12],
        request_id=request_id or "",
        source_ip=source_ip,
        route="POST /api/admin/access-control/decision",
        action=f"access_control.decision:{decision}",
        target_type="actor",
        target_id=f"{actor_fingerprint[:12]}...{source_ip}",
        decision=Decision.DENIED if decision == "deny" else Decision.ALLOWED,
        reason=f"operator set {decision}",
    ))


def _emit_clear_event(
    actor_fingerprint: str,
    source_ip: str,
    reason: str,
    request_id: str | None = None,
) -> None:
    """Emit a structured ACCESS_CONTROL_CLEAR audit event."""
    from app.audit import AuditEvent, AuditEventLogger, AuditEventType, Decision

    event_logger: AuditEventLogger | None = getattr(_state, "event_audit_logger", None)
    if event_logger is None:
        return
    event_logger.append(AuditEvent(
        event_type=AuditEventType.ACCESS_CONTROL_CLEAR,
        actor_type="operator",
        actor_fingerprint=actor_fingerprint[:16],
        request_id=request_id or "",
        source_ip=source_ip,
        route="POST /api/admin/access-control/clear",
        action="access_control.clear",
        target_type="actor",
        target_id=f"{actor_fingerprint[:12]}...{source_ip}",
        decision=Decision.ALLOWED,
        reason=reason or "operator cleared decision",
    ))
