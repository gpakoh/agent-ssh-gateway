# OpenAI/ChatGPT MCP Attach Path — Audit

Audit-only document. No code was changed to produce this. Determines what is
*actually* required, per official sources, to connect ChatGPT or the OpenAI
API to a custom MCP server — as opposed to assumption or blog-derived
folklore — and compares it against this repository's current capability.

**Public ChatGPT/OpenAI connector is still NOT live for this project.** This
document does not change that. Nothing here authorizes exposing anything
publicly.

## Sources used (official only)

| # | Source | What it covers | Fetch status |
|---|--------|-----------------|--------------|
| 1 | [MCP Specification — Transports (2025-06-18)](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) | Canonical transport definitions | Fetched directly, full text |
| 2 | [MCP Specification — Authorization (2025-06-18)](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) | Canonical auth/OAuth flow | Fetched directly, full text |
| 3 | [OpenAI — MCP and Connectors guide](https://developers.openai.com/api/docs/guides/tools-connectors-mcp) | Responses API remote MCP tool requirements | Fetched directly, full text |
| 4 | [OpenAI Help Center — Developer Mode and MCP apps in ChatGPT](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt) | ChatGPT UI Developer Mode / custom connector availability | **Direct fetch blocked (HTTP 403).** Content below is from a web search summary that quotes this page; treat as official-sourced but not independently re-verified by this audit. Flagged explicitly wherever used. |

No blog posts, forum threads, or third-party tutorials were used as a source
of truth for any requirement stated below. Where a claim below traces only to
source #4 (the one blocked from direct re-verification), it is marked
**[unverified-direct]**.

## 1. Official requirements

### 1.1 Transports (source #1, MCP spec — authoritative for the protocol itself)

> "The protocol currently defines two standard transport mechanisms for
> client-server communication: 1. stdio ... 2. Streamable HTTP ... Clients
> SHOULD support stdio whenever possible."

> "This replaces the HTTP+SSE transport from protocol version 2024-11-05."
> (i.e. the older `GET /sse` + separate `POST /messages` pattern is
> **deprecated** in the current spec, replaced by a single unified endpoint
> supporting both GET and POST, called the "MCP endpoint".)

Backwards compatibility is explicitly permitted: servers wanting to support
older clients "should continue to host both the SSE and POST endpoints of
the old transport, alongside the new MCP endpoint."

### 1.2 Transports per OpenAI (source #3, Responses API)

> "The Responses API works with remote MCP servers that support either the
> Streamable HTTP or the HTTP/SSE transport protocols."

So OpenAI's Responses API explicitly still accepts the **old** HTTP/SSE
transport (matching what this repo currently implements), alongside the
newer Streamable HTTP. This is narrower than "SSE is required" and narrower
than "only Streamable HTTP is accepted" — both are accepted per this source.

### 1.3 ChatGPT UI custom connectors (source #4, **[unverified-direct]**)

- Custom MCP connectors require **ChatGPT Developer Mode**, itself gated to
  Plus/Pro/Team/Enterprise/Edu plans (not the free tier).
- Enabled via Settings → Connectors → Advanced → Developer Mode.
- Stated to support "Server-Sent Events (SSE) and streaming HTTP protocols,
  with optional OAuth authentication."

This is consistent with sources #1–#3 but is the one item in this audit not
independently re-verified by a direct fetch of the primary page — flagged
per this document's own red line against guessed claims.

### 1.4 Public HTTPS endpoint requirement

- **Not explicitly stated as a hard requirement** in source #3's normative
  text — no sentence says "the server_url MUST be public" or "MUST use
  HTTPS." All examples shown use `https://` URLs, and OAuth 2.1 (source #2)
  independently mandates HTTPS for authorization server endpoints and
  redirect URIs regardless of the MCP server's own transport endpoint.
- **Practical conclusion (not a direct quote):** OpenAI's infrastructure
  (ChatGPT servers, Responses API backend) must reach the MCP server over
  the network to invoke it. A server bound to `127.0.0.1` on an operator's
  own machine, as this repo's rehearsal entrypoint is, is by construction
  unreachable from OpenAI's infrastructure. This is a logical consequence of
  network topology, not a documented "MUST be public" requirement — the
  distinction matters because it means the requirement is really "reachable
  from OpenAI's network," which a private tunnel or reverse proxy could
  satisfy without a literal public IP, though such setups have their own
  distinct risk profile not addressed by this audit.

### 1.5 Authentication (sources #1, #2, #3)

- MCP spec (source #2): **"Authorization is OPTIONAL for MCP
  implementations."** When an HTTP-based implementation *does* support
  authorization, it "SHOULD conform to this specification" — i.e. the full
  OAuth 2.1 resource-server flow: OAuth 2.0 Protected Resource Metadata
  (RFC 9728) is a **MUST** for the MCP server, `WWW-Authenticate` on 401
  pointing to that metadata is a **MUST**, OAuth 2.0 Authorization Server
  Metadata (RFC 8414) is a **MUST** be provided by the authorization server
  and used by the client, Dynamic Client Registration (RFC 7591) is a
  **SHOULD** (recommended, not mandatory) for both authorization servers and
  clients, and PKCE is a **MUST** for the client side of the flow.
- MCP spec transport security note (source #1): "Servers SHOULD implement
  proper authentication for all connections" — phrased as a SHOULD at the
  transport-security level, distinct from the full OAuth flow described
  above being conditionally required only "when supported."
- OpenAI (source #3): **"The most common scheme is an OAuth access token.
  Provide this token using the `authorization` field."** OpenAI does not
  state, in this source, that OAuth is the *only* acceptable scheme, nor
  does it explicitly bless a static bearer token as sufficient — this is
  genuinely **ambiguous** in source #3 alone. OpenAI's separate MCP-building
  guide (referenced but not independently verified in this audit) is quoted
  elsewhere as saying an authenticated MCP server is "expected to implement
  an OAuth 2.1 flow that conforms to the MCP authorization spec" — i.e. if
  you want ChatGPT/the Responses API to treat your connector as
  authenticated (versus fully open), the expected mechanism is the full
  OAuth 2.1 flow, not a bare static token.
- Source #4 **[unverified-direct]**: "optional OAuth authentication" for
  ChatGPT custom connectors — consistent with MCP's own "authorization is
  OPTIONAL" framing.

**Conclusion:** a static bearer token (what this repo's private SSE
entrypoint uses today) is not documented anywhere as a rejected mechanism,
but it also does not satisfy the "MCP authorization spec" that OpenAI points
to for an *authenticated* connector. It is adequate only for a private,
operator-controlled, non-public rehearsal — exactly its current stated
purpose — and would need to be replaced by real OAuth 2.1 (Protected
Resource Metadata + Authorization Server Metadata + PKCE on the client side)
before this project could honestly claim "OpenAI-authenticated connector"
compliance.

### 1.6 Required URL paths / metadata endpoints

- If authorization is implemented: `/.well-known/oauth-protected-resource`
  (RFC 9728, MCP server side) and `/.well-known/oauth-authorization-server`
  (RFC 8414, authorization server side) are both explicit **MUST**s per
  source #2.
- No other manifest/discovery endpoint is mandated by sources #1–#3 for the
  MCP transport layer itself (tool listing happens over the MCP protocol's
  own `list_tools` call, not a separate REST manifest).

### 1.7 CORS

**Not addressed in any of sources #1–#3.** Explicitly not stated — this
audit does not guess a requirement here.

### 1.8 Tool type restrictions (read vs write)

**Not restricted by protocol or by OpenAI's documented guides in sources
#1–#3.** Source #4 **[unverified-direct]** notes ChatGPT Developer Mode
connectors "let ChatGPT securely take action" including writes, and
describes the mode itself as "powerful but dangerous," recommending
operators test carefully and confirm write actions — this is guidance/risk
framing, not a protocol-level restriction. No source states that ChatGPT
*requires* write tools to be excluded; it is this project's own,
independently-adopted policy (`MCP_CLIENT_BLOCKED_TOOLS`) to exclude them,
which remains a valid and recommended posture regardless.

### 1.9 Transport-level security requirements (source #1, direct quote)

> "1. Servers MUST validate the `Origin` header on all incoming connections
> to prevent DNS rebinding attacks
> 2. When running locally, servers SHOULD bind only to localhost
> (127.0.0.1) rather than all network interfaces (0.0.0.0)
> 3. Servers SHOULD implement proper authentication for all connections"

## 2. Current gateway capability

| Capability | State |
|---|---|
| stdio transport | Implemented (`examples/mcp_server/server.py`), stable, default |
| Streamable HTTP transport (current spec's unified endpoint) | **Not implemented.** Current private entrypoint uses the legacy split `/sse` + `/messages` pattern (2024-11-05 style), not the unified MCP endpoint |
| HTTP/SSE transport (legacy, still OpenAI-accepted per source #3) | Implemented (`scripts/mcp_sse_serve.py`), private/loopback-only, manually rehearsed once |
| `Origin` header validation | **Not implemented** — confirmed by inspection of `scripts/mcp_sse_serve.py`; this is a MUST per source #1 for any HTTP transport, private or public |
| Bind to loopback by default | Implemented and verified (`127.0.0.1` default, `MCP_HTTP_ALLOW_NON_LOOPBACK` opt-in guard) |
| Bearer/token auth on the transport | Implemented (`BearerAuthMiddleware`, independent static token) — sufficient for "proper authentication" in the loose transport-security sense, **not** sufficient for the formal MCP/OpenAI "authorization spec" |
| OAuth 2.1 Protected Resource Metadata (`/.well-known/oauth-protected-resource`) | **Not implemented** for the HTTP entrypoint |
| OAuth 2.0 Authorization Server Metadata (`/.well-known/oauth-authorization-server`) | **Not implemented** for the HTTP entrypoint |
| DCR (RFC 7591) | Existing `GatewayOAuthProvider` (used for the stdio/oauth path) already implements DCR + PKCE S256 — **not wired to any HTTP transport** (see Phase 16A finding: default oauth mode wires into FastMCP for stdio only; the private SSE entrypoint deliberately disables it and uses its own bearer layer instead) |
| PKCE | Present in `GatewayOAuthProvider`, unused by the current HTTP entrypoint (PKCE is a client-side requirement in any case — relevant only once a real OAuth flow is wired) |
| Public HTTPS reachability | **Not implemented.** No TLS termination, no reverse proxy, no public bind |
| Safe tool mode (`mcp_client` + `MCP_CLIENT_SAFE_MODE=true`) | Implemented, mandatory, fail-fast enforced |
| Write/docker/agent-launch tools excluded from safe mode | Implemented (`MCP_CLIENT_BLOCKED_TOOLS`, 30 tools) |
| Manual rehearsal evidence | Recorded (`docs/operations/MCP_PRIVATE_SSE_REHEARSAL.md`) |

## 3. Gap matrix

| Requirement | Official status | Current state | Gap |
|---|---|---|---|
| Some HTTP-based transport reachable by the client | MUST have one (stdio alone cannot serve a remote client) | HTTP/SSE (legacy) implemented, loopback-only | Reachability gap only — legacy transport itself is accepted by OpenAI per source #3 |
| Streamable HTTP (current spec's preferred transport) | Not mandatory per OpenAI (source #3 accepts either); mandatory per the MCP spec itself for new implementations | Not implemented | Real gap if aiming for spec-current compliance, not required to merely work with OpenAI today |
| `Origin` header validation | MUST (source #1) | Missing | Real gap, applies even to the current private/loopback entrypoint |
| Localhost bind by default | SHOULD (source #1) | Done | No gap |
| "Proper authentication" (loose sense) | SHOULD (source #1) | Done (static bearer) | No gap for private use |
| Full OAuth 2.1 + Protected Resource Metadata + AS Metadata | Required *if* claiming "authenticated connector" status (source #2 is normative once authorization is "supported"; source #3 implies this is the expected scheme for authenticated connectors) | Not implemented for HTTP; DCR/PKCE exist unused in `GatewayOAuthProvider` | Real gap, but only blocks the "authenticated public connector" goal, not private rehearsal |
| Public HTTPS reachability from OpenAI's network | Practical necessity, not a literal spec MUST | Not implemented (loopback-only, by design) | Real, deliberate gap — this is the private-only posture, intentional |
| CORS | Not addressed by any source | N/A | Not a gap — nothing to satisfy |
| Write-tool exclusion | Not required by protocol; this project's own policy | Done | No gap; exceeds requirement |
| ChatGPT Developer Mode / plan tier | Operator-side prerequisite, not a server-side requirement | N/A (nothing to build) | Not a gap for this repo |

## 4. Minimal safe path (if a real, non-rehearsal attach is ever pursued)

This section describes the smallest set of changes that would be needed to
go from "private rehearsal" to "a real, officially-compliant attach point,"
**without** implying this should be done now or without operator approval.

1. Add `Origin` header validation to the existing private entrypoint
   regardless of any other change — it is a MUST for any HTTP-based MCP
   transport, public or private, and costs nothing to add while staying
   loopback-only.
2. Decide, deliberately, whether to pursue Streamable HTTP (spec-current,
   more work, single unified endpoint) or to keep the legacy HTTP/SSE
   pattern (works with OpenAI's Responses API today per source #3, already
   implemented, no new transport code). This is a real design decision, not
   a default.
3. If real OpenAI/ChatGPT authentication (not just private rehearsal) is
   ever required: wire the *existing* `GatewayOAuthProvider`
   (DCR + PKCE S256 already implemented) to the HTTP transport, and add the
   two mandatory metadata endpoints (`/.well-known/oauth-protected-resource`,
   `/.well-known/oauth-authorization-server`) plus `WWW-Authenticate` on 401.
   This reuses existing code rather than writing a new OAuth stack.
4. Public reachability (TLS termination, reverse proxy or tunnel, real
   domain) is a separate, later decision — explicitly deferred, requiring
   its own dedicated design/approval pass, not bundled into steps 1–3.
5. At every step, `MCP_CLIENT_SAFE_MODE=true` / `MCP_GATEWAY_TOOL_MODE=mcp_client`
   remain mandatory and fail-fast, and `MCP_CLIENT_BLOCKED_TOOLS` remains
   enforced — nothing in this minimal path loosens either.

## 5. Non-goals (explicit)

- This audit does not implement, start, or schedule any of the steps in
  Section 4.
- Public ChatGPT/OpenAI connector exposure is **NOT live** and this
  document does not change that.
- No claim is made that this repository is "ChatGPT-connector-ready" —
  the gap matrix above is the honest current state.
- This audit does not evaluate or recommend a specific reverse-proxy/tunnel
  provider, TLS certificate strategy, or hosting location — those are
  separate decisions outside this audit's scope.

## 6. Risks / Red lines (restated, unconditional)

- **Never publish the private SSE (or any future Streamable HTTP)
  entrypoint without TLS.** Bearer tokens and OAuth access tokens sent over
  plaintext HTTP are trivially interceptable.
- **Never use the gateway master key as an MCP runtime credential**, in any
  transport, present or future.
- **Safe mode (`MCP_CLIENT_SAFE_MODE=true`, `MCP_GATEWAY_TOOL_MODE=mcp_client`)
  is mandatory** for anything ChatGPT-facing; this is fail-fast enforced
  today and must remain so in any future iteration.
- **Public exposure requires explicit operator approval** as a distinct,
  deliberate decision — never a side effect of an unrelated change.
- **No write, Docker, or agent-launch tools in ChatGPT mode.** This project
  already enforces this via `MCP_CLIENT_BLOCKED_TOOLS`; any future work must
  not weaken it.
- **No real tokens, IPs, domains, or paths appear in this document** or in
  any future document derived from it — placeholders only.
