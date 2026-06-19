# Context7 Remote MCP Adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose opencode's local Context7 MCP server (`@upstash/context7-mcp`) to ChatGPT as a remote MCP endpoint behind `ssh.xloud.ru/mcp/context7`.

**Architecture:** Python FastMCP adapter that spawns `@upstash/context7-mcp` via `npx`, bridges stdio to Streamable HTTP using `mcp.client.stdio.StdioClientSession`, with TokenAuthMiddleware. Context7 is safe for raw proxy (2 tools, documentation-only, no system access). Systemd service behind nginx reverse proxy.

**Tech Stack:** Python + FastMCP (`mcp` package `>=1.6.0`), `@upstash/context7-mcp` (v3.2.1), nginx, systemd.

## Context7 Discovery

From `~/.config/opencode/opencode.json`:
- Command: `["npx", "-y", "@upstash/context7-mcp"]`
- Env: `CONTEXT7_MCP_URL=https://mcp.context7.com/mcp`
- Tools: `resolve-library-id(query, libraryName)` and `query-docs(libraryId, query)`
- Safe for raw proxy — no server access, no credentials, no write.

## Global Constraints

- Token auth via `mcp_token` query parameter (never header — ChatGPT limitation).
- SSO bypass on nginx for `/mcp/context7` — no Authelia redirect.
- Wrong/missing token returns 401/403, never 302.
- `proxy_pass` to internal `/mcp` path (not `/mcp/context7`).
- Env file at `/etc/agent-mcp-context7.env`, 600 permissions, no gateway keys.
- `MCP_PUBLIC_TOKEN` generated via `secrets.token_urlsafe(64)`.
- Port 8790 internal.
- Context7 can use raw proxy style (no wrapper tools needed) because it has no system/DB/docker access.

---

### Task 1: shared.py — Fleet shared utilities

**Files:**
- Create: `examples/chatgpt_remote_mcp/fleet/__init__.py`
- Create: `examples/chatgpt_remote_mcp/fleet/shared.py`

**Interfaces:**
- Produces: `get_bearer_token(request) -> str | None` — extracts `mcp_token` from query params
- Produces: `create_token_middleware(valid_tokens: set[str]) -> callable` — ASGI middleware returning 401/403 on bad token
- Produces: `make_env_config() -> dict[str, str]` — reads `MCP_PUBLIC_TOKEN`, `MCP_HOST`, `MCP_PORT` from env

- [ ] **Step 1: Create `fleet/__init__.py`**

```python
```

- [ ] **Step 2: Write `fleet/shared.py`**

```python
"""Shared utilities for fleet MCP adapters."""

from __future__ import annotations

import os
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid mcp_token query parameter."""

    def __init__(self, app: Any, valid_tokens: set[str]) -> None:
        super().__init__(app)
        self._valid_tokens = valid_tokens

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        token = request.query_params.get("mcp_token")
        if not token:
            return JSONResponse(
                {"error": "missing mcp_token"}, status_code=401
            )
        if token not in self._valid_tokens:
            return JSONResponse(
                {"error": "invalid mcp_token"}, status_code=403
            )
        return await call_next(request)


def get_fleet_env() -> dict[str, str]:
    """Read standard fleet env vars, raise if missing."""
    token = os.environ.get("MCP_PUBLIC_TOKEN", "")
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = os.environ.get("MCP_PORT", "")
    if not token:
        raise RuntimeError("MCP_PUBLIC_TOKEN is required")
    if not port:
        raise RuntimeError("MCP_PORT is required")
    return {"token": token, "host": host, "port": int(port)}
```

- [ ] **Step 3: Commit**

```bash
git add examples/chatgpt_remote_mcp/fleet/__init__.py examples/chatgpt_remote_mcp/fleet/shared.py
git commit -m "fleet: add shared.py with TokenAuthMiddleware"
```

---

### Task 2: context7_server.py — Stdio-to-HTTP MCP bridge

**Files:**
- Create: `examples/chatgpt_remote_mcp/fleet/context7_server.py`

**Interfaces:**
- Consumes: `TokenAuthMiddleware` from `shared.py`, `get_fleet_env()` from `shared.py`
- Produces: FastMCP server on `<MCP_HOST>:<MCP_PORT>/mcp` with Context7 tools proxied over Streamable HTTP
- Command: `python -m examples.chatgpt_remote_mcp.fleet.context7_server`

- [ ] **Step 1: Write `context7_server.py` — follows the same pattern as `chatgpt_remote_mcp/server.py` (two-thread: internal FastMCP streamable-http + external auth proxy)**

```python
"""Context7 MCP adapter — stdio-to-HTTP bridge for ChatGPT remote access."""

from __future__ import annotations

import os
import threading
from typing import Any

import httpx
import uvicorn
from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .shared import get_fleet_env

# ── Config ────────────────────────────────────────────────────────────
INTERNAL_PORT = 8780  # FastMCP streamable-http (no auth, localhost only)
CONTEXT7_ENV = {
    "CONTEXT7_MCP_URL": os.environ.get(
        "CONTEXT7_MCP_URL", "https://mcp.context7.com/mcp"
    ),
}

# ── FastMCP with tools ────────────────────────────────────────────────
mcp = FastMCP("context7-remote")

# Reusable stdio session to Context7 subprocess
_session: ClientSession | None = None
_lock = threading.Lock()


async def _get_session() -> ClientSession:
    global _session
    if _session is not None:
        return _session
    params = StdioServerParameters(
        command="npx",
        args=["-y", "@upstash/context7-mcp"],
        env=CONTEXT7_ENV,
    )
    read, write = await stdio_client(params).__aenter__()
    _session = await ClientSession(read, write).__aenter__()
    await _session.initialize()
    return _session


@mcp.tool()
async def resolve_library_id(query: str, libraryName: str) -> Any:
    """Resolve a package/product name to a Context7-compatible library ID."""
    session = await _get_session()
    result = await session.call_tool(
        "resolve-library-id", {"query": query, "libraryName": libraryName}
    )
    return result.content[0].text


@mcp.tool()
async def query_docs(libraryId: str, query: str) -> Any:
    """Query Context7 for documentation on a resolved library."""
    session = await _get_session()
    result = await session.call_tool(
        "query-docs", {"libraryId": libraryId, "query": query}
    )
    return result.content[0].text


# ── Auth middleware ───────────────────────────────────────────────────
def create_auth_proxy(
    *, upstream_port: int, valid_tokens: set[str]
) -> Starlette:
    """Return an ASGI app that proxies /mcp to the internal FastMCP
    with mcp_token auth."""
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{upstream_port}")

    async def proxy(request: Request) -> Response:
        token = request.query_params.get("mcp_token")
        if not token:
            return JSONResponse({"error": "missing mcp_token"}, 401)
        if token not in valid_tokens:
            return JSONResponse({"error": "invalid mcp_token"}, 403)

        body = await request.body()
        headers = dict(request.headers)
        headers.pop("host", None)
        resp = await client.post(
            "/mcp",
            content=body,
            headers=headers,
            params={k: v for k, v in request.query_params.items() if k != "mcp_token"},
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
        )

    return Starlette(routes=[{"path": "/mcp", "endpoint": proxy, "methods": ["POST"]}])


# ── Main ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    env = get_fleet_env()

    # Start internal FastMCP (streamable HTTP, no auth, localhost only)
    threading.Thread(
        target=mcp.run,
        kwargs={
            "transport": "streamable-http",
            "host": "127.0.0.1",
            "port": INTERNAL_PORT,
        },
        daemon=True,
    ).start()

    # External auth proxy
    app = create_auth_proxy(
        upstream_port=INTERNAL_PORT, valid_tokens={env["token"]}
    )
    uvicorn.run(app, host=env["host"], port=env["port"])
```

- [ ] **Step 2: Commit**

```bash
git add examples/chatgpt_remote_mcp/fleet/context7_server.py
git commit -m "fleet: add Context7 MCP adapter (stdio bridge)"
```

---

### Task 3: Local smoke test

- [ ] **Step 1: Create env file for test**

```bash
cat > /tmp/test-context7.env << 'EOF'
MCP_PUBLIC_TOKEN=test-context7-token
MCP_HOST=127.0.0.1
MCP_PORT=8790
CONTEXT7_MCP_URL=https://mcp.context7.com/mcp
EOF
```

- [ ] **Step 2: Start adapter in background**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway
set -a; source /tmp/test-context7.env; set +a
examples/chatgpt_remote_mcp/.venv/bin/python -m examples.chatgpt_remote_mcp.fleet.context7_server &
PID=$!
sleep 5
echo "Server PID: $PID"
```

- [ ] **Step 3: Initialize + tools/list**

```bash
curl -s --noproxy '*' -X POST 'http://127.0.0.1:8790/mcp?mcp_token=test-context7-token' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}'
```

Expected: HTTP 200, `mcp-session-id` header, server info Context7 v3.2.1.

```bash
# Extract session ID and query tools
SID=$(curl -s -D - -o /dev/null 'http://127.0.0.1:8790/mcp?mcp_token=test-context7-token' \
  -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}' | grep -i mcp-session-id | awk '{print $2}' | tr -d '\r')
curl -s 'http://127.0.0.1:8790/mcp?mcp_token=test-context7-token' \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

Expected: 2 tools (`resolve-library-id`, `query-docs`).

- [ ] **Step 4: Test bad token**

```bash
curl -s -o /dev/null -w '%{http_code}' 'http://127.0.0.1:8790/mcp?mcp_token=wrong'
```

Expected: 401 or 403 (not 302, not 200).

- [ ] **Step 5: Kill test server**

```bash
kill $PID 2>/dev/null; wait $PID 2>/dev/null
```

---

### Task 4: Systemd service + env file

- [ ] **Step 1: Generate MCP_PUBLIC_TOKEN**

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

Save output for env file.

- [ ] **Step 2: Create env file at `/etc/agent-mcp-context7.env`**

```bash
cat > /etc/agent-mcp-context7.env << 'EOF'
MCP_PUBLIC_TOKEN=<generated-token>
MCP_HOST=0.0.0.0
MCP_PORT=8790
CONTEXT7_MCP_URL=https://mcp.context7.com/mcp
EOF
chmod 600 /etc/agent-mcp-context7.env
```

- [ ] **Step 3: Create systemd service at `/etc/systemd/system/agent-mcp-context7.service`**

```ini
[Unit]
Description=Context7 remote MCP adapter (ChatGPT fleet)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
EnvironmentFile=/etc/agent-mcp-context7.env
ExecStart=/media/1TB/Python/web_ssh/web-ssh-gateway/examples/chatgpt_remote_mcp/.venv/bin/python \
  -m examples.chatgpt_remote_mcp.fleet.context7_server
WorkingDirectory=/media/1TB/Python/web_ssh/web-ssh-gateway
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Enable and start**

```bash
systemctl daemon-reload
systemctl enable agent-mcp-context7.service
systemctl start agent-mcp-context7.service
systemctl is-active agent-mcp-context7.service
```

Expected: `active`.

- [ ] **Step 5: Commit**

```bash
git add examples/chatgpt_remote_mcp/fleet/context7_server.py
git commit -m "fleet: Context7 adapter ready for deploy"
```

---

### Task 5: nginx config + iptables (on VPS)

- [ ] **Step 1: Add to nginx config**

Edit nginx config on VPS (above Authelia auth location, inside `server { server_name ssh.xloud.ru; ... }`):

```nginx
location /mcp/context7 {
    proxy_pass http://10.0.0.3:8790/mcp;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 3600s;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    access_log off;
}
```

```bash
nginx -t && systemctl reload nginx
```

- [ ] **Step 2: Add iptables rule**

```bash
iptables -A INPUT -s 10.0.0.0/24 -p tcp --dport 8790 -j ACCEPT
netfilter-persistent save
```

- [ ] **Step 3: Commit nginx config changes to repo**

---

### Task 6: End-to-end verification

- [ ] **Step 1: Verify from public endpoint**

```bash
MCP_TOKEN=<generated-token>
SID=$(curl -s -D - -o /dev/null 'https://ssh.xloud.ru/mcp/context7?mcp_token='$MCP_TOKEN \
  -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' | grep -i mcp-session-id | awk '{print $2}' | tr -d '\r')

curl -s 'https://ssh.xloud.ru/mcp/context7?mcp_token='$MCP_TOKEN \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

Expected: HTTP 200, 2 tools.

- [ ] **Step 2: Test resolve-library-id**

```
curl -s 'https://ssh.xloud.ru/mcp/context7?mcp_token='$MCP_TOKEN \
  -H 'Content-Type: application/json' -H 'mcp-session-id: <SID>' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"resolve_library_id","arguments":{"query":"how to create middleware","libraryName":"FastAPI"}}}'
```

Expected: returns library IDs for FastAPI.

- [ ] **Step 3: Create ChatGPT App**

- URL: `https://ssh.xloud.ru/mcp/context7?mcp_token=<generated-token>`
- Auth: None
- Verify in ChatGPT: tools appear as Context7 namespace

---


