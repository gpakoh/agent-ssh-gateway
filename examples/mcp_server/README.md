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
- `standard` — default read/audit workflow. Includes file reading, repo status, session listing, and job waiting.
- `full` — reserved for diagnostics and future handoff/context tools. Currently identical to `standard`.

Tool mode controls visibility only. Future write/handoff permissions will be controlled separately by `MCP_GATEWAY_WRITE_MODE`.

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

## Excluded by design

- file write/edit/upload
- token management
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
