# Local OpenCode Smoke Runbook

This runbook is for an operator who wants to run local OpenCode against an already prepared task and worktree. It is a smoke path only: it must not replace the trusted delivery contract, the candidate receipt flow, CI, or architect review.

## Scope

Use this path only when a task/worktree has already been prepared by the normal handoff flow.

Required inputs:

- `.ai-bridge/current-plan.md` describing the current task and constraints.
- `.ai-bridge/tasks/<task_id>/task.json` for the exact task being executed.
- A clean, task-scoped worktree or checkout.
- A known allowlist of files the agent may edit.

The operator must copy the allowed-files list from `current-plan.md` or the task contract before launching OpenCode. If the allowlist is missing, ambiguous, or conflicts with the task text, stop and ask the architect to correct the task. Do not infer broader write permissions.

## Local command shape

Run OpenCode from the prepared worktree root, not from an unrelated clone:

```bash
cd <prepared-worktree-root>
opencode run --dangerously-skip-permissions --model <model> < .ai-bridge/current-plan.md
```

The local command may vary by wrapper, but the execution directory and task contract must not vary. The agent must operate only inside the prepared worktree and only within the allowed files.

## Required output artifacts

After the run, the operator must preserve enough evidence for independent review:

- `.ai-bridge/tasks/<task_id>/agent-status.md`
- `.ai-bridge/tasks/<task_id>/agent-report.md`
- `.ai-bridge/tasks/<task_id>/implementation-diff.patch`
- bounded stdout/stderr log tail, if the wrapper records one
- exact final `git status --short`
- exact final `git diff --stat`
- exact final `git diff -- <allowed-files>`

If `implementation-diff.patch` is missing, empty despite code changes, or includes forbidden paths, the run is not a trusted candidate.

## Checks to run

Run the narrowest checks that match the task, then the project-required verification listed in `current-plan.md`.

Minimum local checks:

```bash
git status --short
git diff --stat
git diff --check
git diff -- <allowed-files>
```

For code changes, run the task-specific tests first. If the task changes Python source or tests, run the relevant project checks, for example:

```bash
uv run --extra dev pytest <targeted-tests>
uv run --extra dev ruff check <touched-files>
uv run --extra dev mypy <touched-python-source>
python -m compileall <touched-python-source-or-tests>
```

For docs-only changes, `git diff --check` and path-scope verification are normally sufficient unless `current-plan.md` requires more.

## Prohibitions

The local smoke path must not perform delivery actions.

Do not:

- push any branch, tag, or ref;
- merge, rebase onto shared branches, or amend published commits;
- delete local or remote branches;
- read secrets, tokens, private env files, credential helpers, SSH keys, or unrelated config;
- edit files outside the allowed-files list;
- modify `.git/config` or persistent credentials;
- run deploy, restart, docker admin, or production mutation commands;
- hide failed checks or rewrite artifacts to make a run appear clean.

If OpenCode touches a forbidden file, stop the run if possible, preserve the diff/logs, and report the scope violation. Do not manually trim the diff and call the candidate clean.

## Review rule

This smoke path can produce useful local evidence, but it never decides readiness. Merge-ready remains an architect verdict after independent diff review, tests review, security review, exact-head verification, and CI result review.
