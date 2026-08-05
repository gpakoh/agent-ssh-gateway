"""Operator approval store for ASK mode.

When a command triggers a policy block in ASK mode, an ApprovalRequest
is created. An operator can approve or deny it via API endpoint.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

# In-memory store: approval_id -> ApprovalRequest
# TTL: 300 seconds (5 minutes) after which requests auto-expire
_APPROVAL_TTL_S = 300

_store: dict[str, ApprovalRequest] = {}


@dataclass
class ApprovalRequest:
    approval_id: str
    command: str
    profile: str
    blocked_by: str | None
    reason: str
    created_at: float
    expires_at: float
    approved: bool | None = None  # None=pending, True=approved, False=denied
    approved_by: str | None = None


def create_approval_request(
    command: str,
    profile: str,
    blocked_by: str | None,
    reason: str,
) -> ApprovalRequest:
    """Create a new pending approval request."""
    _expire_stale()
    approval_id = uuid.uuid4().hex[:16]
    now = time.time()
    req = ApprovalRequest(
        approval_id=approval_id,
        command=command,
        profile=profile,
        blocked_by=blocked_by,
        reason=reason,
        created_at=now,
        expires_at=now + _APPROVAL_TTL_S,
    )
    _store[approval_id] = req
    return req


def approve_request(approval_id: str, operator: str = "operator") -> bool:
    """Approve a pending request. Returns True if approved."""
    req = _store.get(approval_id)
    if req is None or req.expires_at < time.time() or req.approved is not None:
        return False
    req.approved = True
    req.approved_by = operator
    return True


def deny_request(approval_id: str, operator: str = "operator") -> bool:
    """Deny a pending request. Returns True if denied."""
    req = _store.get(approval_id)
    if req is None or req.expires_at < time.time() or req.approved is not None:
        return False
    req.approved = False
    req.approved_by = operator
    return True


def get_request(approval_id: str) -> ApprovalRequest | None:
    """Get an approval request (or None if expired/missing)."""
    req = _store.get(approval_id)
    if req is None or req.expires_at < time.time():
        return None
    return req


def get_pending_requests() -> list[ApprovalRequest]:
    """List all non-expired pending requests."""
    _expire_stale()
    return [r for r in _store.values() if r.approved is None]


def find_and_consume_approval(command: str, profile: str) -> ApprovalRequest | None:
    """Look up an already-approved request matching this exact command+profile.

    Regression: approve_request() used to only flip a flag nothing ever
    read back — evaluate_command_policy()'s ASK-mode branch created a
    brand-new ApprovalRequest (a fresh UUID) on every single evaluation of
    a blocked command, with no lookup of prior decisions at all, so an
    operator approving a request never actually let the caller's retry of
    the identical command through; it always hit the same block and
    another 202 with a new approval_id, forever.

    Consumes (removes) the matched request so it can't be replayed for a
    later, unrelated call with the same command text. Returns None if no
    matching, still-pending-to-consume approved request exists (denied,
    expired, or never approved).
    """
    _expire_stale()
    for aid, req in list(_store.items()):
        if req.command == command and req.profile == profile and req.approved is True:
            _store.pop(aid, None)
            return req
    return None


def _expire_stale() -> None:
    """Remove expired requests from the store."""
    now = time.time()
    expired = [aid for aid, req in _store.items() if req.expires_at < now]
    for aid in expired:
        _store.pop(aid, None)
