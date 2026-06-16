# ⚠️ RLM Gateway Auditor (experimental)

**EXPERIMENTAL — NOT PRODUCTION-READY.**

This example shows how to use RLM as an orchestration layer while keeping all infrastructure access behind agent-ssh-gateway.

## Safety warnings

- **Do not use a master/root API token.** Use a scoped read-only token with a command policy allowlist.
- **This example is not a sandbox.** RLM's `LocalREPL` environment executes code via `exec()`. Do not run against production hosts.
- **The gateway is the execution boundary.** RLM never gets direct SSH, filesystem, or unrestricted shell access.
- **All gateway calls use `redact_output=true` and `async_mode=true`** by default.
- **Root-agent commands are restricted by allowlist** (`ALLOWED_COMMAND_PREFIXES`) — no write, deploy, or destructive commands.
- **Subagents are disabled by default** and get only read-only tools (`gateway_job_status`, `gateway_job_result`, `gateway_read_file`). No direct SSH, no write endpoints, no command execution.

## Quick start

```bash
cp .env.example .env
# edit .env with your gateway URL, API key, session ID

# 1. Smoke test — no RLM/OpenAI needed
python auditor.py --dry-run

# 2. Full RLM audit (requires OPENAI_API_KEY + rlms installed)
pip install rlms
python auditor.py "Investigate why tests are failing"

# 3. With controlled subagents (experimental, disabled by default)
python auditor.py --enable-subagents "Investigate CI failure"
# or: RLM_ENABLE_SUBAGENTS=1 python auditor.py "Investigate CI failure"
```

### Smoke test checklist

`--dry-run` verifies without calling RLM or OpenAI:

- [ ] `GATEWAY_BASE_URL` is reachable (GET /health)
- [ ] `GATEWAY_API_KEY` is set and accepted (GET /api/ssh/sessions)
- [ ] `GATEWAY_SESSION_ID` is set and the session is alive
- [ ] `gateway_repo_status()` can execute git status/log via async jobs

> **Note:** `git status/log` will fail if the SSH session's working directory is not inside a git repository. This is expected — `gateway_repo_status` runs raw git commands on the remote host. To test against a real repo, connect to a session where the working directory is a git checkout, or adjust the commands in `gateway_repo_status()`.

**Expected output:**

```
============================================================
RLM Auditor — dry-run mode
Gateway connectivity & session check
============================================================

  Subagents:           disabled
  Subagent tools:      (none)
  Max depth:           1
  Command allowlist:   enabled (18 prefixes)

--- repo_status smoke ---
    M README.md
  [PASS] GET http://localhost:8085/health -> 200
  [PASS] GATEWAY_API_KEY set
  [PASS] GET /api/ssh/sessions -> API key accepted
  [PASS] GATEWAY_SESSION_ID set
  [PASS] GET /api/ssh/session/abc123/health -> session alive
  [PASS]   git status: completed
  [PASS]   git recent_commits: completed
  [PASS]   git tags: completed

8/8 passed
```

## Controlled subagents

**Experimental — disabled by default.**

When enabled, RLM's root-agent can delegate subcalls to subagents. Subagents receive only read-only tools — no command execution, no write endpoints:

| Tool | Root-agent | Subagents |
|------|-----------|-----------|
| `gateway_execute_restricted` | with allowlist | — |
| `gateway_repo_status` | via allowed git commands | — |
| `gateway_job_status` | yes | yes |
| `gateway_job_result` | yes | yes |
| `gateway_read_file` | yes | yes |
| `gateway_wait_job` | yes | — |

Safety boundaries:
- subagents disabled by default (`RLM_ENABLE_SUBAGENTS=0`)
- subagent tools: read-only only (no SSH, no execute, no write)
- root-agent commands limited to allowlist (see `ALLOWED_COMMAND_PREFIXES`)
- denied commands explicitly blocked (write, deploy, destructive, network)
- `max_depth=2` when subagents enabled, `max_depth=1` otherwise
- `redact_output=true`, `async_mode=true` on all execute calls
- use a scoped read-only token with a command-policy allowlist on the gateway side

Enable via:

```bash
python auditor.py --enable-subagents "Investigate CI failure"
# or
RLM_ENABLE_SUBAGENTS=1 python auditor.py "Investigate CI failure"
```

## Setup

```bash
export GATEWAY_BASE_URL=http://localhost:8085
export GATEWAY_API_KEY=...
export GATEWAY_SESSION_ID=...
export OPENAI_API_KEY=...
```
