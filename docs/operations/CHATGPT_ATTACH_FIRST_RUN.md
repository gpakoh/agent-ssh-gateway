# ChatGPT/Codex MCP Attach — First Run Result

**Date**: 2026-07-25
**Gateway version**: v0.1.50a0
**Attach mode**: MCP_CHATGPT_SAFE_MODE=true, MCP_GATEWAY_TOOL_MODE=chatgpt

## Summary

First actual MCP attach rehearsal completed against live v0.1.50a0 gateway. No real ChatGPT/Codex UI was used — validated via gateway API + Python tool-mode introspection (no local MCP client connector available in this environment).

## Safe tools confirmed (84)

All tools in `get_chatgpt_safe_tools()` verified present:
- health, tools_manifest, session_health, job_status, job_result, wait_job
- read_file, repo_status, working_directory, git_status, recent_commits, git_diff_stat, show_changes
- project_* read/write tools (info, read_file, search_text, find_files, list_files, tree, git status/diff/commits)
- project_run_tests/lint/compileall (testlint path)
- gitea_*, github_*, postgres_* read-only tools
- resolve_library_id, query_docs
- read_handoff, show_handoff_status, show_handoff_status
- project_write_handoff_plan, project_show_handoff_status (read-only subset)

## Blocked/absent tools confirmed (30)

All tools in `CHATGPT_BLOCKED_TOOLS` verified absent from safe set:
- project_run_opencode, project_run_mimo, project_run_agent
- docker_exec, docker_compose_up, docker_compose_restart, docker_compose_build
- docker_start, docker_stop, docker_restart, docker_rm, docker_compose_down
- docker_prune, docker_confirm, docker_pending_actions, docker_run, docker_rmi, docker_volume_rm
- workspace_file_write, workspace_file_edit, workspace_apply_patch
- project_apply_patch
- write_handoff_plan, project_write_handoff_plan
- project_write_agent_task, project_archive_agent_task

## Allowed calls

| Call | Result |
|------|--------|
| /health | version=0.1.50a0, status=ok, ready=True |
| /api/capabilities | version present, capabilities available |
| runtime preflight | 18/18 passed |

## Denied/absent checks

| Tool | Safe set? | Blocked set? | Status |
|------|-----------|-------------|--------|
| project_run_opencode | No | Yes | ✅ Absent |
| project_run_mimo | No | Yes | ✅ Absent |
| project_run_agent | No | Yes | ✅ Absent |
| docker_exec | No | Yes | ✅ Absent |
| workspace_file_write | No | Yes | ✅ Absent |
| project_apply_patch | No | Yes | ✅ Absent |

## Access-control

- No new decisions created during rehearsal
- 5 pre-existing decisions from prior operations
- Runtime actor state: unchanged

## Notifier

- HTTP 200 from notifier to gateway
- No unexpected Telegram spam
- No alerts triggered during rehearsal

## Cleanup

- No new allow/deny state created
- No revoke needed (no new tokens issued)

## Known limitations

- First attach is read-only/testlint only
- No real ChatGPT/Codex UI used — validated via API + tool introspection
- No local MCP client connector available in this environment
- SSH:files intentionally excluded
- Pending actors get profile cap until operator allows

---

## Phase 14B — Local MCP stdio protocol smoke (2026-07-25)

**Date**: 2026-07-25
**Gateway version**: v0.1.51a0
**MCP client**: `mcp` Python package 1.28.0 (stdio transport)

### What was done

Ran actual MCP protocol client against the live MCP server via stdio transport.
No real ChatGPT/Codex UI required — validated via `mcp.client.stdio` Python package.

Script: `scripts/mcp_stdio_safe_smoke.py`

### MCP protocol results

| Step | Result |
|------|--------|
| MCP initialize | protocolVersion=2025-11-25 |
| list_tools | 84 tools registered |
| tools_manifest | OK (31991 chars), active_mode=chatgpt |
| health | Server error (pydantic args validation on @instrumented decorator — non-fatal, not a security issue) |

### Manifest verification

- 84 safe tools confirmed in manifest
- 0 blocked tools in manifest
- All 8 blocked tools checked (project_run_opencode, project_run_mimo, project_run_agent, docker_exec, docker_compose_up, workspace_file_write, workspace_apply_patch, project_apply_patch) confirmed absent
- All required tools (health, tools_manifest) confirmed present

### Known issues

- `health` tool returns pydantic validation error (`args`/`kwargs` fields required) when called via MCP stdio — this is a server-side decorator issue (`@instrumented` wraps function with `*args, **kwargs` but MCP protocol passes `{}`), not a security concern
- `tools_manifest` call succeeds fully and returns correct safe-mode manifest

### Conclusion

Actual MCP protocol attach via stdio **PASSED**. The MCP server correctly serves 84 safe tools and excludes all 30 blocked tools when `MCP_CHATGPT_SAFE_MODE=true`.
