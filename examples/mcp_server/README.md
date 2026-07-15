# Agent SSH Gateway MCP Server

Experimental MCP server for exposing safe, read/audit-first agent-ssh-gateway operations to MCP clients.

**⚠️ Experimental. Do not use this with a master/root token. Use a scoped token and command policy.**

## Tool output format

Tools return both human-readable text and machine-readable `structuredContent`.

The `_meta.agent_ssh_gateway_tool` field identifies the tool that produced the response.

Errors use `isError: true` with an `Error:` prefix in the text.

## Tool modes

`MCP_GATEWAY_TOOL_MODE` controls which tools are exposed to the MCP client.

- `minimal` — health, session health, restricted execute, job status/result. Suitable for limited-scope automation.
- `standard` — default read/audit workflow. Includes file reading, repo status, session listing, job waiting, and all workspace tools (write, edit, patch, preview ×3, verify).
- `full` — reserved for diagnostics, handoff, and workspace tools. Adds `gateway_self_test` plus all standard workspace tools.
- `chatgpt` — designed for ChatGPT remote MCP. Replaces `gateway_execute_restricted` with high-level read-only tools. **No workspace tools** — write, preview, and verify are intentionally excluded.

Tool mode controls visibility only. Write permissions are orthogonal — see [Handoff mode](#handoff-mode) below.

## Tools

- `gateway_health` — check gateway liveness
- `gateway_list_sessions` — list SSH sessions visible to the API key
- `gateway_session_health` — check a specific session health
- `gateway_execute_restricted` — run an allowlisted read-only command as a redacted async job
- `gateway_job_status` — get background job status
- `gateway_job_result` — get background job result
- `gateway_wait_job` — wait for a job and return its result
- `gateway_read_file` — read a file through the gateway file API
- `gateway_repo_status` — collect basic git repository status
- `gateway_working_directory` — print working directory (chatgpt mode)
- `gateway_git_status` — git status --short (chatgpt mode)
- `gateway_recent_commits` — git log --oneline -10 (chatgpt mode)
- `gateway_git_diff_stat` — git diff --stat (chatgpt mode)
- `gateway_show_changes` — combined git status + diff stat (chatgpt mode)
- `gateway_run_tests` — pytest -q (chatgpt mode)
- `gateway_run_lint` — ruff check (chatgpt mode)
- `gateway_run_compileall` — python -m compileall (chatgpt mode)
- `gateway_self_test` — full-mode diagnostic: tool mode, gateway health, session health, command policy, optional repo status
- `gateway_read_handoff` — read .ai-bridge handoff files
- `gateway_show_handoff_status` — show compact handoff file availability
- `gateway_write_handoff_plan` — write `.ai-bridge/current-plan.md` (requires `MCP_GATEWAY_WRITE_MODE=handoff`)

## ChatGPT-safe mode

For ChatGPT remote MCP, use:

```bash
export MCP_GATEWAY_TOOL_MODE=chatgpt
```

This mode hides the generic `gateway_execute_restricted` tool and exposes high-level read-only / verification tools instead:

- `gateway_working_directory` — print working directory
- `gateway_git_status` — git status --short
- `gateway_recent_commits` — git log --oneline -10
- `gateway_git_diff_stat` — git diff --stat
- `gateway_show_changes` — combined git status + diff stat
- `gateway_run_tests` — pytest -q
- `gateway_run_lint` — ruff check
- `gateway_run_compileall` — python -m compileall

This is intended to reduce platform-level blocking and avoid exposing a generic SSH command surface.

## Handoff mode

Handoff tools are full-mode tools. They remain write-disabled unless `MCP_GATEWAY_WRITE_MODE` is set to `handoff` or `full`.

The first write surface exposed by this example is intentionally limited to:

- `.ai-bridge/current-plan.md`

It does not allow source file writes, edits, uploads, deletes, deploys, or token management.

Enable handoff explicitly:

```bash
export MCP_GATEWAY_TOOL_MODE=full
export MCP_GATEWAY_WRITE_MODE=handoff
```

Use this mode when you want an MCP client to prepare a plan for a local or remote implementation agent without giving it direct source-write access.

Tools:

- `gateway_read_handoff` — read `.ai-bridge/current-plan.md`, `agent-status.md`, and `implementation-diff.patch`
- `gateway_show_handoff_status` — compact handoff file availability check
- `gateway_write_handoff_plan` — write `.ai-bridge/current-plan.md` (requires `WRITE_MODE=handoff`)

## Workspace tools

The MCP server exposes scoped workspace write, preview, and verify tools.
All require the `mcp:project` scope.

### Available by mode

| Tool | standard | full | chatgpt |
|------|----------|------|---------|
| `workspace_file_write` | yes | yes | — |
| `workspace_file_edit` | yes | yes | — |
| `workspace_apply_patch` | yes | yes | — |
| `workspace_preview_write` | yes | yes | — |
| `workspace_preview_edit` | yes | yes | — |
| `workspace_preview_patch` | yes | yes | — |
| `workspace_verify` | yes | yes | — |

**chatgpt mode** intentionally excludes all workspace tools — it is
read-only by design (no write, no preview, no verify via MCP).

### Safe flag

`workspace_file_write`, `workspace_file_edit`, and `workspace_apply_patch`
accept an optional `safe` parameter (bool, default `false`). When `safe=true`,
the response includes a receipt object with: `receipt_id`, `before_hash`,
`after_hash`, `changed`, `verified`, `diff_summary`. Safe is fully wired
through MCP to the C1 library.

### Preview and verify

Preview tools return diff metadata without writing to disk.
`workspace_verify` returns `matches` (bool, plural), `current_hash`,
and `file_exists`. No file content is returned.

### Rollback

**Rollback is NOT available** via MCP tools, REST endpoints, or SDK.
Rollback is a separate lifecycle managed by SnapshotStore (Python API only).

## Excluded by design

- unrestricted command execution
- deployment or destructive operations
- WebSocket/PTTY streaming

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r examples/mcp_server/requirements.txt

export GATEWAY_BASE_URL=http://localhost:8085
export GATEWAY_API_KEY=...
export GATEWAY_SESSION_ID=...

python examples/mcp_server/server.py
```

## Claude Desktop example

```json
{
  "mcpServers": {
    "agent-ssh-gateway": {
      "command": "python",
      "args": ["/path/to/agent-ssh-gateway/examples/mcp_server/server.py"],
      "env": {
        "GATEWAY_BASE_URL": "http://localhost:8085",
        "GATEWAY_API_KEY": "...",
        "GATEWAY_SESSION_ID": "..."
      }
    }
  }
}
```

## OpenCode setup

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "agent-ssh-gateway": {
      "type": "local",
      "command": [
        "python",
        "/ABSOLUTE/PATH/TO/agent-ssh-gateway/examples/mcp_server/server.py"
      ],
      "environment": {
        "GATEWAY_BASE_URL": "http://localhost:8085",
        "GATEWAY_API_KEY": "your-scoped-api-key",
        "GATEWAY_SESSION_ID": "your-existing-session-id"
      },
      "enabled": true
    }
  }
}
```

Add to your project or global `opencode.jsonc`. Restart OpenCode — tools
appear automatically. An example file lives at
[opencode.example.jsonc](opencode.example.jsonc) in this directory.

## Required scopes

| Scope | Required for |
|-------|-------------|
| `ssh:execute` | `gateway_execute_restricted` |
| `ssh:files` | `gateway_read_file` |
| `jobs:read` | `gateway_job_status`, `gateway_job_result`, `gateway_wait_job` |
| `mcp:project` | `workspace_file_write`, `workspace_file_edit`, `workspace_apply_patch`, `workspace_preview_write`, `workspace_preview_edit`, `workspace_preview_patch`, `workspace_verify` |

Use a **scoped agent token**, not a master key. The `scopes` parameter on
`POST /api/tokens/create` allows setting custom scopes.

## Example prompt

Once configured, ask your agent:

```
Use the agent-ssh-gateway MCP server. Check gateway health, check the SSH
session health, then collect repo status with read-only commands. Do not
modify files. Do not run destructive commands. Return a short report.
```

## Security

This server is not the security boundary. The gateway is.

Keep using:
- scoped API tokens
- command policy
- session ownership
- output redaction
- audit logging
