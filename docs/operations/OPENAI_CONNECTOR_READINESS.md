# OpenAI Connector Readiness Audit

## Current state

- **Transport**: stdio remains the default/stable path (`examples/mcp_server/server.py`'s `mcp.run()` still defaults to stdio, unchanged). Two **private-network rehearsal entrypoints** now exist for local/private use — neither is a public connector, no TLS/reverse proxy/OAuth wired for either, neither is deployed as a persistent service:
  - `scripts/mcp_sse_serve.py` — the deprecated HTTP+SSE transport (protocol version 2024-11-05). Bind defaults to `127.0.0.1:8086`, routes are `/sse` and `/messages`, gated by `BearerAuthMiddleware` (env `MCP_HTTP_BEARER_TOKEN`) and `OriginValidationMiddleware` (env `MCP_HTTP_ALLOWED_ORIGINS`), independent of `MCP_AUTH_MODE`.
  - `scripts/mcp_streamable_http_serve.py` — the MCP spec's **current** transport, Streamable HTTP (protocol version 2025-06-18), superseding SSE at the spec level. SSE remains fully supported in this repo; this is additive, not a migration. Bind defaults to `127.0.0.1:8087` (distinct port, both entrypoints can run at once), single route `/mcp` handling GET/POST/DELETE, gated by the same `BearerAuthMiddleware`/`OriginValidationMiddleware` classes reused unchanged (env `MCP_STREAMABLE_HTTP_BEARER_TOKEN` / `MCP_STREAMABLE_HTTP_ALLOWED_ORIGINS`).
- **Auth**: `GatewayOAuthProvider` exists with PKCE S256, DCR, 10 scopes. Token auth mode works for stdio. Both private HTTP entrypoints use their own independent bearer-token layer instead (see below) — OAuth is still not wired for any HTTP transport.
- **Safe mode**: 84 safe tools, 30 blocked. Confirmed via MCP stdio protocol, via the private SSE entrypoint (`scripts/mcp_sse_safe_smoke.py`, real subprocess, 11/11 checks), and via the private Streamable HTTP entrypoint (`scripts/mcp_streamable_http_safe_smoke.py`, real subprocess, 11/11 checks).
- **Health**: `health` tool returns 507 chars (version, build, toolset hash) over stdio.
- **Manifest**: `tools_manifest` returns 84-tool list over stdio and over both private HTTP entrypoints.
- **Env template**: `mcp_client.safe.env.example` (stdio), `mcp_client.sse.env.example` (private SSE), and `mcp_client.streamable-http.env.example` (private Streamable HTTP) with GATEWAY_URL, GATEWAY_API_KEY, safe mode flags, and (per-transport) their own `MCP_*_HOST`/`MCP_*_PORT`/`MCP_*_BEARER_TOKEN`/`MCP_*_ALLOWED_ORIGINS`.
- **Operator checklist**: 10-step checklist in CHATGPT_ATTACH_CHECKLIST.md (stdio); private SSE and Streamable HTTP steps in CHATGPT_TOOL_ATTACH.md.
- **Handoff package**: CHATGPT_CONNECTOR_HANDOFF.md with env/token checklists, manifest JSON.

## What is already ready

- MCP protocol layer: initialize, list_tools, call_tool all work via stdio, via the private SSE entrypoint, **and** via the private Streamable HTTP entrypoint
- Private-network SSE transport: `scripts/mcp_sse_serve.py` (entrypoint) + `scripts/mcp_sse_safe_smoke.py` (smoke) — bind `127.0.0.1` only by default, independent bearer-token + Origin-validation auth, verified end-to-end against a real subprocess
- Private-network Streamable HTTP transport: `scripts/mcp_streamable_http_serve.py` (entrypoint) + `scripts/mcp_streamable_http_safe_smoke.py` (smoke) — bind `127.0.0.1` only by default, same bearer-token + Origin-validation middleware reused from the SSE entrypoint, route `/mcp` empirically discovered (not assumed from spec prose), verified end-to-end against a real subprocess
- Tool mode system: mcp_client mode with safe_mode filter
- Command policy: readonly/testlint allowlist for SSH execution
- Access-control: pending/allow/deny flow with Telegram operator notifications
- Audit: structured AuditEvent logging, redaction
- OAuth provider: PKCE S256, DCR, token store (in-memory)
- `mcp` package 1.28.0 installed with SSE + StreamableHTTP transport classes available and now exercised by both private entrypoints

## What is missing for ChatGPT/OpenAI connector

### Transport
- **Public-facing HTTP/SSE or StreamableHTTP transport** — ChatGPT/OpenAI connectors require a public HTTP endpoint. Both private entrypoints (SSE and Streamable HTTP) are local/private-network only (default bind `127.0.0.1`, no TLS, no reverse proxy) and neither satisfies this on its own.
- `examples/mcp_server/server.py`'s stdio path is unchanged; both private entrypoints are separate scripts that import and reuse the same tool set.

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

**B1 — SSE** (deprecated at the MCP spec level, still supported here):
- SSE transport entrypoint: `scripts/mcp_sse_serve.py`
- Bind default `127.0.0.1:8086` (env `MCP_HTTP_HOST`/`MCP_HTTP_PORT`); non-loopback bind requires explicit `MCP_HTTP_ALLOW_NON_LOOPBACK=true` and is not exercised in this phase
- Routes: `/sse`, `/messages`
- Independent bearer-token auth (`MCP_HTTP_BEARER_TOKEN`, `BearerAuthMiddleware`) + Origin validation (`MCP_HTTP_ALLOWED_ORIGINS`, `OriginValidationMiddleware`) — not reliant on `MCP_AUTH_MODE`
- No TLS termination, no reverse proxy, no OAuth wired for this entrypoint
- Not deployed as a persistent/systemd service — run manually for local/private rehearsal
- **Status: IMPLEMENTED (private/local rehearsal only)** — `scripts/mcp_sse_serve.py` + `scripts/mcp_sse_safe_smoke.py`, verified via a real subprocess smoke run (11/11 checks: route/auth rejection and acceptance, MCP initialize/list_tools/tools_manifest, 84 safe/30 blocked)

**B2 — Streamable HTTP** (the MCP spec's current transport, additive alongside B1, not a replacement):
- Streamable HTTP transport entrypoint: `scripts/mcp_streamable_http_serve.py`
- Bind default `127.0.0.1:8087` (env `MCP_STREAMABLE_HTTP_HOST`/`MCP_STREAMABLE_HTTP_PORT` — distinct port from B1, both can run at once); non-loopback bind requires explicit `MCP_STREAMABLE_HTTP_ALLOW_NON_LOOPBACK=true` and is not exercised in this phase
- Route: `/mcp` (single endpoint, GET/POST/DELETE — empirically discovered, not assumed from the spec's prose)
- Independent bearer-token auth (`MCP_STREAMABLE_HTTP_BEARER_TOKEN`) + Origin validation (`MCP_STREAMABLE_HTTP_ALLOWED_ORIGINS`) — the same `BearerAuthMiddleware`/`OriginValidationMiddleware` classes reused from B1, not reimplemented
- No TLS termination, no reverse proxy, no OAuth wired for this entrypoint
- Not deployed as a persistent/systemd service — run manually for local/private rehearsal
- **Status: IMPLEMENTED (private/local rehearsal only)** — `scripts/mcp_streamable_http_serve.py` + `scripts/mcp_streamable_http_safe_smoke.py`, verified via a real subprocess smoke run (11/11 checks: route/auth/Origin rejection and acceptance, MCP initialize/list_tools/tools_manifest, 84 safe/30 blocked, `Mcp-Session-Id` presence reported honestly)

### Option C: Public OpenAI connector/app readiness
- Option B (either or both transports) + public URL + TLS + OAuth app registration
- Requires: Nginx/Cloudflare reverse proxy, persistent token store, OAuth DCR for ChatGPT
- Higher risk, more complex, deferred until Option B is exercised beyond local rehearsal

**Recommendation:** Option B is implemented for private/local rehearsal, both as SSE (B1) and Streamable HTTP (B2). Option C remains deferred — no public URL, TLS, or OAuth work has started.

## Explicit non-goals

- Public ChatGPT connector is NOT live
- Neither private entrypoint (Option B1 SSE, B2 Streamable HTTP) is a public connector — both are private/local rehearsal only, no TLS/reverse proxy/OAuth
- Master key is NOT used as MCP runtime credential
- No secrets, topology, or real domains/IPs in this document
- No auth changes implemented beyond the private entrypoints' own independent bearer-token + Origin-validation layer
- No Docker/deploy changes, no compose/systemd/autostart wiring, for either private entrypoint
