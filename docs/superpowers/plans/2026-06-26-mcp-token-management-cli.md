# MCP Token Management CLI — Design Spec

**Date:** 2026-06-26
**Status:** Draft
**Session:** 125

## Problem

MCP tokens are currently hardcoded in environment variables (`MCP_HEALTHCHECK_BEARER_TOKEN`, `MCP_EXTRA_TOKENS_JSON`, `MCP_EXTRA_TOKENS_FILE`) and stored as raw tokens in `GatewayOAuthProvider._tokens`. There is no:
- CLI to create/list/revoke/rotate tokens
- Persistent token store that survives server restarts
- Way to audit token usage (last_used_at, created_at)

## Goals

1. **Hash-based token store**: provider stores `sha256(raw_token)` as dict key — never raw token in memory or on disk
2. **Persistent store file**: JSON file at `MCP_TOKEN_STORE_FILE` that survives server restarts; contains only hashed tokens
3. **Unified registration API**: all token sources (healthcheck, extras, store file) go through two methods:
   - `register_static_token(raw_token, ...)` — hashes internally
   - `register_hashed_token(token_hash, ...)` — direct insert for file-backed tokens
4. **CLI (`mcp-token`)**: create, list, revoke, rotate commands; raw token printed once at create

## Non-Goals

- REST API for token management (CLI-only, root-only via file perms)
- UI for token management
- Custom access profiles (still hardcoded in `tool_scopes.py`)
- Token expiry/rotation enforcement (expires_at stored but not enforced beyond existing mechanism)
- Rate limiting per token

## Architecture Decisions

### ADR-1: Hash-lookup provider storage

**Decision:** Provider stores tokens keyed by `sha256(raw_token)` with `"sha256:"` prefix.

- `_tokens[hash_token(raw)] = StoredToken(...)`
- `verify_access_token(raw)` → hashes input, looks up by hash
- Token store file contains only token hashes, never raw tokens
- Provider loads persisted hashed tokens after restart via `register_hashed_token()`

### ADR-2: MCP_TOKEN_STORE_FILE via env var

**Decision:** Configurable via `MCP_TOKEN_STORE_FILE` env var, default `/var/lib/agent-ssh-gateway/mcp_tokens.json`.

Why env var over hardcoded path:
- Different paths per environment (dev, staging, prod)
- Tests use temp files
- Docker/systemd may have different volume mounts
- Rollback is simpler (just change env, no code deploy)

### ADR-3: CLI over REST API

**Decision:** CLI tool (`mcp-token`) rather than REST API or web UI.

- Smaller attack surface (CLI runs as root, not exposed on network)
- No need for admin auth scheme
- Token store file at `/var/lib/agent-ssh-gateway/mcp_tokens.json` with `chmod 600`
- CLI reads/writes store file directly (not through provider)

## Detailed Design

### 1. hash_token() helper

```python
def hash_token(token: str) -> str:
    """Return sha256 hash with explicit prefix."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
```

The `"sha256:"` prefix makes the format explicit and leaves room for future hash algorithm upgrades.

Location: `examples/mcp_server/oauth_provider.py` (module-level function)

### 2. Provider storage changes

**`_tokens` dict**: key changes from `raw_token` to `hash_token(raw_token)`.

**`register_static_token(raw_token, ...)`**: new method that:
1. Calls `hash_token(raw_token)`
2. Resolves scopes from `profile` argument (via `get_profile_scopes()`)
3. Creates `StoredToken` with `expires_at=float("inf")`, `type="access"`
4. Stores in `_tokens[token_hash]`

**`register_hashed_token(token_hash, ...)`**: new method that:
1. Validates `token_hash` starts with `"sha256:"`
2. Same as above but skips hashing step

All external callers (in `server.py`) must use these methods — no direct `_tokens[key] = StoredToken(...)`.

Internal OAuth flow methods also use hash keys and store hash in `StoredToken.token`:
- `exchange_code_for_token()`: `self._tokens[hash_token(access_token)] = StoredToken(token=hash_token(access_token), ...)`
- `refresh_access_token()`: same for new access token
- `revoke_client_token(client_id, token_str)`: look up by `hash_token(token_str)`
- `revoke_token(token_str)`: look up by `hash_token(token_str)`

### 3. StoredToken.token stores hash, not raw

After this change, `StoredToken.token` stores the hash (`"sha256:..."`), not the raw token.
`load_access_token()` receives `token_str` (raw), looks up by hash, and passes `token_str` directly
to `AccessToken(token=token_str, ...)` — never reading `stored.token` for the Bearer value.

This ensures no raw token is held in memory at any point. All `StoredToken` entries created
through OAuth flows (`exchange_code_for_token`, `refresh_access_token`) also use the hash as key
and store the hash in `StoredToken.token`.

### 4. verify_access_token() change

```python
def verify_access_token(self, token_str: str) -> StoredToken | None:
    token_hash = hash_token(token_str)
    stored = self._tokens.get(token_hash)
    if not stored:
        return None
    if stored.type != "access":
        return None
    if time.time() > stored.expires_at:
        return None
    return stored
```

`load_access_token()` gets the same treatment.

### 4. Sources migrated to register_static_token / register_hashed_token

| Source | Method | Notes |
|--------|--------|-------|
| `MCP_HEALTHCHECK_BEARER_TOKEN` | `register_static_token(raw, profile="full", name="healthcheck")` | All scopes, infinite expiry |
| `MCP_EXTRA_TOKENS_JSON` | `register_static_token(raw, profile=profile, name=f"extra_{profile}")` | Iterates dict items |
| `MCP_EXTRA_TOKENS_FILE` | `register_static_token(raw, profile=profile, name=f"extra_{profile}")` | Same as JSON |
| `MCP_PUBLIC_TOKEN` (token mode) | `register_static_token(raw, profile=MCP_PUBLIC_TOKEN_PROFILE or "operator", name="mcp_public")` | Default scopes, profile from env |
| `MCP_TOKEN_STORE_FILE` | `register_hashed_token(hash, ...)` | Hash already, no raw token |

### 5. Token store file format

```json
{
  "version": 1,
  "tokens": [
    {
      "id": "tok_20260626_private_chatgpt",
      "token_hash": "sha256:abc123...",
      "name": "private-chatgpt",
      "profile": "full",
      "scopes": [
        "mcp:read", "mcp:project", "mcp:handoff",
        "mcp:agent-run", "mcp:execute", "mcp:repo",
        "mcp:docker", "mcp:postgres", "mcp:docs", "mcp:admin"
      ],
      "created_at": "2026-06-26T12:00:00Z",
      "expires_at": null,
      "revoked_at": null,
      "last_used_at": null
    }
  ]
}
```

- `version` field for future migration
- `token_hash` never contains raw token
- `revoked_at` non-null means the token is revoked (soft delete)
- `last_used_at` updated by CLI or optional provider callback

### 6. TokenStore class

```python
@dataclass
class StoredTokenEntry:
    id: str
    token_hash: str
    name: str
    profile: str
    scopes: list[str]
    created_at: str  # ISO 8601
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None

class TokenStore:
    def __init__(self, path: str): ...
    def load(self) -> list[StoredTokenEntry]: ...
    def save(self, entries: list[StoredTokenEntry]): ...
    def add(self, entry: StoredTokenEntry): ...
    def revoke(self, token_id: str) -> StoredTokenEntry | None: ...
    def find_by_hash(self, token_hash: str) -> StoredTokenEntry | None: ...
```

**Atomic write + flock:** `TokenStore.save()` writes via temp file + `os.replace()`, with `fcntl.flock()` on a `.lock` file alongside the store. This prevents corruption under concurrent CLI invocations. Provider only reads the store, never writes, so contention is minimal.

**Permissions:** `TokenStore` enforces `chmod 600` on the store file, `chmod 700` on the parent directory (created if missing). Refuses to load a world/group-writable store file with a warning.

Location: `examples/mcp_server/token_store.py`

### 7. CLI commands

| Command | Description | Example |
|---------|-------------|---------|
| `mcp-token create --profile full --name private-chatgpt` | Create token, print raw once | `tok_abc...` |
| `mcp-token list` | List all tokens (no hashes) | id, name, profile, created |
| `mcp-token revoke <id>` | Soft-delete (set revoked_at) | — |
| `mcp-token rotate <id>` | Revoke old, create new with same name/profile; new id, raw printed once | `tok_20260626_...` |

Location: `scripts/mcp_token_cli.py`

### 8. Server startup sequence

```
server.py startup:
1. Create GatewayOAuthProvider
2. register_static_token for MCP_HEALTHCHECK_BEARER_TOKEN (if set)
3. register_static_token for MCP_EXTRA_TOKENS_JSON (if set)
4. register_static_token for MCP_EXTRA_TOKENS_FILE (if set)
5. If MCP_TOKEN_STORE_FILE exists:
   a. TokenStore.load() → list of StoredTokenEntry
   b. register_hashed_token() for each non-revoked entry
6. MCP_PUBLIC_TOKEN (token mode): register_static_token(raw, profile=MCP_PUBLIC_TOKEN_PROFILE or "operator", name="mcp_public")
```

## Files to Modify

| File | Changes |
|------|---------|
| `examples/mcp_server/oauth_provider.py` | Add `hash_token()`, `register_static_token()`, `register_hashed_token()`; change `verify_access_token()`, `load_access_token()` to hash lookup |
| `examples/mcp_server/server.py` | Replace all `_tokens[raw] = ...` with `register_static_token()`; add store file loading |
| `examples/mcp_server/token_store.py` | **New file**: `TokenStore` class with load/save/add/remove |
| `scripts/mcp_token_cli.py` | **New file**: argparse CLI for `mcp-token` |
| `tests/test_oauth_provider.py` | Update tests for hash-lookup; add tests for `hash_token()`, `register_static_token()`, `register_hashed_token()` |
| `tests/test_token_store.py` | **New file**: tests for TokenStore I/O |
| `.env.example` (server) | Add `MCP_TOKEN_STORE_FILE`, `MCP_PUBLIC_TOKEN_PROFILE` |

## Backward Compatibility

- All existing `_tokens[raw] = ...` in `server.py` are internal code, not public API. Breaking them is acceptable.
- Test `test_access_token_verification` uses `verify_access_token()` — must still pass after migration.
- OAuth authorization code flow (`exchange_code_for_token`) generates fresh tokens — those will also be stored via hash.
- Internal OAuth methods (`exchange_code_for_token`, `refresh_access_token`, `revoke_client_token`, `revoke_token`) use hash keys internally.

## Open Questions

1. **`last_used_at`** — Provider never writes to store file. `last_used_at` stays `null` (v1). Future: periodic usage flush or separate audit log.
2. **Lock contention** — `TokenStore.save()` uses temp file + `os.replace()` + `fcntl.flock()` on `.lock` file. Provider only reads, never writes. Contention is minimal in practice.
3. **Revoked token behavior in provider?** — `register_hashed_token()` skips revoked entries. Provider loads only non-revoked tokens.

## Implementation Plan

See separate implementation plan (writing-plans skill).
