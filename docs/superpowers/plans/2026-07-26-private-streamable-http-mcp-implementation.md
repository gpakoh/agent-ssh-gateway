# Phase 18B — Private Streamable HTTP MCP Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the MCP spec's current Streamable HTTP transport (protocol version 2025-06-18) as a second, private, loopback-bound entrypoint for the `examples/mcp_server` FastMCP instance, alongside the existing stdio (default) and private SSE (deprecated-protocol) transports — additive, not a replacement — per the accepted design in `docs/superpowers/specs/2026-07-26-private-streamable-http-mcp-transport.md`. Five small sequential PRs. No runtime code in this plan document itself — plan-doc only.

**Non-goal:** Public ChatGPT/OpenAI connector, OAuth 2.1 resource-server flow, Dynamic Client Registration, TLS termination, reverse proxy, Docker Compose/systemd wiring, or any change to `examples/chatgpt_remote_mcp` (a separate, already-live system — see Phase 17A audit, `docs/operations/OPENAI_MCP_ATTACH_PATH_AUDIT.md`). Removing, deprecating, or code-freezing `scripts/mcp_sse_serve.py` is also out of scope — SSE stays supported.

---

## Global Constraints (red lines, carried from the spec and this task)

- No runtime code changes accepted in **this planning task** — only this plan document is written/committed here.
- Bind default `127.0.0.1` for the new entrypoint, identical loopback guard and `MCP_HTTP_ALLOW_NON_LOOPBACK=true` escape hatch as SSE — reused, not reimplemented.
- Safe mode mandatory: `MCP_GATEWAY_TOOL_MODE=chatgpt`, `MCP_CHATGPT_SAFE_MODE=true` — fail-fast precondition before any FastMCP app is built, exactly as `require_safe_mode()` already enforces for SSE.
- Bearer auth + Origin validation mandatory on the new entrypoint — reuse `BearerAuthMiddleware`/`OriginValidationMiddleware` from `mcp_sse_serve.py` unchanged; no new middleware code.
- No Docker Compose entry, no systemd unit, no autostart — manually started/stopped operator process, same posture as `mcp_sse_serve.py` today.
- No other repository is touched by any slice in this plan.
- No claim anywhere in any PR that a public/OpenAI connector is live.
- Empirical route discovery (real `discover_routes()` call against a real built app, real subprocess + curl) is required **before** PR2 is considered done — source-reading alone (as the Phase 18A spec did) is not sufficient evidence for an implementation PR.
- `scripts/mcp_sse_serve.py` and `scripts/mcp_sse_safe_smoke.py` must keep passing, unmodified in behavior, through every slice below.
- No tagging, no release, no deploy at any point in this plan except PR5's version/changelog bump itself — and PR5 does not deploy.

---

## PR1 — Route discovery spike

**Purpose:** Replace the spec's source-reading-only claims (§4, §9 of the Phase 18A doc) about `streamable_http_app()`'s routes, methods, and lifespan behavior with empirical evidence, before any serving code is written.

**Files to change:**
- New: `scripts/mcp_streamable_http_route_spike.py` — a small, standalone script (not a server, not a smoke test) that:
  1. Builds the `examples/mcp_server` FastMCP instance the same way `mcp_sse_serve.py` does for its own app-building step (reusing `require_safe_mode()`, `_force_fastmcp_auth_unwired()` if applicable — read, don't duplicate).
  2. Calls `.streamable_http_app()` and passes the result to the existing `discover_routes()` helper (import from `mcp_sse_serve.py`, do not fork a second copy).
  3. Prints the discovered route path(s), methods, and — separately — starts the app in-process via `uvicorn` on an ephemeral loopback port for the duration of the script only, and fires real HTTP requests (`GET /mcp` with no body, `POST /mcp` with a minimal malformed body, `DELETE /mcp` with no session header) to record actual status codes for each, then exits. No persistent process left running.
  4. Explicitly checks and prints whether the ASGI lifespan (`streamable_http_app()`'s `lifespan=lambda app: self.session_manager.run()`) fires correctly when the app is served bare (no wrapping middleware yet) — this is the specific unverified risk flagged in the spec §4.

**Tests to add/run:**
- No new pytest suite for this slice — the spike script's own printed output *is* the evidence artifact. Its output must be pasted into the PR description verbatim (route path, methods, status codes for the three probe requests, lifespan-fired: yes/no).
- Run existing full suite (`pytest -q`) to confirm the spike script's imports/reuse of `mcp_sse_serve.py` internals didn't break anything by accident.
- `ruff check .` clean.

**Security invariants:**
- The spike binds loopback only, for the script's own runtime only, and must not require or accept any bearer token/Origin allowlist wiring (it is pre-auth-wiring, by design — PR2 adds auth). It must not be runnable against a non-ephemeral port, and must not be added to any Compose/systemd file.
- No production credentials, no `GATEWAY_API_KEY`, are needed or read by this script — it only exercises `examples/mcp_server`'s own FastMCP instance construction path already used by tests.

**Rollback:** Delete the one new file. Nothing else in the repo depends on it — PR2 depends on its *findings* (documented in its PR description and folded into PR2's own code comments), not on the script itself continuing to exist. The script may be kept or deleted after PR1 merges; this plan does not require it to persist.

**Out of scope:** No auth, no smoke test, no operator-facing script. This slice exists purely to convert the spec's "must be empirically discovered" open item into a documented fact before PR2 writes real serving code against it.

---

## PR2 — Private Streamable HTTP entrypoint

**Purpose:** Ship `scripts/mcp_streamable_http_serve.py`, a private, loopback-bound, auth-required Streamable HTTP entrypoint — structurally a sibling of `mcp_sse_serve.py`, not a rewrite of it.

**Files to change:**
- New: `scripts/mcp_streamable_http_serve.py` — mirrors `mcp_sse_serve.py`'s structure function-for-function where applicable:
  - `require_safe_mode()` — reused import, not reimplemented.
  - `require_bearer_token()` — reused import. New env var name: `MCP_STREAMABLE_HTTP_BEARER_TOKEN` (separate from `MCP_HTTP_BEARER_TOKEN` used by SSE, so the two entrypoints can run with independently rotatable tokens during a rehearsal session where both are up at once) — **decision to confirm in this PR, not assumed**: if the team prefers a single shared bearer token env var for both transports, that is an acceptable, smaller-surface alternative; document whichever is chosen in the PR description and in PR4's docs update.
  - `validate_bind_host()` — reused import, unchanged.
  - `parse_allowed_origins()` — reused import, unchanged (`MCP_HTTP_ALLOWED_ORIGINS` stays the single shared Origin allowlist var per the spec's §6 finding that both transports read the same `transport_security` object).
  - `_force_fastmcp_auth_unwired()` and `_extend_sdk_transport_security()` — reused imports, called before `.streamable_http_app()` instead of (or in addition to) before `.sse_app()`, per spec §4's finding that both read the same `mcp_instance.settings.transport_security` object.
  - New port env var: `MCP_STREAMABLE_HTTP_PORT`, default **`8087`** (explicitly not `8086`, to avoid two processes silently colliding on the same port during a rehearsal session running both transports — per spec §8.2).
  - `BearerAuthMiddleware(OriginValidationMiddleware(inner_app, extra_allowed_origins), token)` wrapping `mcp_instance.streamable_http_app()` — same composition order and same two middleware classes as `build_app()` uses for SSE today, imported not duplicated.
  - Apply PR1's lifespan finding: if PR1 found the bare lifespan does *not* propagate through a naive wrapper, this PR must fix that specifically (e.g. an ASGI wrapper that forwards `scope["type"] == "lifespan"` unchanged before the auth/origin checks) — do not ship silently-broken session handling.
- No changes to `examples/mcp_server/server.py`'s `if __name__ == "__main__":` stdio path — untouched, per red line.
- No changes to `scripts/mcp_sse_serve.py` beyond what's needed to export `discover_routes()`/other helpers for reuse (a plain import should already work if they're module-level functions — check before assuming a refactor is needed).

**Tests to add/run:**
- `tests/test_mcp_streamable_http_serve.py` (new) — unit tests mirroring whatever `tests/test_mcp_sse_serve.py`-equivalent coverage exists for SSE (config parsing, `ConfigError` on missing token/port, safe-mode fail-fast, middleware composition) — same test *shape*, new module under test.
- `pytest -q` full suite green, including unmodified `tests/test_chatgpt_preflight.py` (136 tests, confirmed passing at Phase 18A gate) and whatever SSE-specific tests exist.
- `ruff check .` clean.
- Manual: one `curl` against the running entrypoint confirming a request with no bearer token is rejected (401) and Origin validation still applies, informed by PR1's actual discovered status codes rather than assumed ones.

**Security invariants:**
- Loopback bind by default, same escape hatch (`MCP_HTTP_ALLOW_NON_LOOPBACK=true`) reused, not a new flag with different semantics.
- Bearer auth and Origin validation both required, checked ahead of any MCP-level routing — same ordering guarantee as SSE.
- Safe mode (`MCP_GATEWAY_TOOL_MODE=chatgpt` + `MCP_CHATGPT_SAFE_MODE=true`) checked before the FastMCP app is even constructed — fail fast, not per-request.
- The underlying `GatewayClient` credential remains a restricted agent token — never the master `API_KEY` — identical invariant to the private SSE runbook, re-verified in this PR's own code, not just inherited by assumption.
- No Docker Compose entry, no systemd unit added for this script.

**Rollback:** Delete `scripts/mcp_streamable_http_serve.py` and its test file. No other file in the repo imports from it (SSE and stdio paths are untouched by construction). A revert is a clean two-file removal.

**Out of scope:** OAuth, DCR, public bind, TLS, any change to `examples/chatgpt_remote_mcp`. Session-ID statefulness decision (`stateless_http=True` vs default) must be made explicitly in this PR's description with a one-line justification — not silently defaulted without comment, since the spec flagged it as an open decision (§9).

---

## PR3 — Streamable HTTP smoke

**Purpose:** A real subprocess + real HTTP client smoke test for the new entrypoint, modeled on `scripts/mcp_sse_safe_smoke.py`, not `TestClient` (per the Host-header limitation already documented for SSE — same constraint applies here, the Streamable HTTP transport is still served by the same Starlette/uvicorn stack).

**Files to change:**
- New: `scripts/mcp_streamable_http_safe_smoke.py` — starts `mcp_streamable_http_serve.py` as a real subprocess on its own port (`MCP_STREAMABLE_HTTP_PORT`, default 8087, or an ephemeral override for the smoke run itself to avoid colliding with a manually-running rehearsal instance), then asserts, against the **actual discovered status codes from PR1/PR2** (not assumed ones):
  1. Missing bearer token → rejected (expected 401, confirmed against real behavior).
  2. Wrong/disallowed `Origin` header → rejected (expected 403, confirmed against real behavior).
  3. Correct token + allowed Origin → `initialize` succeeds per the spec's POST request/response framing (`MCP-Protocol-Version` header handling included).
  4. `list_tools`/`tools_manifest` → exactly 84 safe tools present, all 30 blocked tools confirmed **absent** (same split enforced at `should_register_tool` time, transport-independent — this smoke test verifies the split holds for *this* transport specifically, not just re-asserts a known constant).
  5. Session-ID behavior — documented **honestly** as actually observed (per PR2's stateless/stateful decision): if stateful, assert `Mcp-Session-Id` is issued at `initialize` and required on subsequent requests; if `stateless_http=True` was chosen in PR2, assert explicitly that no session ID is issued and say so in the smoke test's own docstring — do not describe a behavior the code doesn't have.
  6. No secrets appear in any response body or subprocess stdout/stderr captured by the smoke test.
- The subprocess must be torn down (SIGTERM, then confirm dead) in a `finally`/fixture-teardown block regardless of assertion outcome — mirroring whatever cleanup discipline `mcp_sse_safe_smoke.py` already has.

**Tests to add/run:**
- The smoke script itself, run directly: `python3 scripts/mcp_streamable_http_safe_smoke.py`.
- If this repo's convention wraps smoke scripts in pytest too (check `mcp_sse_safe_smoke.py`'s own invocation pattern — CLI script vs. pytest-collected — and match it, don't invent a new convention), add the equivalent.
- Confirm `scripts/mcp_sse_safe_smoke.py` still passes unmodified in the same CI run — regression guard for the "SSE must not regress" red line.
- `ruff check .` clean.

**Security invariants:**
- Same as PR2, exercised end-to-end this time instead of by code inspection.
- No token or session ID value is logged verbatim by the smoke test — assert on presence/shape, not by printing the literal value to stdout.

**Rollback:** Delete the one new smoke script. No production code depends on it.

**Out of scope:** Load testing, concurrent-session testing, or anything beyond the single-client happy-path + auth/origin-rejection cases listed above. Multi-session concurrency behavior is not part of this phase's acceptance criteria.

---

## PR4 — Docs/env sync

**Purpose:** Make the new transport discoverable and operable by a human, without re-describing SSE as gone.

**Files to change:**
- `.env.example` — add `MCP_STREAMABLE_HTTP_BEARER_TOKEN` (or the shared-token var name settled in PR2), `MCP_STREAMABLE_HTTP_PORT` (default `8087`), commented, alongside the existing MCP section. **Note found during this planning task:** `MCP_HTTP_BEARER_TOKEN`/`MCP_HTTP_PORT`/`MCP_HTTP_ALLOWED_ORIGINS` (the SSE vars) are **not currently present in `.env.example` at all** — this is a pre-existing gap, not introduced by this plan. PR4 should add the SSE vars too while it's in this file, called out as a fix-in-passing in the PR description, not silently bundled without a note.
- `docs/operations/CHATGPT_TOOL_ATTACH.md` — add a Streamable HTTP section side by side with the existing SSE section, each clearly labeled by protocol version (2024-11-05 SSE vs. 2025-06-18 Streamable HTTP) per spec §8.5, so an operator picks deliberately.
- `docs/operations/MCP_PRIVATE_SSE_RUNBOOK.md` (or a new sibling `MCP_PRIVATE_STREAMABLE_HTTP_RUNBOOK.md`, decide in this PR based on how much genuinely differs — if the operational steps are near-identical apart from script/port names, prefer extending the existing runbook with a Streamable HTTP subsection over forking a whole new document) — must state the port, token var, and smoke command for the new transport, and must **not** remove or water down any existing SSE instructions.
- Any "readiness" doc (`docs/operations/OPENAI_CONNECTOR_READINESS.md` or equivalent — locate exact filename in this PR, don't assume) — update to reflect that a second private transport exists, while preserving the existing "public ChatGPT/OpenAI connector is NOT live [for `examples/mcp_server`]" framing verified at the Phase 18A gate. Do not touch the separate `examples/chatgpt_remote_mcp` readiness framing (per the Phase 16A plan's own note that the two systems are deliberately not reconciled in this workstream).

**Tests to add/run:**
- `python3 scripts/check_public_hygiene.py` and `python3 scripts/check_no_hardcoded_secrets.py` — both must stay green after doc edits (no real tokens/IPs/domains introduced).
- `ruff check .` (docs changes shouldn't affect this, but run for completeness since `.env.example` is sometimes lint-checked for format).
- Full `pytest -q` — doc-only changes shouldn't break tests, but this confirms nothing in `.env.example` parsing (if any test reads it) regressed.

**Security invariants:**
- No real bearer tokens, IPs, domains, or paths in any new doc content — placeholders only, matching the existing SSE docs' style.
- Explicitly reaffirm in every touched doc: no public exposure, no OAuth, added by this phase.

**Rollback:** Doc-only PR — revert is a plain `git revert`, no code coupling.

**Out of scope:** Rewriting existing SSE documentation beyond the minimal "here's the second transport" addition. No renaming of existing runbooks.

---

## PR5 — Release gate

**Purpose:** Version/changelog bookkeeping only — marks that Phase 18B shipped, without deploying anything.

**Files to change:**
- `CHANGELOG.md` — one entry describing the additive Streamable HTTP transport, referencing this plan doc and the Phase 18A spec.
- `app/version.py` (or wherever the version constant lives — confirm exact location in this PR) — bump per this repo's existing versioning convention (check how `a3b3c48 Release pack v0.1.59a0` bumped it, and match that pattern exactly, do not invent a new scheme).

**Tests to add/run:**
- Full `pytest -q`, `ruff check .`, both hygiene/secrets scanners — must all be green before this PR is even opened, since it's the gate for calling the phase done.

**Security invariants:** None beyond what PR1–4 already established — this slice touches no runtime behavior.

**Rollback:** Revert the version bump commit. No runtime coupling.

**Out of scope, explicitly:**
- **No git tag.**
- **No deploy of any kind** — this repo's existing deploy mechanism (if any) is not triggered by this PR, and no slice in this plan invokes it.
- No announcement, no external communication draft.
- Any actual `git tag`/deploy step happens only on a later, separate, explicit human command — not automatically because PR5 merged.

---

## Sequencing and dependencies

PR1 → PR2 → PR3 → PR4 → PR5, strictly sequential — each PR's description must link back to the specific finding from the previous PR that shaped it (PR2 cites PR1's discovered routes/status codes; PR3 cites PR2's actual session-ID decision; PR4 cites PR2/PR3's final env var names). No PR in this sequence should be opened before the previous one has merged and its CI is green, matching this repo's existing small-sequential-PR convention (see the four-PR Phase 16A private-SSE plan this document mirrors in structure).

## Acceptance criteria for "Phase 18B done"

- `scripts/mcp_streamable_http_serve.py` exists, defaults to loopback bind, mandatory safe mode, mandatory bearer auth, mandatory Origin validation.
- `scripts/mcp_streamable_http_safe_smoke.py` passes against a real subprocess, asserting real (not assumed) status codes, 84 safe / 30 blocked tools, and an honestly-documented session-ID behavior.
- `scripts/mcp_sse_serve.py` and `scripts/mcp_sse_safe_smoke.py` still pass, unmodified in behavior.
- `examples/mcp_server/server.py`'s stdio path is untouched.
- No Compose/systemd/autostart wiring exists for the new transport.
- No claim, anywhere in the shipped docs, that a public/OpenAI connector is live.
- `.env.example`, the operator runbook, and the readiness doc all describe both private transports side by side, correctly labeled by protocol version.
