# MCP Operator Runbook

Practical day-to-day operations for the MCP fleet at `ssh-gateway.example.com`.

**See also**: [`MCP_FLEET_RUNBOOK.md`](./MCP_FLEET_RUNBOOK.md) — adapter reference,
service management, nginx, iptables.
[`MCP_TOKEN_LEDGER.md`](./MCP_TOKEN_LEDGER.md) — token lifecycle, secret policy,
known behavior, bugfix history.
[`AGENT_HANDOFF_RUNBOOK.md`](./AGENT_HANDOFF_RUNBOOK.md) — agent task lifecycle.

---

## 1. Current Production Baseline

| Parameter | Value |
|-----------|-------|
| Auth mode | `oauth` — Bearer token only, `?mcp_token=` rejected |
| Scope enforcement | `audit` — denied scopes logged, not blocked |
| Default profile | `full` |
| Token store | `/var/lib/agent-ssh-gateway/mcp_tokens.json` (chmod 600) |
| Service | `agent-ssh-gateway-mcp.service` |

| Adapter | Tools | Port | Public path |
|---------|-------|------|-------------|
| Gateway | 85 | 8788/8789 | `/mcp` |
| Context7 | 2 | 8790 | `/mcp/context7` |
| GitHub | 8 | 8791 | `/mcp/github` |
| Gitea | 12 | 8792 | `/mcp/gitea` |
| Docker | 14 | 8793 | `/mcp/docker` |
| Postgres | 6 | 8794 | `/mcp/postgres` |

---

## 2. Daily Checks

```bash
# Fleet health — the one canonical check
python scripts/mcp_fleet_healthcheck.py --verbose

# Scope enforcement audit log — shows denied scopes
sudo journalctl -u agent-ssh-gateway-mcp.service --since today | grep 'SCOPE_DENIED'

# Token list — check for expired/leaked tokens
python scripts/mcp_token_cli.py list

# Full scope enforcement smoke test (run after any scope change)
python scripts/mcp_enforce_smoke.py
```

Expected output:
- 6/6 adapters healthy
- Gateway: 85 tools
- No unexpected `SCOPE_DENIED` entries (legitimate denials are OK)
- Token list shows only expected tokens

---

## 3. Token Operations

All operations via `mcp-token` CLI (run from project root with `PYTHONPATH=.`).

### Create a full token (ChatGPT remote)

```bash
cd <repo-root>
PYTHONPATH=. python scripts/mcp_token_cli.py create "my-token-name" --profile full
```

- Raw token printed **once** — copy immediately
- Never commit, never paste into logs, never store in git
- Token written to store automatically

### Create a viewer token (read-only)

```bash
PYTHONPATH=. python scripts/mcp_token_cli.py create "monitor-token" --profile viewer
```

### List tokens

```bash
PYTHONPATH=. python scripts/mcp_token_cli.py list
PYTHONPATH=. python scripts/mcp_token_cli.py list --output json
```

### Revoke a token

```bash
PYTHONPATH=. python scripts/mcp_token_cli.py revoke <token_id>
sudo systemctl restart agent-ssh-gateway-mcp.service
```

### Rotate a token

```bash
PYTHONPATH=. python scripts/mcp_token_cli.py rotate <token_id> --name "new-label"
```

Creates a new token ID, revokes the old one, prints new raw token once.

### Service restart (required after revoke/rotate)

```bash
sudo systemctl restart agent-ssh-gateway-mcp.service
python scripts/mcp_fleet_healthcheck.py
```

---

## 4. ChatGPT App Setup

The ChatGPT Developer Mode MCP endpoint uses Streamable HTTP.

### Endpoint

```
URL:  https://ssh-gateway.example.com/mcp
Type: streamable-http
Auth: Bearer token — paste raw token from `mcp-token create`
```

### Setup steps

1. `mcp-token create "chatgpt-remote" --profile full`
2. Copy raw token from stdout
3. In ChatGPT → Developer Mode → Add MCP endpoint:
   - URL: `https://ssh-gateway.example.com/mcp`
   - Auth: Bearer → paste token
   - Type: Streamable HTTP
4. Save → ChatGPT will call `initialize` → `tools/list`
5. Verify: tools appear in the chat UI

### Token refresh

If the token is revoked for any reason:
1. Create a new token
2. Paste it into the ChatGPT endpoint config
3. ChatGPT automatically re-initializes on next request

---

## 5. Common Failures

### 401 `invalid_token`

Token not recognized by the auth provider.

Checklist:
- Token was created but service wasn't restarted → `systemctl restart agent-ssh-gateway-mcp.service`
- Token was revoked → check `mcp-token list`, `revoked_at` field
- Token store file is missing or wrong path → check `MCP_TOKEN_STORE_FILE` env var
- Token was created by a different service instance (container mismatch)

### 401 `Missing Authorization: Bearer header`

- You're using `?mcp_token=` query param in oauth mode → switch to `Authorization: Bearer <token>` header
- Nginx is stripping the Authorization header → check nginx config for `proxy_set_header Authorization`

### 403 `insufficient_scope`

The token's profile doesn't include the required scope for the tool.

- Check token profile: `mcp-token list --output json | python3 -c "import sys,json; [print(f'{t[\"name\"]}: {t[\"profile\"]}') for t in json.load(sys.stdin)]"`
- Check required scopes in `tool_scopes.py` → `TOOL_SCOPES` map
- Current enforcement mode is `audit` — scopes are logged, **not blocked**

### 500 `Internal Server Error` on every Bearer request

Historical bug (v0.1.20-alpha, fixed in `a076169`):
`load_tokens()` passed `client_id=None` causing Pydantic rejection.
If you see this, check the service logs for `ValidationError: client_id`.

Fix: ensure running commit `a076169` or later.

### tools/list empty from direct curl

Known Streamable HTTP session lifecycle quirk (see `MCP_TOKEN_LEDGER.md`).

**Do not rely on direct curl for tools/list.** Always use:

```bash
python scripts/mcp_fleet_healthcheck.py --verbose
```

### Scope smoke test: `full` profile passes but `viewer` denied

Expected — `viewer` only has `mcp:read`. The `mcp_enforce_smoke.py` script
tests each profile against allowed/denied tools. A denial for `viewer` on
`docker_ps` is correct behavior.

---

## 6. Emergency Rollback

If oauth mode is broken and tools need to come back immediately:

1. Set `MCP_AUTH_MODE=token` in `<mcp-env-file>`
2. Ensure `MCP_PUBLIC_TOKEN` is set (it already is in the env file)
3. Restart:

```bash
sudo systemctl restart agent-ssh-gateway-mcp.service
python scripts/mcp_fleet_healthcheck.py
```

In token mode:
- `?mcp_token=<MCP_PUBLIC_TOKEN>` query param works (legacy clients)
- Bearer header also works with `MCP_PUBLIC_TOKEN`
- TokenStore tokens are still loaded BUT `load_access_token` is not called
  by the proxy middleware in token mode — only static `MCP_PUBLIC_TOKEN` is checked

**Rollback is safe** — no data loss, no schema change, just less secure.

---

## 7. Security Rules

1. **Raw token printed once** — at creation. Copy immediately, never store elsewhere
2. **No raw tokens in git** — not in commits, not in `.env`, not in docs, not in logs
3. **TokenStore chmod 600** — owner root only. Check: `stat -c '%a' /var/lib/agent-ssh-gateway/mcp_tokens.json`
4. **No raw tokens in shell history** — use `HISTCONTROL=ignorespace` and prepend space to commands with tokens
5. **Rotate on suspicion** — if a token might be leaked, `revoke` it and `create` a new one
6. **Service env files chmod 600** — all `<mcp-env-file>` files must be 600.
   Fleet healthcheck verifies this automatically

---

## 8. Docker Operations Tools

### Added in Session 160 (2026-07-08)

New Docker tools for container lifecycle management:

| Tool | Purpose | Safety |
|------|---------|--------|
| `docker_start` | Start a stopped container | Container name validation |
| `docker_stop` | Stop a running container | Container name validation, timeout 1-120s |
| `docker_restart` | Restart a container | Container name validation, timeout 1-120s |
| `docker_compose_up` | Start Compose services | Path validation, service name validation |
| `docker_compose_restart` | Restart Compose services | Path validation, service name validation |
| `docker_compose_build` | Build Compose services | Path validation, service name validation |
| `docker_compose_logs` | Fetch Compose service logs | Path validation, service name validation, tail 1-1000 |

### Safety

- All Docker commands use argv arrays (`["docker", "restart", "--time", "10", "web"]`) — never `shell=True`
- Container names validated against `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$`
- Compose paths validated against path traversal and restricted to allowed roots
- Service names validated against `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`
- Timeouts bounded: 1-120s for stop/restart, 1-300s for compose build

### Not included (reserved for future)

- `docker_compose_down` — too destructive for current scope
- `docker_rm/rmi/volume_rm/prune/exec/run` — dangerous operations
