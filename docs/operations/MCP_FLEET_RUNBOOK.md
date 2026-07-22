# MCP Fleet Runbook

Six adapters deployed behind nginx on `ssh-gateway.example.com` via Streamable HTTP/SSE.

## Quick healthcheck

```bash
python scripts/mcp_fleet_healthcheck.py
```

Output:

```
  OK    Gateway  [62/62 tools]
  OK   Context7  [2/2 tools]
  OK     GitHub  [8/8 tools]
  OK      Gitea  [12/12 tools]
  OK     Docker  [7/7 tools]
  OK   Postgres  [6/6 tools]
  ─────────────────────────────
  All 6/6 adapters healthy
```

For detailed per-adapter output:

```bash
python scripts/mcp_fleet_healthcheck.py --verbose
```

## Adapter reference

| Adapter | Systemd service | Internal | Public | Env file | Tools |
|---------|----------------|----------|--------|----------|-------|
| Gateway | `agent-ssh-gateway-mcp` | `<ip-address>:8788` | `/mcp` | `<mcp-env-file>` | 62 |
| Context7 | `agent-mcp-context7` | `<ip-address>:8790` | `/mcp/context7` | `<mcp-env-file>` | 2 |
| GitHub | `agent-mcp-github` | `<ip-address>:8791` | `/mcp/github` | `<mcp-env-file>` | 8 |
| Gitea | `agent-mcp-gitea` | `<ip-address>:8792` | `/mcp/gitea` | `<mcp-env-file>` | 12 |
| Docker | `agent-mcp-docker` | `<ip-address>:8793` | `/mcp/docker` | `<mcp-env-file>` | 7 |
| Postgres | `agent-mcp-postgres` | `<ip-address>:8794` | `/mcp/postgres` | `<mcp-env-file>` | 6 |

All public endpoints: `https://ssh-gateway.example.com/<path>?mcp_token=<token>`

## Per-adapter public tokens

Tokens are stored in `<mcp-env-file>` (chmod 600).

```bash
grep MCP_PUBLIC_TOKEN <mcp-env-file> | cut -d= -f2
```

Exception: Gitea token is hardcoded in AGENTS.md table. Gateway token is shared between `docker/.env` and the env file.

## Service management

```bash
# Status of all fleet services
for s in agent-ssh-gateway-mcp agent-mcp-{github,gitea,context7,docker,postgres}; do
  systemctl is-active "$s" && echo "  $s" || echo "  $s: DOWN"
done

# Restart a single adapter
systemctl restart agent-mcp-gitea.service

# Check logs
journalctl -u agent-mcp-gitea.service -n 30 --no-pager
```

## Nginx

All routes are in `/etc/nginx/sites-available/ssh-gateway.example.com` on VPS `<ip-address>`.

```bash
ssh root@<ip-address>
grep -n 'location ^~ /mcp' /etc/nginx/sites-available/ssh-gateway.example.com
nginx -t && systemctl reload nginx
```

## Iptables

Each adapter's public port is opened for `<ip-address>`:

```bash
iptables -L INPUT -n | grep 879
```

## Troubleshooting

### Adapter shows DOWN

```bash
systemctl status agent-mcp-<name>.service --no-pager
journalctl -u agent-mcp-<name>.service -n 50 --no-pager
```

Common causes:
- Env file deleted or chmod changed from 600
- Port conflict (check `ss -tlnp | grep 879`)
- Backend dependency unreachable (Postgres host, Docker socket)

### Endpoint returns 502

```bash
# Check nginx error log
ssh root@<ip-address> "tail -10 /var/log/nginx/error.log"
```

Common causes:
- Backend process not running
- Iptables blocking VPS IP
- Backend listening only on 127.0.0.1 instead of 0.0.0.0

### Session errors in ChatGPT

Tools not appearing after endpoint update:
1. Try a new chat session
2. If still missing, check `tools/list` directly via curl
3. Token mismatch between env file and ChatGPT configuration

### Tools count mismatch

```bash
python scripts/mcp_fleet_healthcheck.py --verbose
```

Update the expected tool count in the script if tools are added/removed.
