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

from examples.mcp_server.token_store import TokenStore


def hash_token(token: str) -> str:
    """Return sha256 hash with explicit 'sha256:' prefix."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


SUPPORTED_SCOPES: list[str] = [
    "mcp:read",
    "mcp:project",
    "mcp:handoff",
    "mcp:agent-run",
    "mcp:execute",
    "mcp:repo",
    "mcp:docker",
    "mcp:postgres",
    "mcp:docs",
    "mcp:admin",
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
    return (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """Verify PKCE code verifier against challenge (S256 only)."""
    if len(code_verifier) < 43 or len(code_verifier) > 128:
        raise ValueError("code_verifier must be 43-128 characters")
    challenge = _generate_code_challenge(code_verifier)
    return secrets.compare_digest(challenge, code_challenge)


def _parse_scopes(scope_str: str | None) -> list[str]:
    """Parse space-separated scope string, validate against SUPPORTED_SCOPES."""
    if not scope_str:
        return list(SUPPORTED_SCOPES)
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
        self._token_store: TokenStore | None = None
        self.public_base_url: str = ""

    def set_token_store(self, store: TokenStore) -> None:
        """Attach a TokenStore for synchronised revocations."""
        self._token_store = store

    def load_tokens(self) -> int:
        """Load non-revoked tokens from the attached TokenStore.

        Reads all non-revoked entries from the store and registers
        each as a hashed token. Returns the count of tokens loaded.
        """
        if not self._token_store:
            return 0
        entries = self._token_store.load()
        count = 0
        for entry in entries:
            if entry.revoked_at is not None:
                continue
            self.register_hashed_token(
                token_hash=entry.token_hash,
                profile=entry.profile,
                scopes=list(entry.scopes),
            )
            count += 1
        return count

    def register_static_token(
        self,
        raw_token: str,
        profile: str = "operator",
        name: str = "static",
        client_id: str = "mcp_static",
    ) -> str:
        """Register a raw static token. Returns the hash used as key.

        Hashes the token internally, resolves scopes from profile,
        stores with infinite expiry.
        """
        from examples.mcp_server.tool_scopes import get_profile_scopes

        token_hash = hash_token(raw_token)
        scopes = get_profile_scopes(profile)
        self._tokens[token_hash] = StoredToken(
            token=token_hash,
            client_id=client_id,
            scopes=list(scopes),
            expires_at=float("inf"),
            type="access",
        )
        return token_hash

    def register_hashed_token(
        self,
        token_hash: str,
        scopes: list[str],
        profile: str = "operator",
        name: str = "hashed",
        client_id: str = "mcp_static",
    ) -> None:
        """Register a pre-hashed token (from persistent store).

        Validates the 'sha256:' prefix and stores directly.
        """
        if not token_hash.startswith("sha256:"):
            raise ValueError(f"token_hash must start with 'sha256:', got {token_hash[:20]}...")
        self._tokens[token_hash] = StoredToken(
            token=token_hash,
            client_id=client_id,
            scopes=list(scopes),
            expires_at=float("inf"),
            type="access",
        )

    # --- Client Registration ---

    async def register_client(self, client_info: Any) -> None:
        """Register a new OAuth client (DCR, RFC 7591)."""
        from mcp.server.auth.provider import RegistrationError

        redirect_uris = getattr(client_info, "redirect_uris", None) or getattr(
            client_info, "redirect_uris", []
        )
        if not redirect_uris:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="At least one redirect_uri required",
            )

        client_name = getattr(client_info, "client_name", "") or ""
        token_endpoint_auth_method = (
            getattr(client_info, "token_endpoint_auth_method", "none") or "none"
        )
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
            redirect_uris=stored.redirect_uris,  # type: ignore[arg-type]
            client_name=stored.client_name,
            token_endpoint_auth_method=stored.token_endpoint_auth_method,  # type: ignore[arg-type]
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
            code=code,
            client_id=client_id,
            scopes=scopes or list(DEFAULT_SCOPES),
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            state=state,
            expires_at=time.time() + 300,
        )
        return {"code": code, "state": state}

    def exchange_code_for_token(
        self,
        client_id: str,
        code: str,
        code_verifier: str = "",
        redirect_uri: str = "",
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
        if redirect_uri and stored.redirect_uri != redirect_uri:
            raise ValueError("redirect_uri mismatch")
        if code_verifier:
            try:
                _verify_pkce(code_verifier, stored.code_challenge)
            except (ValueError, AssertionError):
                raise ValueError("PKCE verification failed") from None
        stored.used = True
        access_token = _generate_id("mcp_at_", 32)
        refresh_token = _generate_id("mcp_rt_", 32)
        at_hash = hash_token(access_token)
        rt_hash = hash_token(refresh_token)
        self._tokens[at_hash] = StoredToken(
            token=at_hash,
            client_id=client_id,
            scopes=stored.scopes,
            expires_at=time.time() + 7200,
            type="access",
        )
        self._tokens[rt_hash] = StoredToken(
            token=rt_hash,
            client_id=client_id,
            scopes=stored.scopes,
            expires_at=time.time() + 604800,
            type="refresh",
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 7200,
            "refresh_token": refresh_token,
            "scope": " ".join(stored.scopes),
        }

    def refresh_access_token(self, client_id: str, refresh_token: str) -> dict[str, Any]:
        rt_hash = hash_token(refresh_token)
        stored = self._tokens.get(rt_hash)
        if not stored:
            raise ValueError("Refresh token not found")
        if stored.client_id != client_id:
            raise ValueError("Client ID mismatch")
        if stored.type != "refresh":
            raise ValueError("Token is not a refresh token")
        if time.time() > stored.expires_at:
            raise ValueError("Refresh token expired")
        new_access = _generate_id("mcp_at_", 32)
        at_hash = hash_token(new_access)
        self._tokens[at_hash] = StoredToken(
            token=at_hash,
            client_id=client_id,
            scopes=stored.scopes,
            expires_at=time.time() + 7200,
            type="access",
        )
        return {
            "access_token": new_access,
            "token_type": "Bearer",
            "expires_in": 7200,
            "scope": " ".join(stored.scopes),
        }

    def revoke_client_token(self, client_id: str, token_str: str) -> None:
        token_hash = hash_token(token_str)
        stored = self._tokens.get(token_hash)
        if stored and stored.client_id == client_id:
            del self._tokens[token_hash]
            if self._token_store:
                entry = self._token_store.find_by_hash(token_hash)
                if entry and entry.revoked_at is None:
                    self._token_store.revoke(entry.id)

    # --- FastMCP protocol stubs (authorize/token endpoints) ---

    async def authorize(self, client_info: Any, params: Any) -> str:
        raw = getattr(params, "redirect_uri", "")
        redirect_uri = str(raw) if raw else ""
        scope_list = getattr(params, "scopes", None) or DEFAULT_SCOPES
        scope_str = " ".join(scope_list) if isinstance(scope_list, list) else str(scope_list)
        state = getattr(params, "state", "") or ""
        code_challenge = getattr(params, "code_challenge", "")
        resource = getattr(params, "resource", None)
        if not redirect_uri or not code_challenge:
            raise ValueError("Missing redirect_uri or code_challenge in authorization request")
        from urllib.parse import urlencode

        consent_params = {
            "client_id": client_info.client_id,
            "redirect_uri": redirect_uri,
            "scope": scope_str,
            "state": state,
            "code_challenge": code_challenge,
        }
        if resource:
            consent_params["resource"] = str(resource)
        base = self.public_base_url or ""
        return base + "/oauth/consent?" + urlencode(consent_params)

    async def exchange_authorization_code(self, client_info: Any, authorization_code: Any) -> Any:
        from mcp.shared.auth import OAuthToken

        code = getattr(authorization_code, "code", "") or ""
        redirect_uri = str(getattr(authorization_code, "redirect_uri", "") or "")
        if not code:
            raise ValueError("Missing code")
        stored = self._auth_codes.get(code)
        if not stored:
            raise ValueError("Authorization code not found")
        result = self.exchange_code_for_token(
            client_id=client_info.client_id,
            code=code,
            code_verifier="",  # PKCE already verified by FastMCP handler
            redirect_uri=redirect_uri,
        )
        return OAuthToken(
            access_token=result["access_token"],
            refresh_token=result.get("refresh_token"),
            expires_in=result["expires_in"],
            scope=result.get("scope", ""),
        )

    async def exchange_refresh_token(
        self, client_info: Any, refresh_token: Any, scopes: list[str]
    ) -> Any:
        from mcp.shared.auth import OAuthToken

        token = getattr(refresh_token, "token", "") or ""
        if not token:
            raise ValueError("Missing refresh token")
        result = self.refresh_access_token(client_info.client_id, token)
        return OAuthToken(
            access_token=result["access_token"],
            expires_in=result["expires_in"],
            scope=result.get("scope", ""),
        )

    async def load_authorization_code(
        self, client_info: Any, authorization_code: str
    ) -> Any | None:
        from mcp.server.auth.provider import AuthorizationCode

        stored = self._auth_codes.get(authorization_code)
        if not stored:
            return None
        if stored.expires_at < time.time():
            self._auth_codes.pop(authorization_code, None)
            return None
        if stored.used:
            self._auth_codes.pop(authorization_code, None)
            return None
        return AuthorizationCode(
            code=stored.code,
            scopes=stored.scopes,
            expires_at=stored.expires_at,
            client_id=stored.client_id,
            code_challenge=stored.code_challenge,
            redirect_uri=stored.redirect_uri,  # type: ignore[arg-type]
            redirect_uri_provided_explicitly=True,
        )

    async def load_refresh_token(self, client_info: Any, refresh_token: str) -> Any | None:
        from mcp.server.auth.provider import RefreshToken

        rt_hash = hash_token(refresh_token)
        stored = self._tokens.get(rt_hash)
        if not stored or stored.type != "refresh":
            return None
        if stored.expires_at < time.time():
            self._tokens.pop(rt_hash, None)
            return None
        return RefreshToken(
            token=stored.token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=int(stored.expires_at) if stored.expires_at != float("inf") else None,
        )

    async def revoke_token(self, token_str: str) -> None:
        token_hash = hash_token(token_str)
        stored = self._tokens.get(token_hash)
        if stored:
            del self._tokens[token_hash]
            if self._token_store:
                entry = self._token_store.find_by_hash(token_hash)
                if entry and entry.revoked_at is None:
                    self._token_store.revoke(entry.id)

    # --- Internal helpers (used by token-mode code + tests) ---

    def verify_access_token(self, token_str: str) -> StoredToken | None:
        """Verify and return access token using hash lookup."""
        token_hash = hash_token(token_str)
        stored = self._tokens.get(token_hash)
        if not stored:
            return None
        if stored.type != "access":
            return None
        if time.time() > stored.expires_at:
            return None
        return stored

    async def load_access_token(self, token_str: str) -> AccessToken | None:
        """Async token loader for FastMCP ProviderTokenVerifier (hash lookup)."""
        token_hash = hash_token(token_str)
        stored = self._tokens.get(token_hash)
        if not stored:
            return None
        if stored.type != "access":
            return None
        if time.time() > stored.expires_at:
            return None
        expires_at = int(stored.expires_at) if stored.expires_at != float("inf") else 2**63 - 1
        return AccessToken(
            token=token_str,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=expires_at,
        )
