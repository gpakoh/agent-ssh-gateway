# Public MCP Connector — Risk Review

Audit-only document. No runtime code changes were made to produce
this, and nothing here authorizes exposing anything publicly.

**The public ChatGPT/OpenAI connector is still NOT live for this
project.** stdio remains the default transport; the private SSE
(`scripts/mcp_sse_serve.py`) and private Streamable HTTP
(`scripts/mcp_streamable_http_serve.py`) entrypoints are both
loopback-only rehearsal tools. This document reviews what stands
between that current state and any real public exposure — it is the
next logical question after `docs/operations/OPENAI_MCP_ATTACH_PATH_AUDIT.md`
(which established *what the protocol/OpenAI require*) and
`docs/operations/OPENAI_CONNECTOR_READINESS.md` (which tracks
*implementation status*): this document asks *what could go wrong at
the perimeter* if either private entrypoint were ever exposed, and
what has to exist before that is a responsible decision rather than an
accident.

## 1. What is already ready

- **Safe tool mode is mandatory and fail-fast.** `MCP_GATEWAY_TOOL_MODE=chatgpt`
  + `MCP_CHATGPT_SAFE_MODE=true` are checked before either private
  entrypoint builds its FastMCP app at all (`require_safe_mode()`,
  reused unchanged by both `mcp_sse_serve.py` and
  `mcp_streamable_http_serve.py`). 84 safe tools, 30 blocked
  (`CHATGPT_BLOCKED_TOOLS`) — write, Docker, and agent-launch tools are
  excluded, verified by both smoke scripts (11/11 each) and by stdio.
- **Bearer-token auth exists on both HTTP entrypoints**, independent of
  `MCP_AUTH_MODE` (`BearerAuthMiddleware`, constant-time comparison via
  `hmac.compare_digest`) — missing/wrong token is rejected before any
  MCP-level routing.
- **Origin validation exists on both HTTP entrypoints**
  (`OriginValidationMiddleware`), satisfying the MCP spec's DNS-rebinding
  MUST: no `Origin` header allowed (CLI/local clients), loopback
  allowed by default, extra origins explicitly allowlisted via env,
  anything else rejected with `403` — checked ahead of the bearer
  check.
- **Loopback-only bind by default**, with a deliberately named, fail-fast-guarded
  override (`MCP_HTTP_ALLOW_NON_LOOPBACK` / `MCP_STREAMABLE_HTTP_ALLOW_NON_LOOPBACK`)
  that neither script sets itself.
- **FastMCP's own OAuth auth is deliberately kept unwired** for both
  private entrypoints (`_force_fastmcp_auth_unwired()` forces
  `MCP_AUTH_MODE=token` before import) — a considered choice, not an
  oversight, so the entrypoints' own bearer/Origin layer is the sole
  enforcement point today.
- **A real OAuth 2.1 provider already exists in this codebase**:
  `examples/mcp_server/oauth_provider.py`'s `GatewayOAuthProvider`
  implements Dynamic Client Registration (RFC 7591), PKCE S256
  verification, authorization-code exchange, refresh-token rotation,
  and token revocation. It is wired today only for the stdio
  `MCP_AUTH_MODE=oauth` path, not to either HTTP entrypoint.
- **Token persistence partially exists**: `examples/mcp_server/token_store.py`'s
  `TokenStore` persists hashed token entries (never raw tokens) to a
  JSON file with `fcntl` file locking and atomic writes, and supports
  revocation; `GatewayOAuthProvider.load_tokens()` restores non-revoked
  hashed tokens from it at startup. This is real persistence for
  issued/hashed tokens — but in-flight OAuth state (pending
  authorization codes, PKCE challenges mid-flow) lives only in the
  provider's in-memory dicts and would not survive a process restart
  mid-authorization. `OPENAI_CONNECTOR_READINESS.md`'s older "in-memory
  token store, tokens lost on restart" framing is therefore only
  partially accurate today and should be read alongside this
  correction.
- **General rate-limiting infrastructure exists in the main gateway**
  (`app/security.py`) for the REST API — it is not wired to either MCP
  HTTP entrypoint, which are standalone scripts separate from the main
  FastAPI app.
- **Audit logging exists** (structured `AuditEvent` + JSONL, redaction)
  for gateway-level events; MCP transport-level auth rejections
  (401/403 from the bearer/Origin middleware) are printed to the
  entrypoint's own stderr (sanitized — no token or full Origin value),
  not yet emitted as structured `AuditEvent`s.
- **Operator approval flow exists** for gateway actor access
  (pending → Allow/Deny via Telegram) but is keyed on agent-token
  identity/source IP from the gateway's own auth layer — it has no
  concept of "this specific MCP HTTP connection" today, since neither
  entrypoint's bearer/Origin layer feeds into it.

## 2. What is NOT ready

- **No TLS termination anywhere in either private entrypoint.** Both
  serve plain HTTP. A bearer token or OAuth access token sent to
  either today, over any network path beyond `127.0.0.1` itself, would
  be sent in cleartext.
- **No reverse proxy / public DNS design.** Nothing in this repo
  configures a public hostname, certificate, or proxy in front of
  either MCP entrypoint. (This repo's Nginx config for the main
  gateway API is a separate, already-existing pattern — not extended
  to MCP.)
- **OAuth 2.1 is not wired to any HTTP transport.** Both entrypoints
  force `MCP_AUTH_MODE=token`, unconditionally bypassing
  `GatewayOAuthProvider` entirely for HTTP. Wiring it would need real
  design work: which of the existing scopes apply, how `resource`
  parameters map to gateway agent-token scopes, and how token
  revocation propagates to an already-open MCP session.
- **No OAuth 2.0 Protected Resource Metadata** (`/.well-known/oauth-protected-resource`,
  RFC 9728) **and no Authorization Server Metadata**
  (`/.well-known/oauth-authorization-server`, RFC 8414) on either
  entrypoint — both are explicit MCP-spec MUSTs once authorization is
  supported (per the Phase 17A audit), and neither exists today.
- **No `WWW-Authenticate` header on 401** pointing at protected-resource
  metadata — both entrypoints return a bare `401`/`{"error": "unauthorized"}`
  JSON body today.
- **No rate limiting or abuse throttling on either MCP entrypoint.** A
  single static bearer token, if it ever reached a network path beyond
  loopback, has no per-token or per-IP request budget, no
  connection-count cap, and no automatic lockout after repeated
  auth failures.
- **No structured audit events for MCP-transport auth failures.**
  Rejections are visible only in the entrypoint's own process stderr,
  not in the gateway's `AuditEvent` stream, not correlated with
  request IDs, and not visible to the existing Telegram
  operator-notification path.
- **No incident rollback plan for a public exposure.** Today's
  "rollback" for either private entrypoint is "stop the process" —
  adequate for a rehearsal tool nobody else can reach, not adequate
  once a real network path and a real credential exist: a compromised
  token or an abused public endpoint needs a documented, fast,
  operator-executable revocation + shutoff procedure, which does not
  exist yet.
- **No operator-approval gate scoped to MCP connections specifically.**
  The existing pending/Allow/Deny flow is gateway-actor-based, not
  MCP-session-based; a new MCP client presenting a valid bearer token
  today reaches the tool set immediately, with no equivalent "first
  connection needs a human click" step that the gateway's own REST
  path already has for new actors.
- **No persistent-service / process-supervision story.** Neither
  entrypoint is a systemd unit or Compose service (by design, per every
  prior phase's red lines) — but a real public connector, unlike a
  rehearsal tool, needs to actually stay up. That tension is
  unresolved: making it a supervised service is itself a decision with
  its own blast radius, out of scope for this review to make.

## 3. What is required before any public exposure

The following are treated as a checklist, not a suggestion list — an
honest "not ready" against any one of these means "not ready for
public exposure," full stop:

1. **TLS termination** in front of whichever entrypoint is exposed —
   either the entrypoint gains native TLS support or (more
   realistically, reusing this repo's existing Nginx pattern from the
   main gateway) it sits behind a reverse proxy that terminates TLS
   and forwards to the loopback-bound MCP process. Never expose either
   entrypoint's raw HTTP port to any non-loopback network path.
2. **Public DNS / reverse-proxy design**, deliberately chosen, not
   improvised: a real hostname, a real certificate (Let's Encrypt /
   existing CA), and an explicit statement of which single route
   (`/mcp` for Streamable HTTP, or `/sse` + `/messages` for legacy SSE)
   is proxied — nothing else on the host should become reachable as a
   side effect.
3. **An explicit OAuth 2.1 / DCR / PKCE compatibility decision** — not
   "OAuth would be nice" but a concrete choice: wire the existing
   `GatewayOAuthProvider` to the HTTP transport (reusing its DCR + PKCE
   S256 + refresh-token support rather than writing a new stack), decide
   which scopes a ChatGPT/OpenAI client is granted by default, and
   decide whether DCR is open (any client can self-register, per the
   MCP spec's SHOULD) or gated behind an operator step.
4. **Protected Resource Metadata + Authorization Server Metadata**
   (`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`)
   actually implemented and served, plus `WWW-Authenticate` on `401`
   pointing at the former — these are MUSTs per the MCP authorization
   spec once authorization is supported, not optional polish.
5. **Token storage, rotation, and revocation**, verified end-to-end —
   not just "the code exists" (it partially does, see §1) but a tested
   path: issue a token, use it, revoke it, confirm the revoked token is
   rejected immediately, confirm a server restart does not silently
   resurrect a revoked token or lose a valid one.
6. **Rate limiting / abuse throttling** on the exposed endpoint — at
   minimum a per-token and per-IP request budget and a lockout after
   repeated auth failures, so a leaked or guessed token cannot be used
   for unbounded probing or resource exhaustion.
7. **Audit events for connector auth failures**, wired into the
   existing `AuditEvent`/Telegram pipeline — a real public endpoint
   being probed or attacked needs to be *visible to an operator*, not
   just present in a process's stderr on a machine nobody is watching.
8. **A written, rehearsed incident rollback plan** — concretely: how to
   revoke a specific token in under a minute, how to pull the public
   route out of the reverse proxy without restarting unrelated
   services, and who is notified when either happens. This should be
   rehearsed once, the same way the private SSE rehearsal was
   documented in `docs/operations/MCP_PRIVATE_SSE_REHEARSAL.md`, before
   it is ever needed for real.
9. **An operator-approval gate** for new MCP connections, not just new
   gateway actors — mirroring the existing pending/Allow/Deny pattern,
   scoped to "a previously-unseen OAuth client/token attached to the
   public MCP endpoint," with the same Telegram-notified human-in-the-loop
   step the REST path already has.

## 4. Can a static bearer token be used publicly?

**No — not as the final, production public connector.**

A static bearer token is what both private entrypoints use today, and
it is adequate for exactly what it is: a single operator, on their own
machine, holding a token they generated themselves, talking to a
process only they can reach. It fails every one of the following the
moment the network boundary changes to "reachable from the internet":

- No token expiry, rotation, or per-client scoping — it is one shared
  secret, not a credential lifecycle.
- No revocation story beyond "regenerate it and restart the process
  manually" — no per-client blast-radius containment.
- No standard metadata (`/.well-known/oauth-protected-resource`, etc.)
  for a client like ChatGPT to discover or validate it against — per
  the Phase 17A audit, OpenAI's own guidance points at a full OAuth 2.1
  flow for an "authenticated connector," not a bare static token.
- No abuse-throttling story if the single token leaks.

This is not a gap introduced by carelessness — it is the correct,
minimal design for a private rehearsal tool, and it must not be
carried forward unchanged into a public one.

## 5. Temporary lab-only options and their red lines

A **lab-only, time-boxed, single-operator tunnel** (e.g. a short-lived
tunnel tool pointed at one of the existing private entrypoints, for a
single manual test session) is the only kind of "more than loopback"
exposure this review considers acceptable to even discuss before the
full checklist in §3 is met — and only under every one of these
conditions simultaneously:

- **Explicit, single-session operator approval** — started deliberately
  for one rehearsal, not left running.
- **Short TTL, enforced structurally** — the tunnel process itself
  should have a hard time limit or be manually torn down within the
  same session it was started; it must not survive a reboot or be
  something a second person could stumble into later.
- **Strict allowlist** — the tunnel's own access control (if any)
  restricted to a known, small set of source IPs or an equivalently
  strict mechanism; never "anyone with the URL."
- **The existing bearer + Origin layer stays on** — a lab tunnel is
  additive exposure, not a reason to relax the entrypoint's own
  checks.
- **No production/live-connector claim anywhere** — no doc, commit
  message, or operator communication describes this as "the ChatGPT
  connector is live." It is a lab exercise, stated as such.
- **Immediate teardown after the session**, with a note of exactly
  when it was up and for what purpose, mirroring the discipline already
  used for the private SSE rehearsal record.

**Red lines that apply regardless of "lab-only" framing:**

- Never do this without TLS on the tunnel's own public-facing hop —
  "it's temporary" does not make plaintext credential transport
  acceptable.
- Never reuse the master gateway API key as the tunnel-facing
  credential.
- Never disable safe mode "just for this test."
- Never leave the tunnel process running unattended, even briefly,
  once the session ends.
- Never treat a successful lab tunnel session as evidence that Option C
  (below) is complete — it demonstrates reachability, nothing about
  the checklist in §3.

## 6. Recommended implementation options

### Option A — Remain private-only

Keep exactly today's posture: stdio default, both private entrypoints
loopback-only, no tunnel, no public work started. Zero new risk,
zero new capability.

### Option B — Lab-only public tunnel, explicitly non-production

A single, short-TTL, strictly-allowlisted tunnel session per §5, used
to empirically verify that a real external client (not just `curl`
from the same host) can actually complete an MCP handshake through
whatever proxy/tunnel mechanism is chosen — without claiming this is
the production connector, and without skipping any item in §3 before
ever making that claim. This is a reachability experiment, not a
readiness milestone.

### Option C — Real public OAuth connector

The full §3 checklist, in order: TLS + reverse proxy design → OAuth 2.1
wired (reusing `GatewayOAuthProvider`) + metadata endpoints → verified
token lifecycle → rate limiting → audit wiring → rehearsed rollback →
MCP-connection-scoped operator approval → only then, an explicit,
separate "go live" decision.

### Recommendation

**Start with Option A (no change) as the default state; if and only if
there is a concrete, named reason to test real external reachability,
do a single Option B lab session under every red line in §5 — and treat
Option C as a multi-phase project of its own, not a next sprint.** The
single highest-leverage next step, if this workstream continues at
all, is not TLS or a domain: it is deciding, deliberately, whether
`GatewayOAuthProvider` gets wired to the Streamable HTTP entrypoint
first (§3 item 3) — because every other item in the checklist (metadata
endpoints, token lifecycle testing, connector-scoped approval) depends
on that decision being made first, and none of them are worth starting
before it is.

## 7. Explicit non-goals

- This document does not implement, start, or schedule any item in §3.
- It does not choose a reverse-proxy product, tunnel tool, TLS
  certificate authority, or hosting location.
- It does not claim any public connector is live — none is.
- It does not authorize any lab-only tunnel session described in §5;
  that remains a separate, explicit, per-session operator decision.
- It does not weaken safe mode, `CHATGPT_BLOCKED_TOOLS`, bearer auth,
  or Origin validation in any way, for any entrypoint.

## 8. Sources

- `docs/operations/OPENAI_MCP_ATTACH_PATH_AUDIT.md` (Phase 17A) — official
  MCP-spec and OpenAI-doc requirements this review builds on.
- `docs/operations/OPENAI_CONNECTOR_READINESS.md` — current
  implementation-status tracking; §1 of this document corrects its
  token-store framing based on direct source inspection.
- `docs/superpowers/specs/2026-07-26-private-streamable-http-mcp-transport.md`
  (Phase 18A) — Streamable HTTP transport design.
- `scripts/mcp_streamable_http_serve.py`, `scripts/mcp_sse_serve.py` —
  read directly for current auth/Origin/bind behavior.
- `examples/mcp_server/oauth_provider.py`, `examples/mcp_server/token_store.py` —
  read directly for current OAuth/token-persistence capability.
- No `examples/mcp_server/auth/` directory exists in this repo at the
  time of this review.
