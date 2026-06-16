# RLM Gateway Auditor (experimental)

Experimental RLM auditor for agent-ssh-gateway.

This example shows how to use RLM as an orchestration layer while keeping all infrastructure access behind agent-ssh-gateway.

## Usage

```bash
export GATEWAY_BASE_URL=http://localhost:8085
export GATEWAY_API_KEY=...
export GATEWAY_SESSION_ID=...
export OPENAI_API_KEY=...

python auditor.py "Investigate why tests are failing"
```

## Security warning

Do not run this against production with a master token. Use a scoped read-only token and command policy.
