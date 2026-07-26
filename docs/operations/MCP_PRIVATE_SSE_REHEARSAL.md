# Private SSE MCP Transport — Rehearsal Record

Record of the first end-to-end operator rehearsal of `scripts/mcp_sse_serve.py`,
following `docs/operations/MCP_PRIVATE_SSE_RUNBOOK.md`. This is a **manual,
local-only** rehearsal record — not a public connector, not a persistent
service, and not evidence of any change to deployment topology.

## What this rehearsal was

- Manual, local-only: a human ran the runbook steps once, on the local
  host, in a single foreground session, and tore everything down
  afterward. No automation, no scheduled job, no CI step triggers this.
- Not a public ChatGPT/OpenAI connector. Public ChatGPT/OpenAI connector
  readiness is still **NOT live** — this rehearsal changes nothing about
  that status.
- Not a persistent service. No Docker Compose service, no systemd unit,
  and no autostart mechanism was added or modified. The entrypoint ran
  as a single, manually-started and manually-stopped process.

## What was verified

### Bind

Confirmed loopback-only: the process bound to `127.0.0.1` on a locally
assigned free port. Verified directly via the listening-socket table —
the bound address was `127.0.0.1`, never `0.0.0.0` or any non-loopback
address. `MCP_HTTP_ALLOW_NON_LOOPBACK` was not set.

### Bearer auth

Every request to `/sse` and `/messages/` was gated by the entrypoint's
independent bearer-token middleware:

- No token → `401`
- Wrong token → `401`
- Correct token → non-401; the SSE stream opened and the MCP protocol
  session completed successfully

### MCP protocol

Over the authenticated SSE session:

- `initialize` completed successfully
- `list_tools` returned exactly the expected **84** safe tools
- The returned tool set exactly matched
  `tool_modes.get_chatgpt_safe_tools()` — no drift
- None of the 30 `CHATGPT_BLOCKED_TOOLS` were present in the returned
  tool set
- `tools_manifest` was called and returned without error

### Env handling

The private env file used for this rehearsal was created fresh
(gitignored, never committed), used a freshly generated bearer token
(never reused across sessions), and used a restricted gateway agent
token — **never the master key**. The file was deleted immediately
after the rehearsal; no real token, IP, domain, or path from this
rehearsal is recorded in this document or was ever printed in any
report.

## Cleanup

The temporary process was stopped at the end of the rehearsal. The
bound port was confirmed no longer accepting connections afterward. No
process, container, Compose service, or systemd unit was left running
as a result of this rehearsal.

## Non-goals (unchanged by this rehearsal)

- Public ChatGPT/OpenAI connector: still NOT live.
- No Docker Compose or systemd wiring exists for this entrypoint.
- No TLS, reverse proxy, or OAuth is wired for this entrypoint.
- This record does not authorize or imply any future automatic/unattended
  run of this entrypoint — every run remains a deliberate, manual,
  operator-driven action per
  `docs/operations/MCP_PRIVATE_SSE_RUNBOOK.md`.
