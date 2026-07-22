# Remote MCP Fleet — ChatGPT Remote Access Design

## Goal

Expose opencode's local MCP servers (browsermcp, context7, docker, github, postgres) to ChatGPT as separate remote MCP endpoints behind `ssh-gateway.example.com`, each with its own token, auth middleware, and nginx path.

## Non-Goals

- MCP Aggregator (one endpoint routing to many backends) — deferred until individual endpoints are stable.
- Write/modify access for docker and postgres — read-only MVP only.
- Exposing production database — current postgres target is `example_vectordb`.
- Browser MCP with arbitrary navigation — requires explicit user confirmation per action.

## Architecture

Each MCP server follows the same pattern as the existing `agent-ssh-gateway-mcp`:

```
ChatGPT App → nginx (ssh-gateway.example.com) → Auth middleware (token in ?mcp_token=)
  → HTTP MCP adapter → local stdio MCP server
```

### nginx Layout

```
/mcp                      → agent-ssh-gateway (existing, port 8788)
/mcp/context7             → context7 MCP adapter
/mcp/github               → GitHub MCP adapter
/mcp/docker               → Docker MCP adapter (read-only)
/mcp/postgres             → PostgreSQL MCP adapter (read-only)
/mcp/browser              → BrowserMCP adapter
```

All paths reside under `/mcp/` to inherit existing nginx location blocks (SSO bypass, WebSocket support, proxy buffering off).

**proxy_pass path rule:** Each fleet adapter listens on its internal port with path `/mcp` (e.g. `http://127.0.0.1:8790/mcp`). The nginx `location /mcp/context7` must proxy to `http://gateway.example.com:8790/mcp` (not `/mcp/context7`), otherwise the path mismatch doubles. Example:

```nginx
location /mcp/context7 {
    proxy_pass http://gateway.example.com:8790/mcp;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

### Token Scheme

Each MCP server gets its own token, configured independently:

```
context7_mcp_token=...
github_mcp_token=...
docker_mcp_token=...
postgres_mcp_token=...
browser_mcp_token=...
```

Tokens are generated via `secrets.token_urlsafe(64)` and stored in an env file per service.

### Port Allocation (localhost)

| Service | Internal Port | Purpose |
|---------|--------------|---------|
| agent-ssh-gateway | 8789 (FastMCP internal) / 8788 (public) | Existing |
| context7 | 8790 | API docs |
| github | 8791 | GitHub API |
| docker | 8792 | Docker inspect-only |
| postgres | 8793 | DB queries (read-only) |
| browser | 8794 | Browser automation |

## Integration Order

Per user priorities:

### Phase 1 — Prime Trio (stable foundation)

1. **Context7** (`/mcp/context7`, port 8790) — safest: no server access, no credentials, no write.
2. **GitHub** (`/mcp/github`, port 8791) — useful for PR/issue workflow; token already exists.
3. **Docker read-only** (`/mcp/docker`, port 8792) — status/inspect/logs only; no exec/stop/rm/build.

### Phase 2

4. **Postgres read-only** (`/mcp/postgres`, port 8793) — SELECT-only DB user, query timeout, row limit.

### Phase 3 — BrowserMCP (requires confirmation workflow)

5. **BrowserMCP** (`/mcp/browser`, port 8794) — web inspection/screenshots. Requires verified confirmation model before enabling. Disabled by default until explicit per-action confirmation workflow is implemented and tested.

### Phase 4 — Aggregator (if needed)

6. MCP Aggregator design — only if maintaining 5+ individual ChatGPT apps becomes unwieldy.

## Per-Service Adapter Architecture

All target MCP servers use **stdio transport** — they communicate over stdin/stdout. ChatGPT requires an **HTTP endpoint** (Streamable HTTP). Each adapter bridges this gap:

```
ChatGPT → HTTP (Streamable HTTP) → stdio subprocess → MCP server
```

The adapter is a Python script that:
1. Spawns the target MCP server as a subprocess (`npx -y <package>`)
2. Connects via `mcp.client.stdio.StdioClientSession`
3. Exposes a FastMCP server over Streamable HTTP on an internal port
4. Token auth middleware wraps the public endpoint

This is the same proven pattern as `chatgpt_remote_mcp/server.py` but with `mcp.client.stdio` instead of the gateway HTTP client.

**Decision:** All adapters live in `examples/chatgpt_remote_mcp/fleet/` as standalone files. Shared code (TokenAuthMiddleware, env helpers) reused from the parent directory.

## Risk Mitigation

### Docker (read-only only)
- Do not expose raw third-party Docker MCP tools directly. The adapter must register only safe wrapper tools.
- Allowed: `ps`, `stats`, `inspect` (container/image), `logs tail`, `images list`, `compose ps`
- Denied: `exec`, `stop`, `restart`, `rm`, `prune`, `pull`, `build`, `compose down`
- Enforced by registering only wrapper tools in the adapter, not by trusting the upstream MCP server's config.

### Postgres (read-only only)
- New DB user with `SELECT` only on `example_vectordb`
- `default_transaction_read_only = on`
- Query timeout 30s, max rows 1000
- No `gateway_postgres_write` tool registered

### BrowserMCP
- Requires confirmation per navigation (if adapter supports it)
- No file download or form auto-submit
- Clear session after each ChatGPT conversation

### GitHub
- **MVP must be read-only**: repo metadata, issues list, PR list, commits, file contents. No merge, no push, no release, no write comments until explicitly reviewed.
- Current `GITHUB_TOKEN` is a PAT with unknown scope. Audit before exposing; prefer a fine-grained token with minimal scopes (repo metadata read-only).

## Required Changes

### nginx (VPS)
- Add `/mcp/context7`, `/mcp/github`, `/mcp/docker`, `/mcp/postgres`, `/mcp/browser` location blocks above Authelia auth
- Each: `proxy_pass http://gateway.example.com<port>/mcp` (not `/mcp/<name>` — see path rule above)
- `proxy_buffering off`, `proxy_read_timeout 3600s`

### iptables (VPS)
- ACCEPT rules for each new port (8790-8794) from `<ip-address>`

### Systemd
- One service per adapter: `agent-mcp-context7.service`, `agent-mcp-github.service`, etc.
- Each with its own env file (`<mcp-env-file>`, 600 permissions)
- Env file contains only: `MCP_PUBLIC_TOKEN`, `MCP_HOST`, `MCP_PORT`, and service-specific credentials (e.g. `GITHUB_TOKEN`, `POSTGRES_URL`, `DOCKER_HOST`). No gateway keys, no shared secrets.

## Testing

### Per-Service
- `tools/list` returns expected tools for that MCP server
- Read-only tools work and return data
- Write tools (if any) are not registered or return policy error
- Wrong/missing token rejects with 401/403, never returns 302 redirect to auth.example.com

### Integration
- Each ChatGPT App can independently connect and use its MCP server
- No cross-contamination between services (different tokens, different ports)
- Gateway remains unaffected

## Files

```
<repo-root>/examples/chatgpt_remote_mcp/
  server.py                    existing gateway adapter (unchanged)
  fleet/
    __init__.py
    context7_server.py         Context7 MCP adapter
    github_server.py           GitHub MCP adapter
    docker_server.py           Docker MCP adapter (read-only)
    postgres_server.py         Postgres MCP adapter (read-only)
    browser_server.py          BrowserMCP adapter
    shared.py                  TokenAuthMiddleware, mcp_token helper
```

## Implementation Guardrails

- Do not expose raw third-party MCP tools directly for Docker, Postgres, or Browser. Register safe wrapper tools only.
- Every public endpoint must:
  - require its own `MCP_PUBLIC_TOKEN`
  - bypass SSO only for its exact path
  - return 401/403 on bad token, never 302 redirect
  - have `access_log off` or sanitized query logging (token in URL)
- Service env files must not contain unrelated gateway/master keys or shared secrets.
- ChatGPT Apps must be created one per MCP service, each with its own URL and token.
- Before any write-capable tool is added to a ChatGPT profile, explicit security review is required. Write tools are opt-in, never default.

## Out of Scope (Phase 4)

- MCP Aggregator design and implementation
- Single sign-on across MCP servers
- Usage metrics and rate limiting per MCP server
- Automatic token rotation
