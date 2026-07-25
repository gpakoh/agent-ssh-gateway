# Private SSE MCP Transport — Operator Runbook

Manual-run package for `scripts/mcp_sse_serve.py`. This is a **private,
local-rehearsal** entrypoint — not a public ChatGPT/OpenAI connector, not
a persistent service, and not wired into any Docker Compose stack. It is
started by an operator, on demand, on a single loopback port, and stopped
when the rehearsal is done.

## Prerequisites

- [ ] Gateway running, `/health` returns `status: ok`, `ready: true`
- [ ] A restricted **agent token** already issued (see
      `docs/operations/CHATGPT_ATTACH_CHECKLIST.md` step 1) — **never the
      master key**
- [ ] `examples/mcp_server/chatgpt.sse.env.example` present in the repo
- [ ] Python environment with project dependencies installed (`mcp`,
      `starlette`, `uvicorn`, `httpx`)

## 1. Generate a private bearer token

This token protects `/sse` and `/messages` on the SSE entrypoint. It is
**independent** of the gateway agent token — generate a fresh one, do not
reuse an existing gateway/agent token for this:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Save the output somewhere private. Do not paste it into chat logs, issue
trackers, or commit messages.

## 2. Create the gitignored env file

```bash
cp examples/mcp_server/chatgpt.sse.env.example examples/mcp_server/chatgpt.sse.env
```

Edit `examples/mcp_server/chatgpt.sse.env` with your values:

- `GATEWAY_URL`, `GATEWAY_API_KEY` / `GATEWAY_AGENT_TOKEN` — the restricted
  agent token from Prerequisites, **not** the master key
- `MCP_HTTP_BEARER_TOKEN` — the private token generated in step 1
- Leave `MCP_GATEWAY_TOOL_MODE=chatgpt`, `MCP_CHATGPT_SAFE_MODE=true`,
  `MCP_HTTP_HOST=127.0.0.1`, `MCP_HTTP_PORT=8086` as shipped in the
  template — do not change these for a rehearsal run

`examples/mcp_server/chatgpt.sse.env` is gitignored. **Never commit it.**

## 3. Validate the env file (recommended)

```bash
python3 scripts/mcp_sse_env_check.py examples/mcp_server/chatgpt.sse.env
```

Static check only — does not start the server or connect to anything.
Confirms safe mode is on, the bind host is loopback,
`MCP_HTTP_ALLOW_NON_LOOPBACK` is not enabled, and the bearer/agent token
placeholders were actually replaced. Never prints token values.

## 4. Start manually on 127.0.0.1

```bash
set -a && source examples/mcp_server/chatgpt.sse.env && set +a
python3 scripts/mcp_sse_serve.py
```

Expected startup line on stderr:

```
mcp_sse_serve: starting on 127.0.0.1:8086 (bearer auth enabled)
```

If it instead refuses to start, that is the safety guard working as
intended — see "What not to do" below for the two most common causes
(missing safe mode, non-loopback host).

This runs in the foreground. Use a second terminal (or `tmux`/`screen`)
for the next steps, or background it deliberately (`&`) if you understand
the process lifecycle.

## 5. Run the smoke test

In a second terminal, with the same env sourced (or the relevant vars
exported):

```bash
python3 scripts/mcp_sse_safe_smoke.py
```

Expected: `11 passed, 0 failed` — routes `/sse` and `/messages` reject
missing/wrong bearer tokens (401), the correct token opens the stream and
completes MCP `initialize`/`list_tools`/`tools_manifest`, 84 safe tools
present, 30 blocked tools absent, bearer token never printed.

Note: `mcp_sse_safe_smoke.py` starts its **own** subprocess instance of
`scripts/mcp_sse_serve.py` on a separate ephemeral port — it does not
attach to the instance you started in step 3. Both can run
simultaneously without conflict; the smoke test is a self-contained
verification, not a client for your manual session.

## 6. Stop the process

Foreground: `Ctrl-C` in the terminal running `mcp_sse_serve.py`.

Backgrounded: find and stop it directly — this is a plain Python
process, not a managed service:

```bash
pkill -f "scripts/mcp_sse_serve.py"
```

Confirm the port is free:

```bash
python3 -c "import socket; s=socket.socket(); s.settimeout(1); print('still listening' if s.connect_ex(('127.0.0.1', 8086)) == 0 else 'stopped')"
```

## 7. Rollback

There is nothing to roll back at the infrastructure level:

- This entrypoint is not in any Docker Compose file — stopping the
  process is the entire rollback.
- No systemd unit, no container, no reverse proxy, no DNS entry was
  created by this runbook.
- If you revoked or rotated the gateway agent token as part of testing,
  reissue or restore it via the normal agent-token flow
  (`docs/operations/CHATGPT_ATTACH_CHECKLIST.md`).
- Delete `examples/mcp_server/chatgpt.sse.env` if you no longer need the
  private rehearsal env (it is gitignored either way, so this is
  housekeeping, not a security requirement).

## What not to do

- **Do not set `MCP_HTTP_HOST` to `0.0.0.0` or any non-loopback
  address.** The entrypoint refuses this by default; the only override
  is `MCP_HTTP_ALLOW_NON_LOOPBACK=true`, and that override is
  **forbidden** outside a reviewed, temporary, isolated lab environment
  that you fully control and that has no path to the public internet.
  There is no TLS, no reverse proxy, and no OAuth on this entrypoint —
  a non-loopback bind turns a private rehearsal tool into an
  unencrypted, single-bearer-token-protected SSH-capable endpoint on
  your network.
- **Do not add this entrypoint to any `docker-compose*.yml` file**, and
  do not create a systemd unit for it. It is designed to be started and
  stopped by a human, for a bounded rehearsal session — not to run
  unattended or restart automatically.
- **Do not use the master key** as `GATEWAY_API_KEY` /
  `GATEWAY_AGENT_TOKEN` for this entrypoint. Use a restricted agent
  token only, same as the stdio attach flow.
- **Do not run with safe mode off.** `MCP_GATEWAY_TOOL_MODE=chatgpt` and
  `MCP_CHATGPT_SAFE_MODE=true` are mandatory — the entrypoint fails fast
  if either is missing or wrong. Do not work around this by editing the
  script; if you need a different tool set, that is a separate,
  deliberate decision outside the scope of this runbook.
- **Do not reuse `MCP_HTTP_BEARER_TOKEN` across sessions or share it
  outside your own terminal.** Generate a fresh one per rehearsal.
- **Do not paste the bearer token, the agent token, or any gateway
  credential into chat, issues, commit messages, or this runbook.**
