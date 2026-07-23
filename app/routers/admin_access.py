"""Admin endpoint for access-control decisions."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.config import settings

router = APIRouter(tags=["admin-access"])


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

    if req.decision == "allow":
        ttl: int = int(req.ttl_seconds or settings.access_control_allow_ttl)
    else:
        ttl = int(req.ttl_seconds or settings.access_control_deny_ttl)

    entry = store.set(
        req.actor_fingerprint,
        req.source_ip,
        req.decision,
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

    return DecisionResponse(
        decision_id=f"dec_{uuid.uuid4().hex[:12]}",
        key_hash=entry.key_hash,
        decision=req.decision,
        expires_at=entry.expires_at,
        effective_now=True,
    )
