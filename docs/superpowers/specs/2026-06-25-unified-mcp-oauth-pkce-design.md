# Unified MCP OAuth 2.1 + PKCE Design

**Date:** 2026-06-25
**Status:** Design spec, no implementation yet
**Session:** 112

## 1. Current Auth Inventory

### 1.1 Gateway MCP (main, port 8788)

```
examples/chatgpt_remote_mcp/server.py
```

- `TokenAuthMiddleware` (line 42): validates `?mcp_token=` query param against `MCP_PUBLIC_TOKEN` env var.
- Static token, single shared secret.
- Proxies to internal FastMCP on `127.0.0.1:8789` (no auth, localhost only).
- No scopes, no TTL, no rotation.
- 85 tools in `chatgpt` mode.

### 1.2 Fleet Adapters (6 services)

| Adapter | Port | Auth | File |
|---------|------|------|------|
| Context7 | 8790 | `TokenAuthMiddleware` (`fleet/shared.py`) | `fleet/context7_server.py` |
| GitHub | 8791 | `TokenAuthMiddleware` | `fleet/github_server.py` |
| Gitea | 8792 | `TokenAuthMiddleware` | `fleet/gitea_server.py` |
| Docker | 8793 | `TokenAuthMiddleware` | `fleet/docker_server.py` |
| Postgres | 8794 | `TokenAuthMiddleware` | `fleet/postgres_server.py` |

All use the same `TokenAuthMiddleware` from `fleet/shared.py` (line 13):
- Validates `?mcp_token=` against `MCP_PUBLIC_TOKEN` env var.
- 401 if missing, 403 if invalid.
- Shared single token across all fleet adapters.

### 1.3 Nginx Routing

All MCP routes on `ssh-gateway.example.com`:
```
/mcp           → 127.0.0.1:8788  (Gateway)
/mcp/context7  → 127.0.0.1:8790
/mcp/github    → 127.0.0.1:8791
/mcp/gitea     → 127.0.0.1:8792
/mcp/docker    → 127.0.0.1:8793
/mcp/postgres  → 127.0.0.1:8794
```

All routes: **no Authelia SSO, no mTLS** — auth is handled by the Starlette middleware.

### 1.4 Tool Registration

`examples/mcp_server/server.py`:
- `@register_tool(name)` decorator (line 113) wraps tool registration with `should_register_tool()` filter.
- `tool_modes.py` defines 4 modes: `minimal`, `standard`, `full`, `chatgpt` — 85 tools in chatgpt mode.
- No per-tool scopes, no authorization context.

### 1.5 Summary

| Property | Current |
|----------|---------|
| Auth method | Static query token |
| Token rotation | None (manual env change + service restart) |
| Scopes | None (all-or-nothing) |
| ChatGPT compatibility | `?mcp_token=` param |
| Dynamic client registration | None |
| Token TTL | Infinite |
| Audit | None (no token-bound identity) |

## 2. Target OAuth Architecture

### 2.1 Leverage FastMCP Built-in OAuth

FastMCP 2.x already provides all required OAuth components (available in `.venv`):

```
mcp/server/auth/provider.py       → OAuthAuthorizationServerProvider[ClientT, UserT, AuthCodeT]
mcp/server/auth/settings.py       → AuthSettings, ClientRegistrationOptions, RevocationOptions
mcp/server/auth/middleware/bearer_auth.py → BearerAuthBackend
mcp/server/auth/handlers/authorize.py    → AuthorizationHandler
mcp/server/auth/handlers/token.py        → TokenHandler
mcp/server/auth/handlers/register.py     → RegistrationHandler
mcp/server/auth/handlers/revoke.py       → RevocationHandler
mcp/server/auth/handlers/metadata.py     → OAuth metadata
mcp/server/auth/routes.py                → create_auth_routes()
```

**Decision: Use FastMCP native OAuth, not custom handler.**

Rationale:
- Already in the dependency tree
- Implements OAuth 2.1 + PKCE + DCR spec-compliant
- Provides `BearerAuthBackend` + `AuthContextMiddleware` + `AuthorizationContext` — exact pattern needed for tool-level enforcement
- Reduces custom code to configuration + provider subclass

### 2.2 Architecture

```text
ChatGPT / Client
     │
     ├── GET /.well-known/oauth-authorization-server (discovery)
     │
     ├── POST /oauth/register  (DCR — optional, ChatGPT dynamic)
     │
     ├── GET /oauth/authorize?response_type=code&client_id=...&redirect_uri=...&code_challenge=S256...
     │       → user authenticates → authorization code returned
     │
     ├── POST /oauth/token (grant_type=authorization_code + code_verifier)
     │       → access_token + refresh_token returned
     │
     └── POST /mcp (Authorization: Bearer <access_token>)
             → FastMCP BearerAuthBackend → AuthContextMiddleware → run_tool with scopes
```

### 2.3 New Endpoints

| Endpoint | Purpose | Auth |
|----------|---------|------|
| `/.well-known/oauth-authorization-server` | OAuth discovery (RFC 8414) | None |
| `/oauth/authorize` | Authorization code request | End-user auth (session cookie) |
| `/oauth/token` | Exchange code for tokens | Client auth or none (public client) |
| `/oauth/register` | Dynamic client registration (optional) | None (or pre-shared token) |
| `/oauth/revoke` | Revoke token (optional) | Client auth |
| `/oauth/introspect` | Admin token inspection (internal) | Internal auth |

All under the same nginx route `/mcp/*` — FastMCP adds them when `AuthSettings` is configured.

### 2.4 Provider Implementation

Subclass `OAuthAuthorizationServerProvider`:

```python
from mcp.server.auth.provider import (
    OAuthAuthorizationServerProvider,
    AuthorizationParams,
    AccessToken,
    RefreshToken,
    AuthCode,
)

class GatewayOAuthProvider(OAuthAuthorizationServerProvider[Client, User, AuthCode]):
    """OAuth provider for agent-ssh-gateway MCP fleet."""
    ...
```

## 3. Endpoint List

### 3.1 OAuth Discovery

`GET /.well-known/oauth-authorization-server`

Returns RFC 8414 metadata:
```json
{
  "issuer": "https://ssh-gateway.example.com",
  "authorization_endpoint": "https://ssh-gateway.example.com/oauth/authorize",
  "token_endpoint": "https://ssh-gateway.example.com/oauth/token",
  "registration_endpoint": "https://ssh-gateway.example.com/oauth/register",
  "revocation_endpoint": "https://ssh-gateway.example.com/oauth/revoke",
  "scopes_supported": [
    "mcp:read", "mcp:project", "mcp:handoff",
    "mcp:agent-run", "mcp:docker", "mcp:postgres", "mcp:repo"
  ],
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none"],
  "tls_client_certificate_bounded_access_tokens": false
}
```

### 3.2 Authorization Endpoint

`GET /oauth/authorize`

Parameters:
- `response_type` (required, must be `"code"`)
- `client_id` (required)
- `redirect_uri` (required)
- `scope` (optional, space-separated)
- `state` (recommended, anti-CSRF)
- `code_challenge` (required for PKCE)
- `code_challenge_method` (required, must be `"S256"`)

Response: 302 redirect to `redirect_uri?code=...&state=...`

### 3.3 Token Endpoint

`POST /oauth/token`

Parameters:
- `grant_type` (required, `"authorization_code"` or `"refresh_token"`)
- `code` (for authorization_code)
- `code_verifier` (for authorization_code, PKCE verification)
- `redirect_uri` (for authorization_code)
- `client_id` (for public client)
- `refresh_token` (for refresh_token)

Response:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "def50200...",
  "scope": "mcp:read mcp:project"
}
```

## 4. Dynamic Client Registration (DCR)

### 4.1 Support Level

**MVP: Enable DCR for public clients.**

ChatGPT uses DCR for custom remote MCP servers when static credentials are not pre-configured (OpenAI docs: `client_registration` in MCP auth config). Therefore DCR must be available.

### 4.2 Registration Endpoint

`POST /oauth/register`

Request:
```json
{
  "redirect_uris": ["https://chatgpt.com/callback"],
  "client_name": "ChatGPT MCP Client",
  "token_endpoint_auth_method": "none",
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"]
}
```

Response (RFC 7591):
```json
{
  "client_id": "chatgpt-abc123",
  "client_secret": null,
  "client_id_issued_at": 1782402834,
  "client_secret_expires_at": 0,
  "redirect_uris": ["https://chatgpt.com/callback"],
  "token_endpoint_auth_method": "none",
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"]
}
```

### 4.3 Registration Options

```python
ClientRegistrationOptions(
    enabled=True,
    client_secret_expiry_seconds=None, # public clients, no secret
    valid_scopes=["mcp:read", "mcp:project", "mcp:handoff", "mcp:agent-run", "mcp:docker", "mcp:postgres", "mcp:repo"],
    default_scopes=["mcp:read", "mcp:project"],
)
```

## 5. PKCE Flow

### 5.1 Authorization Code + PKCE Flow

```
Client (ChatGPT)                     Server (ssh-gateway.example.com)
     │                                     │
     │  1. Generate code_verifier           │
     │     code_challenge = SHA256(verifier) │
     │                                     │
     │  2. GET /oauth/authorize?           │
     │     response_type=code               │
     │     client_id=chatgpt-abc123         │
     │     redirect_uri=https://chatgpt...  │
     │     code_challenge=S256...           │
     │     code_challenge_method=S256       │
     │     state=xyz                        │
     │     scope=mcp:read mcp:project       │
     │─────────────────────────────────────>│
     │                                     │
     │  (User authenticates via             │
     │   session cookie or SSO redirect)    │
     │                                     │
     │  3. 302 redirect to redirect_uri?   │
     │     code=auth_abc123                │
     │     state=xyz                       │
     │<─────────────────────────────────────│
     │                                     │
     │  4. POST /oauth/token               │
     │     grant_type=authorization_code    │
     │     code=auth_abc123                 │
     │     code_verifier=original_verifier  │
     │     client_id=chatgpt-abc123         │
     │     redirect_uri=https://chatgpt...  │
     │─────────────────────────────────────>│
     │                                     │
     │  5. { access_token, refresh_token,  │
     │     expires_in, scope }              │
     │<─────────────────────────────────────│
     │                                     │
     │  6. POST /mcp                        │
     │     Authorization: Bearer eyJ...     │
     │─────────────────────────────────────>│
     │                                     │
     │  7. Tool response                    │
     │<─────────────────────────────────────│
```

### 5.2 TTL Values

| Item | Value |
|------|-------|
| Authorization code TTL | 5 minutes |
| Access token TTL | 1 hour |
| Refresh token TTL | 30 days |
| Authorization code | Single-use (invalidated on first exchange) |

### 5.3 PKCE Requirements

- `code_challenge_method=S256` only (plain text `"plain"` not accepted)
- `code_verifier`: 43–128 characters, unreserved chars (A-Z, a-z, 0-9, -, ., _, ~)
- Server MUST verify `base64url(sha256(code_verifier)) == code_challenge`
- `state` parameter required for CSRF protection
- `redirect_uri` exact match against registered URIs

## 6. Token Storage Decision

### 6.1 Options

| Option | Pro | Con |
|--------|-----|-----|
| **Redis** | Fast, TTL-native (EXPIRE), already in gateway stack, shared across adapters | Adds dependency for standalone adapter mode |
| **SQLite** | Zero dependencies, portable, file-based | No TTL-native expiry, slower at scale, file lock contention |
| **In-memory** | Fastest, simplest | Lost on restart (all tokens invalidated — acceptable for MVP) |

### 6.2 Decision

**Phase 1 (MVP): In-memory storage via FastMCP default provider.**

FastMCP provider stores auth codes and tokens in-memory by default. For MVP, this is sufficient — token loss on restart is acceptable because the fleet is stateless.

**Phase 2: Redis** — if token persistence across restarts or cross-adapter token sharing is needed.

Redis keys:
```
oauth:client:<client_id>          → client metadata (hash)
oauth:code:<code>                 → authorization code (string) TTL: 5m
oauth:token:<jti>                 → access token (hash) TTL: 1h
oauth:refresh:<jti>               → refresh token (hash) TTL: 30d
```

## 7. Scope Model

### 7.1 Scope Definitions

| Scope | Purpose | Default for ChatGPT |
|-------|---------|---------------------|
| `mcp:read` | tools/list, health, docs, read-only info tools | ✅ |
| `mcp:project` | Project read/search/test/lint tools (`gateway_project_*` read flavors) | ✅ |
| `mcp:handoff` | Write/read/archive agent tasks (handoff lifecycle) | ❌ (requires consent) |
| `mcp:agent-run` | Execute OpenCode/Mimo runs in worktrees | ❌ (requires consent) |
| `mcp:docker` | Docker read-only tools (ps, images, inspect, logs, stats) | ❌ (optional) |
| `mcp:postgres` | Postgres read-only tools (list schemas, tables, describe) | ❌ (optional) |
| `mcp:repo` | GitHub/Gitea read-only repository tools | ❌ (optional) |
| `mcp:admin` | Reserved — token introspection, admin ops | ❌ (never issue) |

### 7.2 Tool-to-Scope Mapping

Each tool in `server.py` annotated with required scope:

```python
TOOL_SCOPES: dict[str, str] = {
    # Gateway/health — mcp:read
    "gateway_health": "mcp:read",
    "gateway_session_health": "mcp:read",
    "gateway_list_sessions": "mcp:read",
    "gateway_job_status": "mcp:read",
    "gateway_job_result": "mcp:read",

    # Project read — mcp:project
    "gateway_project_read_file": "mcp:project",
    "gateway_project_search_text": "mcp:project",
    "gateway_project_find_files": "mcp:project",
    "gateway_project_tree": "mcp:project",
    "gateway_project_git_diff": "mcp:project",
    "gateway_project_run_pytest": "mcp:project",
    "gateway_project_run_ruff": "mcp:project",
    "gateway_project_run_mypy": "mcp:project",

    # Handoff — mcp:handoff
    "gateway_project_write_agent_task": "mcp:handoff",
    "gateway_project_archive_agent_task": "mcp:handoff",

    # Agent execution — mcp:agent-run
    "project_run_opencode": "mcp:agent-run",
    "gateway_project_run_mimo": "mcp:agent-run",

    # Docker — mcp:docker
    "docker_ps": "mcp:docker",
    "docker_images": "mcp:docker",
    "docker_logs": "mcp:docker",

    # Postgres — mcp:postgres
    "postgres_health": "mcp:postgres",
    "postgres_list_tables": "mcp:postgres",
    "postgres_describe_table": "mcp:postgres",

    # Repo — mcp:repo
    "github_get_repo": "mcp:repo",
    "gitea_get_repo": "mcp:repo",

    # Context7 — mcp:read
    "resolve_library_id": "mcp:read",
    "query_docs": "mcp:read",
}
```

**Implementation:** `should_register_tool()` extended to check `required_scopes ∩ granted_scopes`:

```python
def should_register_tool(
    tool_name: str,
    mode: ToolMode | None = None,
    granted_scopes: set[str] | None = None,
) -> bool:
    # mode check
    if not tool_name in TOOL_NAMES_BY_MODE[mode]:
        return False
    # scope check (if OAuth)
    if granted_scopes is not None:
        required = TOOL_SCOPES.get(tool_name, "mcp:read")
        if required not in granted_scopes:
            return False
    return True
```

### 7.3 Scope Enforcement Layers

```
Layer 1: Nginx (auth check / Bearer presence)
Layer 2: TokenAuthMiddleware (validate mcp_token OR Bearer token)
Layer 3: BearerAuthBackend + AuthContextMiddleware (OAuth only)
Layer 4: should_register_tool + granted_scopes (tool-level enforcement)
```

## 8. Migration Modes

### 8.1 Three Auth Modes

```python
MCP_AUTH_MODE = os.getenv("MCP_AUTH_MODE", "token")  # token | oauth | mixed
```

| Mode | mcp_token | Bearer token | Behavior |
|------|-----------|-------------|----------|
| `token` | ✅ Required | ❌ Ignored | Current — static token only |
| `oauth` | ❌ Rejected | ✅ Required | OAuth-only, strict |
| `mixed` | ✅ Fallback | ✅ Preferred | Bearer if present, else mcp_token |

### 8.2 Migration Plan

**Phase 1 (current): `token` mode** — no changes.

**Phase 2 (first deployment): `mixed` mode**
- FastMCP gets `AuthSettings` with `OAuthAuthorizationServerProvider`
- `TokenAuthMiddleware` wraps FastMCP app
- If `Authorization: Bearer` header present → validate via `BearerAuthBackend`
- If `?mcp_token=` query param present → validate via `TokenAuthMiddleware`
- If both present → Bearer takes precedence
- If neither → 401

**Phase 3 (future): `oauth` mode**
- `mcp_token` disabled
- Only Bearer token auth
- Client MUST complete full OAuth flow

### 8.3 Mixed Mode Middleware

```python
class MixedAuthMiddleware(BaseHTTPMiddleware):
    """Accept either mcp_token (query) or Bearer token (header)."""

    def __init__(self, app, valid_tokens: set[str]):
        super().__init__(app)
        self._valid_tokens = valid_tokens

    async def dispatch(self, request, call_next):
        auth_header = request.headers.get("Authorization", "")
        mcp_token = request.query_params.get("mcp_token", "")

        if auth_header.startswith("Bearer "):
            # Let FastMCP handle Bearer validation
            return await call_next(request)

        if mcp_token:
            if mcp_token in self._valid_tokens:
                return await call_next(request)
            return JSONResponse({"error": "invalid mcp_token"}, 403)

        # Fall through to regular auth (FastMCP will reject)
        return await call_next(request)
```

## 9. Nginx Routing Requirements

### 9.1 Current Routes (no changes needed)

```
location /mcp {
    proxy_pass http://127.0.0.1:8788;
    # No Authelia, no mTLS (handled by middleware)
}
```

### 9.2 OAuth Endpoint Routing

OAuth endpoints are served by FastMCP on the same `/mcp` route:
```
/.well-known/oauth-authorization-server  → /mcp/.well-known/oauth-authorization-server
/oauth/authorize                          → /mcp/oauth/authorize
/oauth/token                              → /mcp/oauth/token
/oauth/register                           → /mcp/oauth/register
```

**The `/oauth/authorize` endpoint requires end-user authentication.**
Options for user auth:
1. **Authelia SSO integration** — redirect to `auth.example.com` for login, return to `/oauth/authorize`
2. **Simple session cookie** — basic auth form on the same server
3. **Separate auth subdomain** — `auth.ssh-gateway.example.com` with its own nginx block

**Decision for MVP:** Use simple session cookie (no external SSO dependency for `/oauth`). Can be upgraded to Authelia in Phase 3.

### 9.3 Fleet Adapter Changes

Each fleet adapter (context7, github, gitea, docker, postgres) currently has its own `create_auth_proxy()` + `TokenAuthMiddleware`. For OAuth:

**Option A: Gateway-level auth, pass-through to adapters.**
- All fleet adapters sit behind the main Gateway MCP
- Gateway validates auth, proxies to internal fleet ports
- Fleet adapters remove their own auth middlewares (localhost-only)
- Single Bearer token validation point

**Option B: Each adapter validates Bearer independently.**
- Each fleet adapter adds `BearerAuthBackend`
- Each adapter has its own `OAuthAuthorizationServerProvider` (or shared Redis-backed)
- More complex, more independent

**Decision: Option A for MVP.**
- Gateway MCP validates auth centrally
- Fleet adapters become localhost-only (remove `create_auth_proxy()`)
- Nginx routes `/mcp/*` only to Gateway port 8788
- Gateway proxies to fleet adapters on internal ports without auth

## 10. Backward Compatibility

### 10.1 mcp_token Fallback

In `mixed` mode:
1. If `?mcp_token=...` present and valid → full access (current behavior)
2. If `Authorization: Bearer ...` present → OAuth scoped access
3. If both → Bearer wins (scoped)
4. If neither → 401

### 10.2 Healthcheck Compatibility

Healthcheck uses `?mcp_token=` currently. In `mixed` mode, it continues to work.
In `oauth` mode, healthcheck must be updated to use Bearer token or be exempted.

### 10.3 No Breaking Changes for Registered Clients

During Phase 2 (mixed), existing ChatGPT integration using `?mcp_token=...` URL continues unchanged.
Clients can opt-in to OAuth by registering and using Bearer token.

## 11. Security Risks

| Risk | Mitigation |
|------|-----------|
| **Authorization code interception** | PKCE S256, single-use code, 5-min TTL |
| **Token leakage** | Short-lived access tokens (1h), refresh rotation, Bearer in headers not URLs |
| **CSRF on /authorize** | `state` parameter required, validated on callback |
| **Client impersonation** | DCR requires valid `redirect_uris`, public clients only (`token_endpoint_auth_method=none`) |
| **Refresh token theft** | Refresh rotation (old refresh becomes invalid on use), 30-day TTL |
| **Tool escalation** | Scopes enforced at tool registration level; admin scope never issued |
| **Mixed mode bypass** | If Bearer is invalid, fallback to mcp_token preserves service continuity; `oauth` mode removes fallback |
| **DCR abuse** | Rate limit on `/oauth/register`; optional registration token for trusted clients |

## 12. Rollback Plan

### 12.1 Rollback Triggers
- ChatGPT OAuth flow fails consistently
- Token issuance/validation errors > 1% of requests
- Tool-level scope enforcement breaks existing tools
- Healthcheck reports degraded fleet

### 12.2 Rollback Steps

```bash
# 1. Set MCP_AUTH_MODE=token (disables Bearer, restores mcp_token-only)
ssh root@192.0.2.10
sed -i 's/MCP_AUTH_MODE=mixed/MCP_AUTH_MODE=token/' /etc/agent-ssh-gateway-mcp.env
systemctl restart agent-ssh-gateway-mcp.service

# 2. Verify
python scripts/mcp_fleet_healthcheck.py --verbose

# 3. If needed, revert code changes
cd /media/1TB/Python/web_ssh/web-ssh-gateway
git revert <oauth-commit>
git push origin master
```

### 12.3 Safe State

Before any OAuth deployment, ensure:
- `MCP_AUTH_MODE=token` is default
- `mcp_token` path is fully functional
- Healthcheck passes 6/6
- Git tag `v0.1.15-alpha` is the known-good state

## 13. Testing Plan

### 13.1 Unit Tests
- Token validation (valid, invalid, missing, mixed mode)
- Bearer validation (valid token, expired, wrong scopes)
- PKCE challenge verification (valid, invalid verifier)
- Authorization code flow (happy path, expired, reused)
- DCR (valid registration, invalid redirect_uris, missing fields)
- Tool-level scope enforcement (allowed, denied, partial)
- Mixed mode precedence (Bearer > mcp_token > 401)

### 13.2 Integration Tests
- Full OAuth flow with mock ChatGPT client
- Fleet adapter pass-through (Gateway validates → adapter called without auth)
- Token refresh cycle
- Revocation flow

### 13.3 End-to-End Tests
- Register new client via DCR
- Complete authorization code + PKCE flow
- Call tools with Bearer token
- Verify tool-level scope gating
- Verify mcp_token still works in mixed mode
- Verify mcp_token blocked in oauth mode

### 13.4 Security Tests
- Replay authorization code (must fail)
- Invalid code_verifier (must fail)
- Expired access token (must fail)
- Token with insufficient scope (must fail)
- Registration with suspicious redirect_uris (must fail)
- Rate limit on register/token endpoints

## 14. Implementation Phases

### Phase 1 (Current) — `token` mode
- Static `?mcp_token=` auth
- No changes needed

### Phase 2 (MVP) — `mixed` mode
- Add `GatewayOAuthProvider` (subclass `OAuthAuthorizationServerProvider`)
- Add `AuthSettings` to FastMCP config
- Enable DCR (public clients only)
- Implement `MixedAuthMiddleware`
- Scope model defined but NOT enforced at tool level yet
- Fleet adapters: remove auth from internal listeners
- Nginx: no changes needed
- **Risk:** Scope model defined but not enforced in Phase 2.
  - `mcp_token` = full access (as today)
  - Bearer token = has scopes, but tools don't check them yet
  - Gap documented: tool-level enforcement in Phase 3

Estimate: 2–3 sessions (spec → provider → middleware → test)

### Phase 3 — Tool-level enforcement
- `TOOL_SCOPES` dict in `server.py`
- `should_register_tool` extended with scope check
- `AuthorizationContext` propagated to `run_tool()`
- Each tool function checks `required_scopes ∩ granted_scopes`
- Tests for scope gating

Estimate: 1–2 sessions

### Phase 4 (Future) — `oauth` mode
- Deprecate `mcp_token`
- Fleet adapters fully behind Gateway auth
- Authelia SSO for `/oauth/authorize`
- Audit logging for token operations

### Phase 5 (Future) — Passwordless/SSO
- WebAuthn or OIDC for `/oauth/authorize` user auth
- Replace session cookie with external identity provider
- Admin UI for client management

## 15. Recommendation

### Immediate (Session 113)

**Implement Phase 1 → Phase 2 transition:**

1. **Define `GatewayOAuthProvider`** in `examples/mcp_server/oauth_provider.py`
   - In-memory storage
   - PKCE S256 validation
   - Scopes: `mcp:read`, `mcp:project`, `mcp:handoff`, `mcp:agent-run`
   - Public client DCR

2. **Integrate into `server.py`**
   - Add `AuthSettings` to FastMCP config
   - `MCP_AUTH_MODE` env var controls middleware behavior

3. **Update `MixedAuthMiddleware`**
   - Replace `TokenAuthMiddleware` in `chatgpt_remote_mcp/server.py`
   - Accept both `?mcp_token=` and `Authorization: Bearer`

4. **Fleet adapters: remove internal auth**
   - Each adapter port becomes localhost-only
   - Gateway is the single auth point

5. **Test DCR with ChatGPT**
   - Verify discovery flow
   - Verify authorize + token flow
   - Verify Bearer-authenticated tool calls

**Not for Phase 2:**
- Tool-level scope enforcement (Phase 3)
- Token persistence to Redis (Phase 2+)
- `/oauth/authorize` UI polish (minimal MVP acceptable)
- Fleet adapter scope isolation (all adapters behind Gateway)

**Key constraint preserved throughout:**
```text
mcp_token fallback always works
85 tools always available
healthcheck 6/6 always green
no secrets committed
```
