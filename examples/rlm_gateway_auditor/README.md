# ⚠️ RLM Gateway Auditor (experimental)

**EXPERIMENTAL — NOT PRODUCTION-READY.**

This example shows how to use RLM as an orchestration layer while keeping all infrastructure access behind agent-ssh-gateway.

## Safety warnings

- **Do not use a master/root API token.** Use a scoped read-only token with a command policy allowlist.
- **This example is not a sandbox.** RLM's `LocalREPL` environment executes code via `exec()`. Do not run against production hosts.
- **The gateway is the execution boundary.** RLM never gets direct SSH, filesystem, or unrestricted shell access.
- **All gateway calls use `redact_output=true` and `async_mode=true`** by default. Recursion depth is limited to 1.

## Quick start

```bash
cp .env.example .env
# edit .env with your gateway URL, API key, session ID

# 1. Smoke test — no RLM/OpenAI needed
python auditor.py --dry-run

# 2. Full RLM audit (requires OPENAI_API_KEY + rlms installed)
pip install rlms
python auditor.py "Investigate why tests are failing"
```

### Smoke test checklist

`--dry-run` verifies without calling RLM or OpenAI:

- [ ] `GATEWAY_BASE_URL` is reachable (GET /health)
- [ ] `GATEWAY_API_KEY` is set and accepted (GET /api/ssh/sessions)
- [ ] `GATEWAY_SESSION_ID` is set and the session is alive
- [ ] `gateway_repo_status()` can execute git status/log via async jobs

**Expected output:**

```
============================================================
RLM Auditor — dry-run mode
Gateway connectivity & session check
============================================================
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

## Setup

```bash
export GATEWAY_BASE_URL=http://localhost:8085
export GATEWAY_API_KEY=...
export GATEWAY_SESSION_ID=...
export OPENAI_API_KEY=...
```
