"""In-memory confirmation store for dangerous Docker operations.

One-time tokens, 60s TTL, process-local, no I/O.
"""

from __future__ import annotations

import enum
import hmac
import time
import uuid
from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import Any

CONFIRM_TTL_SECONDS = 60


class ConfirmStatus(str, enum.Enum):
    OK = "ok"
    INVALID = "invalid"
    EXPIRED = "expired"
    CONSUMED = "consumed"


@dataclass
class ConfirmAction:
    action_id: str
    tool: str
    kwargs: dict[str, Any]
    confirm_token: str
    summary: str
    risk: str = "high"
    created_at: float = field(default_factory=time.monotonic)
    consumed: bool = False


class ConfirmStore:
    """Process-local, in-memory store for pending dangerous actions."""

    def __init__(self) -> None:
        self._actions: dict[str, ConfirmAction] = {}
        self._token_map: dict[str, str] = {}

    def create_action(
        self,
        tool: str,
        kwargs: dict[str, Any],
        summary: str,
        *,
        risk: str = "high",
    ) -> ConfirmAction:
        action_id = uuid.uuid4().hex
        confirm_token = token_urlsafe(16)
        action = ConfirmAction(
            action_id=action_id,
            tool=tool,
            kwargs=kwargs,
            confirm_token=confirm_token,
            summary=summary,
            risk=risk,
        )
        self._actions[action_id] = action
        self._token_map[confirm_token] = action_id
        return action

    def confirm_action(self, token: str) -> tuple[ConfirmAction | None, ConfirmStatus]:
        action_id = self._token_map.get(token)
        if action_id is None:
            for aid, act in self._actions.items():
                if hmac.compare_digest(act.confirm_token, token):
                    action_id = aid
                    self._token_map[token] = aid
                    break
            if action_id is None:
                return None, ConfirmStatus.INVALID

        action = self._actions.get(action_id)
        if action is None:
            return None, ConfirmStatus.INVALID

        if action.consumed:
            return None, ConfirmStatus.CONSUMED

        elapsed = time.monotonic() - action.created_at
        if elapsed > CONFIRM_TTL_SECONDS:
            return None, ConfirmStatus.EXPIRED

        action.consumed = True
        return action, ConfirmStatus.OK

    def list_pending(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        result: list[dict[str, Any]] = []
        for action in self._actions.values():
            if action.consumed:
                continue
            elapsed = now - action.created_at
            if elapsed > CONFIRM_TTL_SECONDS:
                continue
            remaining = max(0, int(CONFIRM_TTL_SECONDS - elapsed))
            token_preview = action.confirm_token[:6] + "..."
            result.append(
                {
                    "action_id": action.action_id,
                    "tool": action.tool,
                    "summary": action.summary,
                    "risk": action.risk,
                    "expires_in_sec": remaining,
                    "confirm_token": token_preview,
                }
            )
        return result

    def cleanup_expired(self) -> int:
        now = time.monotonic()
        expired = [
            aid
            for aid, action in self._actions.items()
            if now - action.created_at > CONFIRM_TTL_SECONDS
        ]
        for aid in expired:
            action = self._actions.pop(aid, None)
            if action:
                self._token_map.pop(action.confirm_token, None)
        return len(expired)
