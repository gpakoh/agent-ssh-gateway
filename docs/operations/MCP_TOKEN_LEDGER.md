# MCP Token Ledger

Token management lifecycle, secret policy, known behavior, and session history.

## Token Lifecycle

```text
mcp-token create --profile <name> --name <label>
  │
  ├─ raw token printed once to stdout
  ├─ hash (sha256:) stored in TokenStore
  ├─ store file: chmod 600, no raw token
  └─ provider loaded on service restart
      │
      ├─ Bearer auth works
      │   initialize → tools/call → tools/list
      │
      ├─ mcp-token list → shows hash + metadata
      │
      └─ mcp-token revoke <id>
          │
          ├─ store entry: revoked_at set
          ├─ service restart required
          └─ same Bearer token → 401 invalid_token
```

### Commands

```bash
# Create
mcp-token create "my-token" --profile full

# List
mcp-token list
mcp-token list --output json

# Revoke
mcp-token revoke <token_id>
sudo systemctl restart agent-ssh-gateway-mcp.service

# Rotate (new token ID, old revoked)
mcp-token rotate <token_id> --name "rotated-token"
```

## Secret Policy

1. **Raw token printed exactly once** — at `mcp-token create` stdout
2. **Never write to**: git, AI bridge, logs, shell history, README, CHANGELOG, `.env`
3. **TokenStore** (`/var/lib/agent-ssh-gateway/mcp_tokens.json`):
   - `chmod 600`
   - Contains only `sha256:` hash — no raw token
   - `id` field starts with `mcp_tok_` prefix (reference ID, not a bearer token)
4. **In-memory**: provider stores hash only, never raw token
5. **Environment variables** (`/etc/agent-ssh-gateway-mcp.env`):
   - `MCP_PUBLIC_TOKEN` — legacy, pre-dates TokenStore
   - `MCP_TOKEN_STORE_FILE` — path to TokenStore (default: `/var/lib/agent-ssh-gateway/mcp_tokens.json`)

## Token Profiles

| Profile | Scopes | Use case |
|---------|--------|----------|
| `full` | All scopes (mcp:read, mcp:project, mcp:handoff, mcp:agent-run, mcp:execute, mcp:repo, mcp:docker, mcp:postgres, mcp:docs, mcp:admin) | ChatGPT remote, operator |
| `infra` | gateway_health, docker_ps | Infrastructure monitoring |
| `operator` | gateway_health | Basic status checks |
| `viewer` | gateway_health | Read-only status |
| `agent-runner` | gateway_health | Runner health |

Profiles are defined in `examples/mcp_server/tool_scopes.py` (`ACCESS_PROFILES`).

## Known Behavior

### tools/list via direct curl in Streamable HTTP

Direct curl to the MCP endpoint may return an empty body for `tools/list` even when:
- `initialize` succeeds (session ID returned)
- individual `tools/call` requests work (HTTP 200)
- fleet healthcheck correctly reads 85/85 tools via the adapter

**Root cause**: Streamable HTTP session lifecycle quirk — session may expire or not
propagate to the tools/list handler in some request patterns.

**Canonical verification path**:
```bash
python scripts/mcp_fleet_healthcheck.py --verbose
```
This is the only reliable tools/list verification. The adapter path handles
session lifecycle correctly.

If you need to debug tools/list, use the adapter healthcheck code in
`scripts/mcp_fleet_healthcheck.py` rather than raw `curl`.

## Bugfix Log

### a076169 — load_tokens client_id=None (2026-06-27)

**Symptoms**: Every Bearer request returned 500 Internal Server Error after
TokenStore was introduced (`v0.1.20-alpha`). The existing `MCP_PUBLIC_TOKEN`
in env worked because it was registered through a separate code path.

**Root cause**: `GatewayOAuthProvider.load_tokens()` explicitly passed
`client_id=None` to `register_hashed_token()`, overriding the method's
default `client_id="mcp_static"`. The `AccessToken` Pydantic model requires
a string `client_id`, so `None` caused a validation error at runtime.

**Fix**: Removed the `client_id=None` override so `register_hashed_token`
uses its default `"mcp_static"`. Updated
`test_revoke_client_token_syncs_to_store` to match.

**Files changed**:
- `examples/mcp_server/oauth_provider.py` — removed `client_id=None`
- `tests/test_server_token_integration.py` — updated test to use `"mcp_static"`

**Tests**: 743/743 passed after fix.

## Session History

| Session | Date | Scope | Result | Key artifact |
|---------|------|-------|--------|-------------|
| 126 | 2026-06-27 | MCP token CLI production rollout smoke | PASSED | This ledger |
