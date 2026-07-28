# Private Streamable HTTP MCP Transport — Audit / Design

## Status

Design only. No runtime code in this phase. Nothing in this document
changes what is deployed at `v0.1.59a0`.

## Purpose

Determine whether/how to add the MCP spec's current **Streamable HTTP**
transport alongside the existing stdio and private SSE transports,
without breaking either, and without expanding this repo's exposure
posture. This is a design document, not an implementation — Phase 18B
(if approved) would do the actual code change.

## 1. Current state (verified against this repo, `v0.1.59a0`)

- **stdio remains the default transport.** `examples/mcp_server/server.py`
  runs over stdio when invoked directly; this is the stable, primary
  attach path for local MCP clients (Claude Code, Codex, etc.) and is
  unaffected by anything in this document.
- **Private SSE is available**, not a public connector.
  `scripts/mcp_sse_serve.py` serves the same `examples/mcp_server`
  FastMCP instance over the deprecated HTTP+SSE transport
  (protocol version 2024-11-05), bound to `127.0.0.1:8086` by default,
  behind bearer-token auth (`MCP_HTTP_BEARER_TOKEN`) and Origin
  validation (`MCP_HTTP_ALLOWED_ORIGINS`, Phase 17B). It is a manual,
  operator-run rehearsal tool — no Docker Compose entry, no systemd
  unit, no persistent service.
- **The public ChatGPT/OpenAI connector is NOT live.** No public HTTPS
  endpoint, no OAuth 2.1 resource-server flow, no DCR-issued tokens are
  wired to either transport in this repo's mainline gateway. (A
  separate, already-existing `examples/mcp_client_remote` system with
  its own public tunnel exists but is explicitly out of scope for this
  workstream — see `docs/operations/OPENAI_MCP_ATTACH_PATH_AUDIT.md`,
  Phase 17A.)

## 2. Why Streamable HTTP is relevant now

Per the official MCP specification
(`modelcontextprotocol.io/specification/2025-06-18/basic/transports`,
fetched directly, no blog/forum sources used):

> This replaces the [HTTP+SSE transport] from protocol version
> 2024-11-05. See the backwards compatibility guide below.

The SSE transport this repo currently implements (`mcp_sse_serve.py`) is
the **deprecated** 2024-11-05 transport. The current spec (2025-06-18)
defines a single replacement, "Streamable HTTP", as one of exactly two
standard transports (the other being stdio). The spec's own backwards
compatibility section says a server wanting to support older clients
should "continue to host both the SSE and POST endpoints of the old
transport, alongside the new 'MCP endpoint'" — i.e., **the two are
meant to coexist**, not one replacing the other in-place. This document
treats that coexistence as the design constraint, not an either/or
choice.

## 3. What the official spec requires (direct quotes)

Source: `modelcontextprotocol.io/specification/2025-06-18/basic/transports`,
section "Streamable HTTP" (fetched directly, official source).

- "The server **MUST** provide a single HTTP endpoint path (hereafter
  referred to as the **MCP endpoint**) that supports both POST and GET
  methods."
- Security Warning (verbatim):
  1. "Servers **MUST** validate the `Origin` header on all incoming
     connections to prevent DNS rebinding attacks"
  2. "When running locally, servers **SHOULD** bind only to localhost
     (127.0.0.1) rather than all network interfaces (0.0.0.0)"
  3. "Servers **SHOULD** implement proper authentication for all
     connections"
- Session management: the server **MAY** assign a session ID via an
  `Mcp-Session-Id` response header at initialization; if assigned,
  clients **MUST** echo it on every subsequent request; a client
  **SHOULD** send `DELETE` with that header to explicitly terminate a
  session.
- Protocol version: clients **MUST** send `MCP-Protocol-Version` on all
  requests after initialization; servers **MUST** reply `400 Bad
  Request` to an invalid/unsupported version.

These MUST/SHOULD items — Origin validation, loopback bind, auth — are
**exactly** the three controls already built for the private SSE
entrypoint in Phase 17B, which is why this design treats Streamable
HTTP primarily as a second FastMCP app to wrap with the *same*
middleware stack, not new security work.

## 4. How to mount `FastMCP.streamable_http_app()` (verified by source inspection)

Verified against the locally installed `mcp` package
(`pip show mcp` → version `1.28.0`,
`mcp/server/fastmcp/server.py`, `mcp/server/streamable_http_manager.py`,
`mcp/server/transport_security.py` — no blog/forum sources).

- `FastMCP.streamable_http_app()` (default path `/mcp`, configurable via
  the `streamable_http_path` constructor kwarg, mirroring
  `sse_path`/`message_path`) builds a `Starlette` app with **one**
  route: `Route(self.settings.streamable_http_path, endpoint=<ASGI
  callable>)`. Because the endpoint is registered without a `methods=`
  restriction and is a raw ASGI callable (not a function-based
  endpoint), Starlette forwards **any** HTTP method — GET, POST, DELETE
  — to `StreamableHTTPSessionManager.handle_request()`, matching the
  spec's single-endpoint, multi-method requirement.
- That returned `Starlette` app also carries its own
  `lifespan=lambda app: self.session_manager.run()`. This is different
  from `sse_app()`, which has no custom lifespan. The session manager's
  `run()` context manager owns an `anyio` task group that stateful
  requests are dispatched through; without it entered, `handle_request()`
  raises `RuntimeError("Task group is not initialized...")`. Practically:
  as long as the ASGI server (uvicorn) drives the standard
  startup/shutdown lifespan events through to this `Starlette` app
  unmodified, this is automatic — no manual wiring required. This
  **must be empirically verified** once any wrapping middleware is
  added (see §8), since a middleware that intercepts or swallows
  `scope["type"] == "lifespan"` instead of forwarding it would silently
  break every request.
- `self.settings.transport_security` (a single
  `TransportSecuritySettings` instance) is shared between `sse_app()`
  and `streamable_http_app()` — both pass
  `security_settings=self.settings.transport_security` into their
  respective transport classes. `FastMCP.__init__` auto-populates this
  with loopback-only defaults
  (`allowed_hosts=["127.0.0.1:*","localhost:*","[::1]:*"]`,
  `allowed_origins=["http://127.0.0.1:*","http://localhost:*","http://[::1]:*"]`)
  whenever the `host` constructor kwarg is loopback (the default,
  `"127.0.0.1"`, which `examples/mcp_server/server.py` does not
  override). This means the existing
  `_extend_sdk_transport_security()` helper in `mcp_sse_serve.py`
  — which appends `MCP_HTTP_ALLOWED_ORIGINS` entries onto
  `mcp_instance.settings.transport_security.allowed_origins` — already
  operates on the object that `streamable_http_app()` reads too. No new
  code would be needed there; calling it before
  `.streamable_http_app()` (instead of, or in addition to, before
  `.sse_app()`) would already extend both transports' SDK-level
  allowlist consistently.
- `FastMCP.settings.auth` stays `None` for this repo's private
  entrypoints today because `mcp_sse_serve.py` forces
  `MCP_AUTH_MODE=token` before importing
  `examples.mcp_server.server`, which skips the module-level branch
  that would otherwise build `_auth_settings` (the OAuth path). With
  `auth=None`, `streamable_http_app()` — like `sse_app()` — skips
  FastMCP's own `RequireAuthMiddleware`/DCR wiring entirely and appends
  only the one bare `Route`. This is the same pre-condition the private
  SSE entrypoint already depends on; it would not need to be
  re-established for Streamable HTTP.

### Routes — what actually appears

Based on source inspection, a bare `streamable_http_app()` (auth
unwired, default path) exposes exactly:

- `/mcp` — GET, POST, DELETE (single endpoint, per spec)

Compare to the private SSE entrypoint's two routes:

- `GET /sse` — SSE stream
- `POST /messages/` — message channel (mounted sub-app)

**This has been verified only by reading SDK source, not by starting a
process and inspecting `app.routes` or hitting the port with curl.**
Per this repo's own established practice (`discover_routes()` in
`mcp_sse_serve.py`, and the real-subprocess-based
`mcp_sse_safe_smoke.py`), the actual route list, exact methods
Starlette registers, and exact response codes for method-mismatch cases
**must be empirically discovered** — by building the app and calling
`discover_routes()` against it, and by a real subprocess + curl smoke
test — **before** any implementation is considered complete. Source
inspection is a reliable basis for a design, not a substitute for that
verification step.

## 5. Reusing existing safe-mode enforcement

No new mechanism needed. `require_safe_mode()` in `mcp_sse_serve.py`
already validates `MCP_GATEWAY_TOOL_MODE=mcp_client` and
`MCP_CLIENT_SAFE_MODE=true` by calling into
`examples/mcp_server/tool_modes.get_tool_mode()` /
`is_mcp_client_safe_mode()` — the single source of truth also used by the
stdio and private-SSE paths. **`MCP_CLIENT_SAFE_MODE=true` and
`MCP_GATEWAY_TOOL_MODE=mcp_client` would remain mandatory** for a private
Streamable HTTP entrypoint, exactly as they are for private SSE today —
this is a fail-fast precondition checked before any FastMCP app is
built, not a per-request check, and a Streamable HTTP entrypoint would
call the exact same function before importing
`examples.mcp_server.server`. The 84-safe/30-blocked tool split does
not change per-transport — it is enforced at tool-registration time
(`should_register_tool`), so whichever transport serves the resulting
FastMCP instance serves the same restricted tool set.

## 6. Reusing bearer auth + Origin validation

Both `BearerAuthMiddleware` and `OriginValidationMiddleware` in
`mcp_sse_serve.py` are plain ASGI wrapper classes with no assumption
about what inner app they wrap — they inspect `scope["type"]`,
`scope["headers"]`, and forward everything else (including
`scope["type"] == "lifespan"`) to `self.app(scope, receive, send)`
unchanged. Wrapping a `streamable_http_app()` output the same way
`build_app()` currently wraps `sse_app()` output —
`OriginValidationMiddleware(BearerAuthMiddleware(inner_app, token),
extra_allowed_origins)` — requires no new middleware code, only a
second `build_*_app()`-style function that swaps which FastMCP method
is called. Bearer-token auth and Origin validation would remain
**required** on the Streamable HTTP entrypoint precisely as they are on
the SSE one, checked ahead of any MCP-level routing, independent of
`MCP_AUTH_MODE`.

## 7. Bind default and exposure posture (unchanged from private SSE)

- Default bind host stays **`127.0.0.1`** — a Streamable HTTP
  entrypoint would reuse `resolve_host()` / `validate_bind_host()`
  unchanged; the loopback-only guard and its explicit
  `MCP_HTTP_ALLOW_NON_LOOPBACK=true` escape hatch apply identically.
- **No public exposure.** This document does not add, and does not
  design, a public HTTPS endpoint, a TLS terminator, or any OAuth 2.1
  resource-server flow. A public/OpenAI connector remains a separate,
  explicitly approved design (per the Phase 17A audit), out of scope
  here.
- **No Docker Compose or systemd wiring.** Any Streamable HTTP
  entrypoint would be, like `mcp_sse_serve.py` today, a manually
  started/stopped operator process — not added to
  `docker/docker-compose.yml`, not given a systemd unit, not
  autostarted.
- **No master key.** The gateway credential used by the underlying
  `GatewayClient` (whatever transport serves it) must remain a
  restricted agent token, exactly as documented in the private SSE
  runbook — never the master `API_KEY`.

## 8. Migration strategy: additive, not a cutover

Given the spec's own backwards-compatibility guidance ("continue to
host both... alongside the new... endpoint"), the recommended path if
Phase 18B is approved is **additive**, not a replacement:

1. Keep `scripts/mcp_sse_serve.py` and `scripts/mcp_sse_safe_smoke.py`
   exactly as they are — **the existing SSE smoke test must keep
   passing unmodified** throughout any Streamable HTTP work. SSE is
   deprecated at the protocol-spec level, not removed from this repo;
   existing operator workflows and any client still speaking the
   2024-11-05 transport must not regress.
2. Add a separate `scripts/mcp_streamable_http_serve.py` (new file, new
   default port — do not repurpose `MCP_HTTP_PORT`/8086, to avoid two
   processes silently binding the same port in a rehearsal session)
   that mirrors `mcp_sse_serve.py`'s structure: same
   `require_safe_mode()`, `require_bearer_token()`,
   `validate_bind_host()`, `parse_allowed_origins()`,
   `_force_fastmcp_auth_unwired()`, `_extend_sdk_transport_security()`,
   `BearerAuthMiddleware`, `OriginValidationMiddleware` — swapping only
   `module.mcp.sse_app()` for `module.mcp.streamable_http_app()`.
3. Add a new `scripts/mcp_streamable_http_safe_smoke.py`, modeled on
   `mcp_sse_safe_smoke.py` (real subprocess + real HTTP client, not
   `TestClient`, per the Host-header limitation already documented for
   SSE), asserting: `/mcp` rejects missing/wrong bearer (401), rejects
   disallowed Origin (403), the correct token completes
   `initialize`/`list_tools`/`tools_manifest` per the spec's POST
   request/response framing, 84 safe tools present, 30 blocked tools
   absent, session ID header behavior (if a session ID is assigned)
   matches spec, and no secrets in output.
4. Do not touch stdio (`examples/mcp_server/server.py`'s
   `if __name__ == "__main__":` stdio path) at all.
5. Document both transports side by side in
   `docs/operations/CHATGPT_TOOL_ATTACH.md`, clearly labeled by
   protocol version (2024-11-05 SSE vs. 2025-06-18 Streamable HTTP),
   so an operator picks deliberately rather than by accident.

## 9. Risks, blockers, unknowns

- **Unverified**: exact route list, exact HTTP status codes for
  method-mismatch and malformed-session-ID cases, and whether the
  ASGI lifespan actually propagates correctly through the existing
  middleware stack — all confirmed only by source reading here, not by
  running the code. Must be empirically discovered (real
  `discover_routes()` call + real subprocess/curl smoke) before Phase
  18B implementation is considered done, exactly as this document's own
  contract tests require.
- **Session-ID statefulness**: unlike SSE (which has no session
  concept in this repo's usage), Streamable HTTP's default
  (non-stateless) mode assigns an `Mcp-Session-Id` and expects it
  echoed back. `FastMCP.settings.stateless_http` (default `False`) can
  disable this entirely (`stateless=True` in
  `StreamableHTTPSessionManager`, "creates a completely fresh transport
  for each request with no session tracking"), which would simplify a
  first rehearsal slice at the cost of resumability — an open decision
  for Phase 18B, not resolved here.
- **Protocol version header**: the spec requires clients to send
  `MCP-Protocol-Version` after initialization and servers to reject
  unsupported versions with `400`. Whether the installed SDK enforces
  this automatically or leaves it to the caller is unverified — flagged
  as unverified rather than assumed.
- **Two long-lived processes in one rehearsal session**: running SSE
  and Streamable HTTP smoke tests back to back means two subprocess
  servers on two ports; port collision or leftover processes are an
  operational risk during manual testing, mitigated only by using
  distinct default ports (§8.2) and by each smoke script owning its own
  subprocess lifecycle, same as today.
- **Not addressed here, by design**: OAuth 2.1 resource-server wiring,
  Dynamic Client Registration, and any public/OpenAI-facing endpoint.
  These remain explicitly out of scope, per the red lines for this
  phase and the unresolved gaps already tracked in the Phase 17A audit.
- **No live ChatGPT/OpenAI connector claim is made anywhere in this
  document** — this is a private, local, loopback-bound design
  exercise only.

## 10. Sources used

- `modelcontextprotocol.io/specification/2025-06-18/basic/transports`
  — fetched directly (official spec site), Streamable HTTP section
  quoted verbatim above.
- Locally installed `mcp` Python SDK, version `1.28.0`
  (`pip show mcp`): `mcp/server/fastmcp/server.py`,
  `mcp/server/streamable_http_manager.py`,
  `mcp/server/transport_security.py` — read directly, no blog/forum
  sources.
- This repo: `scripts/mcp_sse_serve.py`,
  `examples/mcp_server/server.py`,
  `docs/operations/OPENAI_MCP_ATTACH_PATH_AUDIT.md` (Phase 17A, for
  the public-connector gap analysis this document deliberately does
  not repeat).

No blog posts, forum threads, or unofficial summaries were used for any
claim in this document.
