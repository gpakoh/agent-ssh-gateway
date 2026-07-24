# Phase 16 — Private HTTP MCP Transport Design

## Current state

- **Transport**: stdio only. `mcp.run()` defaults to stdio.
- **HTTP/SSE available but not wired**: `FastMCP.sse_app() -> Starlette` and `FastMCP.run_sse_async()` exist in `mcp` package 1.28.0. `StreamableHTTPServerTransport` also available.
- **Starlette 1.3.1 + uvicorn 0.50.0** installed — both needed for HTTP transport.
- **OAuth provider**: `GatewayOAuthProvider` with PKCE S256, DCR, token store already implemented.
- **Safe mode**: 84 safe tools, 30 blocked, confirmed via stdio protocol.

## Recommended transport: SSE via Starlette (private network)

**Why SSE over StreamableHTTP:**
- `FastMCP.run_sse_async()` is a complete ready-to-use entry point — no manual server wiring needed.
- `sse_app() -> Starlette` returns a Starlette app that can be mounted in existing ASGI server.
- SSE is the most widely supported MCP transport in ChatGPT/Claude/Cursor clients.
- StreamableHTTP is newer but has less client ecosystem support.

**Why not StreamableHTTP:**
- Available via `FastMCP.run_streamable_http_async()` and `streamable_http_app()`.
- Can be added later as a second transport option on the same port.
- Not needed for initial private-network testing.

## Bind defaults

| Setting | Default | Override |
|---------|---------|----------|
| Host | `127.0.0.1` | `MCP_HTTP_HOST` env var |
| Port | `8086` | `MCP_HTTP_PORT` env var |
| Transport | SSE at `/mcp/sse` | Fixed |
| Mount path | `/mcp/sse` | Configurable via `mount_path` |

**Red line**: Server MUST NOT bind to `0.0.0.0` by default. Public bind requires explicit `MCP_HTTP_BIND_PUBLIC=true` AND operator acknowledgment.

## Auth

- **Agent token to gateway**: same as stdio path — `GATEWAY_API_KEY` environment variable.
- **MCP-level auth**: token mode (`MCP_AUTH_MODE=token`, `MCP_PUBLIC_TOKEN=<agent-token>`).
- **No master key as runtime credential**: enforced by preflight and manifest tests.
- **OAuth for future public endpoint**: `GatewayOAuthProvider` ready but not wired for HTTP transport in this phase.

## Safe mode mandatory

```
MCP_GATEWAY_TOOL_MODE=chatgpt
MCP_CHATGPT_SAFE_MODE=true
MCP_ACCESS_PROFILE=chatgpt_safe
```

Preflight script validates these. HTTP transport startup must fail-fast if safe mode is not configured.

## Network exposure red lines

1. **No public bind by default** — `127.0.0.1` only.
2. **No public URL in repository** — placeholders only (`http://<gateway>:8086/mcp/sse`).
3. **Reverse proxy / TLS explicitly deferred** — until private HTTP endpoint is validated.
4. **Docker macvlan disabled for MCP** — container stays on Docker internal network only.
5. **Cloudflare/tunnel not configured** for this phase.

## Architecture

```
ChatGPT/OpenAI connector (future)
  ↓ HTTP SSE
MCP HTTP Server (uvicorn, private network)
  ↓ MCP protocol
FastMCP tools (chatgpt safe mode, 84 tools)
  ↓ HTTP API
Gateway API (agent-ssh-gateway, port 8085)
  ↓ SSH
Target hosts
```

## Implementation plan

### Entry point

New script: `scripts/mcp_http_serve.py`

```python
# Pseudocode — not implemented yet
import uvicorn
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agent-ssh-gateway")
# ... register tools (same as server.py) ...

app = mcp.sse_app(mount_path="/mcp/sse")

if __name__ == "__main__":
    host = os.environ.get("MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_HTTP_PORT", "8086"))
    uvicorn.run(app, host=host, port=port)
```

### Startup validation

1. Verify safe mode env vars are set → fail-fast if missing.
2. Verify `GATEWAY_API_KEY` is set → fail-fast if missing.
3. Log bind address and port (redacted if public bind attempted).
4. Start uvicorn with Starlette SSE app.

### Health / readiness

- SSE endpoint itself is the health check — client connects to `/mcp/sse` and receives `endpoint` event.
- Optional: `/health` HTTP endpoint on same server for infrastructure probes.
- Returns gateway version, transport mode, tool count.

## Docker / compose

### Private overlay

New Docker Compose profile: `mcp-http` (not in default profile).

```yaml
mcp-http:
  build: .
  command: python3 scripts/mcp_http_serve.py
  environment:
    - MCP_HTTP_HOST=0.0.0.0  # only inside container
    - MCP_HTTP_PORT=8086
    - MCP_GATEWAY_TOOL_MODE=chatgpt
    - MCP_CHATGPT_SAFE_MODE=true
    - MCP_ACCESS_PROFILE=chatgpt_safe
    - MCP_AUTH_MODE=token
  ports:
    - "127.0.0.1:8086:8086"  # host-only bind, not exposed to network
  networks:
    - internal_net
```

**Key constraints:**
- Port mapping `127.0.0.1:8086:8086` — host-only, not `0.0.0.0:8086:8086`.
- Container uses `0.0.0.0` inside (container network is isolated).
- `internal_net` only — no `external` network exposure.

## Smoke plan

### HTTP smoke script: `scripts/mcp_http_safe_smoke.py`

1. Start MCP HTTP server as subprocess (or use existing Docker container).
2. Connect via SSE client (`mcp.client.sse.sse_client`).
3. Perform MCP initialize → list_tools → call_tool(health) → call_tool(tools_manifest).
4. Verify: 84 safe tools, 0 blocked, health OK.
5. Exit nonzero on unsafe manifest.

### Integration with existing smoke

- `chatgpt_tool_attach_smoke.py` — no change (tests stdio manifest).
- `mcp_stdio_safe_smoke.py` — no change (tests stdio protocol).
- New: `mcp_http_safe_smoke.py` — tests HTTP SSE protocol.

## Rollback plan

1. Stop MCP HTTP server process (or disable `mcp-http` Docker profile).
2. No gateway restart needed — HTTP transport is independent process.
3. No tag/deploy rollback needed — HTTP transport is additive.

## Threat model

| Threat | Mitigation |
|--------|-----------|
| Accidental public bind | Default `127.0.0.1`, `MCP_HTTP_BIND_PUBLIC=true` requires explicit flag, Docker `127.0.0.1:port` mapping |
| Unsafe mode enabled | Preflight validates `MCP_CHATGPT_SAFE_MODE=true`, fail-fast on startup |
| Token leakage in logs | Redaction in audit logger, no token in HTTP response headers |
| Tool drift (new tools not in manifest) | Contract tests verify manifest counts match code, CI gate |
| Agent token as master key | Preflight + manifest tests reject master key usage |
| HTTP transport bypasses access-control | Access-control middleware applies to all transport paths |

## Explicit non-goals

- Public ChatGPT/OpenAI connector is NOT live
- TLS/HTTPS NOT configured in this phase
- OAuth for public clients NOT implemented yet
- Reverse proxy NOT configured in this phase
- Master key NOT used as MCP runtime credential
- No secrets, topology, or real IPs in this document
