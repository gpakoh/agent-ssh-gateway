# OSS Maintainer Workflows with AI Agents

This document describes how `agent-ssh-gateway` is used in AI-assisted open-source maintenance — the primary use case the project was designed for.

## Why this exists

AI agents (Codex, Copilot, custom assistants) managing open-source infrastructure need controlled SSH access. Direct SSH access is risky: credentials leak, commands are unlogged, and agents can execute destructive operations without oversight.

`agent-ssh-gateway` solves this by wrapping SSH in a structured API with audit, redaction, and access control.

## PR review workflow

1. Agent calls `POST /api/ssh/execute` to run tests on the remote CI runner.
2. Sets `async_mode=true` for long test suites — polls `GET /api/jobs/{job_id}/status` until completion.
3. On failure, retrieves output via `GET /api/jobs/{job_id}/result` with `redact_output=true` to strip tokens from logs.
4. Agent formats results into PR review comment.

```bash
# Start test job
RESP=$(curl -s -X POST http://gateway:8085/api/ssh/execute \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "$SESSION_ID",
    "command": "pytest -q tests/",
    "async_mode": true,
    "redact_output": true
  }')
JOB_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

# Poll until done
sleep 30
curl -s "http://gateway:8085/api/jobs/$JOB_ID/result?redact_output=true"
```

## CI/debug workflow

Debugging a CI failure on a remote host:

1. Agent creates a session to the CI runner: `POST /api/ssh/connect`.
2. Runs diagnostic commands: `journalctl -u runner --no-pager -n 100`, `df -h`, `docker ps`.
3. Inspects logs with output redaction to avoid leaking secrets into agent context.
4. Disconnects: `POST /api/ssh/disconnect`.

```bash
curl -s http://gateway:8085/api/ssh/execute \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "$SESSION_ID",
    "command": "journalctl -u act-runner --no-pager -n 50",
    "redact_output": true
  }'
```

## Release workflow

1. Agent runs build commands on the build server (`python -m build`, `docker compose build`).
2. Uses `async_mode=true` with status polling for multi-minute builds.
3. Retrieves final artifact checksums via a follow-up command.
4. Tags and pushes release commit.

Commands that may contain secrets (npm token, SSH key path) should use `redact_output=true`:

```bash
curl -s -X POST http://gateway:8085/api/ssh/execute \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "$SESSION_ID",
    "command": "python -m build && twine check dist/*",
    "async_mode": true,
    "redact_output": true
  }'
```

## Security audit workflow

1. Agent connects to the gateway's own management interface (separate restricted session).
2. Runs: `ss -tlnp`, `auditctl -l`, `grep -r API_KEY /etc/`.
3. All responses use `redact_output=true` so any keys found in command output are not stored in agent context.
4. Writes findings to a structured audit report.

## Why async jobs matter

- Test suites and builds run 5–60+ minutes — synchronous HTTP would timeout.
- `async_mode=true` returns a `job_id` immediately; the agent polls for completion.
- Result retrieval supports `redact_output=true` so secrets from test output are stripped at the API boundary.
- Jobs survive agent crashes — on reconnect, the agent can poll existing jobs.

## Why output redaction matters

- CI logs commonly contain API keys, tokens, environment variables, or secrets printed by test suites.
- AI agents may inadvertently store or reproduce these credentials in PR comments, issue reports, or chat.
- `redact_output=true` applies a blocklist-based filter at the API level — secrets never reach the agent.
- Disabled by default; the agent must explicitly opt in per request.

## Safe usage boundaries

- **Never expose the gateway to the public Internet** — it is a management-plane tool.
- Always use short-lived agent tokens scoped to specific sessions.
- Commands that modify system state (`rm -rf`, `>`, pipe-to-shell) are blocked by default guardrails — see [SECURITY.md](../SECURITY.md).
- Output redaction is a best-effort safety net, not a DLP solution.
- The gateway audits all commands; review audit logs regularly.
- Use separate sessions for different tasks — the gateway tracks session lifecycle independently.
