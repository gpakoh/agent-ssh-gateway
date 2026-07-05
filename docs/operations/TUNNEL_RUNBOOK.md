# TUNNEL RUNBOOK

> **2026-07-05: Production tunnel is now VPS nginx relay + autossh.**
> See [`MCP_PUBLIC_ENDPOINT_RUNBOOK.md`](./MCP_PUBLIC_ENDPOINT_RUNBOOK.md) for the
> canonical endpoint. This file only documents the legacy `scripts/tunnel.mjs`
> subprocess manager.

`scripts/tunnel.mjs` — standalone tunnel subprocess manager extracted from
[codexpro](https://github.com/rebel0789/codexpro).

## Why (legacy)

The MCP gateway (`agent-ssh-gateway-mcp.service`, port 8788) needs to be
reachable from ChatGPT's cloud. Three tunnel backends were available:

| Mode | URL | Stability | Priority |
|------|-----|-----------|----------|
| `cloudflare` | Named tunnel on your domain | Production | 1 (preferred) |
| `quick` | `*.trycloudflare.com` | Ephemeral (new URL each restart) | 2 |
| `ngrok` | reserved `*.ngrok-free.dev` domain | Stable after one-time setup | 3 |

Default fallback order: `cloudflare` → `quick` → `ngrok`.

## Usage

```bash
# Cloudflare named tunnel (production)
node scripts/tunnel.mjs cloudflare \
  --config /etc/cloudflared/config.yml \
  --health http://127.0.0.1:8788/healthz

# Cloudflare named tunnel with inline token
node scripts/tunnel.mjs cloudflare \
  --token "$TUNNEL_TOKEN" \
  --health http://127.0.0.1:8788/healthz

# Cloudflare quick tunnel (ephemeral)
node scripts/tunnel.mjs quick \
  --local http://127.0.0.1:8788

# Ngrok
node scripts/tunnel.mjs ngrok \
  --local http://127.0.0.1:8788 \
  --hostname ssh.xloud.ru
```

## Stdout

Only the public MCP URL is printed to **stdout** as the last line:

```
https://mcp.nodsync.org/healthz
```

All progress messages go to **stderr**. This makes it safe to pipe:

```bash
PUBLIC_URL=$(node scripts/tunnel.mjs quick --local http://127.0.0.1:8788 2>/dev/null)
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Tunnel started, health check passed |
| 1 | Startup error (bad args, binary not found, health timeout) |
| 130 | Interrupted by SIGINT (Ctrl+C) |
| 143 | Killed by SIGTERM |

## Health check

`waitForHealth` accepts any response as "alive":
- **200** — server healthy
- **401** / **403** — server is up but requires auth
- All other status codes or connection errors are retried until timeout.

Configure which endpoint is checked:

```bash
# default: /healthz on the --local port
--health http://127.0.0.1:8788/healthz

# custom path on same port
--health-path /mcp
```

## Binaries

**cloudflared** is resolved in this order:

1. `--cloudflared <path>` argument
2. `CLOUDFLARED_BIN` env var
3. `cloudflared` from PATH
4. `~/.codexpro/bin/cloudflared` (from previous codexpro install)
5. Auto-downloads from `github.com/cloudflare/cloudflared/releases/latest` to
   `~/.codexpro/bin/cloudflared`

**ngrok** is resolved in this order:

1. `NGROK_BIN` env var
2. `ngrok` from PATH

Install ngrok: `https://ngrok.com/download`

## How to stop

Send SIGTERM or SIGINT to the process. The script forwards the signal to all
child processes and waits 1.5s before sending SIGKILL.

```bash
kill <pid>
# or
pkill -f "tunnel.mjs"
```

## Logs

When `--verbose` is passed, the tunnel process stdout/stderr is forwarded in
real-time with a `[cloudflared]` or `[ngrok]` prefix.

Without `--verbose`, only the last 120 lines are kept in memory and shown on
error.

Set `TUNNEL_DEBUG=1` to see full stack traces on errors.

## Security

- Secrets (tunnel tokens) are passed via `--token` or `--token-file`, never
  logged
- `--token` value is passed as a CLI argument to cloudflared — visible in `ps`
  output. Prefer `--token-file` for production
- The health endpoint should be local-only (127.0.0.1) to avoid exposing it to
  the network

## Status (2026-07-05)

**`npm run tunnel cloudflare` is no longer the production path.**

The VPS nginx relay replaced Cloudflare Tunnel because:
- ISP-level QUIC filtering blocks `cloudflared tunnel` from home
- `cloudflared tunnel route dns` requires `cert.pem` (unobtainable without OAuth)
- VPS uses the same ISP as home — no clean egress

Production setup:
- `mcp.nodsync.org` A record → `171.25.251.242` (Cloudflare orange cloud)
- VPS nginx terminates TLS, proxies to `127.0.0.1:18788`
- autossh reverse tunnel carries traffic home to port 8788
- `scripts/tunnel.mjs` kept as emergency fallback via localtunnel mode

See [`MCP_PUBLIC_ENDPOINT_RUNBOOK.md`](./MCP_PUBLIC_ENDPOINT_RUNBOOK.md).
