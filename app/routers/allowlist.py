"""Admin endpoint for managing the four-layer command allowlist (Gate 0).

An allowlist match bypasses ALL subsequent command-policy gates
(metachar denial, argument shape, heredoc scanner, profile, denylist) —
see app/command_policy.py's evaluate_command_policy(). Master key only:
any entry added here can let a matching command through regardless of
the caller's configured profile/mode, so this must never be reachable
with a narrower scope than the master key already implies.
"""

from __future__ import annotations

import re as _re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.allowlist import LAYERS, SELECTOR_TYPES, AllowlistEntry, get_allowlist
from app.auth_middleware import AuthIdentity, require_master_key
from app.state import _err

router = APIRouter(tags=["admin-allowlist"])


class AllowlistAddRequest(BaseModel):
    layer: str = Field(..., description="agent | project | user | system")
    selector_type: str = Field(..., description="rule_id | exact | prefix | regex")
    selector_value: str = Field(..., min_length=1)
    reason: str = Field(default="")
    ttl_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Override the default TTL (1h for agent/project/user, none for system)",
    )


class AllowlistEntryResponse(BaseModel):
    id: str
    layer: str
    selector_type: str
    selector_value: str
    created_at: float
    expires_at: float | None
    created_by: str | None
    reason: str


class AllowlistListResponse(BaseModel):
    entries: list[AllowlistEntryResponse]
    total: int


class AllowlistRemoveResponse(BaseModel):
    removed: bool
    entry_id: str


class AllowlistClearResponse(BaseModel):
    cleared: int


def _to_response(entry: AllowlistEntry) -> AllowlistEntryResponse:
    return AllowlistEntryResponse(
        id=entry.id,
        layer=entry.layer,
        selector_type=entry.selector_type,
        selector_value=entry.selector_value,
        created_at=entry.created_at,
        expires_at=entry.expires_at,
        created_by=entry.created_by,
        reason=entry.reason,
    )


@router.post("/api/admin/allowlist", response_model=AllowlistEntryResponse, status_code=201)
async def add_allowlist_entry(
    req: AllowlistAddRequest,
    _identity: AuthIdentity = Depends(require_master_key),
) -> AllowlistEntryResponse:
    """Add an allowlist entry."""
    if req.layer not in LAYERS:
        raise HTTPException(422, detail=_err(422, f"layer must be one of {LAYERS}"))
    if req.selector_type not in SELECTOR_TYPES:
        raise HTTPException(
            422, detail=_err(422, f"selector_type must be one of {SELECTOR_TYPES}")
        )
    if req.selector_type == "regex":
        try:
            _re.compile(req.selector_value)
        except _re.error as exc:
            raise HTTPException(422, detail=_err(422, f"Invalid regex: {exc}")) from exc

    entry = get_allowlist().add(
        req.layer,
        req.selector_type,
        req.selector_value,
        created_by=_identity.name or _identity.token_type,
        reason=req.reason,
        ttl=req.ttl_seconds,
    )
    return _to_response(entry)


@router.get("/api/admin/allowlist", response_model=AllowlistListResponse)
async def list_allowlist_entries(
    layer: str | None = Query(default=None, description="Filter by layer"),
    _identity: AuthIdentity = Depends(require_master_key),
) -> AllowlistListResponse:
    """List allowlist entries, optionally filtered to a single layer."""
    if layer is not None and layer not in LAYERS:
        raise HTTPException(422, detail=_err(422, f"layer must be one of {LAYERS}"))
    allowlist = get_allowlist()
    entries = allowlist.list_layer(layer) if layer else allowlist.list_all()
    return AllowlistListResponse(entries=[_to_response(e) for e in entries], total=len(entries))


@router.delete("/api/admin/allowlist/{entry_id}", response_model=AllowlistRemoveResponse)
async def remove_allowlist_entry(
    entry_id: str,
    _identity: AuthIdentity = Depends(require_master_key),
) -> AllowlistRemoveResponse:
    """Remove a single allowlist entry by id."""
    removed = get_allowlist().remove(entry_id)
    if not removed:
        raise HTTPException(404, detail=_err(404, f"Allowlist entry not found: {entry_id}"))
    return AllowlistRemoveResponse(removed=True, entry_id=entry_id)


@router.delete("/api/admin/allowlist", response_model=AllowlistClearResponse)
async def clear_allowlist_entries(
    layer: str | None = Query(
        default=None, description="Clear only this layer; omit to clear every layer"
    ),
    _identity: AuthIdentity = Depends(require_master_key),
) -> AllowlistClearResponse:
    """Clear allowlist entries — a single layer, or everything."""
    allowlist = get_allowlist()
    if layer is not None:
        if layer not in LAYERS:
            raise HTTPException(422, detail=_err(422, f"layer must be one of {LAYERS}"))
        count = allowlist.clear_layer(layer)
    else:
        count = allowlist.clear_all()
    return AllowlistClearResponse(cleared=count)
