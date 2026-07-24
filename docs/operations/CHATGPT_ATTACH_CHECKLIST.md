# ChatGPT/Codex Attach — Operator Checklist

Private checklist for the first safe MCP runtime attach to ChatGPT/Codex.

## Prerequisites

- [ ] Gateway v0.1.49a0+ running, `/health` returns `version=0.1.49a0`
- [ ] Master key available (for token creation only, never for MCP runtime)
- [ ] SSH target host reachable (optional — needed only for live tool tests)

## 1. Create restricted agent token

```bash
curl -s -X POST http://<gateway>:8085/api/agent/token \
  -H "X-API-Key: <master-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "scopes": ["ssh:connect", "ssh:execute", "jobs:read", "diagnostics:read"],
    "ttl": 0
  }'
```

- Save token securely. **Never print it.**
- **Never use master key as MCP runtime credential.**
- Allowed scopes: `ssh:connect`, `ssh:execute`, `jobs:read`, `diagnostics:read`
- **Forbidden**: `ssh:files`, `project:write`, `project:patch`, `jobs:run`, `auth/admin`, `docker` scopes

## 2. Copy env template to private env

```bash
cp examples/mcp_server/chatgpt.safe.env.example examples/mcp_server/chatgpt.safe.env
# Edit chatgpt.safe.env with your values (GATEWAY_URL, GATEWAY_AGENT_TOKEN)
# This file is gitignored — never commit it.
```

## 3. Run runtime preflight

```bash
set -a && source examples/mcp_server/chatgpt.safe.env && set +a
python3 scripts/mcp_chatgpt_runtime_preflight.py
```

Expected: all config checks pass, blocked tools confirmed excluded.

## 4. Start MCP server

```bash
set -a && source examples/mcp_server/chatgpt.safe.env && set +a
python3 examples/mcp_server/server.py
```

## 5. Verify manifest

```bash
python3 scripts/chatgpt_tool_attach_smoke.py
```

Expected: 7/7 pass. Safe tools: 84. Blocked tools: 30.

## 6. First allowed tool call

Via MCP: `health`, `read_file`, or `project_info`. Verify response includes version and no secrets.

## 7. First denied tool call

Via MCP: attempt `project_run_opencode` or `docker_exec`. Should return structured blocked error or be absent from manifest.

## 8. Notifier expectations

- `command.deny` alerts include Allow/Deny buttons with decision label, fingerprint prefix, source_ip, TTL
- Access-control decisions emit `access_control.decision` events (not `system.error`)
- No raw tokens, commands, hosts, or paths in alert text

## 9. Access-control cleanup

```bash
# Clear any rehearsal/test decisions
curl -s -X POST http://<gateway>:8085/api/admin/access-control/clear \
  -H "X-API-Key: <master-key>" \
  -H "Content-Type: application/json" \
  -d '{"actor_fingerprint":"<rehearsal-fp>","source_ip":"<rehearsal-ip>","reason":"cleanup"}'
```

## 10. Rollback / revoke

1. Revoke agent token: `DELETE /api/agent/token/<token_id>` (master key)
2. Clear access-control decisions
3. No gateway restart needed

## Red flags / stop conditions

- If preflight fails: do not proceed, fix config first
- If gateway returns auth errors: check token scopes
- If blocked tools appear in manifest: verify `MCP_CHATGPT_SAFE_MODE=true`
- If notifier shows false critical alerts: verify `access_control.decision` audit type
- If real tokens/IPs appear in logs: stop, rotate, report

## Known limitations

- First attach is readonly/testlint only
- No docker, write, or agent launch tools
- SSH:files intentionally excluded (protects write/edit/patch/upload)
- Pending actors get profile cap until operator allows
- Notifier alert TTL defaults to 24h
