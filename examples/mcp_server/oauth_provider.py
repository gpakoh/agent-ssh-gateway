"""OAuth provider for agent-ssh-gateway MCP fleet.

Uses FastMCP's native OAuthAuthorizationServerProvider with in-memory
storage. Supports PKCE S256, public DCR, and 7 scopes.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from mcp.server.auth.provider import AccessToken

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


class GatewayOAuthProvider:
    """In-memory OAuth 2.1 + PKCE provider for the MCP Gateway.

    Uses FastMCP-compatible interface for integration with
    BearerAuthBackend and AuthContextMiddleware.

    Thread-safe via dict locks (GIL + single-process FastMCP in practice).
    """

    def __init__(self) -> None:
        self._clients: dict[str, StoredClient] = {}
        self._auth_codes: dict[str, StoredAuthCode] = {}
        self._tokens: dict[str, StoredToken] = {}

    # --- Client Registration ---

    async def register_client(self, client_info: Any) -> None:
        """Register a new OAuth client (DCR, RFC 7591)."""
        from mcp.server.auth.provider import RegistrationError

        redirect_uris = getattr(client_info, "redirect_uris", None) or getattr(client_info, "redirect_uris", [])
        if not redirect_uris:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="At least one redirect_uri required",
            )

        client_name = getattr(client_info, "client_name", "") or ""
        token_endpoint_auth_method = getattr(client_info, "token_endpoint_auth_method", "none") or "none"
        scope_str = getattr(client_info, "scope", None)

        parsed_scopes = _parse_scopes(scope_str)
        client_id = _generate_id("mcp_client_", 24)
        client = StoredClient(
            client_id=client_id,
            redirect_uris=list(str(u) for u in redirect_uris),
            client_name=client_name,
            token_endpoint_auth_method=token_endpoint_auth_method,
            scopes=parsed_scopes,
        )
        self._clients[client_id] = client

        client_info.client_id = client_id
        client_info.client_id_issued_at = int(client.created_at)
        client_info.client_secret = None

    async def get_client(self, client_id: str) -> Any | None:
        stored = self._clients.get(client_id)
        if not stored:
            return None
        from mcp.shared.auth import OAuthClientInformationFull

        return OAuthClientInformationFull(
            client_id=stored.client_id,
            redirect_uris=stored.redirect_uris,
            client_name=stored.client_name,
            token_endpoint_auth_method=stored.token_endpoint_auth_method,
            grant_types=stored.grant_types,
            response_types=stored.response_types,
            scope=" ".join(stored.scopes),
            client_id_issued_at=int(stored.created_at),
        )

    def list_clients(self) -> list[StoredClient]:
        return list(self._clients.values())

    # --- Internal helpers (for tests + token-mode pre-registration) ---

    async def _find_client(self, client_id: str) -> StoredClient | None:
        return self._clients.get(client_id)

    def create_authorization_code(
        self,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        state: str,
        scopes: list[str] | None = None,
    ) -> dict[str, str]:
        client = self._clients.get(client_id)
        if not client:
            raise ValueError(f"Unknown client: {client_id}")
        if not _is_redirect_uri_exact_match(client.redirect_uris, redirect_uri):
            raise ValueError(f"redirect_uri not registered: {redirect_uri}")
        code = _generate_id("auth_", 32)
        self._auth_codes[code] = StoredAuthCode(
            code=code, client_id=client_id, scopes=scopes or list(DEFAULT_SCOPES),
            code_challenge=code_challenge, redirect_uri=redirect_uri, state=state,
            expires_at=time.time() + 300,
        )
        return {"code": code, "state": state}

    def exchange_code_for_token(
        self, client_id: str, code: str, code_verifier: str, redirect_uri: str,
    ) -> dict[str, Any]:
        stored = self._auth_codes.get(code)
        if not stored:
            raise ValueError("Authorization code not found")
        if stored.used:
            raise ValueError("Authorization code already used")
        if stored.client_id != client_id:
            raise ValueError("Client ID mismatch")
        if time.time() > stored.expires_at:
            raise ValueError("Authorization code expired")
        if stored.redirect_uri != redirect_uri:
            raise ValueError("redirect_uri mismatch")
        try:
            _verify_pkce(code_verifier, stored.code_challenge)
        except (ValueError, AssertionError):
            raise ValueError("PKCE verification failed") from None
        stored.used = True
        access_token = _generate_id("mcp_at_", 32)
        refresh_token = _generate_id("mcp_rt_", 32)
        self._tokens[access_token] = StoredToken(
            token=access_token, client_id=client_id, scopes=stored.scopes,
            expires_at=time.time() + 7200, type="access",
        )
        self._tokens[refresh_token] = StoredToken(
            token=refresh_token, client_id=client_id, scopes=stored.scopes,
            expires_at=time.time() + 604800, type="refresh",
        )
        return {
            "access_token": access_token, "token_type": "Bearer",
            "expires_in": 7200, "refresh_token": refresh_token,
            "scope": " ".join(stored.scopes),
        }

    def refresh_access_token(self, client_id: str, refresh_token: str) -> dict[str, Any]:
        stored = self._tokens.get(refresh_token)
        if not stored:
            raise ValueError("Refresh token not found")
        if stored.client_id != client_id:
            raise ValueError("Client ID mismatch")
        if stored.type != "refresh":
            raise ValueError("Token is not a refresh token")
        if time.time() > stored.expires_at:
            raise ValueError("Refresh token expired")
        new_access = _generate_id("mcp_at_", 32)
        self._tokens[new_access] = StoredToken(
            token=new_access, client_id=client_id, scopes=stored.scopes,
            expires_at=time.time() + 7200, type="access",
        )
        return {"access_token": new_access, "token_type": "Bearer", "expires_in": 7200, "scope": " ".join(stored.scopes)}

    def revoke_client_token(self, client_id: str, token_str: str) -> None:
        stored = self._tokens.get(token_str)
        if stored and stored.client_id == client_id:
            del self._tokens[token_str]

    # --- FastMCP protocol stubs (authorize/token endpoints) ---

    async def authorize(self, client_info: Any, params: Any) -> str:
        raise NotImplementedError("authorize — not implemented; use pre-registered service token")

    async def exchange_authorization_code(
        self, client_info: Any, authorization_code: str
    ) -> Any:
        raise NotImplementedError(
            "exchange_authorization_code — not implemented; use pre-registered service token"
        )

    async def exchange_refresh_token(
        self, client_info: Any, refresh_token: str, scopes: list[str]
    ) -> Any:
        raise NotImplementedError(
            "exchange_refresh_token — not implemented; use pre-registered service token"
        )

    async def load_authorization_code(self, client_info: Any, authorization_code: str) -> Any | None:
        return None

    async def load_refresh_token(self, client_info: Any, refresh_token: str) -> Any | None:
        return None

    async def revoke_token(self, token_str: str) -> None:
        stored = self._tokens.get(token_str)
        if stored:
            del self._tokens[token_str]

    # --- Internal helpers (used by token-mode code + tests) ---

    def verify_access_token(self, token_str: str) -> StoredToken | None:
        """Verify and return access token."""
        stored = self._tokens.get(token_str)
        if not stored:
            return None
        if stored.type != "access":
            return None
        if time.time() > stored.expires_at:
            return None
        return stored

    async def load_access_token(self, token_str: str) -> AccessToken | None:
        """Async token loader for FastMCP ProviderTokenVerifier."""
        stored = self._tokens.get(token_str)
        if not stored:
            return None
        if stored.type != "access":
            return None
        if time.time() > stored.expires_at:
            return None
        expires_at = int(stored.expires_at) if stored.expires_at != float("inf") else 2**63 - 1
        return AccessToken(
            token=stored.token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=expires_at,
        )
