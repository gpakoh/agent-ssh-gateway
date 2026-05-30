"""Auth middleware: API key + IP allowlist for agent-ssh-gateway."""

import ipaddress
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request, WebSocket, HTTPException

logger = logging.getLogger(__name__)

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


def get_client_ip(request: Request, trusted_proxy_networks: list) -> str:
    client_host = request.client.host if request.client else "127.0.0.1"

    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and _is_trusted(client_host, trusted_proxy_networks):
        ips = [ip.strip() for ip in forwarded.split(",")]
        for ip in reversed(ips):
            if not _is_trusted(ip, trusted_proxy_networks):
                return ip
        return ips[-1]

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


async def is_agent_token_valid(settings, provided: str, token_store=None) -> bool:
    if not provided:
        return False
    if token_store is not None and token_store.connected:
        return await token_store.validate_token(provided)
    if not settings.agent_token:
        return False
    expires_at = getattr(settings, "agent_token_expires_at", None)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expires_at:
            return False
    return secrets.compare_digest(provided, settings.agent_token)


async def verify_api_key(
    request: Request, expected_key: str, extra_key: str = "", settings=None, token_store=None
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
        return await is_agent_token_valid(settings, provided, token_store)
    if extra_key and secrets.compare_digest(provided, extra_key):
        return True
    return False


# ---------------------------------------------------------------------------
# Always‑public Paths (even When Auth Is Enabled)
# ---------------------------------------------------------------------------

ALWAYS_PUBLIC = frozenset({"/health", "/api/capabilities"})


def _normalise_path(path: str) -> str:
    return path.rstrip("/") or "/"


# ---------------------------------------------------------------------------
# Main Auth Check
# ---------------------------------------------------------------------------


async def auth_check(request: Request, settings, token_store=None) -> Optional[HTTPException]:
    if request.method == "OPTIONS":
        return None

    path = _normalise_path(request.url.path)

    # Auth Disabled — Everything Is Public
    if not settings.api_auth_enabled:
        return None

    # Always‑public Health Endpoint
    if request.method == "GET" and path in ALWAYS_PUBLIC:
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
    if not await verify_api_key(request, settings.api_key, settings.agent_token, settings, token_store):
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
# Websocket Auth Check
# ---------------------------------------------------------------------------

CLOSE_POLICY_VIOLATION = 1008


async def ws_auth_check(websocket: WebSocket, settings, token_store=None) -> tuple[int, str] | None:
    """WebSocket auth guard — call before accept().

    Returns None if allowed, or a (close_code, reason) tuple to reject.
    Does NOT log or reveal the configured key value.
    """
    if not settings.api_auth_enabled:
        return None

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

    provided = websocket.headers.get("X-API-Key", "")
    if not provided:
        auth_header = websocket.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]
    if not provided:
        return (CLOSE_POLICY_VIOLATION, "Invalid or missing API key")
    if secrets.compare_digest(provided, settings.api_key):
        return None
    if await is_agent_token_valid(settings, provided, token_store):
        return None
    return (CLOSE_POLICY_VIOLATION, "Invalid or missing API key")
