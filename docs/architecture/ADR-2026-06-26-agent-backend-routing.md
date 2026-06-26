# ADR-2026-06-26: Agent Backend Routing and Proxy Policy

## Status

Accepted.

## Context

OpenCode CLI has a free-tier rate limit that returns `Free usage exceeded, retrying in 7h` once exhausted. A local tool (`opencode-proxy`, at `/usr/local/bin/opencode-proxy`) was proposed that rotates through scraped free public HTTP proxies from sources like `sslproxies.org` to circumvent this limit by changing the egress IP.

While technically functional (TLS is end-to-end so the proxy cannot see request bodies or credentials), the approach raises architectural, operational, and policy concerns:

- Free public proxies are unreliable, potentially malicious, and may serve modified content or inject MITM if the client has custom CA trust.
- Proxy rotation to circumvent rate limits may violate the provider's terms of service.
- It introduces non-deterministic behaviour into CI/CD and agent workflows.
- It obscures the real cause of failures (quota exhaustion) rather than handling it explicitly.

## Decision

1. **Free public proxy rotation is NOT part of `agent-ssh-gateway` core.** The repository must not contain logic for scraping, testing, or rotating public proxies.

2. **`opencode-proxy` may exist as a local operator tool outside the repository** (at `/usr/local/bin/opencode-proxy`). It is a manual wrapper, not an integrated component.

3. **The project supports an `OPENCODE_BIN` override**. The runner layer (`project_run_opencode`) reads `OPENCODE_BIN` from environment and defaults to `/root/.opencode/bin/opencode`. An operator can point this to a custom wrapper, including `opencode-proxy`, at their own discretion. This keeps the decision explicit and traceable.

4. **Future direction: quota-aware backend router.** Instead of bypassing rate limits, the system should:

   - Detect provider cooldown (e.g. OpenCode returns `Free usage exceeded`)
   - Record a structured cooldown entry: `{provider, status, until, reason}`
   - Fall back to an alternative backend (e.g. Mimo with local Ollama)
   - If all backends are unavailable, set task status to `blocked/provider-cooldown`

5. **Preferred fallback: local inference (Mimo/Ollama), not proxy rotation.** Switching to a local model respects the rate limit, avoids third-party risk, and keeps behaviour reproducible.

## Consequences

### Positive

- Core project stays clean of proxy-scraping dependencies and policy risk.
- Operator can still use `opencode-proxy` explicitly without the project endorsing it.
- Future backend router will handle rate limits transparently and testably.

### Negative

- Without the backend router implemented, a rate-limited operator currently has no automated fallback — they must wait or use the manual wrapper.
- The backend router adds complexity to the runner layer when implemented.

## Related

- `examples/mcp_server/handoff_runner.py` — `project_run_opencode()` reads `OPENCODE_BIN`.
- `docs/operations/AGENT_HANDOFF_RUNBOOK.md` — provider cooldown (see note below).
- Future: `examples/mcp_server/agent_backend_router.py` (not yet implemented).
