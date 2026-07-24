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
