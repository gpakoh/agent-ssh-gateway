# MCP Public Endpoint Runbook

Production deployment: **VPS nginx relay** with Cloudflare DNS proxy (orange cloud).

## Route

```text
ChatGPT / Agent
  → https://gateway.example.com/mcp
  → Cloudflare DNS proxy (A record → 203.0.113.10)
  → VPS nginx (:443 TLS, :80 plain)
  → proxy_pass http://127.0.0.1:18788
  → autossh reverse tunnel (VPS:18788 ↔ home:8788)
  → agent-ssh-gateway-mcp.service (home, port 8788)
```

## Canonical Endpoint

| Field | Value |
|-------|-------|
| URL | `https://gateway.example.com/mcp` |
| Fallback | localtunnel URL (unchanged, emergency only) |
| Auth | `Authorization: Bearer <token>` |
| Protocol | Streamable HTTP / SSE |
| TLS | Let's Encrypt (auto-renew via certbot) |
| HSTS | `max-age=31536000` |

## Expected Checks

```bash
# 401 without auth
curl -s -o /dev/null -w "%{http_code}" https://gateway.example.com/mcp
# → 401

# 200 with auth + tools/list
curl -s https://gateway.example.com/mcp \
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

### SSH Relay (VPS ↔ home)

- **Service**: `autossh-mcp-relay.service` (home, enabled on boot)
- **Tunnel**: `VPS:127.0.0.1:18788 → home:127.0.0.1:8788`
- **Connection**: `autossh -R 127.0.0.1:18788:127.0.0.1:8788 root@192.0.2.10`
- **Resilience**: autossh auto-reconnects on failure; `ExitOnForwardFailure=yes`
- **Key**: home `id_ed25519` added to VPS `authorized_keys` (passwordless)

### VPS (nginx, port 80 + 443)

- **Host**: `192.0.2.10` (Debian, same ISP as home)
- **SSL**: Let's Encrypt, `/etc/letsencrypt/live/gateway.example.com/`
- **Nginx config**: `/etc/nginx/sites-enabled/gateway.example.com`
- **Upstream**: `http://127.0.0.1:18788` (SSH relay local listener)

### Cloudflare DNS

- **Record**: `gateway.example.com` A → `203.0.113.10` (orange cloud ON)
- **Zone**: `example.com` (zone ID: `deadbeefdeadbeefdeadbeefdeadbeef`)
- **SSL mode**: Flexible (Cloudflare terminates TLS → plain HTTP to origin on :80)

## TLS 525 Fix (critical)

### Symptom

`https://gateway.example.com/mcp` returns **error 525** (SSL handshake failure) through Cloudflare, but direct `openssl s_client` to origin `:443` succeeds (verify return:1).

### Root cause

nginx on VPS is compiled with **OpenSSL 3.x** which enables the post-quantum hybrid key exchange `X25519MLKEM768` (ML-KEM 768, a.k.a. Kyber-768). Cloudflare edge servers do not support this key exchange and fail the handshake.

### Fix

Add to nginx `server` block:

```nginx
ssl_ecdh_curve prime256v1:secp384r1;
```

This restricts the TLS key agreement to standard curves and removes the post-quantum hybrid from the handshake. Cloudflare edge negotiates `prime256v1` or `secp384r1` successfully.

**Never omit this directive** when proxying through Cloudflare — even if future OpenSSL versions fix the interop, the explicit curve list is defensive against regression.

## DNS History

| Date | Record type | Target | Reason |
|------|-------------|--------|--------|
| session 139 | CNAME | `<tunnel-id>.cfargotunnel.com` | Cloudflare Tunnel (abandoned) |
| session 139 | A | `203.0.113.10` | Direct VPS IP + Cloudflare proxy (current) |

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
