"""SSH-related routes — connect, execute, disconnect, session management, and WebSocket PTY."""

import asyncio
import logging
import os
import re
import shlex
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)

from app import state as _state
from app.access_control import AccessDeniedError
from app.auth_middleware import (
    VALID_AGENT_SCOPES,
    AuthIdentity,
    ensure_session_owner,
    get_client_ip,
    parse_cidrs,
    require_master_key,
    require_scope,
    ws_auth_check,
)
from app.config import settings
from app.models import (
    AgentTokenRefreshRequest,
    AgentTokenRefreshResponse,
    AgentTokenRequest,
    AgentTokenResponse,
    ConnectRequest,
    ConnectResponse,
    DisconnectRequest,
    DisconnectResponse,
    ExecuteArgvRequest,
    ExecuteArgvResponse,
    ExecuteRequest,
    ExecuteResponse,
    JobRunResponse,
    PrewarmRequest,
    PrewarmResponse,
    SessionConfigResponse,
    SessionInfo,
    SessionsResponse,
    SessionTimeoutRequest,
    SessionTimeoutResponse,
)
from app.output_redaction import should_redact_command_output
from app.security import rate_limit_mutation, redact_secrets, sanitize_command, validate_target_host
from app.services.command_gate import evaluate_with_access_gate, record_allowed
from app.ssh_manager import SessionNotFoundError
from app.state import _err

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ssh"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUEST_ID_RE = re.compile(r"^[a-zA-Z0-9\-]+$")
_REQUEST_ID_MAX_LEN = 64


async def _persist_command_audit(
    *,
    session_id: str,
    command: str,
    exit_code: int | None,
    duration_ms: int,
    actor_type: str,
    actor_name: str,
    actor_fingerprint: str,
    source_ip: str,
    route: str,
    request_id: str,
    decision: str = "allowed",
    reason: str = "",
    event_type: str = "command.execute",
) -> None:
    """Persist a command execution record to the PostgreSQL audit log.

    No-op when the persistent audit store is not configured, so existing
    JSONL-only deployments keep working unchanged.
    """
    store = _state.audit_log_store
    if store is None:
        return
    try:
        await store.insert_command_execution(
            session_id=session_id,
            command=command,
            exit_code=exit_code,
            duration_ms=duration_ms,
            actor_type=actor_type,
            actor_name=actor_name,
            actor_fingerprint=actor_fingerprint,
            source_ip=source_ip,
            route=route,
            request_id=request_id,
            decision=decision,
            reason=reason,
            event_type=event_type,
        )
    except Exception:
        logger.warning("audit: failed to persist command execution", exc_info=True)


def _extract_ws_request_id(websocket: WebSocket) -> str:
    """Extract and validate X-Request-ID from websocket upgrade headers.

    Returns the inbound value if valid (<=64 chars, alphanumeric + hyphens),
    otherwise generates a new UUID hex.
    """
    inbound = websocket.headers.get("x-request-id", "")
    if inbound and len(inbound) <= _REQUEST_ID_MAX_LEN and _REQUEST_ID_RE.match(inbound):
        return inbound
    return uuid.uuid4().hex


def time_to_iso(timestamp: float) -> str:
    """Convert Unix timestamp to ISO format."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def get_connect_auth_method(req: ConnectRequest) -> str:
    """Return the authentication method that will be used."""
    if req.private_key:
        return "private_key"
    if req.password:
        return "password"
    return "agent_or_default"


# ---------------------------------------------------------------------------
# Agent Token
# ---------------------------------------------------------------------------


@router.post("/api/agent/token", response_model=AgentTokenResponse)
@rate_limit_mutation(5, "minute")
async def agent_token_generate(
    req: AgentTokenRequest, request: Request, _identity: AuthIdentity = Depends(require_master_key)
):
    """Generate a short-lived agent token (separate from API_KEY).

    Requires API_KEY auth. The generated token can be rotated
    without affecting the main API_KEY. Token stored in Redis with TTL.
    The token carries a scope list that limits which endpoints it can access.
    """
    invalid_scopes = sorted(set(req.scopes) - VALID_AGENT_SCOPES)
    if invalid_scopes:
        raise HTTPException(
            status_code=400,
            detail=_err(400, f"Invalid scopes: {', '.join(invalid_scopes)}"),
        )

    from app.rbac import VALID_ROLE_NAMES, default_role_for_scopes

    role = req.role
    if role is not None and role not in VALID_ROLE_NAMES:
        raise HTTPException(
            status_code=400,
            detail=_err(
                400, f"Invalid role: {role}. Valid roles: {', '.join(sorted(VALID_ROLE_NAMES))}"
            ),
        )
    if role is None:
        role = default_role_for_scopes(req.scopes)

    import secrets as _secrets
    from datetime import timedelta

    if not _state.agent_token_store:
        raise HTTPException(
            status_code=503,
            detail=_err(503, "Agent token store not available (Redis required)"),
        )

    token = _secrets.token_urlsafe(32)
    ttl = req.ttl_seconds
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
    await _state.agent_token_store.set_token(
        token, ttl, scopes=req.scopes, role=role, labels=req.labels
    )
    logger.info(
        "Agent token generated (ttl=%ds, scopes=%s, role=%s, labels=%s)",
        ttl, req.scopes, role, req.labels,
    )
    return AgentTokenResponse(
        token=token,
        ttl=ttl,
        expires_at=expires_at.isoformat(),
        scopes=req.scopes,
        role=role,
        labels=req.labels,
    )


@router.post("/api/agent/token/refresh", response_model=AgentTokenRefreshResponse)
@rate_limit_mutation(5, "minute")
async def agent_token_refresh(
    req: AgentTokenRefreshRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Refresh (rotate) the agent token.

    Invalidates the previous agent token and issues a new one atomically.
    Only master API key can refresh tokens.
    The new token preserves the scopes of the old token — refresh never expands rights.
    """
    import secrets as _secrets
    from datetime import timedelta

    if not _state.agent_token_store:
        raise HTTPException(
            status_code=503,
            detail=_err(503, "Agent token store not available (Redis required)"),
        )

    # Validate old token and preserve its scopes, role and labels
    old_valid, old_scopes, old_meta = await _state.agent_token_store.validate_token(req.token)
    if not old_valid:
        raise HTTPException(
            status_code=400,
            detail=_err(400, "Old token is invalid or expired"),
        )

    token = _secrets.token_urlsafe(32)
    ttl = req.ttl_seconds
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
    old_role = old_meta.get("role") if isinstance(old_meta, dict) else None
    old_labels = old_meta.get("labels") if isinstance(old_meta, dict) else None
    await _state.agent_token_store.set_token(
        token, ttl, scopes=old_scopes, role=old_role, labels=old_labels
    )
    logger.info("Agent token refreshed (ttl=%ds, scopes=%s)", ttl, old_scopes)
    return AgentTokenRefreshResponse(
        token=token,
        ttl=ttl,
        expires_at=expires_at.isoformat(),
        scopes=old_scopes or [],
    )


# ---------------------------------------------------------------------------
# Session Config
# ---------------------------------------------------------------------------


@router.get("/api/config/session", response_model=SessionConfigResponse)
async def get_session_config(_identity: AuthIdentity = Depends(require_master_key)):
    """Get current session configuration."""
    active = await _state.manager.list_sessions()
    return SessionConfigResponse(
        session_timeout=_state.manager._session_timeout,
        cleanup_interval=_state.manager._cleanup_interval,
        max_sessions_per_ip=settings.max_sessions_per_ip,
        active_sessions=len(active),
    )


@router.patch("/api/config/session/timeout", response_model=SessionTimeoutResponse)
async def update_session_timeout(
    req: SessionTimeoutRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Update session timeout dynamically."""
    if not 60 <= req.timeout <= 86400:
        raise HTTPException(
            status_code=422,
            detail=_err(422, "Timeout must be between 60 and 86400 seconds"),
        )
    previous = _state.manager._session_timeout
    _state.manager._session_timeout = req.timeout
    return SessionTimeoutResponse(
        timeout=req.timeout,
        previous_timeout=previous,
    )


# ---------------------------------------------------------------------------
# SSH Connection
# ---------------------------------------------------------------------------


@router.post("/api/ssh/connect", response_model=ConnectResponse)
@rate_limit_mutation(10, "minute")
async def ssh_connect(
    req: ConnectRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:connect")),
):
    """Create a new SSH session."""
    source_ip = get_client_ip(request, parse_cidrs(settings.trusted_proxy_cidrs))

    # Access control gate
    if settings.access_control_enabled and _state.access_control_store is not None:
        try:
            _state.access_control_store.resolve_access_policy(
                actor_fingerprint=_identity.fingerprint,
                token_type=_identity.token_type,
                source_ip=source_ip,
                requested_profile=settings.command_policy_profile,
                enforce_master=settings.access_control_enforce_master,
            )
        except AccessDeniedError:
            raise HTTPException(status_code=403, detail=_err(403, "ACCESS_DENIED")) from None

    try:
        validated_ips = validate_target_host(
            req.host,
            settings.allowed_target_cidrs,
            settings.denied_target_cidrs,
        )
    except ValueError as exc:
        _state.audit_logger.log_security_event(
            "BLOCKED_TARGET_HOST",
            str(exc),
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=403, detail=_err(403, str(exc))) from exc

    _password = req.password.get_secret_value() if req.password else None
    _private_key = req.private_key.get_secret_value() if req.private_key else None
    _passphrase = req.key_passphrase.get_secret_value() if req.key_passphrase else None

    session_id = await _state.manager.create_session(
        host=req.host,
        port=req.port,
        username=req.username,
        password=_password,
        private_key=_private_key,
        key_passphrase=_passphrase,
        owner_type=_identity.token_type,
        owner_name=_identity.name,
        owner_token_fingerprint=_identity.fingerprint,
        source_ip=source_ip,
        tenant_labels=_identity.tenant_labels,
        pinned_ip=validated_ips[0],
    )

    from app.audit import emit_session_lifecycle_event as _emit_session

    _emit_session(
        event_logger=_state.event_audit_logger,
        connected=True,
        session_id=session_id,
        actor_type=_identity.token_type,
        actor_name=_identity.name or "",
        actor_fingerprint=_identity.fingerprint[:12],
        source_ip=request.client.host if request.client else "unknown",
        route="POST /api/ssh/connect",
        request_id=getattr(request.state, "request_id", ""),
    )

    if _state.session_store:
        try:
            await _state.session_store.save_session(
                session_id=session_id,
                host=req.host,
                port=req.port,
                username=req.username,
                password=_password,
                private_key=_private_key,
                key_passphrase=_passphrase,
                ttl=settings.session_timeout,
            )
        except Exception as exc:
            logger.warning("Failed to persist session %s: %s", session_id, exc)

    return ConnectResponse(session_id=session_id)


async def _await_prewarm(session_id: str, *, timeout: float = 35.0) -> None:
    """Wait for an in-flight pre-warm connection to finish.

    Used by execute/execute-argv so a command issued right after prewarm
    waits for the background connection instead of hitting "session not
    found". No-op when the session was not pre-warmed.
    """
    task = _state.prewarm_tasks.pop(session_id, None)
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=_err(504, "Pre-warmed connection not ready in time"),
        ) from None
    except Exception as exc:
        logger.warning("Pre-warm for session %s failed: %s", session_id, exc)
        raise HTTPException(
            status_code=502,
            detail=_err(502, f"Pre-warm connection failed: {exc}"),
        ) from exc


@router.post("/api/ssh/prewarm", response_model=PrewarmResponse)
@rate_limit_mutation(10, "minute")
async def ssh_prewarm(
    req: PrewarmRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:connect")),
):
    """Pre-warm an SSH session in the background.

    Accepts the same body as /api/ssh/connect but returns the session_id
    immediately while the TCP connection is established asynchronously.
    """
    source_ip = get_client_ip(request, parse_cidrs(settings.trusted_proxy_cidrs))

    # Access control gate (same as connect)
    if settings.access_control_enabled and _state.access_control_store is not None:
        try:
            _state.access_control_store.resolve_access_policy(
                actor_fingerprint=_identity.fingerprint,
                token_type=_identity.token_type,
                source_ip=source_ip,
                requested_profile=settings.command_policy_profile,
                enforce_master=settings.access_control_enforce_master,
            )
        except AccessDeniedError:
            raise HTTPException(status_code=403, detail=_err(403, "ACCESS_DENIED")) from None

    try:
        validated_ips = validate_target_host(
            req.host,
            settings.allowed_target_cidrs,
            settings.denied_target_cidrs,
        )
    except ValueError as exc:
        _state.audit_logger.log_security_event(
            "BLOCKED_TARGET_HOST",
            str(exc),
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=403, detail=_err(403, str(exc))) from exc

    session_id = str(uuid.uuid4())
    _password = req.password.get_secret_value() if req.password else None
    _private_key = req.private_key.get_secret_value() if req.private_key else None
    _passphrase = req.key_passphrase.get_secret_value() if req.key_passphrase else None

    async def _prewarm_connect() -> None:
        try:
            await _state.manager.create_session(
                host=req.host,
                port=req.port,
                username=req.username,
                password=_password,
                private_key=_private_key,
                key_passphrase=_passphrase,
                owner_type=_identity.token_type,
                owner_name=_identity.name,
                owner_token_fingerprint=_identity.fingerprint,
                source_ip=source_ip,
                session_id=session_id,
                pinned_ip=validated_ips[0],
            )
            if _state.session_store:
                try:
                    await _state.session_store.save_session(
                        session_id=session_id,
                        host=req.host,
                        port=req.port,
                        username=req.username,
                        password=_password,
                        private_key=_private_key,
                        key_passphrase=_passphrase,
                        ttl=settings.session_timeout,
                    )
                except Exception as exc:
                    logger.warning("Failed to persist prewarmed session %s: %s", session_id, exc)

            from app.audit import emit_session_lifecycle_event as _emit_session

            _emit_session(
                event_logger=_state.event_audit_logger,
                connected=True,
                session_id=session_id,
                actor_type=_identity.token_type,
                actor_name=_identity.name or "",
                actor_fingerprint=_identity.fingerprint[:12],
                source_ip=request.client.host if request.client else "unknown",
                route="POST /api/ssh/prewarm",
                request_id=getattr(request.state, "request_id", ""),
            )
            logger.info("Pre-warmed SSH session %s for %s@%s", session_id, req.username, req.host)
        except Exception as exc:
            logger.warning("Pre-warm connect failed for session %s: %s", session_id, exc)
            raise

    _state.prewarm_tasks[session_id] = asyncio.create_task(_prewarm_connect())
    return PrewarmResponse(session_id=session_id)


@router.post("/api/ssh/execute", response_model=ExecuteResponse | JobRunResponse)
@rate_limit_mutation(60, "minute")
async def ssh_execute(
    req: ExecuteRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:execute")),
):
    """Execute a command on an existing SSH session."""
    # Sanitize Command
    try:
        sanitized = sanitize_command(req.command)
    except ValueError as exc:
        _state.audit_logger.log_security_event("BLOCKED_COMMAND", str(exc), request.client.host)
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc

    # Access control + command policy gate (single implementation, see services)
    gate = evaluate_with_access_gate(
        request,
        _identity,
        req.command,
        route="POST /api/ssh/execute",
        raise_on_deny=False,
    )

    # Structured audit event
    from app.audit import emit_command_policy_decision as _emit_policy
    _emit_policy(
        event_logger=_state.event_audit_logger,
        command=req.command,
        session_id=req.session_id,
        effective_profile=gate.effective_profile,
        decision_allowed=gate.allowed,
        decision_reason=gate.reason,
        command_root=gate.command_root,
        source_ip=request.client.host if request.client else "unknown",
        route="POST /api/ssh/execute",
        actor_fingerprint=_identity.fingerprint[:12] if _identity else "",
        request_id=getattr(request.state, "request_id", ""),
        approval_id=gate.approval_id,
        requires_approval=gate.requires_approval,
    )

    if not gate.allowed:
        await _persist_command_audit(
            session_id=req.session_id,
            command=req.command,
            exit_code=None,
            duration_ms=0,
            actor_type=_identity.token_type,
            actor_name=_identity.name or "",
            actor_fingerprint=_identity.fingerprint[:12],
            source_ip=request.client.host if request.client else "unknown",
            route="POST /api/ssh/execute",
            request_id=getattr(request.state, "request_id", ""),
            decision="denied",
            reason=gate.reason,
            event_type="command.deny",
        )
        if gate.requires_approval and gate.approval_id:
            raise HTTPException(
                status_code=202,
                detail={
                    "code": 202,
                    "message": "Command requires operator approval (ASK mode)",
                    "approval_id": gate.approval_id,
                    "command_root": gate.command_root,
                    "reason": gate.reason,
                },
            )
        raise HTTPException(
            status_code=403,
            detail=_err(403, f"Command denied by policy: {gate.reason}"),
        )

    # Audit Log
    _state.audit_logger.log_command(req.session_id, sanitized, request.client.host)

    # Wait for a pre-warmed connection if this session is still connecting
    await _await_prewarm(req.session_id)

    # Session Ownership Check
    session = await _state.manager.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=_err(404, "Session not found"))
    ensure_session_owner(session, _identity)

    if req.async_mode:
        job_id = await _state.job_manager.create_job(
            session_id=req.session_id,
            command=sanitized,
            owner_id=_identity.fingerprint,
        )
        await _persist_command_audit(
            session_id=req.session_id,
            command=req.command,
            exit_code=None,
            duration_ms=0,
            actor_type=_identity.token_type,
            actor_name=_identity.name or "",
            actor_fingerprint=_identity.fingerprint[:12],
            source_ip=request.client.host if request.client else "unknown",
            route="POST /api/ssh/execute",
            request_id=getattr(request.state, "request_id", ""),
            decision="allowed",
            event_type="command.job",
        )
        return JobRunResponse(
            job_id=job_id,
            status="running",
            message="Job started",
        )

    result = await _state.manager.execute(
        session_id=req.session_id,
        command=sanitized,
        timeout=req.timeout,
    )
    record_allowed(profile=gate.effective_profile, command_root=gate.command_root)
    max_output = 10 * 1024 * 1024
    stdout = result["stdout"][:max_output]
    stderr = result["stderr"][:max_output]
    if should_redact_command_output(req.redact_output):
        stdout = redact_secrets(stdout)
        stderr = redact_secrets(stderr)

    await _persist_command_audit(
        session_id=req.session_id,
        command=req.command,
        exit_code=result["exit_code"],
        duration_ms=int(round(result.get("duration", 0.0) * 1000)),
        actor_type=_identity.token_type,
        actor_name=_identity.name or "",
        actor_fingerprint=_identity.fingerprint[:12],
        source_ip=request.client.host if request.client else "unknown",
        route="POST /api/ssh/execute",
        request_id=getattr(request.state, "request_id", ""),
        decision="allowed",
        event_type="command.execute",
    )
    return ExecuteResponse(
        stdout=stdout,
        stderr=stderr,
        exit_code=result["exit_code"],
        duration=result.get("duration", 0.0),
    )


@router.post("/api/ssh/execute-argv", response_model=ExecuteArgvResponse)
@rate_limit_mutation(60, "minute")
async def ssh_execute_argv(
    req: ExecuteArgvRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:execute:argv")),
):
    """Execute an argv command with stdin support on an existing SSH session.

    argv is serialized via shlex.join — no bash -c wrapping.
    """
    command_str = shlex.join(req.argv)

    # Access control + command policy gate (single implementation, see services)
    gate = evaluate_with_access_gate(
        request,
        _identity,
        command_str,
        route="POST /api/ssh/execute-argv",
        raise_on_deny=False,
    )

    # Structured audit event
    from app.audit import emit_command_policy_decision as _emit_argv
    _emit_argv(
        event_logger=_state.event_audit_logger,
        command=command_str,
        session_id=req.session_id,
        effective_profile=gate.effective_profile,
        decision_allowed=gate.allowed,
        decision_reason=gate.reason,
        command_root=gate.command_root,
        source_ip=request.client.host if request.client else "unknown",
        route="POST /api/ssh/execute-argv",
        actor_fingerprint=_identity.fingerprint[:12] if _identity else "",
        request_id=getattr(request.state, "request_id", ""),
    )

    if not gate.allowed:
        await _persist_command_audit(
            session_id=req.session_id,
            command=command_str,
            exit_code=None,
            duration_ms=0,
            actor_type=_identity.token_type,
            actor_name=_identity.name or "",
            actor_fingerprint=_identity.fingerprint[:12],
            source_ip=request.client.host if request.client else "unknown",
            route="POST /api/ssh/execute-argv",
            request_id=getattr(request.state, "request_id", ""),
            decision="denied",
            reason=gate.reason,
            event_type="command.deny",
        )
        if gate.requires_approval and gate.approval_id:
            raise HTTPException(
                status_code=202,
                detail={
                    "code": 202,
                    "message": "Command requires operator approval (ASK mode)",
                    "approval_id": gate.approval_id,
                    "command_root": gate.command_root,
                    "reason": gate.reason,
                },
            )
        raise HTTPException(
            status_code=403,
            detail=_err(403, f"Command denied by policy: {gate.reason}"),
        )

    _state.audit_logger.log_command(req.session_id, command_str, request.client.host)

    # Wait for a pre-warmed connection if this session is still connecting
    await _await_prewarm(req.session_id)

    session = await _state.manager.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=_err(404, "Session not found"))
    ensure_session_owner(session, _identity)

    final_command = command_str
    if req.cwd:
        final_command = f"cd {shlex.quote(req.cwd)} && {command_str}"

    stdin_bytes = req.stdin.encode("utf-8") if req.stdin else b""

    result = await _state.manager.execute_argv(
        session_id=req.session_id,
        command_str=final_command,
        stdin_data=stdin_bytes,
        timeout=req.timeout_s,
    )
    record_allowed(profile=gate.effective_profile, command_root=gate.command_root)

    max_output = 10 * 1024 * 1024
    stdout = result["stdout"][:max_output]
    stderr = result["stderr"][:max_output]

    await _persist_command_audit(
        session_id=req.session_id,
        command=final_command,
        exit_code=result["exit_code"],
        duration_ms=int(round(result.get("duration", 0.0) * 1000)),
        actor_type=_identity.token_type,
        actor_name=_identity.name or "",
        actor_fingerprint=_identity.fingerprint[:12],
        source_ip=request.client.host if request.client else "unknown",
        route="POST /api/ssh/execute-argv",
        request_id=getattr(request.state, "request_id", ""),
        decision="allowed",
        event_type="command.execute",
    )

    return ExecuteArgvResponse(
        stdout=stdout,
        stderr=stderr,
        exit_code=result["exit_code"],
        duration=result.get("duration", 0.0),
    )


@router.post("/api/ssh/disconnect", response_model=DisconnectResponse)
async def ssh_disconnect(
    req: DisconnectRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:disconnect")),
):
    """Close an SSH session."""
    session = await _state.manager.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=_err(404, "Session not found"))
    ensure_session_owner(session, _identity)
    await _state.manager.disconnect(req.session_id)

    from app.audit import emit_session_lifecycle_event as _emit_session

    _emit_session(
        event_logger=_state.event_audit_logger,
        connected=False,
        session_id=req.session_id,
        actor_type=_identity.token_type,
        actor_name=_identity.name or "",
        actor_fingerprint=_identity.fingerprint[:12],
        source_ip=request.client.host if request.client else "unknown",
        route="POST /api/ssh/disconnect",
        request_id=getattr(request.state, "request_id", ""),
        reason="manual",
    )

    if _state.session_store:
        try:
            await _state.session_store.deactivate_session(req.session_id)
        except Exception as exc:
            logger.warning("Failed to deactivate session %s in store: %s", req.session_id, exc)

    return DisconnectResponse()


@router.get("/api/ssh/sessions", response_model=SessionsResponse)
async def ssh_sessions(
    request: Request, _identity: AuthIdentity = Depends(require_scope("ssh:execute"))
):
    """List active SSH sessions with details.

    Master token and admin role see all sessions.
    Other agents see only sessions they created plus sessions whose
    tenant labels intersect their own (cross-tenant grant).
    """
    from app.rbac import session_visible_to

    records = await _state.manager.list_sessions()

    if _identity.token_type != "master" and _identity.role != "admin":
        records = [r for r in records if session_visible_to(r, _identity)]

    now = time.time()
    sessions = []
    for r in records:
        connected = r.is_connected()
        idle = round(now - r.last_activity, 1)
        if connected:
            status = "active" if idle < 60 else "idle"
        else:
            status = "reconnecting"
        sessions.append(
            SessionInfo(
                session_id=r.session_id,
                host=r.host,
                port=r.port,
                username=r.username,
                connected_at=time_to_iso(r.connected_at),
                last_command_at=time_to_iso(r.last_activity) if r.last_activity else None,
                idle_seconds=idle,
                owner_type=r.owner_type,
                owner_name=r.owner_name,
                status=status,
                created_at=time_to_iso(r.connected_at),
            )
        )
    return SessionsResponse(sessions=sessions, count=len(sessions))


# ---------------------------------------------------------------------------
# Heartbeat / Keepalive
# ---------------------------------------------------------------------------


@router.post("/api/ssh/heartbeat")
async def ssh_heartbeat(
    req: DisconnectRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:execute")),
):
    """Refresh session timeout by touching it."""
    record = await _state.manager.get_session(req.session_id)
    if not record:
        raise SessionNotFoundError(f"Session {req.session_id} not found")
    ensure_session_owner(record, _identity)
    record.touch()
    return {"status": "ok", "session_id": req.session_id, "idle_time": record.idle_time}


@router.get("/api/ssh/session/{session_id}/health")
async def session_health(
    session_id: str,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:execute")),
):
    """Check session health and auto-reconnect if needed."""
    record = await _state.manager.get_session(session_id)
    if not record:
        raise SessionNotFoundError(f"Session {session_id} not found")
    ensure_session_owner(record, _identity)

    is_connected = record.is_connected()

    if not is_connected:
        logger.info("Session %s disconnected, attempting auto-reconnect", session_id)
        reconnected = await _state.manager.reconnect(session_id)
        return {
            "session_id": session_id,
            "connected": reconnected,
            "reconnected": True,
            "reconnect_count": record.reconnect_count,
            "reconnect_reason": record.last_reconnect_reason or "timeout",
            "idle_time": record.idle_time,
        }

    return {
        "session_id": session_id,
        "connected": True,
        "reconnected": False,
        "reconnect_count": record.reconnect_count,
        "reconnect_reason": None,
        "idle_time": record.idle_time,
    }


# ---------------------------------------------------------------------------
# Websocket Streaming
# ---------------------------------------------------------------------------


@router.websocket("/api/ssh/execute/stream")
async def ssh_execute_stream(websocket: WebSocket):
    """Execute a command and stream output via WebSocket."""
    identity = await ws_auth_check(
        websocket, settings, _state.agent_token_store, required_scope="ssh:execute"
    )
    if isinstance(identity, tuple):
        await websocket.close(code=identity[0], reason=identity[1])
        return
    await websocket.accept()
    _state.active_websockets.add(websocket)
    try:
        data = await asyncio.wait_for(websocket.receive_json(), timeout=600)
        session_id = data.get("session_id", "")
        command = data.get("command", "")

        if not session_id or not command:
            await websocket.send_json(
                {"type": "error", "data": "session_id and command are required"}
            )
            await websocket.close()
            return

        try:
            command = sanitize_command(command)
        except ValueError as exc:
            _state.audit_logger.log_security_event(
                "BLOCKED_COMMAND", str(exc), websocket.client.host
            )
            await websocket.send_json({"type": "error", "data": str(exc)})
            await websocket.close()
            return

        ws_source_ip = websocket.client.host if websocket.client else "unknown"

        # Access control + command policy gate (single implementation, see services)
        try:
            gate = evaluate_with_access_gate(
                None,
                identity,
                command,
                route="WS /api/ssh/execute/stream",
                source_ip=ws_source_ip,
                raise_on_deny=False,
            )
        except HTTPException:
            # With raise_on_deny=False the only raise path is access-control denial
            await websocket.send_json(
                {"type": "error", "code": "ACCESS_DENIED", "message": "Actor denied by access control"}
            )
            await websocket.close()
            return

        # Structured audit event
        from app.audit import emit_command_policy_decision as _emit_ws
        _emit_ws(
            event_logger=_state.event_audit_logger,
            command=command,
            session_id=session_id,
            effective_profile=gate.effective_profile,
            decision_allowed=gate.allowed,
            decision_reason=gate.reason,
            command_root=gate.command_root,
            source_ip=ws_source_ip,
            route="WS /api/ssh/execute/stream",
            actor_fingerprint=identity.fingerprint[:12] if identity else "",
            request_id=_extract_ws_request_id(websocket),
        )

        if not gate.allowed:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "COMMAND_POLICY_DENIED",
                    "message": f"Command denied by policy: {gate.reason}",
                }
            )
            await websocket.close()
            return

        record = await _state.manager.get_session(session_id)
        if not record:
            await websocket.send_json({"type": "error", "data": "Session not found"})
            await websocket.close()
            return

        try:
            ensure_session_owner(record, identity)
        except HTTPException:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "SESSION_OWNERSHIP",
                    "message": "Agent token cannot access this session",
                }
            )
            await websocket.close()
            return

        async for msg_type, msg_data in _state.manager.execute_stream(session_id, command):
            await websocket.send_json({"type": msg_type, "data": msg_data})

        record_allowed(profile=gate.effective_profile, command_root=gate.command_root)

    except WebSocketDisconnect:
        logger.info("Websocket Client Disconnected")
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
        try:
            await websocket.send_json({"type": "error", "data": str(exc)})
        except Exception:
            pass
        await websocket.close()
    finally:
        _state.active_websockets.discard(websocket)


@router.websocket("/api/ssh/pty/{session_id}/stream")
async def pty_stream(websocket: WebSocket, session_id: str):
    """Interactive PTY via WebSocket."""
    identity = await ws_auth_check(
        websocket, settings, _state.agent_token_store, required_scope="ssh:execute"
    )
    if isinstance(identity, tuple):
        await websocket.close(code=identity[0], reason=identity[1])
        return

    record = await _state.manager.get_session(session_id)
    if not record:
        await websocket.close(code=4403, reason="Session not found")
        return

    try:
        ensure_session_owner(record, identity)
    except HTTPException:
        await websocket.close(code=4403, reason="Agent token cannot access this session")
        return

    # Access control gate (PTY)
    if settings.access_control_enabled and _state.access_control_store is not None:
        pty_source_ip = websocket.client.host if websocket.client else "unknown"
        try:
            _state.access_control_store.resolve_access_policy(
                actor_fingerprint=identity.fingerprint,
                token_type=identity.token_type,
                source_ip=pty_source_ip,
                requested_profile=settings.command_policy_profile,
                enforce_master=settings.access_control_enforce_master,
            )
        except AccessDeniedError:
            await websocket.close(code=4403, reason="ACCESS_DENIED")
            return

    await websocket.accept()
    _state.active_websockets.add(websocket)

    channel = None
    try:
        init_data = await asyncio.wait_for(websocket.receive_json(), timeout=600)
        term = str(init_data.get("term", "xterm-256color"))[:64]
        rows = min(max(int(init_data.get("rows", 24)), 1), 500)
        cols = min(max(int(init_data.get("cols", 80)), 1), 1000)

        channel = await _state.manager.create_pty_channel(session_id, term, rows, cols)

        async def read_from_channel():
            try:
                while True:
                    if channel.recv_ready():
                        data = channel.recv(4096).decode("utf-8", errors="replace")
                        await websocket.send_json({"type": "output", "data": data})
                    elif channel.closed:
                        break
                    await asyncio.sleep(0.01)
            except Exception:
                pass

        async def write_to_channel():
            try:
                while True:
                    msg = await websocket.receive_json()
                    msg_type = msg.get("type")
                    if msg_type == "input":
                        data = msg.get("data", "")
                        if not isinstance(data, str):
                            continue
                        channel.send(data[:65536])
                    elif msg_type == "resize":
                        channel.resize_pty(
                            width=min(max(int(msg.get("cols", 80)), 1), 1000),
                            height=min(max(int(msg.get("rows", 24)), 1), 500),
                        )
                    elif msg_type == "close":
                        break
            except WebSocketDisconnect:
                pass

        await asyncio.gather(read_from_channel(), write_to_channel())
    except Exception as exc:
        logger.error("PTY WebSocket error: %s", exc)
    finally:
        _state.active_websockets.discard(websocket)
        if channel:
            try:
                channel.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Port Checker
# ---------------------------------------------------------------------------


@router.get("/api/ssh/check-port")
@rate_limit_mutation(60, "minute")
async def check_port(
    request: Request,
    host: str = Query(..., description="Target hostname or IP"),
    port: int = Query(22, ge=1, le=65535, description="Target port"),
    timeout: float = Query(5.0, ge=0.5, le=30.0, description="Connection timeout in seconds"),
    _identity: AuthIdentity = Depends(require_scope("ssh:port-check")),
):
    """Check if a remote TCP port is reachable. Requires API authentication."""
    try:
        validated_ips = validate_target_host(
            host,
            settings.allowed_target_cidrs,
            settings.denied_target_cidrs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=_err(403, str(exc))) from exc

    start = time.monotonic()
    try:
        # Connect to the already-validated IP, not `host` — otherwise this
        # is a second, independent DNS lookup a low-TTL record could answer
        # differently from the one validate_target_host just checked
        # (DNS rebinding bypassing the allowed/denied CIDR policy).
        _reader, _writer = await asyncio.wait_for(
            asyncio.open_connection(validated_ips[0], port),
            timeout=timeout,
        )
        _writer.close()
        await _writer.wait_closed()
        elapsed = int((time.monotonic() - start) * 1000)
        return {"host": host, "port": port, "reachable": True, "duration_ms": elapsed}
    except (TimeoutError, OSError, ConnectionError):
        elapsed = int((time.monotonic() - start) * 1000)
        return {"host": host, "port": port, "reachable": False, "duration_ms": elapsed}


# ---------------------------------------------------------------------------
# Environment Inspect
# ---------------------------------------------------------------------------


@router.get("/api/ssh/session/{session_id}/env")
async def session_env(
    session_id: str,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:execute")),
    prefix: str = Query(None, description="Filter env vars by prefix (e.g. PATH)"),
):
    """Read environment variables from an active SSH session."""
    record = await _state.manager.get_session(session_id)
    if not record:
        raise SessionNotFoundError(f"Session {session_id} not found")
    ensure_session_owner(record, _identity)
    result = await _state.manager.execute(session_id=session_id, command="printenv", timeout=10)
    if result["exit_code"] != 0:
        raise HTTPException(
            status_code=502, detail=_err(502, f"Failed to read env: {result['stderr']}")
        )

    env = {}
    for line in result["stdout"].splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        if prefix and not key.startswith(prefix):
            continue
        env[key] = val
    return env


# ---------------------------------------------------------------------------
# SSH Key Upload
# ---------------------------------------------------------------------------


@router.post("/api/ssh/keys")
async def upload_ssh_key(
    file: UploadFile = File(...),
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Upload an SSH private key. Stored in ./ssh_keys/."""
    if not settings.ssh_key_upload_enabled:
        raise HTTPException(
            status_code=403,
            detail=_err(403, "SSH key upload is disabled"),
        )

    content = await file.read()
    if len(content) > 64 * 1024:
        raise HTTPException(status_code=400, detail=_err(400, "Key file too large (max 64KB)"))
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400, detail=_err(400, "Key must be valid UTF-8 text")
        ) from None

    if not text.startswith("-----BEGIN"):
        raise HTTPException(status_code=400, detail=_err(400, "Not a valid private key format"))

    keys_dir = settings.ssh_key_upload_dir
    os.makedirs(keys_dir, exist_ok=True)

    raw_name = file.filename or f"key-{uuid.uuid4().hex[:8]}.pem"
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "", raw_name)
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(status_code=400, detail=_err(400, "Invalid key filename"))

    keys_dir_path = Path(keys_dir).resolve()
    fpath = keys_dir_path / safe_name
    if not str(fpath.resolve()).startswith(str(keys_dir_path)):
        raise HTTPException(status_code=400, detail=_err(400, "Path traversal detected"))

    with open(fpath, "w") as f:
        f.write(text)
    os.chmod(fpath, 0o600)

    return {"name": safe_name, "path": str(fpath), "size": len(content)}
