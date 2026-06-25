"""OAuth provider for agent-ssh-gateway MCP fleet.

Uses FastMCP's native OAuthAuthorizationServerProvider with in-memory
storage. Supports PKCE S256, public DCR, and 7 scopes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

SUPPORTED_SCOPES: list[str] = [
    "mcp:read",
    "mcp:project",
    "mcp:handoff",
    "mcp:agent-run",
    "mcp:docker",
    "mcp:postgres",
    "mcp:repo",
]

DEFAULT_SCOPES: list[str] = ["mcp:read", "mcp:project"]

ADMIN_SCOPE: str = "mcp:admin"


@dataclass
class StoredClient:
    client_id: str
    redirect_uris: list[str]
    client_name: str = ""
    token_endpoint_auth_method: str = "none"
    grant_types: list[str] = field(default_factory=lambda: ["authorization_code", "refresh_token"])
    response_types: list[str] = field(default_factory=lambda: ["code"])
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))
    created_at: float = field(default_factory=time.time)


@dataclass
class StoredAuthCode:
    code: str
    client_id: str
    scopes: list[str]
    code_challenge: str
    redirect_uri: str
    state: str
    expires_at: float
    used: bool = False


@dataclass
class StoredToken:
    token: str
    client_id: str
    scopes: list[str]
    expires_at: float
    type: str = "access"  # "access" or "refresh"


def _generate_id(prefix: str = "", length: int = 32) -> str:
    raw = secrets.token_hex(length)
    return f"{prefix}{raw}" if prefix else raw


def _is_redirect_uri_exact_match(allowed: list[str], actual: str) -> bool:
    return actual in allowed


def _generate_code_challenge(code_verifier: str) -> str:
    """Generate S256 PKCE code challenge from verifier."""
    return base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """Verify PKCE code verifier against challenge (S256 only)."""
    if len(code_verifier) < 43 or len(code_verifier) > 128:
        raise ValueError("code_verifier must be 43-128 characters")
    challenge = _generate_code_challenge(code_verifier)
    return secrets.compare_digest(challenge, code_challenge)


def _parse_scopes(scope_str: str | None) -> list[str]:
    """Parse space-separated scope string, validate against SUPPORTED_SCOPES."""
    if not scope_str:
        return list(DEFAULT_SCOPES)
    scopes = scope_str.strip().split()
    for s in scopes:
        if s not in SUPPORTED_SCOPES:
            raise ValueError(f"Unsupported scope: {s}")
    return scopes
