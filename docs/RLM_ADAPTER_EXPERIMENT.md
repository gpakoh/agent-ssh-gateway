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

## MVP workflow: CI failure investigator

Input: "Investigate CI failure".

Output:
1. likely root cause
2. evidence
3. minimal fix plan
4. verification commands

## Allowed tools

- `gateway_execute`
- `gateway_job_status`
- `gateway_job_result`
- `gateway_read_file`
- `gateway_repo_status`

## Non-goals

- No autonomous writes.
- No production local REPL.
- No unrestricted command execution.
- No deployment automation in MVP.

## Future

- DockerREPL / E2B sandbox
- read-only scopes
- command allowlist profile
- trajectory visualizer
