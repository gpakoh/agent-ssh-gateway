"""Auth middleware: API key + IP allowlist + scope enforcement for agent-ssh-gateway."""

import hashlib
import ipaddress
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, WebSocket

from app.agent_token_store import AgentTokenStore
from app.config import Settings, settings
from app.rbac import (
    default_role_for_scopes,
    labels_overlap,
    role_allows_scope,
    scopes_for_role,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token Fingerprint
# ---------------------------------------------------------------------------


def token_fingerprint(token: str) -> str:
    """Stable non-reversible token fingerprint for ownership checks."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Auth Identity — carried on request.state after auth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthIdentity:
    """Identifies the caller after successful authentication.

    - ``master`` tokens (API_KEY) always have ``("*",)`` scopes — full access.
    - ``agent`` tokens have a restricted scope tuple.
    - ``role`` (admin/operator/viewer/custom) sits on top of scopes and is
      resolved by ``app.rbac``; ``tenant_labels`` enable cross-tenant grants.
    """

    token_type: str
    token: str
    name: str | None = None
    scopes: tuple[str, ...] = ()
    role: str | None = None
    role_permissions: frozenset[str] | None = None
    tenant_labels: tuple[str, ...] = ()
    via_cookie: bool = False

    @property
    def fingerprint(self) -> str:
        return token_fingerprint(self.token)


# ---------------------------------------------------------------------------
# Valid scopes for agent tokens
# ---------------------------------------------------------------------------

VALID_AGENT_SCOPES: set[str] = {
    "ssh:connect",
    "ssh:execute",
    "ssh:execute:argv",
    "ssh:pty",
    "ssh:disconnect",
    "ssh:files",
    "ssh:port-check",
    "jobs:read",
    "jobs:run",
    "auth:read",
    "diagnostics:read",
    "project:read",
    "project:patch",
    "project:write",
}

# ---------------------------------------------------------------------------
# CIDR Helpers
# ---------------------------------------------------------------------------


def parse_cidrs(value: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks = []
    for part in value.split(","):
        part = part.strip()
        if part:
            try:
                networks.append(ipaddress.ip_network(part, strict=False))
            except ValueError as exc:
                logger.warning("Invalid CIDR %r: %s", part, exc)
    return networks


def get_client_ip(
    request: Request, trusted_proxy_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> str:
    client_host = request.client.host if request.client else "127.0.0.1"

    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and _is_trusted(client_host, trusted_proxy_networks):
        ips = [ip.strip() for ip in forwarded.split(",")]
        for ip in reversed(ips):
            if not _is_trusted(ip, trusted_proxy_networks):
                return ip
        return ips[-1]

    return client_host


def _is_trusted(
    ip_str: str, trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in trusted_networks)


def is_ip_allowed(
    ip_str: str, allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in allowed_networks)


async def is_agent_token_valid(
    settings: Settings, provided: str, token_store: AgentTokenStore | None = None
) -> AuthIdentity | None:
    if not provided:
        return None
    if token_store is not None and token_store.connected:
        valid, scopes, meta = await token_store.validate_token(provided)
        if not valid:
            return None
        role = None
        tenant_labels: tuple[str, ...] = ()
        if isinstance(meta, dict):
            role = meta.get("role")
            labels = meta.get("labels") or []
            tenant_labels = tuple(labels) if isinstance(labels, list) else ()
            if role is None:
                role = default_role_for_scopes(scopes)
        return AuthIdentity(
            token_type="agent",
            token=provided,
            name="agent",
            scopes=tuple(scopes or ()),
            role=role,
            tenant_labels=tenant_labels,
        )
    if not settings.agent_token:
        return None
    expires_at = getattr(settings, "agent_token_expires_at", None)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if datetime.now(UTC) >= expires_at:
            return None
    if not secrets.compare_digest(provided, settings.agent_token):
        return None
    return AuthIdentity(
        token_type="agent",
        token=provided,
        name="agent",
        scopes=tuple(settings.agent_token_scopes),
        role=default_role_for_scopes(settings.agent_token_scopes),
    )


async def verify_api_key(
    request: Request,
    expected_key: str,
    extra_key: str = "",
    settings: Settings | None = None,
    token_store: AgentTokenStore | None = None,
) -> AuthIdentity | None:
    provided = request.headers.get("X-API-Key", "")
    if not provided:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]
    if not provided:
        return None
    if secrets.compare_digest(provided, expected_key):
        return AuthIdentity(token_type="master", token=provided, name="master", scopes=("*",))
    if settings is not None:
        return await is_agent_token_valid(settings, provided, token_store)
    if extra_key and secrets.compare_digest(provided, extra_key):
        return AuthIdentity(token_type="master", token=provided, name="master", scopes=("*",))
    return None


async def require_any_auth(
    request: Request,
) -> AuthIdentity:
    """FastAPI Depends: accept master API key or agent token (rejects unauthenticated).

    Use on endpoints that need to know the caller identity but are available
    to both master keys and agent tokens.  Falls back to the identity that the
    global middleware already stored on ``request.state.auth_identity``.
    """
    identity: AuthIdentity | None = getattr(request.state, "auth_identity", None)
    if identity is not None:
        return identity
    if not settings.api_auth_enabled:
        return AuthIdentity(token_type="master", token="", name="auth-disabled", scopes=("*",))
    identity = await verify_api_key(request, settings.api_key, settings.agent_token, settings, None)
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail={
                "message": "Authentication required. Provide a valid X-API-Key header",
                "code": "AUTH_REQUIRED",
                "retryable": False,
                "hint": "Send the master API key or an agent token in the X-API-Key header",
            },
        )
    return identity


async def require_master_key(
    request: Request,
) -> AuthIdentity:
    """FastAPI Depends: accept only the master API key (rejects agent tokens).

    Use on privileged endpoints that should never be accessible to agent tokens.
    If ``api_auth_enabled`` is ``False``, allows all requests.
    """
    if not settings.api_auth_enabled:
        return AuthIdentity(token_type="master", token="", name="auth-disabled", scopes=("*",))
    identity = await verify_master_api_key(request, settings.api_key)
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail={
                "message": "Master API key required — agent token not accepted",
                "code": "MASTER_KEY_REQUIRED",
                "retryable": False,
            },
        )
    return identity


async def verify_master_api_key(request: Request, expected_key: str) -> AuthIdentity | None:
    """Verify only the master API key — rejects agent tokens.

    Use this for privileged operations like agent token management.
    Agent tokens must not be able to create or refresh other tokens.
    """
    provided = request.headers.get("X-API-Key", "")
    if not provided:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]
    if not provided:
        return None
    if secrets.compare_digest(provided, expected_key):
        return AuthIdentity(token_type="master", token=provided, name="master", scopes=("*",))
    return None


# ---------------------------------------------------------------------------
# Always‑public Paths (even When Auth Is Enabled)
# ---------------------------------------------------------------------------

ALWAYS_PUBLIC = frozenset({"/", "/health", "/api/capabilities"})
PUBLIC_AUTH_PATHS = frozenset({
    "/api/auth/check",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/verify",
    "/api/auth/oauth/authorize",
    "/api/auth/oauth/callback",
    "/api/auth/oauth/config",
})


def _normalise_path(path: str) -> str:
    return path.rstrip("/") or "/"


# ---------------------------------------------------------------------------
# Main Auth Check
# ---------------------------------------------------------------------------


async def auth_check(
    request: Request, settings: Settings, token_store: AgentTokenStore | None = None
) -> HTTPException | None:
    if request.method == "OPTIONS":
        return None

    path = _normalise_path(request.url.path)

    # Auth Disabled — Everything Is Public
    if not settings.api_auth_enabled:
        request.state.auth_identity = AuthIdentity(
            token_type="master",
            token="",
            name="auth-disabled",
            scopes=("*",),
        )
        return None

    # Always‑public Health Endpoint
    if request.method == "GET" and path in ALWAYS_PUBLIC:
        return None

    # Static files (Web UI) are always public
    if request.method == "GET" and (path == "/static" or path.startswith("/static/")):
        return None

    # Public auth endpoints (register, login, verify, check — no API key required)
    if path in PUBLIC_AUTH_PATHS:
        return None

    # Fail-closed: Auth Enabled But No Key Configured
    if not settings.api_key:
        logger.error("Api_auth_enabled=true But API_KEY Is Not Configured")
        return HTTPException(
            status_code=503,
            detail={
                "message": "API authentication is enabled but API_KEY is not configured",
                "code": "AUTH_MISCONFIGURED",
                "retryable": False,
                "hint": "Set API_KEY environment variable and restart the gateway",
                "http_status": 503,
            },
        )

    # IP Allowlist
    allowed_nets = parse_cidrs(settings.allowed_client_cidrs)
    trusted_nets = parse_cidrs(settings.trusted_proxy_cidrs)

    if not allowed_nets and settings.allowed_client_cidrs.strip():
        logger.error(
            "ALLOWED_CLIENT_CIDRS=%r produced no valid networks",
            settings.allowed_client_cidrs,
        )
        return HTTPException(
            status_code=503,
            detail={
                "message": "ALLOWED_CLIENT_CIDRS contains no valid CIDR networks",
                "code": "AUTH_MISCONFIGURED",
                "retryable": False,
                "hint": "Fix ALLOWED_CLIENT_CIDRS environment variable",
                "http_status": 503,
            },
        )
    if not trusted_nets and settings.trusted_proxy_cidrs.strip():
        logger.error(
            "TRUSTED_PROXY_CIDRS=%r produced no valid networks",
            settings.trusted_proxy_cidrs,
        )
        return HTTPException(
            status_code=503,
            detail={
                "message": "TRUSTED_PROXY_CIDRS contains no valid CIDR networks",
                "code": "AUTH_MISCONFIGURED",
                "retryable": False,
                "hint": "Fix TRUSTED_PROXY_CIDRS environment variable",
                "http_status": 503,
            },
        )

    client_ip = get_client_ip(request, trusted_nets)

    if not is_ip_allowed(client_ip, allowed_nets):
        logger.warning("Blocked request from disallowed IP %s to %s", client_ip, path)
        return HTTPException(
            status_code=403,
            detail={
                "message": "Access denied: client IP is not in the allowed range",
                "code": "IP_NOT_ALLOWED",
                "retryable": False,
                "hint": "Ensure your request originates from an allowed network",
                "http_status": 403,
            },
        )

    # API Key Check (also Accept Agent_token)
    identity = await verify_api_key(
        request, settings.api_key, settings.agent_token, settings, token_store
    )
    if identity is not None:
        request.state.auth_identity = identity
        return None

    # JWT fallback for web UI (Bearer token from login/register)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        from app.user_auth import verify_jwt

        payload = verify_jwt(auth_header[7:])
        if payload is not None:
            role = payload.get("role", "admin")
            request.state.auth_identity = AuthIdentity(
                token_type="web-ui",
                token=auth_header[7:],
                name=payload["sub"],
                scopes=scopes_for_role(role),
                role=role,
            )
            return None

    # JWT fallback via httpOnly cookie (T79.18)
    from app.user_auth import AUTH_COOKIE_NAME, verify_jwt

    cookie_token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if cookie_token:
        payload = verify_jwt(cookie_token)
        if payload is not None:
            role = payload.get("role", "admin")
            if not _cookie_csrf_ok(request):
                return HTTPException(
                    status_code=403,
                    detail={
                        "message": "CSRF check failed: cross-origin state change rejected",
                        "code": "CSRF_BLOCKED",
                        "retryable": False,
                        "http_status": 403,
                    },
                )
            request.state.auth_identity = AuthIdentity(
                token_type="web-ui",
                token=cookie_token,
                name=payload["sub"],
                scopes=scopes_for_role(role),
                role=role,
                via_cookie=True,
            )
            return None

    return HTTPException(
        status_code=401,
        detail={
            "message": "Invalid or missing API key.",
            "code": "INVALID_API_KEY",
            "retryable": False,
            "hint": "Provide a valid API key via the X-API-Key header",
            "http_status": 401,
        },
    )


def _cookie_csrf_ok(request: Request) -> bool:
    """CSRF guard for cookie-authenticated state changes (T79.18).

    SameSite=Strict already prevents the browser from sending the cookie on
    cross-site requests; this is a defence-in-depth check for older clients
    that ignore SameSite. Safe methods are always allowed; state-changing
    requests must present an Origin/Referer matching the Host.
    """
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return True
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    host = request.headers.get("host", "")
    if not origin and not referer:
        return True
    candidate = origin or referer
    from urllib.parse import urlparse

    parsed = urlparse(candidate)
    return bool(parsed.hostname) and parsed.netloc == host


# ---------------------------------------------------------------------------
# Websocket Auth Check
# ---------------------------------------------------------------------------

CLOSE_POLICY_VIOLATION = 1008


async def ws_auth_check(
    websocket: WebSocket,
    settings: Settings,
    token_store: AgentTokenStore | None = None,
    required_scope: str = "",
) -> AuthIdentity | tuple[int, str]:
    """WebSocket auth guard — call before accept().

    Returns AuthIdentity on success.
    Returns (close_code, reason) tuple if the connection should be rejected.
    """
    if not settings.api_auth_enabled:
        return AuthIdentity(token_type="master", token="", name="auth-disabled", scopes=("*",))

    if not settings.api_key:
        logger.error("Api_auth_enabled=true But API_KEY Is Not Configured")
        return (CLOSE_POLICY_VIOLATION, "Server configuration error")

    allowed_nets = parse_cidrs(settings.allowed_client_cidrs)
    trusted_nets = parse_cidrs(settings.trusted_proxy_cidrs)

    if not allowed_nets and settings.allowed_client_cidrs.strip():
        logger.error(
            "ALLOWED_CLIENT_CIDRS=%r produced no valid networks",
            settings.allowed_client_cidrs,
        )
        return (CLOSE_POLICY_VIOLATION, "Server configuration error")
    if not trusted_nets and settings.trusted_proxy_cidrs.strip():
        logger.error(
            "TRUSTED_PROXY_CIDRS=%r produced no valid networks",
            settings.trusted_proxy_cidrs,
        )
        return (CLOSE_POLICY_VIOLATION, "Server configuration error")

    client_host = websocket.client.host if websocket.client else "127.0.0.1"
    forwarded = websocket.headers.get("X-Forwarded-For", "")
    if forwarded and _is_trusted(client_host, trusted_nets):
        ips = [ip.strip() for ip in forwarded.split(",")]
        for ip in reversed(ips):
            if not _is_trusted(ip, trusted_nets):
                client_ip = ip
                break
        else:
            client_ip = ips[-1]
    else:
        client_ip = client_host

    if not is_ip_allowed(client_ip, allowed_nets):
        logger.warning("Blocked WebSocket from disallowed IP %s", client_ip)
        return (CLOSE_POLICY_VIOLATION, "Access denied: client IP not allowed")

    origin = websocket.headers.get("origin", "")
    if origin:
        host = websocket.headers.get("host", "")
        from urllib.parse import urlparse

        origin_host = urlparse(origin).netloc
        if origin_host and origin_host != host:
            logger.warning(
                "Blocked WebSocket with mismatched Origin %s vs Host %s", origin, host
            )
            return (CLOSE_POLICY_VIOLATION, "Access denied: cross-site WebSocket rejected")

    provided = websocket.headers.get("X-API-Key", "")
    if not provided:
        auth_header = websocket.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]
    if not provided:
        # The browser automatically attaches the httpOnly auth cookie to the
        # WebSocket handshake (same-origin, like any other request) — this
        # is what the shipped Web UI actually relies on (T79.18); check it
        # before the query-param fallback so the common browser case never
        # touches ?token= at all (T79.10 — a query-string token risks
        # leaking into browser history / intermediate proxy logs beyond
        # this app's own access logs, which _RedactTokenFilter covers).
        from app.user_auth import AUTH_COOKIE_NAME

        provided = websocket.cookies.get(AUTH_COOKIE_NAME, "")
    if not provided:
        # Fallback for non-browser WS clients that can't set custom headers
        # or retain cookies (e.g. a bare websocket-client script).
        provided = websocket.query_params.get("token", "")
    if not provided:
        return (CLOSE_POLICY_VIOLATION, "Invalid or missing API key")

    identity: AuthIdentity | None = None
    if secrets.compare_digest(provided, settings.api_key):
        identity = AuthIdentity(token_type="master", token=provided, name="master", scopes=("*",))
    else:
        from app.user_auth import verify_jwt

        payload = verify_jwt(provided)
        if payload is not None:
            role = payload.get("role", "admin")
            identity = AuthIdentity(
                token_type="web-ui",
                token=provided,
                name=payload["sub"],
                scopes=scopes_for_role(role),
                role=role,
                via_cookie=True,
            )
        else:
            identity = await is_agent_token_valid(settings, provided, token_store)

    if identity is None:
        return (CLOSE_POLICY_VIOLATION, "Invalid or missing API key")

    if required_scope and identity.token_type != "master" and "*" not in identity.scopes:
        if required_scope not in identity.scopes and not role_allows_scope(
            identity.role, identity.role_permissions, required_scope
        ):
            return (CLOSE_POLICY_VIOLATION, f"Missing required scope: {required_scope}")

    return identity


# ---------------------------------------------------------------------------
# Scope Check — used as FastAPI Depends on individual endpoints
# ---------------------------------------------------------------------------


def require_scope(required: str) -> Callable[[Request], Awaitable[AuthIdentity]]:
    """FastAPI dependency: require a specific scope on the endpoint.

    Master API key bypasses all scope checks.
    Agent tokens must have the required scope in their scopes list.

    The returned function exposes ``.required_scope`` for introspection
    by the route auth contract test.
    """

    async def _scope_check(request: Request) -> AuthIdentity:
        identity: AuthIdentity | None = getattr(request.state, "auth_identity", None)
        if identity is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "message": "Authentication required",
                    "code": "UNAUTHORIZED",
                    "retryable": False,
                    "http_status": 401,
                },
            )
        if identity.token_type == "master":
            return identity
        if "*" in identity.scopes or required in identity.scopes:
            return identity
        if role_allows_scope(identity.role, identity.role_permissions, required):
            return identity
        raise HTTPException(
            status_code=403,
            detail={
                "message": f"Missing required scope: {required}",
                "code": "MISSING_SCOPE",
                "retryable": False,
                "hint": f"This endpoint requires the '{required}' scope",
                "http_status": 403,
            },
        )

    _scope_check.required_scope = required
    return _scope_check


def ensure_session_owner(session: Any, identity: AuthIdentity) -> None:
    """Allow master/admin to access any session, agent only its own sessions.

    Non-admin agents may also access sessions whose tenant labels intersect
    their own (cross-tenant grant).
    """
    if identity.token_type == "master":
        return
    if identity.role == "admin":
        return
    if getattr(session, "owner_type", None) != "agent":
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Agent token cannot access this session",
                "code": "SESSION_OWNERSHIP",
                "retryable": False,
                "hint": "Use the agent token that created this session",
                "http_status": 403,
            },
        )
    if getattr(session, "owner_token_fingerprint", None) != identity.fingerprint:
        if labels_overlap(
            identity.tenant_labels, getattr(session, "tenant_labels", ()) or ()
        ):
            return
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Agent token cannot access this session",
                "code": "SESSION_OWNERSHIP",
                "retryable": False,
                "hint": "Use the agent token that created this session",
                "http_status": 403,
            },
        )
