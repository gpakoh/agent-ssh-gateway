# ChatGPT Remote MCP Adapter

Experimental Streamable HTTP MCP adapter for ChatGPT Developer Mode.

This adapter exposes the existing `examples/mcp_server` tools over HTTPS so ChatGPT can
connect through **Create App**.

## Security

**Do not expose this without a token.**

The public ChatGPT URL uses `mcp_token`, not the real gateway API key:

```
https://ssh-gateway.example.com/mcp?mcp_token=...
```

The real gateway scoped token stays server-side in `GATEWAY_API_KEY`.

Use a scoped gateway token, not a master key.

## Quick start

```bash
cd examples/chatgpt_remote_mcp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export GATEWAY_BASE_URL=http://127.0.0.1:8085
export GATEWAY_API_KEY=...
export GATEWAY_SESSION_ID=...
export MCP_PUBLIC_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

python server.py
```

## ChatGPT Create App

| Field | Value |
|-------|-------|
| Connection | Server URL |
| Server URL | `https://ssh-gateway.example.com/mcp?mcp_token=<MCP_PUBLIC_TOKEN>` |
| Authentication | None |

## Tools

All tools from `examples/mcp_server` — see [README.md](../mcp_server/README.md).

Tool visibility is controlled by `MCP_GATEWAY_TOOL_MODE`:

- `minimal` — health, session health, restricted execute, job status/result
- `standard` — adds file reading, repo status, session listing, job waiting
- `full` — adds `gateway_self_test` and handoff tools
- `chatgpt` (recommended for ChatGPT) — replaces `gateway_execute_restricted` with high-level read-only tools to avoid platform-level blocking

## Reverse proxy (nginx)

Add to the existing nginx config for `ssh-gateway.example.com`:

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8788;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

**Important:** This path must bypass `auth.example.com` SSO because ChatGPT cannot complete
that SSO flow for MCP tool calls. Add an `access_bypass` or `satisfy any; allow all;`
rule.

## Architecture

```
ChatGPT (Create App)
     │
     │  POST https://ssh-gateway.example.com/mcp?mcp_token=...
     ▼
nginx (ssh-gateway.example.com)
     │  proxy_pass to 127.0.0.1:8788
     ▼
chatgpt_remote_mcp/server.py
     │  TokenAuthMiddleware (validates mcp_token)
     │  mounts mcp_server.streamable_http_app()
     ▼
examples/mcp_server tools
     │  httpx → GATEWAY_BASE_URL
     ▼
agent-ssh-gateway backend
```
