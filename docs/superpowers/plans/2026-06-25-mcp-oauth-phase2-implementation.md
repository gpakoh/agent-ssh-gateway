# Unified MCP OAuth Phase 2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OAuth 2.1 + PKCE + DCR to the Gateway MCP endpoint in mixed mode (mcp_token fallback preserved).

**Architecture:** GatewayOAuthProvider (FastMCP native `OAuthAuthorizationServerProvider` subclass, in-memory) + MixedAuthMiddleware (accepts Bearer or mcp_token) wrapping the existing proxy. FastMCP's built-in `BearerAuthBackend` + `AuthContextMiddleware` handle token validation. Fleet adapters unchanged until Phase 3.

**Tech Stack:** FastMCP 2.x (`.venv`), Starlette, httpx, PyJWT or opaque tokens (FastMCP default), Python 3.12.

## Global Constraints

- `MCP_AUTH_MODE=token` is the default — no behavioral changes unless opt-in
- `MCP_PUBLIC_TOKEN` is never removed — mcp_token fallback always works
- Healthcheck 6/6 must remain green
- 85 tools must remain available in mixed mode
- Fleet adapters (context7, github, gitea, docker, postgres) are NOT modified
- No secrets committed to the repository

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `examples/mcp_server/oauth_provider.py` | `GatewayOAuthProvider` — FastMCP `OAuthAuthorizationServerProvider` subclass, in-memory storage, PKCE S256, DCR for public clients, 7 scopes |
| `tests/test_oauth_provider.py` | Tests for GatewayOAuthProvider: PKCE, auth code, token exchange, DCR, refresh, scope filtering |

### Modified Files

| File | Change |
|------|--------|
| `examples/chatgpt_remote_mcp/server.py` | `TokenAuthMiddleware` → `MixedAuthMiddleware`; OAuth endpoint passthrough; MCP_AUTH_MODE env integration |
| `examples/mcp_server/server.py` | FastMCP `AuthSettings` config; OAuth provider init when mode != `token` |
| `tests/test_mcp_auth.py` | Mixed mode tests: Bearer valid/invalid, mcp_token fallback, mode switching, OAuth passthrough |
| `pyproject.toml` | Dependencies if needed (check if `mcp` package version has AuthSettings — already in .venv) |
| `CHANGELOG.md` | Added after implementation |

---

### Task 1: GatewayOAuthProvider

**Files:**
- Create: `examples/mcp_server/oauth_provider.py`
- Create: `tests/test_oauth_provider.py`

**Interfaces:**
- Consumes: FastMCP `OAuthAuthorizationServerProvider[ClientT, UserT, AuthCodeT]` from `.venv`
- Produces: `GatewayOAuthProvider(oauth_provider.OAuthAuthorizationServerProvider)` with:
  - `create_client(redirect_uris, name, scopes) -> OAuthClientInformationFull`
  - `authorize(params: AuthorizationParams) -> str` (redirect URL)
  - `load_authorization_code(client, code) -> AuthCode | None`
  - `exchange_authorization_code(client, auth_code) -> OAuthToken`
  - `verify_access_token(token_str) -> AccessToken | None`
  - `verify_refresh_token(token_str) -> RefreshToken | None`
  - `refresh_access_token(client, refresh_token) -> OAuthToken`
  - `revoke_token(client, token_hint) -> None`

- [ ] **Step 1: Define data classes and scope constants**

```python
"""OAuth provider for agent-ssh-gateway MCP fleet.

Uses FastMCP's native OAuthAuthorizationServerProvider with in-memory
storage. Supports PKCE S256, public DCR, and 7 scopes.
"""

from __future__ import annotations

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
```

- [ ] **Step 2: Write failing PKCE verification test**

```python
# tests/test_oauth_provider.py
"""Tests for GatewayOAuthProvider."""

import hashlib
import base64
import secrets
import time

import pytest

from examples.mcp_server.oauth_provider import (
    _verify_pkce,
    _generate_code_challenge,
    SUPPORTED_SCOPES,
    DEFAULT_SCOPES,
)


def test_pkce_verification_valid():
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _generate_code_challenge(code_verifier)
    assert _verify_pkce(code_verifier, code_challenge) is True


def test_pkce_verification_invalid():
    code_verifier = secrets.token_urlsafe(64)
    wrong_challenge = "AAAA" + _generate_code_challenge(code_verifier)[4:]
    assert _verify_pkce(code_verifier, wrong_challenge) is False


def test_pkce_verifier_too_short():
    with pytest.raises(ValueError):
        _verify_pkce("short", "challenge")


def test_generate_code_challenge_deterministic():
    verifier = secrets.token_urlsafe(64)
    c1 = _generate_code_challenge(verifier)
    c2 = _generate_code_challenge(verifier)
    assert c1 == c2


def test_generate_code_challenge_differs():
    v1 = secrets.token_urlsafe(64)
    v2 = secrets.token_urlsafe(64)
    assert _generate_code_challenge(v1) != _generate_code_challenge(v2)


def test_scope_constants():
    assert "mcp:read" in SUPPORTED_SCOPES
    assert "mcp:admin" not in DEFAULT_SCOPES
```

Run: `pytest tests/test_oauth_provider.py::test_pkce_verification_valid -v`
Expected: `FAILED` (function not defined yet)

- [ ] **Step 3: Implement PKCE helpers**

```python
# examples/mcp_server/oauth_provider.py — add after dataclass definitions

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
```

- [ ] **Step 4: Verify PKCE tests pass**

Run: `pytest tests/test_oauth_provider.py -v`
Expected: 6/6 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_oauth_provider.py examples/mcp_server/oauth_provider.py
git commit -m "feat: add PKCE helpers and scope validation for OAuth provider"
```

- [ ] **Step 6: Implement GatewayOAuthProvider class**

```python
# examples/mcp_server/oauth_provider.py — add at end

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

    def register_client(
        self,
        redirect_uris: list[str],
        client_name: str = "",
        token_endpoint_auth_method: str = "none",
    ) -> dict[str, Any]:
        """Register a new OAuth client (DCR, RFC 7591)."""
        if not redirect_uris:
            raise ValueError("At least one redirect_uri required")
        
        client_id = _generate_id("mcp_client_", 24)
        client = StoredClient(
            client_id=client_id,
            redirect_uris=redirect_uris,
            client_name=client_name,
            token_endpoint_auth_method=token_endpoint_auth_method,
        )
        self._clients[client_id] = client

        return {
            "client_id": client_id,
            "client_id_issued_at": int(client.created_at),
            "client_secret": None,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "grant_types": client.grant_types,
            "response_types": client.response_types,
        }

    def get_client(self, client_id: str) -> StoredClient | None:
        return self._clients.get(client_id)

    # --- Authorization Code ---

    def create_authorization_code(
        self,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        state: str,
        scopes: list[str] | None = None,
    ) -> dict[str, str]:
        """Create authorization code (PKCE-bound)."""
        client = self.get_client(client_id)
        if not client:
            raise ValueError(f"Unknown client: {client_id}")

        if not _is_redirect_uri_exact_match(client.redirect_uris, redirect_uri):
            raise ValueError(f"redirect_uri not registered: {redirect_uri}")

        code = _generate_id("auth_", 32)
        code_obj = StoredAuthCode(
            code=code,
            client_id=client_id,
            scopes=scopes or list(DEFAULT_SCOPES),
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            state=state,
            expires_at=time.time() + 300,  # 5 minutes
        )
        self._auth_codes[code] = code_obj

        return {"code": code, "state": state}

    def exchange_code_for_token(
        self,
        client_id: str,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """Exchange authorization code for access + refresh tokens."""
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
            raise ValueError("PKCE verification failed")

        # Mark code as used (single-use)
        stored.used = True

        access_token = _generate_id("mcp_at_", 32)
        refresh_token = _generate_id("mcp_rt_", 32)

        self._tokens[access_token] = StoredToken(
            token=access_token,
            client_id=client_id,
            scopes=stored.scopes,
            expires_at=time.time() + 3600,  # 1 hour
            type="access",
        )
        self._tokens[refresh_token] = StoredToken(
            token=refresh_token,
            client_id=client_id,
            scopes=stored.scopes,
            expires_at=time.time() + 2592000,  # 30 days
            type="refresh",
        )

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh_token,
            "scope": " ".join(stored.scopes),
        }

    def refresh_access_token(self, client_id: str, refresh_token: str) -> dict[str, Any]:
        """Exchange refresh token for new access token."""
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
            token=new_access,
            client_id=client_id,
            scopes=stored.scopes,
            expires_at=time.time() + 3600,
            type="access",
        )
        return {
            "access_token": new_access,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": " ".join(stored.scopes),
        }

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

    def revoke_token(self, client_id: str, token_str: str) -> None:
        """Revoke a token (any type)."""
        stored = self._tokens.get(token_str)
        if stored and stored.client_id == client_id:
            del self._tokens[token_str]
```

- [ ] **Step 7: Write GatewayOAuthProvider tests**

```python
# tests/test_oauth_provider.py — add after PKCE tests

from examples.mcp_server.oauth_provider import GatewayOAuthProvider


@pytest.fixture
def provider():
    return GatewayOAuthProvider()


def test_dcr_register(provider):
    result = provider.register_client(
        redirect_uris=["https://chatgpt.com/callback"],
        client_name="Test Client",
    )
    assert "client_id" in result
    assert result["client_secret"] is None
    assert result["token_endpoint_auth_method"] == "none"
    assert result["redirect_uris"] == ["https://chatgpt.com/callback"]


def test_dcr_requires_redirect_uri(provider):
    with pytest.raises(ValueError, match="redirect_uri"):
        provider.register_client(redirect_uris=[])


def test_get_client(provider):
    reg = provider.register_client(
        redirect_uris=["https://chatgpt.com/callback"],
        client_name="Test",
    )
    client = provider.get_client(reg["client_id"])
    assert client is not None
    assert client.client_name == "Test"


def test_get_client_unknown(provider):
    assert provider.get_client("nonexistent") is None


def test_authorization_code_flow(provider):
    reg = provider.register_client(
        redirect_uris=["https://chatgpt.com/callback"],
        client_name="Test",
    )
    client_id = reg["client_id"]
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _generate_code_challenge(code_verifier)

    auth = provider.create_authorization_code(
        client_id=client_id,
        redirect_uri="https://chatgpt.com/callback",
        code_challenge=code_challenge,
        state="test-state",
        scopes=["mcp:read"],
    )
    assert "code" in auth
    assert auth["state"] == "test-state"

    tokens = provider.exchange_code_for_token(
        client_id=client_id,
        code=auth["code"],
        code_verifier=code_verifier,
        redirect_uri="https://chatgpt.com/callback",
    )
    assert "access_token" in tokens
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    assert "refresh_token" in tokens


def test_code_reuse_rejected(provider):
    reg = provider.register_client(redirect_uris=["https://example.com/cb"])
    client_id = reg["client_id"]
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code(
        client_id, "https://example.com/cb", cc, "s", ["mcp:read"]
    )
    provider.exchange_code_for_token(client_id, auth["code"], cv, "https://example.com/cb")
    with pytest.raises(ValueError, match="already used"):
        provider.exchange_code_for_token(client_id, auth["code"], cv, "https://example.com/cb")


def test_pkce_verification_rejects_wrong_verifier(provider):
    reg = provider.register_client(redirect_uris=["https://example.com/cb"])
    client_id = reg["client_id"]
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code(
        client_id, "https://example.com/cb", cc, "s", ["mcp:read"]
    )
    with pytest.raises(ValueError, match="PKCE verification"):
        provider.exchange_code_for_token(
            client_id, auth["code"], "wrong_verifier", "https://example.com/cb"
        )


def test_access_token_verification(provider):
    reg = provider.register_client(redirect_uris=["https://example.com/cb"])
    client_id = reg["client_id"]
    cv, cc = secrets.token_urlsafe(64), _generate_code_challenge(secrets.token_urlsafe(64))
    # Use a valid challenge
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code(client_id, "https://example.com/cb", cc, "s")
    tokens = provider.exchange_code_for_token(client_id, auth["code"], cv, "https://example.com/cb")
    
    stored = provider.verify_access_token(tokens["access_token"])
    assert stored is not None
    assert stored.client_id == client_id
    assert stored.type == "access"


def test_refresh_token(provider):
    reg = provider.register_client(redirect_uris=["https://example.com/cb"])
    client_id = reg["client_id"]
    cv, cc = secrets.token_urlsafe(64), _generate_code_challenge(secrets.token_urlsafe(64))
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code(client_id, "https://example.com/cb", cc, "s")
    tokens = provider.exchange_code_for_token(client_id, auth["code"], cv, "https://example.com/cb")
    
    refreshed = provider.refresh_access_token(client_id, tokens["refresh_token"])
    assert "access_token" in refreshed
    assert refreshed["token_type"] == "Bearer"


def test_revoke_token(provider):
    reg = provider.register_client(redirect_uris=["https://example.com/cb"])
    client_id = reg["client_id"]
    cv, cc = secrets.token_urlsafe(64), _generate_code_challenge(secrets.token_urlsafe(64))
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code(client_id, "https://example.com/cb", cc, "s")
    tokens = provider.exchange_code_for_token(client_id, auth["code"], cv, "https://example.com/cb")
    
    provider.revoke_token(client_id, tokens["access_token"])
    assert provider.verify_access_token(tokens["access_token"]) is None


def test_scope_validation():
    from examples.mcp_server.oauth_provider import _parse_scopes
    assert _parse_scopes("mcp:read mcp:project") == ["mcp:read", "mcp:project"]
    assert _parse_scopes(None) == ["mcp:read", "mcp:project"]
    assert _parse_scopes("") == ["mcp:read", "mcp:project"]
    with pytest.raises(ValueError, match="Unsupported scope"):
        _parse_scopes("mcp:admin")
```

- [ ] **Step 8: Verify all provider tests pass**

Run: `pytest tests/test_oauth_provider.py -v`
Expected: 16/16 passed

- [ ] **Step 9: Commit**

```bash
git add tests/test_oauth_provider.py examples/mcp_server/oauth_provider.py
git commit -m "feat: implement GatewayOAuthProvider with PKCE, DCR, token management"
```

---

### Task 2: MixedAuthMiddleware + Proxy Integration

**Files:**
- Modify: `examples/chatgpt_remote_mcp/server.py`

**Interfaces:**
- Consumes: `GatewayOAuthProvider` from `examples/mcp_server/oauth_provider`
- Produces: `MixedAuthMiddleware` class, updated `create_proxy_app()`

- [ ] **Step 1: Write failing auth middleware test**

Create test file for the proxy integration:

```python
# tests/test_mcp_auth.py
"""Tests for MCP auth middleware (mixed mode)."""

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from examples.mcp_server.oauth_provider import GatewayOAuthProvider


@pytest.fixture
def provider():
    return GatewayOAuthProvider()


def _make_mock_app():
    """Create a minimal Starlette app for testing middleware."""
    async def mock_mcp(request):
        return JSONResponse({"tools": ["mock_tool"]})
    
    async def mock_auth(request):
        return JSONResponse({"oauth": "endpoint"})
    
    return Starlette(routes=[
        Route("/", mock_mcp),
        Route("/.well-known/oauth-authorization-server", mock_auth),
        Route("/oauth/register", mock_auth, methods=["POST"]),
    ])


def test_mixed_mode_bearer_token_valid():
    """Bearer token should pass through to app."""
    pytest.skip("Mock app needs middleware — implement in Task 3")


def test_mixed_mode_mcp_token_valid():
    """mcp_token query param should work as fallback."""
    pytest.skip("Mock app needs middleware — implement in Task 3")


def test_mixed_mode_no_auth_rejected():
    """No token at all should be rejected."""
    pytest.skip("Mock app needs middleware — implement in Task 3")


def test_mixed_mode_oauth_endpoints_bypass_auth():
    """OAuth discovery and registration should not require token."""
    pytest.skip("Mock app needs middleware — implement in Task 3")
```

- [ ] **Step 2: Read current server.py to understand the exact code**

```bash
cat -n examples/chatgpt_remote_mcp/server.py
```

Expected: understand the exact structure to modify with MixedAuthMiddleware.

- [ ] **Step 3: Implement MixedAuthMiddleware**

```python
# examples/chatgpt_remote_mcp/server.py — replace entire file with:
"""Remote Streamable HTTP MCP server for ChatGPT Developer Mode.

Architecture:
  public :8788  →  MixedAuthMiddleware  →  reverse proxy  →  internal MCP :8789

Auth modes (MCP_AUTH_MODE env var):
  token  — only ?mcp_token= query param (current behavior)
  mixed  — Authorization: Bearer preferred, ?mcp_token= fallback
  oauth  — only Bearer token (mcp_token rejected)

OAuth paths (/.well-known/, /oauth/) are always public to enable
the OAuth authorization flow.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

EXAMPLES_DIR = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"

sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))

_spec = importlib.util.spec_from_file_location(
    "mcp_server_module", MCP_SERVER_DIR / "server.py"
)
_mcp_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mcp_mod)
mcp = _mcp_mod.mcp

MCP_INTERNAL_HOST = os.environ.get("MCP_INTERNAL_HOST", "127.0.0.1")
MCP_INTERNAL_PORT = int(os.environ.get("MCP_INTERNAL_PORT", "8789"))
MCP_INTERNAL_URL = f"http://{MCP_INTERNAL_HOST}:{MCP_INTERNAL_PORT}"

MCP_AUTH_MODE = os.environ.get("MCP_AUTH_MODE", "token").strip().lower()
MCP_PUBLIC_TOKEN = os.environ.get("MCP_PUBLIC_TOKEN", "")

OAUTH_PUBLIC_PREFIXES = ("/.well-known/", "/oauth/")


def _is_oauth_public_path(path: str) -> bool:
    """OAuth endpoints don't require mcp_token or Bearer."""
    return path.startswith(OAUTH_PUBLIC_PREFIXES)


class MixedAuthMiddleware(BaseHTTPMiddleware):
    """Accept Bearer token (header) or mcp_token (query param).

    mode=token: only mcp_token
    mode=mixed: Bearer preferred, mcp_token fallback
    mode=oauth: only Bearer (mcp_token rejected)
    """

    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path

        # OAuth public paths — always pass through
        if _is_oauth_public_path(path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        mcp_token = request.query_params.get("mcp_token", "")
        has_bearer = auth_header.startswith("Bearer ")
        has_mcp_token = bool(mcp_token)

        if MCP_AUTH_MODE == "token":
            if not has_mcp_token or mcp_token != MCP_PUBLIC_TOKEN:
                return JSONResponse(
                    {"error": "Invalid or missing mcp_token"},
                    status_code=401,
                )
            return await call_next(request)

        if MCP_AUTH_MODE == "oauth":
            if has_bearer:
                # Let FastMCP validate the Bearer token internally
                return await call_next(request)
            if has_mcp_token:
                return JSONResponse(
                    {"error": "mcp_token is not accepted in oauth mode"},
                    status_code=401,
                )
            return JSONResponse(
                {"error": "Missing Authorization: Bearer header"},
                status_code=401,
            )

        # mixed mode: Bearer preferred, mcp_token fallback
        if has_bearer:
            return await call_next(request)

        if has_mcp_token:
            if mcp_token != MCP_PUBLIC_TOKEN:
                return JSONResponse(
                    {"error": "invalid mcp_token"},
                    status_code=403,
                )
            return await call_next(request)

        return JSONResponse(
            {"error": "Missing Authorization header or mcp_token"},
            status_code=401,
        )


async def proxy_request(request: Request) -> StreamingResponse | JSONResponse:
    """Proxy an HTTP request to the internal MCP server."""
    url = f"{MCP_INTERNAL_URL}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)

    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.request(
            method=request.method,
            url=url,
            content=body,
            headers=headers,
        )
        return StreamingResponse(
            content=resp.aiter_bytes(),
            status_code=resp.status_code,
            headers=dict(resp.headers),
        )


def create_proxy_app() -> Starlette:
    """Create auth-guarded proxy to internal MCP server."""
    proxy = Starlette()
    proxy.add_middleware(MixedAuthMiddleware)
    proxy.add_route("/{path:path}", proxy_request, methods=["GET", "POST", "DELETE"])
    return proxy


proxy_app = create_proxy_app()


def run():
    """Start both internal MCP server and public proxy."""
    import threading

    internal_host = MCP_INTERNAL_HOST
    internal_port = MCP_INTERNAL_PORT
    public_host = os.environ.get("MCP_HOST", "127.0.0.1")
    public_port = int(os.environ.get("MCP_PORT", "8788"))

    mcp.settings.host = internal_host
    mcp.settings.port = internal_port

    t = threading.Thread(
        target=mcp.run,
        kwargs={"transport": "streamable-http"},
        daemon=True,
    )
    t.start()

    print(f"  MCP internal : {internal_host}:{internal_port}", file=sys.stderr)
    print(f"  MCP public   : {public_host}:{public_port}", file=sys.stderr)
    print(f"  auth mode    : {MCP_AUTH_MODE}", file=sys.stderr)
    tok_display = MCP_PUBLIC_TOKEN[:8] + "..." if MCP_PUBLIC_TOKEN else "(not set)"
    print(f"  mcp_token    : {tok_display}", file=sys.stderr)

    uvicorn.run(proxy_app, host=public_host, port=public_port)


if __name__ == "__main__":
    run()
```

- [ ] **Step 4: Implement full auth middleware test**

```python
# tests/test_mcp_auth.py — replace skips with real tests

import os
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

# We test via the proxy app factory
from examples.chatgpt_remote_mcp.server import (
    create_proxy_app,
    MixedAuthMiddleware,
    _is_oauth_public_path,
)


@pytest.fixture
def valid_token():
    return "test-token-123"


@pytest.fixture
def token_client(valid_token):
    with patch.dict(os.environ, {"MCP_PUBLIC_TOKEN": valid_token, "MCP_AUTH_MODE": "token"}):
        # Force module reload for env vars
        import importlib
        import examples.chatgpt_remote_mcp.server as srv
        importlib.reload(srv)
        app = srv.create_proxy_app()
        yield TestClient(app)


@pytest.fixture
def mixed_client(valid_token):
    with patch.dict(os.environ, {"MCP_PUBLIC_TOKEN": valid_token, "MCP_AUTH_MODE": "mixed"}):
        import importlib
        import examples.chatgpt_remote_mcp.server as srv
        importlib.reload(srv)
        app = srv.create_proxy_app()
        yield TestClient(app)


def test_oauth_public_paths():
    assert _is_oauth_public_path("/.well-known/oauth-authorization-server")
    assert _is_oauth_public_path("/oauth/authorize")
    assert _is_oauth_public_path("/oauth/token")
    assert _is_oauth_public_path("/oauth/register")
    assert not _is_oauth_public_path("/mcp")
    assert not _is_oauth_public_path("/health")


def test_token_mode_requires_token(token_client):
    resp = token_client.get("/")
    assert resp.status_code == 401


def test_token_mode_valid_token(token_client, valid_token):
    resp = token_client.get(f"/?mcp_token={valid_token}")
    assert resp.status_code in (200, 502)  # 502 if no backend, but auth passed


def test_token_mode_invalid_token(token_client):
    resp = token_client.get("/?mcp_token=wrong")
    assert resp.status_code in (401, 403)


def test_mixed_mode_no_auth(mixed_client):
    resp = mixed_client.get("/")
    assert resp.status_code == 401


def test_mixed_mode_mcp_token_valid(mixed_client, valid_token):
    resp = mixed_client.get(f"/?mcp_token={valid_token}")
    assert resp.status_code in (200, 502)  # auth passed


def test_mixed_mode_mcp_token_invalid(mixed_client):
    resp = mixed_client.get("/?mcp_token=wrong")
    assert resp.status_code == 403


def test_mixed_mode_bearer_passthrough(mixed_client):
    """Bearer token is passed through to FastMCP (not validated by proxy)."""
    resp = mixed_client.get("/", headers={"Authorization": "Bearer some-token"})
    assert resp.status_code in (200, 502)  # auth passed


def test_oauth_endpoints_public_without_token(token_client):
    """OAuth discovery endpoints must work without any auth."""
    resp = token_client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code in (200, 502)  # passed middleware


def test_oauth_endpoints_public_in_mixed_mode(mixed_client):
    resp = mixed_client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code in (200, 502)


def test_mixed_mode_bearer_preferred(mixed_client, valid_token):
    """When both Bearer and mcp_token are present, Bearer wins."""
    resp = mixed_client.get(
        f"/?mcp_token={valid_token}",
        headers={"Authorization": "Bearer some-token"},
    )
    assert resp.status_code in (200, 502)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_mcp_auth.py -v`
Expected: 12/12 passed (some may show 502 for no backend — that's fine, auth passed)

- [ ] **Step 6: Commit**

```bash
git add tests/test_mcp_auth.py examples/chatgpt_remote_mcp/server.py
git commit -m "feat: add MixedAuthMiddleware with token/mixed/oauth modes"
```

---

### Task 3: FastMCP AuthSettings Integration

**Files:**
- Modify: `examples/mcp_server/server.py`

**Interfaces:**
- Consumes: `GatewayOAuthProvider` from `oauth_provider`, `MCP_AUTH_MODE` env var
- Produces: FastMCP with AuthSettings configured; OAuth provider registered

- [ ] **Step 1: Write expected behavior test**

```python
# tests/test_mcp_server.py
"""Tests for MCP server AuthSettings configuration."""

import os
from unittest.mock import patch

import pytest

from examples.mcp_server import oauth_provider
from examples.mcp_server.server import mcp, should_register_tool


def test_auth_disabled_by_default():
    """Default MCP_AUTH_MODE=token should not configure auth."""
    # FastMCP settings.auth should be None
    assert mcp.settings.auth is None
```

- [ ] **Step 2: Add AuthSettings to server.py**

Add at the top of `examples/mcp_server/server.py`, after imports:

```python
# OAuth configuration
MCP_AUTH_MODE = os.environ.get("MCP_AUTH_MODE", "token").strip().lower()

_oauth_provider: oauth_provider.GatewayOAuthProvider | None = None

if MCP_AUTH_MODE in ("mixed", "oauth"):
    _oauth_provider = oauth_provider.GatewayOAuthProvider()
```

Then when building the FastMCP instance, add `auth` parameter conditionally. However, looking at the code, `mcp = FastMCP(...)` is called without the auth setting. To add AuthSettings, we need to modify the FastMCP construction.

Let me look at the current signature... The FastMCP needs `auth=AuthSettings(...)`. But since the FastMCP instance is already created at module level and the auth needs to be set up front, we need to add it to the constructor.

Let me see the actual code first.

- [ ] **Step 3: Read current server.py construction**

```bash
grep -n "FastMCP\|mcp =" examples/mcp_server/server.py | head -5
```

- [ ] **Step 4: Modify FastMCP construction to include AuthSettings**

```python
# examples/mcp_server/server.py — near top, replace mcp = FastMCP(...)

from examples.mcp_server.oauth_provider import GatewayOAuthProvider, SUPPORTED_SCOPES, DEFAULT_SCOPES

MCP_AUTH_MODE = os.environ.get("MCP_AUTH_MODE", "token").strip().lower()

_auth_provider: GatewayOAuthProvider | None = None
_auth_settings = None

if MCP_AUTH_MODE in ("mixed", "oauth"):
    _auth_provider = GatewayOAuthProvider()
    try:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
        from pydantic import AnyHttpUrl

        _auth_settings = AuthSettings(
            issuer_url=AnyHttpUrl(os.environ.get("MCP_ISSUER_URL", "https://ssh-gateway.example.com")),
            service_documentation_url=AnyHttpUrl("https://github.com/gpakoh/agent-ssh-gateway"),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=SUPPORTED_SCOPES,
                default_scopes=DEFAULT_SCOPES,
            ),
            required_scopes=None,  # Don't require scopes at transport level
        )
    except ImportError:
        pass  # Older mcp package without auth — fall back to token-only
```

Then modify the `FastMCP` constructor call — need to find the exact location and add:

```python
mcp = FastMCP(
    "agent-ssh-gateway",
    auth=_auth_settings,
)
```

And after construction, if provider is set:

```python
if _auth_provider and _auth_settings:
    # Register provider so FastMCP creates OAuth endpoints
    def _token_verifier(token_str: str) -> dict | None:
        stored = _auth_provider.verify_access_token(token_str)
        if stored is None:
            return None
        return {"client_id": stored.client_id, "scopes": stored.scopes}
    mcp._token_verifier = _token_verifier
    mcp._auth_server_provider = _auth_provider
```

This is tricky because FastMCP's internal API may change. The alternative is to use FastMCP's documented way to set auth:

```python
mcp = FastMCP("agent-ssh-gateway")
if _auth_settings:
    mcp.settings.auth = _auth_settings
    mcp._auth_server_provider = _auth_provider
```

- [ ] **Step 5: Add OAuth discovery endpoint manually**

Since the proxy is in front, FastMCP's built-in discovery might not be reachable at the right URL. Add a minimal OAuth metadata handler:

```python
# In the proxy app, add a route for discovery that responds without requiring auth
# (already handled by MixedAuthMiddleware's public path pass-through)
```

FastMCP's built-in auth routes add the endpoints automatically when `_auth_server_provider` is set. The proxy passes through `/.well-known/` and `/oauth/` without auth. So this should work out of the box.

- [ ] **Step 6: Write confirmation test**

```python
# tests/test_mcp_server.py — add

@patch.dict(os.environ, {"MCP_AUTH_MODE": "mixed"})
def test_oauth_provider_initialized_in_mixed_mode():
    import importlib
    import examples.mcp_server.server as srv
    importlib.reload(srv)
    assert srv._auth_provider is not None


@patch.dict(os.environ, {"MCP_AUTH_MODE": "token"})
def test_oauth_provider_not_initialized_in_token_mode():
    import importlib
    import examples.mcp_server.server as srv
    importlib.reload(srv)
    assert srv._auth_provider is None
```

- [ ] **Step 7: Commit**

```bash
git add examples/mcp_server/server.py tests/test_mcp_server.py
git commit -m "feat: integrate AuthSettings and GatewayOAuthProvider with FastMCP"
```

---

### Task 4: Configuration, Docs, and Healthcheck

**Files:**
- Modify: `examples/mcp_server/.env.example`
- Modify: `examples/chatgpt_remote_mcp/.env.example`
- Modify: `scripts/mcp_fleet_healthcheck.py` (if needed)
- Create: None

- [ ] **Step 1: Update .env.example**

Add to both `examples/mcp_server/.env.example` and `examples/chatgpt_remote_mcp/.env.example`:

```
# MCP Auth mode: token | mixed | oauth
MCP_AUTH_MODE=token

# OAuth issuer URL (used in discovery endpoints)
MCP_ISSUER_URL=https://ssh-gateway.example.com

# Static token fallback (required for token/mixed mode)
MCP_PUBLIC_TOKEN=
```

- [ ] **Step 2: Verify healthcheck compatibility**

Run: `python scripts/mcp_fleet_healthcheck.py --verbose`
Expected: 6/6 healthy, healthcheck uses `?mcp_token=` which still works in token and mixed modes.

If healthcheck fails in mixed mode without mcp_token — no fix needed (it passes the token).

- [ ] **Step 3: Commit**

```bash
git add examples/mcp_server/.env.example examples/chatgpt_remote_mcp/.env.example
git commit -m "docs: add MCP_AUTH_MODE and MCP_ISSUER_URL to env examples"
```

---

### Task 5: Full Integration Smoke

- [ ] **Step 1: Run all tests**

```bash
make check
```

Expected: 90+ existing tests + 16 provider tests + 12 auth tests all passing.

- [ ] **Step 2: Verify healthcheck**

```bash
python scripts/mcp_fleet_healthcheck.py --verbose
```

Expected: 6/6, 85 tools.

- [ ] **Step 3: Git status clean**

```bash
git status --short
```

Expected: only intended files.

---

### Task 6: CHANGELOG

- [ ] **Step 1: Update CHANGELOG.md**

Add under `[Unreleased]`:

```markdown
### Added

- **OAuth 2.1 + PKCE Phase 2 (mixed mode)** — `GatewayOAuthProvider` with in-memory storage,
  PKCE S256, DCR for public clients, 7 scopes (`mcp:read`, `mcp:project`, `mcp:handoff`,
  `mcp:agent-run`, `mcp:docker`, `mcp:postgres`, `mcp:repo`). (Session 113)
- **MixedAuthMiddleware** — accepts `Authorization: Bearer` (preferred) or `?mcp_token=`
  (fallback). Three modes: `token`, `mixed`, `oauth` via `MCP_AUTH_MODE` env var. (Session 113)
- **OAuth discovery endpoints** — `/.well-known/oauth-authorization-server`, `/oauth/authorize`,
  `/oauth/token`, `/oauth/register` served by FastMCP at `/mcp/*` route. (Session 113)

### Changed

- `examples/chatgpt_remote_mcp/server.py`: `TokenAuthMiddleware` replaced with
  `MixedAuthMiddleware` supporting 3 auth modes.
- `examples/mcp_server/server.py`: FastMCP configured with `AuthSettings` when
  `MCP_AUTH_MODE` is `mixed` or `oauth`.

### Docs

- Design spec: `docs/superpowers/specs/2026-06-25-unified-mcp-oauth-pkce-design.md`.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: add CHANGELOG entry for OAuth Phase 2"
```

---

## Self-Review Checklist

- [ ] **Spec coverage:** Does every requirement from the Phase 2 migration plan have a corresponding task?
  - GatewayOAuthProvider ✅ (Task 1)
  - MixedAuthMiddleware ✅ (Task 2)
  - AuthSettings + FastMCP integration ✅ (Task 3)
  - MCP_AUTH_MODE env var ✅ (Tasks 2, 3, 4)
  - DCR enabled ✅ (Task 1)
  - Discovery endpoints ✅ (Task 2 — public path pass-through)
  - mcp_token fallback preserved ✅ (Task 2 — mixed mode)
  - Fleet adapters unchanged ✅ (excluded by design)
  - Tool-level scopes not added in Phase 2 ✅ (excluded by design)
  - No secrets committed ✅ (env vars only, token from env)
  - Healthcheck green ✅ (Task 5)

- [ ] **Placeholder scan:** No "TBD", "TODO", or placeholder code in the plan.

- [ ] **Type consistency:** `GatewayOAuthProvider` class name and method signatures are consistent across all tasks.

- [ ] **Gap identified:** The FastMCP `_auth_server_provider` and `_token_verifier` setting mechanism is undocumented — may require reading FastMCP source. If FastMCP doesn't support late-binding `_auth_server_provider`, the FastMCP construction in server.py will need restructuring (create a configured FastMCP instance instead of modifying it after construction).

