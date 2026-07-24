# ChatGPT Attach Rehearsal Runbook

This runbook documents the first real safe attach path for ChatGPT/MCP against the live gateway.

## Prerequisites

- Gateway running v0.1.47a0+
- `MCP_CHATGPT_SAFE_MODE=true`
- `MCP_GATEWAY_TOOL_MODE=chatgpt`
- Master key available for token creation only

## Sequence

### 1. Create restricted agent token

```bash
curl -s -X POST http://<gateway>:8085/api/agent/token \
  -H "X-API-Key: <master-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "scopes": ["ssh:connect", "ssh:execute", "jobs:read", "diagnostics:read"],
    "ttl": 0
  }'
```

Save token value securely. Do NOT print it in logs or reports.

### 2. Start MCP server

```bash
GATEWAY_URL=http://<gateway>:8085 \
GATEWAY_AGENT_TOKEN=<agent-token> \
MCP_GATEWAY_TOOL_MODE=chatgpt \
MCP_CHATGPT_SAFE_MODE=true \
python3 examples/mcp_server/server.py
```

### 3. Verify manifest

```bash
python3 scripts/chatgpt_tool_attach_smoke.py
```

Expected: 7/7 pass. Safe tools include health, read_file, project_* inspection. Blocked tools include docker_*, workspace write, agent launch.

### 4. Controlled readonly call

Via MCP tools/call:
- `health` → version, status
- `read_file` → read a file from the project
- `project_run_pytest` → run tests (testlint path)

### 5. Blocked call verification

Via MCP tools/call:
- `project_run_opencode` → must be absent from manifest (not callable)
- `docker_exec` → must be absent from manifest
- `workspace_file_write` → must be absent from manifest

### 6. Access-control flow

1. Unknown agent+IP → **pending** → readonly profile cap
2. Operator sees alert in Telegram → clicks Deny → **denied** → blocks execute
3. Operator clicks Allow → **allowed** → full profile
4. Operator clicks Clear → back to **pending**

### 7. Notifier verification

- Operator decisions emit `access_control.decision` events (not `system.error`)
- Deny/Allow buttons in Telegram alerts include: decision label, fingerprint prefix, source_ip, TTL
- No raw tokens, commands, hosts, or paths in alert text

### 8. Cleanup

```bash
# Clear any rehearsal state
curl -s -X POST http://<gateway>:8085/api/admin/access-control/clear \
  -H "X-API-Key: <master-key>" \
  -H "Content-Type: application/json" \
  -d '{"actor_fingerprint":"<rehearsal-fp>","source_ip":"<rehearsal-ip>","reason":"rehearsal cleanup"}'
```

## Known limitations

- First attach is readonly/testlint only
- No docker, write, or agent launch tools
- SSH:files excluded (protects write/edit/patch/upload)
- Agent token required (never master key)
- Pending actors get profile cap until operator allows
- Notifier alert TTL defaults to 24h

## Rollback

1. Revoke agent token: `DELETE /api/agent/token/<token_id>` (master key)
2. Clear access-control: `POST /api/admin/access-control/clear`
3. No gateway restart needed — token revocation is immediate
