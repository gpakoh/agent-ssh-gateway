"""Opaque action tokens for Telegram inline buttons."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field


@dataclass
class ActionPayload:
    action_type: str
    actor_fingerprint: str
    source_ip: str
    event_type: str
    request_id: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0


_actions: dict[str, ActionPayload] = {}


def create_action(
    *,
    action_type: str,
    actor_fingerprint: str,
    source_ip: str,
    event_type: str,
    request_id: str,
    ttl_seconds: float = 3600.0,
) -> str:
    token = secrets.token_urlsafe(16)
    _actions[token] = ActionPayload(
        action_type=action_type,
        actor_fingerprint=actor_fingerprint,
        source_ip=source_ip,
        event_type=event_type,
        request_id=request_id,
        expires_at=time.time() + ttl_seconds,
    )
    return token


def get_action(token: str) -> ActionPayload | None:
    payload = _actions.get(token)
    if payload is None:
        return None
    if payload.expires_at > 0 and time.time() > payload.expires_at:
        _actions.pop(token, None)
        return None
    return payload


def pop_action(token: str) -> ActionPayload | None:
    payload = _actions.pop(token, None)
    if payload is None:
        return None
    if payload.expires_at > 0 and time.time() > payload.expires_at:
        return None
    return payload


def clear_actions() -> None:
    _actions.clear()
