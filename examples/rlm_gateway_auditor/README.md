# ⚠️ RLM Gateway Auditor (experimental)

**EXPERIMENTAL — NOT PRODUCTION-READY.**

This example shows how to use RLM as an orchestration layer while keeping all infrastructure access behind agent-ssh-gateway.

## Safety warnings

- **Do not use a master/root API token.** Use a scoped read-only token with a command policy allowlist.
- **This example is not a sandbox.** RLM's `LocalREPL` environment executes code via `exec()`. Do not run against production hosts.
- **The gateway is the execution boundary.** RLM never gets direct SSH, filesystem, or unrestricted shell access.
- **All gateway calls use `redact_output=true` and `async_mode=true`** by default. Recursion depth is limited to 1.

## Usage

```bash
export GATEWAY_BASE_URL=http://localhost:8085
export GATEWAY_API_KEY=...
export GATEWAY_SESSION_ID=...
export OPENAI_API_KEY=...

python auditor.py "Investigate why tests are failing"
```
