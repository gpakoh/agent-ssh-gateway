# ChatGPT/Codex Connector Handoff Package

This document packages everything a UI/client operator needs to connect a real ChatGPT/Codex client to the MCP server in safe mode. All values are placeholders — no secrets, no topology, no real tokens.

## What to give the operator

- This document
- `examples/mcp_server/mcp_client.safe.env.example` — copy to ignored private env
- `examples/mcp_server/mcp_client.safe.manifest.expected.json` — machine-readable manifest check
- `scripts/mcp_stdio_safe_smoke.py` — local MCP stdio smoke test

## What NOT to give

- Master key — used only to create restricted agent tokens, never for MCP runtime
- Real gateway URLs or hostnames — use `<gateway-url>` placeholder
- Real IP addresses or host paths
- Docker/admin/write scope tokens
- `.env` files with real values
- Chat IDs, proxy URLs, or internal topology

## Environment variable checklist

Copy `mcp_client.safe.env.example` to `mcp_client.safe.env` (gitignored) and fill:

| Variable | Required | Safe value |
|----------|----------|------------|
| `GATEWAY_URL` | Yes | `<gateway-url>` (e.g. `http://localhost:8085`) |
| `GATEWAY_API_KEY` | Yes | Restricted agent token (NOT master key) |
| `GATEWAY_AGENT_TOKEN` | Yes | Same agent token (used by preflight) |
| `MCP_GATEWAY_TOOL_MODE` | Yes | `mcp_client` |
| `MCP_CLIENT_SAFE_MODE` | Yes | `true` |
| `MCP_ACCESS_PROFILE` | Yes | `mcp_client_safe` |
| `MCP_AUTH_MODE` | Yes | `token` |
| `MCP_PUBLIC_TOKEN` | Yes | Same agent token |

## Token scope checklist

### Allowed scopes (create via master key)

- `ssh:connect`
- `ssh:execute`
- `jobs:read`
- `diagnostics:read`

### Forbidden scopes (must NOT be in agent token)

- `ssh:files` — protects write/edit/patch/upload endpoints
- `project:write` — workspace mutation
- `project:patch` — file patching
- `jobs:run` — command execution
- `auth/admin` — administrative access
- `docker` — container operations

### Never

- **Never use master key as MCP runtime credential**
- **Never print tokens** in logs, docs, or error messages

## Manifest expected counts

- **Safe tools**: 84
- **Blocked tools**: 30
- **Blocked tools present in safe manifest**: 0

See `examples/mcp_server/mcp_client.safe.manifest.expected.json` for machine-readable check.

## First 3 allowed tool calls

1. **`health`** — returns gateway version, build metadata, toolset hash
2. **`tools_manifest`** — returns full safe-mode tool list with counts
3. **`project_info`** or **`repo_status`** — readonly project/repo inspection

## First blocked checks

Confirm these tools are absent from the manifest (should return "not found" or not appear in `list_tools`):

- `project_run_opencode` — agent launch
- `project_run_mimo` — agent launch
- `project_run_agent` — agent launch
- `docker_exec` — container execution
- `docker_compose_up` — container orchestration
- `workspace_file_write` — file mutation
- `workspace_apply_patch` — file mutation
- `project_apply_patch` — file mutation

## Operator approval flow

1. New actor (unknown agent + source_ip) starts in **pending** state
2. Pending actors are capped to `readonly`/`testlint` profile
3. Operator receives Telegram notification with Allow/Deny buttons
4. **Allow** → actor granted `mcp_client_safe` profile (84 tools, readonly)
5. **Deny** → actor blocked entirely, no tools available
6. Operator can clear decision via `POST /api/admin/access-control/clear`

## Rollback / stop

1. **Revoke agent token**: `DELETE /api/agent/token/<token_id>` (master key required)
2. **Clear access-control decisions**: `POST /api/admin/access-control/clear`
3. **Stop MCP process**: kill the MCP server subprocess
4. No gateway restart needed for token revocation

## Stop conditions

- If preflight fails → do not proceed, fix config first
- If gateway returns auth errors → check token scopes
- If blocked tools appear in manifest → verify `MCP_CLIENT_SAFE_MODE=true`
- If notifier shows false critical alerts → verify `access_control.decision` audit type
- If real tokens/IPs appear in logs → stop, rotate token, report
- If operator does not approve within TTL → actor stays pending, limited tools only
