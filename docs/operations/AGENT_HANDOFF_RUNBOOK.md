# Agent Handoff v2 — Operations Runbook

## Overview

Agent Handoff v2 allows ChatGPT to coordinate multiple AI agents (OpenCode, Mimo) through the Gateway MCP endpoint. Each agent receives a structured task contract (`task.json` + `current-plan.md`) and produces `agent-status.md`, `agent-report.md`, and `implementation-diff.patch`.

## Prerequisites

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MCP_GATEWAY_WRITE_MODE` | Yes | — | Must be `handoff` or `full` |
| `MCP_GATEWAY_WORKTREE_ROOT` | Mimo only | — | Root directory for disposable git worktrees |
| `MIMO_BIN` | Mimo only | `command -v mimo` → `/root/.mimocode/bin/mimo` | Path to Mimo binary |

## Task Lifecycle

```
1. write_agent_task        → .ai-bridge/tasks/<id>/task.json + current-plan.md
2. git worktree add ...    → disposable worktree (Mimo only)
3. project_run_opencode    → OpenCode executes in main project
   project_run_mimo        → Mimo executes in worktree (11 guards)
4. read_agent_status       → verify agent-status.md
5. read_agent_report       → review agent-report.md
6. read_agent_diff         → inspect implementation-diff.patch
7. archive_agent_task      → move to .ai-bridge/archive/
8. git worktree remove     → cleanup (Mimo only)
```

## Mimo Runner Guards (11)

All guards run as shell script on the SSH target:

| # | Guard | Failure |
|---|-------|---------|
| 1 | `task.json` exists | exit 1 |
| 2 | `agent` == `"mimo"` | exit 1 |
| 3 | `worktree_path` set | exit 1 |
| 4 | `MCP_GATEWAY_WORKTREE_ROOT` set | exit 1 |
| 5 | worktree_path is a directory | exit 1 |
| 6 | Canonical realpath for all paths | exit 1 |
| 7 | Worktree != project root | exit 1 |
| 8 | Worktree under WORKTREE_ROOT | exit 1 |
| 9 | Valid git worktree | exit 1 |
| 10 | Top-level matches | exit 1 |
| 11 | Linked worktree (not main checkout) | exit 1 |

## Troubleshooting

### Mimo returns 403 / `mimo-free bootstrap failed`

- **Cause**: Mimo free trial auth expired or API key missing.
- **Not a gateway issue** — the runner path (guards, worktree, lifecycle) completes correctly.
- **Workaround**: Set `MIMO_API_KEY` or use a licensed Mimo account.

### OpenCode binary not found

- MCP tool checks `command -v opencode` → `/root/.opencode/bin/opencode`.
- Install via `npm install -g @opencode/cli` or set `OPENCODE_BIN` env var.

### `MCP_GATEWAY_WORKTREE_ROOT` not set

- Mimo runner exits with Guard 4 failure.
- Set in systemd env file or shell before invocation.

## Healthcheck

```bash
# Verify task directories exist
ls .ai-bridge/tasks/

# Read task status
cat .ai-bridge/tasks/<id>/agent-status.md

# Archive completed tasks
mv .ai-bridge/tasks/<id> .ai-bridge/archive/

# Verify no dangling worktrees
git worktree list

# Full fleet healthcheck
python scripts/mcp_fleet_healthcheck.py
```

## Recovery

If a Mimo task dies mid-execution:

```bash
# Force-remove worktree
git worktree remove /path/to/worktree --force
git branch -D mimo/<task_id>

# Archive incomplete task
mv .ai-bridge/tasks/<task_id> .ai-bridge/archive/
```

## Release Checklist

Before tagging a release, verify:

```bash
make check
python scripts/mcp_fleet_healthcheck.py
git status --short
```
