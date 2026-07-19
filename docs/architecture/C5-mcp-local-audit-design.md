# C5: MCP-Local Audit Gap Design

## Current State

- MCP server is a separate process; no shared AuditEventLogger memory.
- Gateway structured audit covers REST-side policy decisions (COMMAND_POLICY_DECISION, WORKSPACE_READONLY, FILE_ACCESS).
- MCP-local blocks currently are **not** persisted in structured audit.

## Paths to Track

| Path | Module | Description |
|------|--------|-------------|
| `execute_restricted` validate_readonly_command deny | `app/routers/mcp_tools.py` | Readonly command validation |
| opencode/mimo hard blocks | `app/routers/mcp_tools.py` | Model-specific blocks |
| project_run_agent backend block | `app/routers/mcp_tools.py` | Agent execution backend |
| docker confirm invalid/expired/consumed | `app/routers/mcp_docker.py` | Docker confirmation states |
| docker deny paths | `app/routers/mcp_docker.py` | Docker command denial |
| `_run_gateway` policy validation blocks | `app/routers/mcp_tools.py` | Gateway policy validation |

## Options

### Option A: MCP-Side JSONL Logger
- MCP process writes its own audit JSONL file
- No gateway coupling
- Metadata only (command root, decision, reason)
- **Near-term recommendation**

### Option B: HTTP Bridge to Gateway
- MCP calls gateway audit endpoint for each decision
- Centralized audit trail
- Adds latency and coupling
- Only if centralized audit becomes required

### Option C: Hybrid Sync
- MCP writes local JSONL + async syncs to gateway
- Best of both worlds
- Higher complexity

## Recommendation

**Option A (MCP-side JSONL) for near-term.** No gateway coupling, metadata-only, minimal implementation. Option B/endpoint bridge later only if centralized audit becomes required.

## Non-Goals

- No command output
- No secrets
- No full prompt/task content

## Tests Required (Future)

- MCP audit log written on deny
- MCP audit log contains command_root, decision, reason
- MCP audit log does not contain secrets or full command text
- Log rotation / size limits

## Status

- **Planned / not implemented in C5**
- Tracked as C5.x follow-up