# Public MCP OAuth — Architecture Decision

## Status

Design/decision only. No runtime code in this document or the phase
that produced it. Nothing here authorizes public exposure — the
public ChatGPT/OpenAI connector for this project is still **not live**,
and this document does not change that.

## Purpose

`docs/operations/PUBLIC_MCP_CONNECTOR_RISK_REVIEW.md` (Phase 19A)
established that a full OAuth 2.1 wiring decision is prerequisite item
#3 on the path to any public exposure, and that every other
prerequisite (metadata endpoints, token lifecycle, connector-scoped
approval) depends on it. This document makes that one decision:
**reuse the existing `GatewayOAuthProvider`, or build a separate OAuth
layer dedicated to a public connector?** It does not implement the
answer.

## 1. What `GatewayOAuthProvider` already implements

Read directly from `examples/mcp_server/oauth_provider.py` and
`examples/mcp_server/token_store.py` (not assumed from memory of an
earlier phase):

- **Dynamic Client Registration** (`register_client()`) — accepts
  `redirect_uris`, `client_name`, `token_endpoint_auth_method`, and a
  requested `scope` string; validates scopes against `SUPPORTED_SCOPES`
  (10 scopes, e.g. `mcp:read`, `mcp:execute`, `mcp:docker`,
  `mcp:admin`); rejects registration with no `redirect_uris`. Tested
  (`tests/test_oauth_provider.py`).
- **PKCE, S256 only** — `_generate_code_challenge()` /
  `_verify_pkce()`, constant-time comparison
  (`secrets.compare_digest`), verifier length validated (43–128 chars
  per RFC 7636). Tested directly, including the "wrong verifier
  rejected" and "too short" cases.
- **Authorization-code flow** — `create_authorization_code()` /
  `exchange_code_for_token()`: exact redirect-URI match enforced,
  single-use codes (`used` flag checked and set), 300-second code
  expiry, `client_id` mismatch rejected. Tested, including "code reuse
  rejected."
- **Refresh-token rotation** — `refresh_access_token()`: 2-hour access
  tokens, 7-day refresh tokens, a new access token minted per refresh
  call. Tested.
- **Revocation, in-memory and persisted** — `revoke_token()` /
  `revoke_client_token()` delete the in-memory entry and, if a
  `TokenStore` is attached, mark the corresponding persisted entry
  `revoked_at`. Tested end-to-end
  (`tests/test_server_token_integration.py`).
- **Hashed token storage** — every token is looked up and stored as
  `sha256:<hex>` (`hash_token()`), never the raw value, both in the
  in-memory dict and in `TokenStore`'s JSON file. `TokenStore` itself
  uses `fcntl`-locked, `tempfile` + `os.replace` atomic writes, and
  refuses to operate against a world-writable store file. Tested
  (`tests/test_token_store.py`).
- **`resource` parameter accepted at the authorize step** —
  `authorize()` reads a `resource` param (RFC 8707 resource indicator)
  if the client sends one and forwards it into the consent URL — but
  see §2 for what this does *not* mean.
- **Already wired for stdio today** — `examples/mcp_server/server.py`
  builds `GatewayOAuthProvider()` and an `AuthSettings` (issuer URL,
  resource server URL, `ClientRegistrationOptions`) whenever
  `MCP_AUTH_MODE=oauth` (the module's own default), and passes both
  into `FastMCP(...)`. This is real, exercised code — not a stub —
  it is simply never reached by either private HTTP entrypoint, which
  force `MCP_AUTH_MODE=token` before import (see §2).

## 2. The most important finding: the metadata endpoints are not missing from the SDK — they're just unwired

This is the single fact that most changes the shape of "what work is
required," so it is stated first and separately from the gap list.

Read directly from the locally installed `mcp` SDK (v1.28.0,
`mcp/server/fastmcp/server.py` and `mcp/server/auth/routes.py`), not
assumed:

- `FastMCP.sse_app()` **and** `FastMCP.streamable_http_app()` already
  contain the exact code path that wires OAuth Authorization Server
  Metadata (`create_auth_routes()`, registering
  `/.well-known/oauth-authorization-server`) and OAuth 2.0 Protected
  Resource Metadata (`create_protected_resource_routes()`, registering
  `/.well-known/oauth-protected-resource<resource-path>`, RFC 9728
  §3.1) — **conditional only on `self.settings.auth` being set and
  `self.settings.auth.resource_server_url` being configured.**
- `RequireAuthMiddleware` (`mcp/server/auth/middleware/bearer_auth.py`),
  also already part of the SDK and already used for the stdio oauth
  path, already sends a `WWW-Authenticate: Bearer ...` header
  pointing at the protected-resource metadata URL on a `401` — this is
  not something that needs to be written.
- Both private entrypoints (`scripts/mcp_sse_serve.py`,
  `scripts/mcp_streamable_http_serve.py`) call
  `_force_fastmcp_auth_unwired()` before importing
  `examples.mcp_server.server`, which forces `MCP_AUTH_MODE=token`.
  This causes `server.py`'s module-level `if MCP_AUTH_MODE == "oauth":`
  branch — the one that builds `_auth_settings` and wires it into
  `FastMCP(..., auth=_auth_settings, ...)` — to simply not run. With
  `auth=None`, `self.settings.auth` is falsy, and every one of the
  routes described above is skipped entirely by `sse_app()` /
  `streamable_http_app()`'s own `if self.settings.auth:` guard.

**Conclusion:** wiring OAuth into either private HTTP entrypoint for
real is not "build Protected Resource Metadata / Authorization Server
Metadata / `WWW-Authenticate` support" — it is "stop deliberately
suppressing the wiring that already exists and is already exercised
today for stdio." That reframes most of Phase 19A's checklist item #4
from new engineering into a configuration/verification task, *if* the
decision in §6 is to reuse `GatewayOAuthProvider`.

## 3. What is genuinely not implemented or not enforced

These are real gaps, independent of the reframing above:

- **Persisted token expiry is not enforced on load.** `TokenStore`'s
  `StoredTokenEntry` has an `expires_at` field, but
  `GatewayOAuthProvider.load_tokens()` calls
  `register_hashed_token(token_hash=..., profile=..., scopes=...)` —
  which does not accept or apply an expiry at all, and always sets
  `expires_at=float("inf")` in-memory. A token entry marked expired in
  the persistent store would still be treated as valid indefinitely
  once loaded, **unless it is also explicitly revoked**. Revocation
  works; time-based expiry of *persisted* tokens does not. This must
  be fixed before any real token-lifecycle claim can be made for a
  public connector.
- **Resource indicator (`resource`/audience) is accepted but not
  enforced.** `authorize()` reads and forwards a `resource` parameter,
  but no code path in `exchange_code_for_token()`,
  `verify_access_token()`, or `load_access_token()` checks that an
  access token's audience matches the resource it is being presented
  to. For a single MCP server this is low-risk today, but it means the
  provider does not currently implement RFC 8707 resource-indicator
  enforcement, only parameter pass-through.
- **No rate limiting on any OAuth endpoint** (`/oauth/authorize`,
  the token exchange path, DCR). Both the general gateway's own
  rate-limiting (`app/security.py`) and this provider are silent on
  this — the gap already identified generically in the Phase 19A
  review applies specifically here too: authorization-code and
  refresh-token exchange are exactly the endpoints an abuse-throttling
  gap would matter most for.
- **`AuthSettings`' `issuer_url` / `resource_server_url` currently
  default to a specific placeholder value hardcoded in
  `examples/mcp_server/server.py`** (overridable via
  `MCP_ISSUER_URL` / `MCP_RESOURCE_URL`, but with a same-project
  internal-looking default baked into source). Before any public
  reuse, these must be explicitly set via env to the real public
  hostname that will actually be used — the current default must never
  be allowed to leak into a publicly-served metadata document by
  accident.
- **No MCP-connection-scoped operator approval.** Same finding as the
  Phase 19A review: a newly-registered OAuth client completing DCR +
  the authorization-code flow gets a working access token immediately,
  with no human-in-the-loop step equivalent to the gateway's own
  pending/Allow/Deny actor flow.
- **No audit events for OAuth-specific failures** (failed PKCE
  verification, expired/reused authorization codes, revoked-token
  presentation) — these currently surface only as raised `ValueError`s
  inside the provider, not as structured, Telegram-visible events.
- **A second, unrelated OAuth-ish implementation already exists in
  this repo, unreconciled.** `examples/chatgpt_remote_mcp/server.py`
  implements its own separate public-path detection
  (`_is_oauth_public_path`, recognizing `/.well-known/oauth-authorization-server`,
  `/oauth/authorize`, `/oauth/token`, `/oauth/register`) and its own
  `MCP_AUTH_MODE` handling, entirely independent of
  `GatewayOAuthProvider` — confirmed by direct inspection (no import of
  `oauth_provider` anywhere in that file). It is not wired into
  Docker Compose and not referenced by any file under
  `docs/operations/`, i.e. not part of this repo's documented,
  maintained connector story — consistent with the Phase 17A audit's
  note that this separate system is deliberately unreconciled with
  this workstream. It is mentioned here only as existing prior art for
  "a separate provider," not as something this decision extends,
  fixes, or endorses.

## 4. Can `GatewayOAuthProvider` be safely connected to the Streamable HTTP entrypoint?

**Mechanically, with moderate, well-scoped changes — yes. Safely,
*for public exposure*, not without closing every gap in §3 first.**

The mechanical path (still not authorized by this document):

1. Give `scripts/mcp_streamable_http_serve.py` a way to *not* call
   `_force_fastmcp_auth_unwired()` — today that override is
   unconditional, by design, because the entrypoint's own
   `BearerAuthMiddleware` was meant to be the sole enforcement layer.
   A public-connector variant would instead need to build
   `AuthSettings` + pass `GatewayOAuthProvider()` into `FastMCP(...)`,
   the same way `server.py`'s existing oauth branch already does for
   stdio — reusing that code path, not duplicating it.
2. This does not remove the entrypoint's own `BearerAuthMiddleware` /
   `OriginValidationMiddleware` — Origin validation stays regardless
   of auth mode (it is a transport-level DNS-rebinding guard,
   orthogonal to OAuth), and the question of whether the static-bearer
   layer is *also* kept as a defense-in-depth layer alongside OAuth,
   or retired once OAuth is live, is an open sub-decision for whoever
   implements this — not resolved here.
3. Every gap in §3 needs to be closed first: enforce persisted-token
   expiry, decide on resource/audience enforcement, add rate limiting
   at the OAuth endpoints, set real (non-default) issuer/resource URLs
   explicitly, add the audit events, and design the connector-scoped
   approval gate.

## 5. Should a public connector use a separate token store / scopes?

**Same `TokenStore` mechanism, a separate logical namespace within
it — not a second implementation.**

- **Reuse the `TokenStore` class** (hashed storage, atomic writes,
  file locking, revocation) — it is transport-agnostic and already
  correct for this purpose. Do not write a second persistence
  mechanism.
- **Do not reuse the stdio path's issued tokens or client
  registrations for a public connector.** A separate
  `MCP_TOKEN_STORE_FILE` path (already configurable via env, per
  `token_store.py`'s `_default_store_path()`) keeps a public
  connector's client/token population physically separate from
  local/stdio operator tokens, so revoking or auditing one never
  touches the other, and a compromise of one store's file does not
  expose the other's hashes.
- **Scopes**: reuse `SUPPORTED_SCOPES` and the existing
  `ACCESS_PROFILES` bundle mechanism (`tool_scopes.py`) rather than
  inventing a parallel scope taxonomy — but a public connector's
  *default* scope grant on successful DCR + authorization should be
  the most restrictive profile that still satisfies the chatgpt-safe
  tool set (mirroring `MCP_CHATGPT_SAFE_MODE`'s own restrictiveness
  default), not `DEFAULT_SCOPES` as currently hardcoded for the
  general case.

## 6. Safe mode and access-control integration

- **Safe mode stays a precondition independent of auth mode.**
  `require_safe_mode()` already runs before any FastMCP app is built,
  regardless of which auth path is chosen — this does not change
  whether OAuth is wired or not, and must not be made conditional on
  it.
- **Access-control integration is a new gate, not a reuse of the
  existing one as-is.** The gateway's pending/Allow/Deny flow is keyed
  on gateway-actor identity (agent token + source IP) from the REST
  API's own auth layer — an OAuth-authenticated MCP client has a
  different identity shape (OAuth `client_id` + token hash). The
  correct integration is: on first successful DCR + authorization-code
  exchange for a previously-unseen `client_id`, emit a
  pending-approval event through the *same* Telegram
  operator-notification channel already used for gateway actors,
  and refuse to hand out a working scope set (or hand out a
  zero-capability token) until an operator approves — reusing the
  notification/approval *channel*, not assuming the existing
  actor-keyed state machine applies unchanged.

## 7. Audit events needed

At minimum, as structured events (not just raised exceptions or stderr
prints), feeding the same pipeline the Phase 19A review already calls
for generically:

- DCR registration (new `client_id`, redirect URI, requested scopes)
- Authorization-code issuance and exchange (success/failure, reason on
  failure — expired, reused, redirect mismatch, PKCE failure)
- Access-token issuance, refresh, and revocation
- Any request presenting a revoked or expired token
- Any request presenting a token whose scope does not cover the
  invoked tool (should already deny at the tool layer, but the OAuth
  layer should log distinctly from tool-layer scope denial for
  correlation)

## 8. Rollback / kill-switch requirements

- **Per-client revocation must be a single, fast, operator-executable
  action** — `revoke_client_token()` already exists and works; what is
  missing is an operator-facing command (CLI, mirroring
  `scripts/mcp-token` patterns already used for static tokens) that
  revokes *all* tokens for a given OAuth `client_id` in one call, not
  just one token at a time.
- **A process-level kill switch**: since neither entrypoint is a
  supervised service today, "kill switch" currently just means "stop
  the process" — adequate for rehearsal, not for a real connector
  (see the Phase 19A review's "no persistent-service story" gap,
  unresolved here too).
- **The `TokenStore` file itself is the durable source of truth for
  revocation** — a rollback plan must include "delete/rotate the
  entire store file" as a last-resort, total-revocation option,
  distinct from per-client revocation.

## 9. Recommended implementation options

### Option A — Do not build a public connector now

No OAuth wiring change to either private entrypoint. Matches the
Phase 19A review's own recommendation to remain private-only absent a
concrete, named reason to proceed.

### Option B — Lab-only public exposure using the existing static bearer, short TTL

Explicitly **not recommended as an OAuth substitute** — this option
does not touch OAuth at all, it is the Phase 19A review's own
lab-tunnel exception (short TTL, strict allowlist, no production
claim). Restated here only for completeness against the four options
this document was asked to weigh: it does not answer the "reuse vs.
separate provider" question because it deliberately avoids OAuth
entirely.

### Option C — Real OAuth public connector, reusing `GatewayOAuthProvider`

Per §4–§6: unwire `_force_fastmcp_auth_unwired()` for a dedicated
public-connector entrypoint variant, wire `AuthSettings` +
`GatewayOAuthProvider` (the same code path already proven for stdio),
close every gap in §3, add the connector-scoped approval gate from
§6, and the audit events from §7. Reuses ~500 lines of already-tested
provider code and the SDK's own already-existing metadata-endpoint
wiring, rather than duplicating either.

### Option D — Separate, dedicated OAuth provider for the connector

Would mean writing a second `OAuthAuthorizationServerProvider`-compatible
class from scratch (or adapting the already-existing, independent
`examples/chatgpt_remote_mcp/server.py` pattern, which itself does not
reuse `GatewayOAuthProvider`) — duplicating PKCE verification, DCR,
token hashing, and revocation logic that already exists, tested, in
`GatewayOAuthProvider`. The only scenario where this would be
justified is if a public connector's trust model needs to diverge
*structurally* from the stdio/private model (e.g., a genuinely
different scope taxonomy, a different client-registration policy, or
regulatory/compliance separation requiring provably independent code
paths) — no such requirement has been identified in this review.

### Recommendation

**Option A now; if this workstream ever proceeds to a real public
connector, Option C (reuse `GatewayOAuthProvider`), not Option D.**
The existing provider already implements the hard parts correctly
(PKCE, DCR, hashed persisted tokens, tested revocation), and the SDK
itself already contains the metadata-endpoint wiring that a from-scratch
provider (Option D) would otherwise have to reinvent. Option D would
only be defensible if a structural (not just configurational) reason
to diverge from the existing provider's trust model is identified —
none exists today. The single highest-leverage next step, *if and only
if* the decision is ever made to proceed past Option A, is closing the
persisted-token-expiry gap (§3) — it is the one item on this list that
is not a design/policy decision but a straightforward correctness bug
in already-existing code, and it should be fixed before any of the
other prerequisites are attempted, regardless of which option is
eventually pursued.

## 10. Non-goals

- This document does not implement any change to
  `_force_fastmcp_auth_unwired()`, `GatewayOAuthProvider`,
  `TokenStore`, or either private entrypoint.
- It does not authorize public exposure, a reverse proxy, TLS
  termination, or any tag/deploy action.
- It does not analyze, fix, or extend `examples/chatgpt_remote_mcp/` —
  mentioned only as existing, unreconciled prior art.
- It does not claim any public connector is live — none is.
- No real token, IP, domain, or file path appears in this document.

## 11. Sources

- `examples/mcp_server/oauth_provider.py`, `examples/mcp_server/token_store.py`,
  `examples/mcp_server/server.py`, `examples/mcp_server/tool_scopes.py` —
  read directly.
- `tests/test_oauth_provider.py`, `tests/test_mcp_auth.py`,
  `tests/test_server_token_integration.py`, `tests/test_token_store.py`,
  `tests/test_mcp_token_cli.py` — read directly for existing coverage.
- Locally installed `mcp` Python SDK (v1.28.0):
  `mcp/server/fastmcp/server.py`, `mcp/server/auth/routes.py`,
  `mcp/server/auth/middleware/bearer_auth.py` — read directly, not
  assumed from an earlier phase's notes.
- `docs/operations/PUBLIC_MCP_CONNECTOR_RISK_REVIEW.md` (Phase 19A) —
  the prerequisite checklist this document narrows to a single
  decision.
- `docs/operations/OPENAI_MCP_ATTACH_PATH_AUDIT.md` (Phase 17A) — MCP
  spec / OpenAI official requirements for OAuth 2.1, DCR, PKCE,
  Protected Resource Metadata, Authorization Server Metadata.
