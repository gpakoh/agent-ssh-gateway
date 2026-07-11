# MCP Public Endpoint Runbook

Production deployment: **VPS nginx relay** with Cloudflare DNS proxy (orange cloud).

## Route

```text
ChatGPT / Agent
  → https://mcp.nodsync.org/mcp
  → VPS nginx (:443 TLS, :80 plain)
  → proxy_pass http://127.0.0.1:18788
  → autossh reverse tunnel (VPS:18788 ↔ home:8788)
  → agent-ssh-gateway-mcp.service (home, port 8788)
```

## Canonical Endpoint

| Field | Value |
|-------|-------|
| URL | `https://mcp.nodsync.org/mcp` |
| Fallback | localtunnel URL (unchanged, emergency only) |
| Auth | `Authorization: Bearer <token>` |
| Protocol | Streamable HTTP / SSE |
| TLS | Let's Encrypt (auto-renew via certbot) |
| HSTS | `max-age=31536000` |

## Expected Checks

```bash
# 401 without auth
curl -s -o /dev/null -w "%{http_code}" https://mcp.nodsync.org/mcp
# → 401

# 200 with auth + tools/list
curl -s https://mcp.nodsync.org/mcp \
  -H "Authorization: Bearer $MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# → 88 tools, contains gateway_project_run_agent

# Fleet healthcheck (home)
cd /media/1TB/Python/web_ssh/web-ssh-gateway
python scripts/mcp_fleet_healthcheck.py --verbose
# → 6/6 adapters healthy
```

## Components

### Home (origin)

- **Service**: `agent-ssh-gateway-mcp.service` (port 8788)
- **Env**: `/etc/agent-ssh-gateway-mcp.env` (chmod 600)
- **Token**: `MCP_PUBLIC_TOKEN` in env file
- **Auto-reconnect**: GatewayClient auto-creates a new SSH session on `SESSION_NOT_FOUND` (session expired after gateway/SSHD/Redis restart). Requires `GATEWAY_SSH_HOST`, `GATEWAY_SSH_USER` (or `USERNAME`), and `GATEWAY_SSH_PRIVATE_KEY` (inline) or `GATEWAY_SSH_KEY_PATH` (file path). See `.env.example`. Retries once; lock prevents concurrent reconnects.

### SSH Relay (VPS ↔ home)

- **Service**: `autossh-mcp-relay.service` (home, enabled on boot)
- **Tunnel**: `VPS:127.0.0.1:18788 → home:127.0.0.1:8788`
- **Connection**: `autossh -R 127.0.0.1:18788:127.0.0.1:8788 root@192.0.2.10`
- **Resilience**: autossh auto-reconnects on failure; `ExitOnForwardFailure=yes`
- **Key**: home `id_ed25519` added to VPS `authorized_keys` (passwordless)

### VPS (nginx, port 80 + 443)

- **Host**: `192.0.2.10` (Debian, same ISP as home)
- **SSL**: Let's Encrypt (auto-renew), `/etc/letsencrypt/live/mcp.nodsync.org/`
- **Nginx config**: `/etc/nginx/sites-available/mcp.nodsync.org` → symlink in `sites-enabled/`
- **Upstream**: `http://127.0.0.1:18788` (SSH relay local listener)
- **SSE settings**: `proxy_buffering off; proxy_cache off;` — required for Streamable HTTP transport
- **http2**: Intentionally OFF — shares SSL listen socket with `AI-Docker.conf` (which has http2 disabled globally)
- **Edit**: SSH → `cat /etc/nginx/sites-available/mcp.nodsync.org` → edit → `nginx -t && systemctl reload nginx`

### Cloudflare DNS

- **Record**: `mcp.nodsync.org` A → `171.25.251.242`
- **Proxy**: DNS only (grey cloud) — VPS serves Let's Encrypt TLS directly, no Cloudflare termination
- **Zone**: `nodsync.org` (zone ID: `4821fc5084744fac025a2dbf42ef656d`)

## Known Issues

### MCP server hang (home service)

The `agent-ssh-gateway-mcp.service` can become unresponsive after several hours if stale SSE sessions accumulate. Symptoms: internal `127.0.0.1:8788` stops responding, all healthchecks fail or timeout.

**Fix**:
```bash
systemctl restart agent-ssh-gateway-mcp.service
```

**Monitoring**: Scheduled restart in `/etc/cron.d/mcp-restart` or via systemd timer.

### Healtchcheck nginx route check

The `mcp_fleet_healthcheck.py` used to do a `GET /mcp` for the nginx route check, which created an SSE session that never ended (causing a 10s timeout). Fixed in Session 148: now uses `POST` with a lightweight `initialize` JSON-RPC message instead.

If a fleet MCP server returns HTTP 406 (Not Acceptable), ensure the healthcheck's `check_nginx_route` sends `Accept: application/json, text/event-stream` header.

## DNS History

| Date | Record type | Target | Reason |
|------|-------------|--------|--------|
| session 139 | CNAME | `<tunnel-id>.cfargotunnel.com` | Cloudflare Tunnel (abandoned) |
| session 139 | A | `171.25.251.242` | Direct VPS IP + Cloudflare proxy (current) |

Cloudflare Tunnel abandoned because:
1. ISP-level QUIC filtering blocks `cloudflared tunnel` from home
2. `cloudflared tunnel route dns` requires `cert.pem` (only obtainable via OAuth login)

## Rollback

Switch ChatGPT / agent MCP endpoint back to localtunnel:

```bash
# localtunnel URL (emergency fallback, unchanged)
https://xloud-gpt-mcp-bridge-7f4c2a.loca.lt/mcp
```

Change the endpoint URL in ChatGPT Developer Mode → MCP → edit URL.
No changes to home services needed — the MCP gateway runs independently of the public relay.

## Version Resolution

Production APP_VERSION is determined by `app/version.py`:

1. **Env `APP_VERSION`** — deployment override (optional)
2. **`FALLBACK_VERSION`** — hardcoded in `app/version.py`, bumped in sync with `pyproject.toml` on each release
3. `importlib.metadata` — fallback only (pip-installed package without source)

The `CapabilitiesResponse` endpoint (`/api/capabilities`) returns both `version` and `version_source` fields for diagnostics.

### Verify After Release

```bash
# Check version via local gateway
curl -s http://127.0.0.1:8085/api/capabilities -H "X-API-Key: $API_KEY" | jq .version

# Check version_source
curl -s http://127.0.0.1:8085/api/capabilities -H "X-API-Key: $API_KEY" | jq .version_source

# If version shows old pip-installed value, restart service
sudo systemctl restart agent-ssh-gateway-mcp.service
```

There is no `pip install` step needed after a release — the service runs from the source worktree.
