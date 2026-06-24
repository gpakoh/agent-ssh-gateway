# MCP Fleet Runbook

Six adapters deployed behind nginx on `ssh.xloud.ru` via Streamable HTTP/SSE.

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
| Gateway | `agent-ssh-gateway-mcp` | `10.0.0.3:8788` | `/mcp` | `/etc/agent-ssh-gateway-mcp.env` | 62 |
| Context7 | `agent-mcp-context7` | `10.0.0.3:8790` | `/mcp/context7` | `/etc/agent-mcp-context7.env` | 2 |
| GitHub | `agent-mcp-github` | `10.0.0.3:8791` | `/mcp/github` | `/etc/agent-mcp-github.env` | 8 |
| Gitea | `agent-mcp-gitea` | `10.0.0.3:8792` | `/mcp/gitea` | `/etc/agent-mcp-gitea.env` | 12 |
| Docker | `agent-mcp-docker` | `10.0.0.3:8793` | `/mcp/docker` | `/etc/agent-mcp-docker.env` | 7 |
| Postgres | `agent-mcp-postgres` | `10.0.0.3:8794` | `/mcp/postgres` | `/etc/agent-mcp-postgres.env` | 6 |

All public endpoints: `https://ssh.xloud.ru/<path>?mcp_token=<token>`

## Per-adapter public tokens

Tokens are stored in `/etc/agent-mcp-<name>.env` (chmod 600).

```bash
grep MCP_PUBLIC_TOKEN /etc/agent-mcp-<name>.env | cut -d= -f2
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

All routes are in `/etc/nginx/sites-available/ssh.xloud.ru` on VPS `192.0.2.10`.

```bash
ssh root@192.0.2.10
grep -n 'location ^~ /mcp' /etc/nginx/sites-available/ssh.xloud.ru
nginx -t && systemctl reload nginx
```

## Iptables

Each adapter's public port is opened for `10.0.0.0/24`:

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
ssh root@192.0.2.10 "tail -10 /var/log/nginx/error.log"
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
