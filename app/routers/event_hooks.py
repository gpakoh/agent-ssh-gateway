"""Event hook CRUD endpoints."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException

from app import state as _state
from app.state import _err
from app.config import settings
from app.models import (
    EventHookCreate,
    EventHookUpdate,
    EventHookResponse,
    EventHookListResponse,
)
from app.event_hook_security import validate_webhook_url

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


@router.get("/api/event-hooks", response_model=EventHookListResponse)
async def list_event_hooks():
    """List all registered event hooks."""
    if not _state.event_hook_store:
        raise HTTPException(status_code=503, detail=_err(503, "Event hook store not available"))
    hooks = await _state.event_hook_store.list()
    return EventHookListResponse(
        hooks=[EventHookResponse(**h.to_dict()) for h in hooks],
        count=len(hooks),
    )


@router.post("/api/event-hooks", response_model=EventHookResponse, status_code=201)
async def create_event_hook(body: EventHookCreate):
    """Register a new event hook."""
    store = _state.event_hook_store
    if not store:
        raise HTTPException(status_code=503, detail=_err(503, "Event hook store not available"))

    url_str = str(body.url)
    result = validate_webhook_url(url_str, allow_http=False)
    if not result.valid:
        raise HTTPException(status_code=422, detail=_err(422, f"Invalid URL: {result.reason}"))

    existing = await store.list()
    if len(existing) >= settings.event_hooks_max:
        raise HTTPException(status_code=409, detail=_err(409, "Max event hooks reached"))

    headers_encrypted = None
    secret_encrypted = None
    if body.headers and _state.secret_manager:
        try:
            headers_encrypted = _state.secret_manager.encrypt(json.dumps(body.headers))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_err(500, f"Failed to encrypt headers: {exc}"))
    if body.secret and _state.secret_manager:
        try:
            secret_encrypted = _state.secret_manager.encrypt(body.secret)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_err(500, f"Failed to encrypt secret: {exc}"))

    hook = await store.create(
        url=url_str,
        events=body.events,
        session_id=body.session_id,
        headers_encrypted=headers_encrypted,
        secret_encrypted=secret_encrypted,
        include_output=body.include_output,
    )
    return EventHookResponse(**hook.to_dict())


@router.get("/api/event-hooks/{hook_id}", response_model=EventHookResponse)
async def get_event_hook(hook_id: str):
    """Get event hook by ID."""
    if not _state.event_hook_store:
        raise HTTPException(status_code=503, detail=_err(503, "Event hook store not available"))
    hook = await _state.event_hook_store.get(hook_id)
    if not hook:
        raise HTTPException(status_code=404, detail=_err(404, f"Event hook not found: {hook_id}"))
    return EventHookResponse(**hook.to_dict())


@router.patch("/api/event-hooks/{hook_id}", response_model=EventHookResponse)
async def update_event_hook(hook_id: str, body: EventHookUpdate):
    """Update an event hook (partial update)."""
    store = _state.event_hook_store
    if not store:
        raise HTTPException(status_code=503, detail=_err(503, "Event hook store not available"))

    existing = await store.get(hook_id)
    if not existing:
        raise HTTPException(status_code=404, detail=_err(404, f"Event hook not found: {hook_id}"))

    if body.url is not None:
        url_str = str(body.url)
        result = validate_webhook_url(url_str, allow_http=False)
        if not result.valid:
            raise HTTPException(status_code=422, detail=_err(422, f"Invalid URL: {result.reason}"))
    else:
        url_str = None

    headers_encrypted = None
    if body.headers is not None:
        if _state.secret_manager:
            try:
                headers_encrypted = _state.secret_manager.encrypt(json.dumps(body.headers))
            except Exception as exc:
                raise HTTPException(status_code=500, detail=_err(500, f"Failed to encrypt headers: {exc}"))

    secret_encrypted = None
    if body.secret is not None:
        if _state.secret_manager:
            try:
                secret_encrypted = _state.secret_manager.encrypt(body.secret)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=_err(500, f"Failed to encrypt secret: {exc}"))

    updated = await store.update(
        hook_id,
        url=url_str,
        events=body.events,
        session_id=body.session_id,
        headers_encrypted=headers_encrypted if body.headers is not None else None,
        secret_encrypted=secret_encrypted if body.secret is not None else None,
        include_output=body.include_output,
        is_active=body.is_active,
    )
    return EventHookResponse(**updated.to_dict())


@router.delete("/api/event-hooks/{hook_id}")
async def delete_event_hook(hook_id: str):
    """Delete an event hook."""
    if not _state.event_hook_store:
        raise HTTPException(status_code=503, detail=_err(503, "Event hook store not available"))
    deleted = await _state.event_hook_store.delete(hook_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=_err(404, f"Event hook not found: {hook_id}"))
    return {"deleted": True}
