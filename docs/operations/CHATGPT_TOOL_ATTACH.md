# ChatGPT Tool Attach — Safe First Connection

## Quick start

```bash
# 1. Copy template to private env (already gitignored)
cp examples/mcp_server/chatgpt.safe.env.example examples/mcp_server/chatgpt.safe.env

# 2. Edit chatgpt.safe.env with your values
#    NEVER commit this file

# 3. Run preflight
python3 scripts/mcp_chatgpt_runtime_preflight.py

# 4. Start MCP server
set -a && source examples/mcp_server/chatgpt.safe.env && set +a
python3 examples/mcp_server/server.py
```

## Architecture

```
ChatGPT / OpenAI tool-use
  → MCP Server (mcp_gateway.py)
    → Gateway API (agent-ssh-gateway)
      → SSH target host
```

## Required environment variables

```bash
GATEWAY_URL=http://localhost:8085          # Gateway API base URL
GATEWAY_AGENT_TOKEN=<agent-token>          # Restricted agent token (NOT master key)
MCP_GATEWAY_TOOL_MODE=chatgpt              # Use chatgpt tool set
MCP_CHATGPT_SAFE_MODE=true                # Strip dangerous tools from chatgpt mode
MCP_GATEWAY_PROJECT_ROOT=/path/to/project # Project root for project tools
```

## Agent token creation

ChatGPT must use a restricted agent token with read-only scopes. Never use the master key.

```bash
# Via gateway API (master key required for token creation only)
curl -s -X POST http://localhost:8085/api/agent/token \
  -H "X-API-Key: <master-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "scopes": ["ssh:connect", "ssh:execute"],
    "ttl": 0
  }'
```

The agent token inherits the gateway's command policy. With the default profile, ChatGPT's `execute_restricted` runs through the server-side readonly/testlint allowlist.

## Safe mode tool set

When `MCP_CHATGPT_SAFE_MODE=true`, only these tools are exposed:

**Read-only / inspection:**
- `health`, `tools_manifest`, `session_health`
- `job_status`, `job_result`, `wait_job`
- `read_file`, `repo_status`
- `working_directory`, `git_status`, `recent_commits`, `git_diff_stat`, `show_changes`
- `project_*` read-only tools (info, read_file, search_text, find_files, list_files, tree, git status/diff/commits/branches)
- `gitea_*`, `github_*` read-only tools
- `resolve_library_id`, `query_docs`
- `read_handoff`, `show_handoff_status`

**Test/lint (testlint profile):**
- `run_tests`, `run_lint`, `run_compileall`
- `project_run_tests`, `project_run_lint`, `project_run_compileall`

**Blocked in safe mode:**
- `project_run_opencode`, `project_run_mimo`, `project_run_agent` (agent launch)
- `project_apply_patch`, `workspace_*` write tools
- `write_handoff_plan`, `project_write_handoff_plan`
- `docker_*` all Docker tools
- `project_write_agent_task`, `project_archive_agent_task`

## Operator approval flow

1. Unknown actor+source_ip starts as **pending**
2. Pending actors are capped to **readonly/testlint** profile
3. `execute_restricted` runs through server-side command policy
4. `command.deny` alerts appear in Telegram with Allow/Deny buttons
5. Operator clicks Allow → access-control decision stored → actor allowed
6. Operator clicks Deny → actor blocked, sessions killed

## First attach smoke

```bash
GATEWAY_URL=http://localhost:8085 \
GATEWAY_AGENT_TOKEN=<token> \
MCP_GATEWAY_TOOL_MODE=chatgpt \
MCP_CHATGPT_SAFE_MODE=true \
python3 scripts/chatgpt_tool_attach_smoke.py
```

The script:
- Calls health and capabilities endpoints
- Verifies safe tool list excludes blocked tools
- Verifies blocked tools are absent from the manifest
- If `TEST_SSH_HOST` is set, connects and runs a readonly command
- Never prints tokens
- Never requires real write/RW overlay

## Rollback / disconnect

1. Revoke the ChatGPT agent token via `DELETE /api/agent/token/<token_id>` (master key required).
2. If actor was denied, use `POST /api/admin/access-control/clear` to reset.
3. No gateway restart needed — token revocation is immediate.
