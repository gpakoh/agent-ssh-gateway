# Parallel Agent Handoff v2

**Date:** 2026-06-24
**Status:** Draft
**Supersedes:** `.ai-bridge` handoff protocol from v0.1.0-alpha
**Related:** Session 98 inventory, Session 99 spec

## Motivation

ChatGPT acts as architect/reviewer/coordinator; OpenCode and Mimo act as
isolated executors. The first `.ai-bridge` protocol (v1) was single-agent,
single-task, with no parallelism, no task scope, and no worktree isolation.

## Task ID format

Task ID is assigned by the coordinator (ChatGPT) before the task is handed
to an executor. Deterministic, human-readable, sortable by date.

```
YYYY-MM-DD-<scope>-<short-slug>-<agent>
```

Examples:

```
2026-06-24-stage-12-15a-rag-search-chunks-opencode
2026-06-24-stage-12-15b-search-tests-mimo
```

Allowed chars: `[a-z0-9][a-z0-9-]{10,120}`

Rules:

- Executor must not rename or reassign `task_id`.
- Executor writes only inside `.ai-bridge/tasks/<task_id>/`.
- Executor may create `runs/run-<n>.jsonl` inside the task directory for
  internal execution tracing, but the `task_id` itself is immutable.

## Directory layout

```
.ai-bridge/
  tasks/
    <task_id>/
      current-plan.md           # задача от ChatGPT (write by coordinator)
      agent-status.md           # прогресс (write by executor)
      agent-report.md           # итоговый отчёт (write by executor)
      implementation-diff.patch # diff изменений (write by executor)
      execution-log.jsonl       # structured JSON events (write by executor)
      runs/                     # optional: внутренние логи исполнителя
        run-001.jsonl
```

## Task contract format

Every `current-plan.md` follows a standard template:

```markdown
# Task: <short title>

## Metadata

- Task ID: <task_id>
- Agent: opencode | mimo
- Coordinator: ChatGPT
- Created: <ISO timestamp>
- Deadline: <optional>

## Scope

<what to do, 2-5 sentences>

## Allowed files

<list of file paths, globs, or directories>

## Forbidden

<what not to touch>

## Required checks

<list of commands to verify work>

## Commit message

<proposed commit message>

## Return

<what executor must produce: commit, PR, CI, build, tests, status>

## Acceptance criteria

<list of pass/fail conditions>

## Constraints

<time limits, model choice, style notes, anything else>
```

## Agent profiles

### OpenCode

- Executor for safe/default tasks.
- Runs without `--dangerously-skip-permissions`.
- Well-suited for: frontend, docs, refactoring, bounded backend changes.
- Launch pattern:

```bash
opencode run \
  --dir /path/to/project \
  --agent opencode \
  --model claude-sonnet-4 \
  "Read .ai-bridge/tasks/<task_id>/current-plan.md and execute it. \
   Write status to agent-status.md, report to agent-report.md, \
   diff to implementation-diff.patch. Do not commit or push."
```

### Mimo

- Executor for power tasks.
- May use `--dangerously-skip-permissions` **only** in disposable worktree.
- Well-suited for: migrations, bulk changes, risky refactoring, CI fixes.
- Launch pattern:

```bash
mimo run \
  --dir /path/to/worktree \
  --agent mimo \
  --model claude-sonnet-4 \
  --dangerously-skip-permissions \
  "Read .ai-bridge/tasks/<task_id>/current-plan.md and execute it. \
   Write status to agent-status.md, report to agent-report.md, \
   diff to implementation-diff.patch. Do not commit or push."
```

## Worktree strategy

Each agent gets an isolated git worktree on a dedicated branch:

```bash
# Coordinator creates worktrees
git worktree add ../agent-worktrees/<task_id>-opencode -b opencode/<task_id>
git worktree add ../agent-worktrees/<task_id>-mimo -b mimo/<task_id>

# Agent works inside its own worktree
cd ../agent-worktrees/<task_id>-opencode
opencode run ...
```

Benefits:

- Two agents never touch the same working directory.
- Branch naming convention: `<agent>/<task_id>`.
- Coordinator can diff branches independently.
- No risk of concurrent file write collisions.

## Safety rules

- **No auto-commit, no auto-push.** Agent must not commit or push unless
  explicitly instructed in the task contract.
- **OpenCode runs without `--dangerously-skip-permissions`.** If a task
  requires permissions that OpenCode cannot auto-approve, escalate to Mimo
  or manual review.
- **Mimo may use `--dangerously-skip-permissions` only in disposable
  worktree.** The worktree branch is ephemeral — it gets deleted after
  review or merge.
- **Allowed/forbidden files are advisory for the agent, but the coordinator
  must verify compliance.** The gateway's `gateway_project_read_agent_diff`
  tool is the audit mechanism.
- **Task IDs are immutable.** Once assigned, a task_id must not be
  reassigned or renamed.

## Gateway MCP tools (v2 additions)

| Tool | Purpose |
|------|---------|
| `gateway_project_write_agent_task` | Write `current-plan.md` for a given task_id and agent |
| `gateway_project_read_agent_status` | Read `agent-status.md` |
| `gateway_project_read_agent_report` | Read `agent-report.md` |
| `gateway_project_read_agent_diff` | Read `implementation-diff.patch` |
| `gateway_project_list_agent_tasks` | List task directories under `.ai-bridge/tasks/` |
| `gateway_project_clear_agent_task` | Remove a completed/abandoned task directory |

All tools require `project` and `task_id`. Some require `agent` parameter.

## Coordinator workflow

```text
1. Define task(s) — scope, files, checks, agent
2. Assign task_id per agent
3. Create worktrees + branches
4. Write current-plan.md via gateway_project_write_agent_task
5. Launch agent: opencode run / mimo run
6. Poll agent-status.md for progress
7. Read agent-report.md + implementation-diff.patch
8. Verify: git diff, pytest, ruff, mypy, CI
9. Present to user for commit/release decision
```

## Implementation order

1. **Session 100** — Gateway tools: write/read/list/clear agent task
2. **Session 101** — OpenCode runner wrapper (reads current-plan.md,
   launches `opencode run`, writes results back)
3. **Session 102** — Mimo runner wrapper (same pattern, with
   `--dangerously-skip-permissions` guard)
4. **Session 103** — E2E parallel smoke: two tasks, two agents, review flow
