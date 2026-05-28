"""Auth middleware: API key + IP allowlist for Web SSH Gateway."""

import ipaddress
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request, WebSocket, HTTPException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CIDR helpers
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


def get_client_ip(request: Request, trusted_proxy_networks: list) -> str:
    client_host = request.client.host if request.client else "127.0.0.1"

    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and _is_trusted(client_host, trusted_proxy_networks):
        return forwarded.split(",")[0].strip()

    return client_host


def _is_trusted(ip_str: str, trusted_networks: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in trusted_networks)


def is_ip_allowed(ip_str: str, allowed_networks: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in allowed_networks)


def is_agent_token_valid(settings, provided: str) -> bool:
    if not provided or not settings.agent_token:
        return False
    expires_at = getattr(settings, "agent_token_expires_at", None)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expires_at:
            return False
    return secrets.compare_digest(provided, settings.agent_token)


def verify_api_key(
    request: Request, expected_key: str, extra_key: str = "", settings=None
) -> bool:
    provided = request.headers.get("X-API-Key", "")
    if not provided:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]
    if not provided:
        return False
    if secrets.compare_digest(provided, expected_key):
        return True
    if settings is not None:
        return is_agent_token_valid(settings, provided)
    if extra_key and secrets.compare_digest(provided, extra_key):
        return True
    return False


# ---------------------------------------------------------------------------
# Always‑public paths (even when auth is enabled)
# ---------------------------------------------------------------------------

ALWAYS_PUBLIC = frozenset({"/health", "/api/capabilities", "/api/ssh/check-port"})


def _normalise_path(path: str) -> str:
    return path.rstrip("/") or "/"


# ---------------------------------------------------------------------------
# Main auth check
# ---------------------------------------------------------------------------


async def auth_check(request: Request, settings) -> Optional[HTTPException]:
    if request.method == "OPTIONS":
        return None

    path = _normalise_path(request.url.path)

    # Auth disabled — everything is public
    if not settings.api_auth_enabled:
        return None

    # Always‑public health endpoint
    if request.method == "GET" and path in ALWAYS_PUBLIC:
        return None

    # Fail-closed: auth enabled but no key configured
    if not settings.api_key:
        logger.error("API_AUTH_ENABLED=true but API_KEY is not configured")
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

    # IP allowlist
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

    # API key check (also accept agent_token)
    if not verify_api_key(request, settings.api_key, settings.agent_token, settings):
        return HTTPException(
            status_code=401,
            detail={
                "message": "Invalid or missing API key. Provide via X-API-Key header",
                "code": "INVALID_API_KEY",
                "retryable": False,
                "hint": "Provide a valid X-API-Key header in your requests",
                "http_status": 401,
            },
        )

    return None


# ---------------------------------------------------------------------------
# WebSocket auth check
# ---------------------------------------------------------------------------

CLOSE_POLICY_VIOLATION = 1008


async def ws_auth_check(websocket: WebSocket, settings) -> tuple[int, str] | None:
    """WebSocket auth guard — call before accept().

    Returns None if allowed, or a (close_code, reason) tuple to reject.
    Does NOT log or reveal the configured key value.
    """
    if not settings.api_auth_enabled:
        return None

    if not settings.api_key:
        logger.error("API_AUTH_ENABLED=true but API_KEY is not configured")
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
        client_ip = forwarded.split(",")[0].strip()
    else:
        client_ip = client_host

    if not is_ip_allowed(client_ip, allowed_nets):
        logger.warning("Blocked WebSocket from disallowed IP %s", client_ip)
        return (CLOSE_POLICY_VIOLATION, "Access denied: client IP not allowed")

    provided = websocket.headers.get("X-API-Key", "")
    if not provided:
        auth_header = websocket.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]
    if not provided:
        return (CLOSE_POLICY_VIOLATION, "Invalid or missing API key")
    if secrets.compare_digest(provided, settings.api_key):
        return None
    if is_agent_token_valid(settings, provided):
        return None
    return (CLOSE_POLICY_VIOLATION, "Invalid or missing API key")
