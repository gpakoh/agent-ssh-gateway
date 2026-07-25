# Phase 16A — Private SSE MCP Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire a private-network-only HTTP/SSE transport for the `examples/mcp_server` FastMCP instance, per the accepted design spec (`docs/superpowers/specs/2026-07-25-private-http-mcp-transport.md`), without any public exposure, OAuth, TLS, or reverse-proxy work. Four small sequential PRs. No code in this plan — plan-doc only.

**Non-goal:** Public ChatGPT/OpenAI connector for `examples/mcp_server`. Not a replacement for, migration of, or comment on `examples/chatgpt_remote_mcp` (see "Related existing system" below).

---

## Related existing system (context, not in scope)

During planning, host inspection found a **separate, already-live, publicly-tunneled** MCP stack, unrelated in codebase to `examples/mcp_server`:

- `examples/chatgpt_remote_mcp/server.py` — different file, different git history (back to v0.1.23-alpha), running as `agent-ssh-gateway-mcp.service` (active since 2026-06-19), bound `MCP_HOST=0.0.0.0:8788`.
- `agent-ssh-gateway-mcp-tunnel.service` (active since 2026-07-04) — `lt --port 8788 --subdomain <redacted-subdomain>`, described as "Public tunnel ... ChatGPT connector".
- Fleet adapters (`agent-mcp-context7/docker/gitea/github/postgres.service`) also active, most bound `0.0.0.0`.
- Config: `MCP_AUTH_MODE=oauth`, `MCP_DEFAULT_ACCESS_PROFILE=full`, `MCP_SCOPE_ENFORCEMENT=audit` (non-blocking per `MCP_OPERATOR_RUNBOOK.md`), `MCP_GATEWAY_WRITE_MODE=handoff`, `MCP_PUBLIC_URL=https://<redacted-domain>`. No `MCP_CHATGPT_SAFE_MODE` set.
- Has its own runbooks: `MCP_PUBLIC_ENDPOINT_RUNBOOK.md`, `TUNNEL_RUNBOOK.md`, `MCP_FLEET_RUNBOOK.md`, `MCP_TOKEN_LEDGER.md`, `MCP_OPERATOR_RUNBOOK.md`.

This system is **mature, documented, and operated** — not treated here as an incident. It is called out because it materially contradicts the "Public ChatGPT/OpenAI connector is NOT live" framing used across `OPENAI_CONNECTOR_READINESS.md` and the Phase 16 design spec, which is accurate only for the `examples/mcp_server` codebase. Reconciling the two tracks (deprecate/replace vs. intentionally parallel; whether `MCP_SCOPE_ENFORCEMENT` should move from `audit` to `enforce` on the public one) is an operator/architect decision, out of scope for this plan. This plan proceeds strictly within `examples/mcp_server`, private-bind-only, as originally scoped.

---

## Global Constraints (red lines, unchanged from spec)

- Bind default `127.0.0.1`; public bind (`0.0.0.0` or any non-loopback) requires explicit `MCP_HTTP_BIND_PUBLIC=true` AND is out of scope for this phase regardless of the flag — the flag existing is a fail-fast guard, not a feature to exercise here.
- Safe mode mandatory: `MCP_GATEWAY_TOOL_MODE=chatgpt`, `MCP_CHATGPT_SAFE_MODE=true`, `MCP_ACCESS_PROFILE=chatgpt_safe`. Startup must fail-fast if any is missing/wrong.
- No OAuth app registration, no TLS termination, no reverse proxy, no Cloudflare/tunnel — all explicitly deferred (matches spec's "Explicit non-goals").
- `tg-bot-service` is never touched by any slice in this plan.
- Master key must never be usable as the MCP runtime credential (existing invariant, re-verified per slice).
- stdio smoke (`scripts/mcp_stdio_safe_smoke.py`) and SSE smoke are kept fully separate — no shared script, no shared subprocess/env mutation.

---

## Key technical findings from spec + code reading (must shape PR1)

1. **FastMCP `sse_app()` mount mechanics** (from `mcp.server.fastmcp.server.FastMCP.sse_app`, package v1.28.1):
   - The Starlette route for the SSE GET endpoint is registered at `self.settings.sse_path` (constructor default `"/sse"`), **not** at whatever string is passed to `mount_path`.
   - `mount_path` only feeds `_normalize_path(mount_path, message_path)`, which computes the message-POST-back URL embedded in the SSE `endpoint` event sent to clients. It does **not** change the actual registered route path.
   - Consequence: the spec's pseudocode (`mcp.sse_app(mount_path="/mcp/sse")` run directly as the ASGI app) would **not** produce a working endpoint at `/mcp/sse` — the GET route stays at `/sse` while the message endpoint text becomes malformed (`/mcp/sse` + `/messages/` normalization is not the intended composition).
   - **Two viable approaches, to be resolved empirically in PR1 before merge:**
     - (a) Construct the FastMCP instance (or a dedicated HTTP-only instance) with `sse_path="/mcp/sse"` and `message_path="/mcp/messages/"` passed directly to the constructor, then serve `mcp.sse_app()` (no `mount_path` override needed since paths are already correct), or
     - (b) Keep default `sse_path="/sse"`, wrap the returned app in an outer Starlette app with `Mount("/mcp", app=mcp.sse_app(mount_path="/mcp"))`, relying on Starlette's `Mount` to strip the `/mcp` prefix so `/mcp/sse` resolves, while `mount_path="/mcp"` makes the embedded message-endpoint text correct.
   - PR1 must include a manual `curl -N http://127.0.0.1:8086/mcp/sse` (or whatever path is chosen) verification step before considering the slice done — do not trust the pseudocode path string without checking.

2. **Token-mode auth is not currently wired to FastMCP transport — this is the primary security gap PR1 must close.**
   - In `examples/mcp_server/server.py`, when `MCP_AUTH_MODE=token`: `_auth_provider = GatewayOAuthProvider()` is created and the static token is registered in `_auth_provider._tokens`, but `_auth_settings` is **never assigned** in the token-mode branch (it stays `None`, set only in the `oauth` branch).
   - `mcp = FastMCP(..., auth=_auth_settings, auth_server_provider=_auth_provider if _auth_settings else None)` — since `_auth_settings is None` in token mode, both `auth` and `auth_server_provider` are `None`.
   - Verified via `tests/test_mcp_server.py`: `test_token_mode_initializes_provider` only asserts `_auth_provider is not None` and that `_auth_provider.verify_access_token(...)` works directly — it never asserts `mcp.settings.auth is not None`. Only the oauth-mode test (`test_oauth_mode_configures_auth`) asserts that.
   - Practical effect verified by reading `FastMCP.sse_app()` source: when `self._token_verifier` is `None` (which follows from `auth`/`auth_server_provider` being `None`), the "auth disabled" branch is taken — the SSE and message routes are registered **with no bearer-token check at all**.
   - **Consequence if PR1 ships `mcp.sse_app()` unmodified in token mode: any process able to reach the bound host:port gets full unauthenticated access to all 84 safe-mode tools — bind-to-loopback becomes the *only* enforcement layer, not defense-in-depth as the spec implies ("MCP-level auth: token mode").**
   - PR1 must wire real enforcement. Two options for PR1 to choose between (decision, not code, in this plan):
     - (a) Pass `token_verifier=` (the FastMCP constructor accepts a dedicated `TokenVerifier` protocol param, separate and lighter-weight than `auth_server_provider`/DCR) — implement a minimal verifier that checks the bearer token against `MCP_PUBLIC_TOKEN` via `_auth_provider.verify_access_token()`, and pass a minimal `AuthSettings` (or check if `token_verifier` alone is sufficient without full `AuthSettings`/DCR — needs a quick spike against `mcp` 1.28.1 source before deciding).
     - (b) Reuse `_auth_settings`/`auth_server_provider=_auth_provider` in token mode too (currently oauth-only), accepting the heavier DCR/OAuth route surface even for simple static-token use.
   - Recommendation for PR1: (a) — matches "no OAuth for this phase" red line more closely, smaller surface.

3. **Env var contract** — reuse exactly what already exists, add only HTTP-specific vars:
   - Existing (unchanged): `GATEWAY_URL`, `GATEWAY_API_KEY`/`GATEWAY_AGENT_TOKEN`, `MCP_GATEWAY_TOOL_MODE=chatgpt`, `MCP_CHATGPT_SAFE_MODE=true`, `MCP_ACCESS_PROFILE=chatgpt_safe`, `MCP_AUTH_MODE=token`, `MCP_PUBLIC_TOKEN`.
   - New: `MCP_HTTP_HOST` (default `127.0.0.1`), `MCP_HTTP_PORT` (default `8086`), `MCP_HTTP_BIND_PUBLIC` (default unset/false — explicit ack gate, not intended to be exercised this phase).
   - No new secret-shaped variables; new vars are non-secret (host/port/bool).

4. **Blocked-tools verification** — no new source of truth. Reuse `tool_modes.CHATGPT_BLOCKED_TOOLS` / `get_chatgpt_safe_tools()` and `examples/mcp_server/chatgpt.safe.manifest.expected.json` (84 safe / 30 blocked / must_include / must_exclude) exactly as `scripts/chatgpt_tool_attach_smoke.py` (REST) and `scripts/mcp_stdio_safe_smoke.py` (stdio) already do. SSE smoke calls the `tools_manifest` tool over the wire and diffs against the same expected JSON.

5. **Avoiding accidental `0.0.0.0` bind** — explicit guard function, unit-testable without network: if resolved host not in `{"127.0.0.1", "localhost", "::1"}` and `MCP_HTTP_BIND_PUBLIC` is not exactly `"true"`, exit nonzero before `uvicorn.run()` is ever called. This must be a plain function (e.g. `validate_bind_host(host: str, allow_public: bool) -> None`) so PR1's tests can call it directly with zero network/process overhead.

6. **Keeping stdio and SSE smoke separate** — mirror the existing pattern in `mcp_stdio_safe_smoke.py`, which builds an isolated `env` dict per-subprocess rather than mutating `os.environ`. New SSE smoke does the same, with its own `MCP_HTTP_*` keys. No shared module, no shared fixture, no modification to `mcp_stdio_safe_smoke.py` in any slice below.

---

## Slice Breakdown

### PR1 — `scripts/mcp_sse_serve.py` entrypoint (private bind only, safe mode forced, real auth wired)

**Files:**
- Create: `scripts/mcp_sse_serve.py` — entrypoint. Imports the existing `mcp` object from `examples/mcp_server/server.py` (reuse tool registration, do not duplicate), resolves/validates bind host, wires token verification (finding #2 above), builds the Starlette app (finding #1 above — empirical path check required), calls `uvicorn.run(...)`.
- Create (if finding #2 resolves to option (a)): a small `TokenVerifier` implementation — either inline in `mcp_sse_serve.py` or a new `examples/mcp_server/http_token_verifier.py` if it needs to be unit-tested independently of the entrypoint's `if __name__ == "__main__"` guard.
- Create: `tests/test_mcp_sse_serve.py` — unit tests, no network, no subprocess.
- No changes to `examples/mcp_server/server.py` tool registrations themselves; only additive wiring for HTTP auth if finding #2 requires touching `_auth_settings`/`auth_server_provider` construction (import-and-extend, not rewrite).

**Tests to add/run:**
- `validate_bind_host("127.0.0.1", allow_public=False)` → passes (no exception/exit).
- `validate_bind_host("0.0.0.0", allow_public=False)` → fails fast (raises / returns nonzero).
- `validate_bind_host("0.0.0.0", allow_public=True)` → passes (flag explicitly set — even though this phase never exercises it in practice).
- Missing `MCP_CHATGPT_SAFE_MODE` / wrong `MCP_GATEWAY_TOOL_MODE` / wrong `MCP_ACCESS_PROFILE` → preflight fails fast (reuse `scripts/mcp_chatgpt_runtime_preflight.py` logic or call it directly as a pre-start check).
- Missing `GATEWAY_API_KEY`/`GATEWAY_AGENT_TOKEN` → fails fast.
- **Regression test guarding finding #2**: after wiring, assert the constructed app/mcp instance has a non-`None` token verifier / `auth` configuration when `MCP_AUTH_MODE=token` — this is the test that would have caught the current gap; it must fail before the fix and pass after.
- Run existing suite untouched: `pytest tests/test_mcp_server.py tests/test_chatgpt_preflight.py -q` (must still pass — no regressions to oauth/token mode stdio behavior).
- Run: `python3 scripts/check_public_hygiene.py`, `python3 scripts/check_no_hardcoded_secrets.py` on the new files.

**Security invariants:**
- Bind defaults to `127.0.0.1`; public bind requires explicit double opt-in (env flag) and is not exercised in this phase's smoke/CI.
- Safe mode fail-fast (no partial startup with unsafe tool set exposed even briefly).
- `MCP_AUTH_MODE=token` must result in actual bearer-token enforcement at the transport layer (closes finding #2) — the manifest of 84 safe tools must be unreachable without a valid token once this ships.
- Master key never read/used by this entrypoint (verify no `settings.master_key` or equivalent import).

**Rollback:**
- Purely additive script; rollback = don't run it / delete the file. No compose, no systemd unit, no existing file behavior changes for stdio users.

**Explicitly out of scope:**
- Docker Compose profile / systemd unit for this entrypoint (deferred to a later slice if ever needed — not in PR2-4 either, per the spec's "Docker / compose" section being marked design-only).
- TLS, reverse proxy, OAuth DCR, public bind exercised, StreamableHTTP transport.
- Any change to `examples/chatgpt_remote_mcp/*` or its systemd services.
- Any change to `tg-bot-service`.

---

### PR2 — SSE smoke script (private bind only)

**Files:**
- Create: `scripts/mcp_http_safe_smoke.py` — spawns `scripts/mcp_sse_serve.py` as a subprocess bound to `127.0.0.1` on an ephemeral/fixed test port, connects via `mcp.client.sse.sse_client`, runs initialize → list_tools → call_tool(`health`) → call_tool(`tools_manifest`), diffs manifest against `chatgpt.safe.manifest.expected.json`, then tears the subprocess down.
- Create: `tests/test_mcp_http_safe_smoke.py` (thin pytest wrapper invoking the smoke script's internal functions, not just a subprocess shell-out, so failures are attributable) — or mark the script itself runnable both standalone and importable, matching `mcp_stdio_safe_smoke.py`'s existing pattern.

**Tests to add/run:**
- Happy path: 84 safe tools, 0 blocked, required tools present (`health`, `tools_manifest`) — same assertions as existing stdio/REST smokes, now over SSE.
- **Negative auth test** (directly exercises the PR1 fix): connecting without a bearer token, or with a wrong token, must be rejected (connection refused / 401 / MCP-level error) — not silently succeed. This is the regression guard for finding #2 at the black-box level, complementing PR1's white-box unit test.
- Bind-safety check: assert the smoke harness itself only ever starts the server with `MCP_HTTP_HOST=127.0.0.1`; never runs the public-bind path.
- Run: `python3 scripts/check_public_hygiene.py`, `python3 scripts/check_no_hardcoded_secrets.py`.
- Run full existing suite to confirm no cross-contamination: `pytest -m "not host_smoke" -q`.

**Security invariants:**
- Smoke must exit nonzero (loud failure) on: unsafe manifest (any blocked tool reachable), missing auth enforcement (unauthenticated call succeeds), non-loopback bind detected in the spawned process's actual listening address.
- Smoke never touches the real gateway's production port/session; it should be runnable fully offline against a local `docker compose --profile demo` sshd or a mocked gateway client, matching how `mcp_stdio_safe_smoke.py` avoids requiring `TEST_SSH_HOST`.

**Rollback:**
- CI/local-only artifact, zero production impact. Rollback = delete script.

**Explicitly out of scope:**
- Load/concurrency/perf testing.
- StreamableHTTP transport smoke (SSE only, per spec's transport choice).
- Wiring this smoke into `.github/workflows/ci.yml` as a required gate (can be proposed in PR4's release-gate checklist, but adding it to the actual CI workflow file is a separate, explicit decision — not silently bundled here).

---

### PR3 — Docs + env example + operator runbook updates

**Files:**
- Modify: `examples/mcp_server/chatgpt.safe.env.example` — add `MCP_HTTP_HOST=127.0.0.1`, `MCP_HTTP_PORT=8086`, `MCP_HTTP_BIND_PUBLIC=false` (placeholders/defaults, no real values).
- Modify: `examples/mcp_server/README.md` — document the new entrypoint under "Quick start" as an alternative to stdio, cross-reference PR1/PR2 scripts, keep the existing "Excluded by design" section intact and add HTTP-specific exclusions (no public bind, no TLS).
- Create: `docs/operations/MCP_HTTP_PRIVATE_ATTACH_RUNBOOK.md` — operator steps: env setup, starting `scripts/mcp_sse_serve.py`, running `scripts/mcp_http_safe_smoke.py`, expected output, rollback (kill process), explicit non-goals restated.
- Modify: `docs/operations/OPENAI_CONNECTOR_READINESS.md` — update "Option B" status from "Recommended next step" to "Implemented (private network only)" once PR1+PR2 are merged and verified; do **not** change the "Explicit non-goals" section (public connector still not live for this codebase).
- Add a short cross-reference note (2-3 lines) in `OPENAI_CONNECTOR_READINESS.md` pointing at the existing `examples/chatgpt_remote_mcp` system and its runbooks, so future readers don't repeat this plan's discovery from scratch — phrased as a pointer, not an audit of that system.

**Tests to add/run:**
- Docs-only — no pytest changes expected beyond what PR1/PR2 already added.
- `python3 scripts/check_public_hygiene.py` — must stay green (no real IPs/domains beyond placeholders in the new runbook).
- `python3 scripts/check_no_hardcoded_secrets.py` — must stay green.
- Manual read-through: confirm the new runbook and env example contain zero references to the real domain/subdomain of the existing live system, or any other real value from it.

**Security invariants:**
- No real topology/IPs/domains/tokens in any new doc.
- Explicit non-goals restated verbatim in the new runbook (no public connector, no OAuth, no TLS) so operators reading only this file still get the constraint.

**Rollback:**
- Revert the doc commit; zero runtime impact.

**Explicitly out of scope:**
- Rewriting or auditing `examples/chatgpt_remote_mcp`'s own docs.
- Architecture diagrams beyond ASCII (matches existing spec style).
- Marketing-style top-level `README.md` changes.

---

### PR4 — Release gate checklist

**Files:**
- Create: `docs/operations/MCP_HTTP_TRANSPORT_RELEASE_GATE.md` — a checklist mirroring the exact verification sequence used for the v0.1.55a0 release pack in this project's history (version bump, changelog entry, `ruff check .`, `mypy app` [and note: extend to `examples/mcp_server/` and `scripts/` explicitly if PR1 added files there — do not silently rely on the narrower CI-configured `mypy app` scope], `pytest -m "not host_smoke"`, `check_public_hygiene.py`, `check_no_hardcoded_secrets.py`, plus the two new smokes from PR1/PR2), commit, push, CI watch, explicit sign-off items.

**Gate items specific to this phase (beyond the generic release checklist):**
- [ ] `MCP_HTTP_HOST` confirmed `127.0.0.1` in the actual running process (`ss -tlnp` or equivalent), not just in the env file, before marking ready.
- [ ] SSE smoke passed including the negative-auth case (PR2).
- [ ] Changelog entry explicitly states: private SSE MCP transport implemented, bind private-only, safe mode enforced, auth enforced at transport layer (finding #2 closed) — and explicitly does **not** claim public connector readiness.
- [ ] No tag/deploy claims until an operator (not this plan) explicitly signs off — same pattern as the v0.1.55a0 gate in this project's history.

**Tests:** N/A — process document. References exact commands, does not introduce new pytest files.

**Security invariants:**
- Checklist itself must not contain real IPs/domains/tokens (hygiene-scan it before commit, same as PR3).

**Rollback:**
- Process doc only; rollback = don't follow the checklist further / halt at first failing gate item.

**Explicitly out of scope:**
- Actually tagging or deploying anything (a future, separately-approved action).
- Any decision about the `examples/chatgpt_remote_mcp` fleet's `MCP_SCOPE_ENFORCEMENT`/`MCP_DEFAULT_ACCESS_PROFILE` posture — flagged in this plan's context section, decision belongs to the operator/architect.

---

## Sequencing

PR1 → PR2 → PR3 → PR4, strictly sequential (each depends on the previous slice existing and passing). PR3 can be drafted in parallel with PR1/PR2 but should not be merged claiming "implemented" until PR1+PR2 are actually merged and green.

## Risks / Open Decisions Carried Into PR1

1. **Blocker-class**: token-mode auth wiring gap (finding #2) — must be resolved as part of PR1, not deferred, or this phase produces an HTTP endpoint with no real access control beyond loopback bind.
2. **Needs empirical verification, not just spec pseudocode**: `sse_app()` mount path composition (finding #1) — resolve via manual curl check before PR1 is considered done.
3. **Decision needed, not blocking this plan**: how `examples/mcp_server` HTTP transport relates to the already-live `examples/chatgpt_remote_mcp` fleet — surfaced to operator/architect, not resolved here.
4. **Minor, unrelated pre-existing tech debt** (noted for completeness, not this phase's problem): local `.venv` has stale `redis==5.0.0` vs. CI's fresh-install `redis==8.0.1`, causing local-only mypy noise in `app/distributed_lock.py`; unrelated to MCP transport work.
