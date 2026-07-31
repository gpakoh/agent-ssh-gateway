"""Command gate service (P19.2).

Encapsulates the access-control + command-policy evaluation chain shared by
all command-executing routes (jobs_run, bulk_execute, ssh execute). One
implementation so the two paths cannot diverge:
  access-control gate -> effective profile -> policy decision -> audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app import state as _state
from app.access_control import AccessDeniedError, AccessPendingApprovalError
from app.auth_middleware import AuthIdentity, get_client_ip, parse_cidrs
from app.command_policy import evaluate_command_policy, parse_key_profiles, profile_for_identity
from app.config import settings
from app.metrics import metrics
from app.state import _err

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandGateDecision:
    """Outcome of the full command gate for a single command."""

    allowed: bool
    reason: str
    effective_profile: str
    policy_mode: str
    command_root: str | None = None
    requires_approval: bool = False
    approval_id: str | None = None


def resolve_effective_profile(identity: AuthIdentity | None) -> str:
    """Resolve the server-owned policy profile for an identity (no access gate)."""
    key_profiles = parse_key_profiles(settings.command_policy_key_profiles)
    return profile_for_identity(
        identity.fingerprint[:12] if identity else None,
        key_profiles=key_profiles,
        default_profile=settings.command_policy_profile,
    )


def resolve_effective_profile_with_access_gate(
    request: Request | None,
    identity: AuthIdentity | None,
    *,
    source_ip: str | None = None,
) -> str:
    """Run the access-control gate and return the effective policy profile.

    Shared access-gate core for every command-executing path. Raises
    HTTPException(403) on access-control denial. `request` may be None for
    websocket paths — then `source_ip` must be provided explicitly.
    """
    access_effective_profile: str | None = None
    if settings.access_control_enabled and _state.access_control_store is not None:
        if source_ip is None:
            assert request is not None
            source_ip = get_client_ip(request, parse_cidrs(settings.trusted_proxy_cidrs))
        try:
            access = _state.access_control_store.resolve_access_policy(
                actor_fingerprint=identity.fingerprint if identity else "",
                token_type=identity.token_type if identity else "unknown",
                source_ip=source_ip,
                requested_profile=settings.command_policy_profile,
                enforce_master=settings.access_control_enforce_master,
            )
            access_effective_profile = access.effective_profile
        except AccessDeniedError:
            raise HTTPException(status_code=403, detail=_err(403, "ACCESS_DENIED")) from None
        except AccessPendingApprovalError:
            pass

    return (
        access_effective_profile
        if access_effective_profile is not None
        else resolve_effective_profile(identity)
    )


def evaluate_with_access_gate(
    request: Request | None,
    identity: AuthIdentity | None,
    command: str,
    route: str = "",
    *,
    source_ip: str | None = None,
    raise_on_deny: bool = True,
) -> CommandGateDecision:
    """Run access-control gate + policy evaluation + audit + metrics for one command.

    By default raises HTTPException(403) on access-control denial and
    HTTPException(403) on policy denial. With raise_on_deny=False the
    decision is returned as-is (caller handles ASK-mode 202 / 403 flow);
    the only exception that can still escape is the access-control 403.
    `request` may be None for websocket paths — then `source_ip` must be
    provided explicitly.
    """
    effective_profile = resolve_effective_profile_with_access_gate(
        request, identity, source_ip=source_ip
    )

    decision = evaluate_command_policy(
        command,
        mode=settings.command_policy_mode,
        profile=effective_profile,
    )

    audit_source_ip = source_ip
    if audit_source_ip is None:
        audit_source_ip = request.client.host if request.client else "unknown"
    _state.audit_logger.log_security_event(
        "COMMAND_POLICY_DECISION",
        f"route={route or '-'}; command_root={decision.command_root}; "
        f"allowed={decision.allowed}; reason={decision.reason}; "
        f"profile={decision.profile}; mode={decision.mode}",
        audit_source_ip,
    )

    if not decision.allowed:
        metrics.record_ssh_command(
            status="denied",
            profile=decision.profile,
            command_root=decision.command_root,
        )
        if raise_on_deny:
            raise HTTPException(
                status_code=403,
                detail=_err(403, f"Command denied by policy: {decision.reason}"),
            )

    return CommandGateDecision(
        allowed=decision.allowed,
        reason=decision.reason,
        command_root=decision.command_root,
        effective_profile=effective_profile,
        policy_mode=decision.mode,
        requires_approval=decision.requires_approval,
        approval_id=decision.approval_id,
    )


def record_allowed(profile: str, command_root: str | None = None) -> None:
    """Record an allowed command metric."""
    metrics.record_ssh_command(
        status="allowed",
        profile=profile,
        command_root=command_root,
    )
