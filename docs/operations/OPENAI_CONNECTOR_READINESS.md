# OpenAI Connector Readiness Audit

## Current state

- **Transport**: stdio only (`mcp.run()` defaults to stdio). No HTTP/SSE transport wired.
- **Auth**: `GatewayOAuthProvider` exists with PKCE S256, DCR, 10 scopes. Token auth mode works for stdio. OAuth is not wired for HTTP transport.
- **Safe mode**: 84 safe tools, 30 blocked. Confirmed via MCP stdio protocol.
- **Health**: `health` tool returns 507 chars (version, build, toolset hash) over stdio.
- **Manifest**: `tools_manifest` returns 84-tool list over stdio.
- **Env template**: `chatgpt.safe.env.example` with GATEWAY_URL, GATEWAY_API_KEY, safe mode flags.
- **Operator checklist**: 10-step checklist in CHATGPT_ATTACH_CHECKLIST.md.
- **Handoff package**: CHATGPT_CONNECTOR_HANDOFF.md with env/token checklists, manifest JSON.

## What is already ready

- MCP protocol layer: initialize, list_tools, call_tool all work via stdio
- Tool mode system: chatgpt mode with safe_mode filter
- Command policy: readonly/testlint allowlist for SSH execution
- Access-control: pending/allow/deny flow with Telegram operator notifications
- Audit: structured AuditEvent logging, redaction
- OAuth provider: PKCE S256, DCR, token store (in-memory)
- `mcp` package 1.28.0 installed with SSE + StreamableHTTP transport classes available

## What is missing for ChatGPT/OpenAI connector

### Transport
- **HTTP/SSE or StreamableHTTP transport** — ChatGPT/OpenAI connectors require HTTP endpoint, not stdio. FastMCP's `mcp.run(transport="sse")` or manual `SseServerTransport`/`StreamableHTTPServerTransport` wiring needed.
- Server currently calls `mcp.run()` with no transport argument (defaults to stdio).

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
- Add SSE/StreamableHTTP transport to server
- Deploy behind internal reverse proxy (e.g. `10.10.10.x:8086/mcp`)
- Token auth for internal clients only
- No TLS termination needed on private network
- **Recommended as next step** — enables real connector testing without public exposure

### Option C: Public OpenAI connector/app readiness
- Option B + public URL + TLS + OAuth app registration
- Requires: Nginx/Cloudflare reverse proxy, persistent token store, OAuth DCR for ChatGPT
- Higher risk, more complex, deferred until Option B validated

**Recommendation: Option B** — enables real MCP HTTP attach testing with minimal risk. Option C deferred.

## Explicit non-goals

- Public ChatGPT connector is NOT live
- Master key is NOT used as MCP runtime credential
- No secrets, topology, or real domains/IPs in this document
- No auth changes implemented in this phase
- No Docker/deploy changes in this phase
