# RLM Adapter Experiment

## Goal

Recursive maintainer workflows over a safe OpenAPI SSH control plane.

## Why RLM + agent-ssh-gateway

- RLM decomposes tasks recursively.
- agent-ssh-gateway provides scoped, audited, policy-controlled execution.
- The gateway remains the execution boundary.

## Architecture

```
RLM → custom gateway tools → agent-ssh-gateway HTTP API → jobs / results / redaction
```

## Safety boundaries

- No direct SSH.
- No direct filesystem.
- No root/master token.
- Read-only token recommended.
- `redact_output=true` by default.
- `async_mode=true` by default.
- Low recursion depth.

## Implementation milestones

| # | Milestone | Status |
|---|-----------|--------|
| v0 | Design doc + skeleton | ✅ |
| v1 | Safety review (warnings, disclaimers) | ✅ |
| v2 | Runnable dry-run (connectivity, session health) | ✅ |
| v3 | **Controlled subagent profile (read-only, allowlist)** | ✅ |
| v4 | Optional `rlms` package extra | ⏳ |
| v5 | Web UI / job integration | ⏳ |

## MVP workflow: CI failure investigator

Input: "Investigate CI failure".

Output:
1. likely root cause
2. evidence
3. minimal fix plan
4. verification commands

## Allowed tools (root-agent)

- `gateway_execute_restricted` (with command allowlist)
- `gateway_job_status`
- `gateway_job_result`
- `gateway_read_file`
- `gateway_repo_status`

## Subagent tools (when enabled)

- `gateway_job_status`
- `gateway_job_result`
- `gateway_read_file`

## Safety boundaries

- No direct SSH.
- No direct filesystem.
- No root/master token.
- Read-only token recommended.
- `redact_output=true` by default.
- `async_mode=true` by default.
- Subagents disabled by default.
- Root-agent command allowlist (no write/deploy/destructive/network).
- Subagents: read-only tools only (no execute, no write endpoints).
- `max_depth=2` with subagents, `max_depth=1` otherwise.

## Non-goals

- No autonomous writes.
- No production local REPL.
- No unrestricted command execution.
- No deployment automation in MVP.

## Future

- DockerREPL / E2B sandbox
- trajectory visualizer
