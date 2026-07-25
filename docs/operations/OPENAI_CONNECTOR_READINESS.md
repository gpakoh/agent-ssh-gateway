# OpenAI Connector Readiness Audit

## Current state

- **Transport**: stdio remains the default/stable path (`examples/mcp_server/server.py`'s `mcp.run()` still defaults to stdio, unchanged). A **private-network SSE entrypoint** (`scripts/mcp_sse_serve.py`) now exists for local/private rehearsal — bind defaults to `127.0.0.1:8086`, routes are `/sse` and `/messages`, and a dedicated `BearerAuthMiddleware` (env `MCP_HTTP_BEARER_TOKEN`) gates both, independent of `MCP_AUTH_MODE`. This is **not** a public connector — no TLS, reverse proxy, or OAuth is wired for it, and it is not deployed as a persistent service.
- **Auth**: `GatewayOAuthProvider` exists with PKCE S256, DCR, 10 scopes. Token auth mode works for stdio. The private SSE entrypoint uses its own independent bearer-token layer instead (see below) — OAuth is still not wired for any HTTP transport.
- **Safe mode**: 84 safe tools, 30 blocked. Confirmed via MCP stdio protocol *and* via the private SSE entrypoint (`scripts/mcp_sse_safe_smoke.py`, real subprocess, 11/11 checks).
- **Health**: `health` tool returns 507 chars (version, build, toolset hash) over stdio.
- **Manifest**: `tools_manifest` returns 84-tool list over stdio and over the private SSE entrypoint.
- **Env template**: `chatgpt.safe.env.example` (stdio) and `chatgpt.sse.env.example` (private SSE) with GATEWAY_URL, GATEWAY_API_KEY, safe mode flags, and (SSE only) `MCP_HTTP_HOST`/`MCP_HTTP_PORT`/`MCP_HTTP_BEARER_TOKEN`.
- **Operator checklist**: 10-step checklist in CHATGPT_ATTACH_CHECKLIST.md (stdio); private SSE steps in CHATGPT_TOOL_ATTACH.md.
- **Handoff package**: CHATGPT_CONNECTOR_HANDOFF.md with env/token checklists, manifest JSON.

## What is already ready

- MCP protocol layer: initialize, list_tools, call_tool all work via stdio **and** via the private SSE entrypoint
- Private-network SSE transport: `scripts/mcp_sse_serve.py` (entrypoint) + `scripts/mcp_sse_safe_smoke.py` (smoke) — bind `127.0.0.1` only by default, independent bearer-token auth, verified end-to-end against a real subprocess
- Tool mode system: chatgpt mode with safe_mode filter
- Command policy: readonly/testlint allowlist for SSH execution
- Access-control: pending/allow/deny flow with Telegram operator notifications
- Audit: structured AuditEvent logging, redaction
- OAuth provider: PKCE S256, DCR, token store (in-memory)
- `mcp` package 1.28.0 installed with SSE + StreamableHTTP transport classes available

## What is missing for ChatGPT/OpenAI connector

### Transport
- **Public-facing HTTP/SSE or StreamableHTTP transport** — ChatGPT/OpenAI connectors require a public HTTP endpoint. The private SSE entrypoint added in this phase is local/private-network only (default bind `127.0.0.1`, no TLS, no reverse proxy) and does not satisfy this on its own.
- `examples/mcp_server/server.py`'s stdio path is unchanged; the private SSE entrypoint is a separate script that imports and reuses the same tool set.

### Public URL / Reverse Proxy
- ChatGPT connector needs a public HTTPS endpoint (e.g. `https://<your-domain>/mcp`).
- Currently no reverse proxy or tunnel exposes the MCP server to the internet.
- Gitea uses Nginx reverse proxy — pattern exists but MCP not configured.

### TLS
- Required for public endpoint. Existing domain has TLS via Cloudflare or Nginx.
- MCP server itself does not terminate TLS — must be behind a reverse proxy.

### OAuth / App Registration
- `GatewayOAuthProvider` supports PKCE S256 + DCR. ChatGPT/OpenAI connectors may use OAuth for authentication.
- In-memory token store — tokens lost on restart. Need persistent token store for production.
- OAuth settings partially configured (issuer_url, resource_server_url) but not wired to HTTP transport.

### Schema Stability
- Tool schemas are generated from Python function signatures via FastMCP. Stable as long as function signatures don't change.
- No versioned tool manifest endpoint for clients to validate compatibility.

### Rate Limits
- No rate limiting on MCP tool calls. Gateway has basic auth but no per-tool throttle.

### Audit / Correlation
- Audit logging works (AuditEvent + JSONL). Correlation IDs (X-Request-ID) in gateway.
- MCP stdio session has no request_id propagation from transport layer.

### Operator Approval Integration
- Access-control flow works: new actor → pending → operator Allow/Deny via Telegram.
- For remote MCP, the actor fingerprint comes from OAuth token or IP, not local process.

## Recommended next phase

### Option A: Local Codex/MCP client attach only
- Transport: stdio (already done)
- Auth: token mode (already done)
- No network exposure needed
- **Status: COMPLETE**

### Option B: HTTP MCP transport behind private network
- SSE transport entrypoint: `scripts/mcp_sse_serve.py`
- Bind default `127.0.0.1:8086` (env `MCP_HTTP_HOST`/`MCP_HTTP_PORT`); non-loopback bind requires explicit `MCP_HTTP_ALLOW_NON_LOOPBACK=true` and is not exercised in this phase
- Routes: `/sse`, `/messages`
- Independent bearer-token auth (`MCP_HTTP_BEARER_TOKEN`, `BearerAuthMiddleware`) — not reliant on `MCP_AUTH_MODE`
- No TLS termination, no reverse proxy, no OAuth wired for this entrypoint
- Not deployed as a persistent/systemd service — run manually for local/private rehearsal
- **Status: IMPLEMENTED (private/local rehearsal only)** — `scripts/mcp_sse_serve.py` + `scripts/mcp_sse_safe_smoke.py`, verified via a real subprocess smoke run (11/11 checks: route/auth rejection and acceptance, MCP initialize/list_tools/tools_manifest, 84 safe/30 blocked)

### Option C: Public OpenAI connector/app readiness
- Option B + public URL + TLS + OAuth app registration
- Requires: Nginx/Cloudflare reverse proxy, persistent token store, OAuth DCR for ChatGPT
- Higher risk, more complex, deferred until Option B is exercised beyond local rehearsal

**Recommendation:** Option B is implemented for private/local rehearsal. Option C remains deferred — no public URL, TLS, or OAuth work has started.

## Explicit non-goals

- Public ChatGPT connector is NOT live
- The private SSE entrypoint (Option B) is not a public connector — private/local rehearsal only, no TLS/reverse proxy/OAuth
- Master key is NOT used as MCP runtime credential
- No secrets, topology, or real domains/IPs in this document
- No auth changes implemented in this phase beyond the private SSE entrypoint's own independent bearer-token layer
- No Docker/deploy changes in this phase
