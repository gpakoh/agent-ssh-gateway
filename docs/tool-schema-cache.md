# Conditional MCP schema discovery (P3-06, scoped server contract)

The `gateway_catalog` MCP tool supports an **opt-in** conditional schema
read for one exact tool name. It does **not** replace MCP `tools/list`, cache
authorization decisions, or create callable tools inside a stale external
client.

## Request / response

1. Call `gateway_catalog(tool_name="health", include_schema=true)`.
   When the request is bound to an authenticated MCP lifecycle, the MCP build
   has a known exact release SHA, Gateway's overall `ready=true` and its live
   command plane is `ready` (health request bounded to 2 seconds),
   the response contains `result.tools[0].input_schema` and
   `result.schema_cache = {state: "fresh", etag: "sha256:...", reuse_permitted:
   true, scope: "authenticated_mcp_session"}`.
2. Within that **same authenticated MCP session**, a client may pass that
   ETag as `if_schema_etag` for the **same exact request**. If nothing
   affecting the schema has changed, the result contains
   `schema_not_modified=true` and
   `schema_cache.state="not_modified"`. No tool schema is duplicated. The
   client must retain its previously received schema.
3. A mismatched token always returns a complete, fresh schema and a new ETag.
   Invalid/oversized tokens are rejected without executing a Gateway probe.
   The token is a fixed, lower-case SHA-256-shaped value; it is not a bearer
   credential and never grants permissions.

The token is an HMAC over the original authenticated client identity, a
server-created per-ServerSession random nonce (rotated on observed
auth/build/registry/plane-state changes), exact release build SHA,
canonical sorted server toolset hash (including all input schemas), live
Gateway command-plane state, selected tool request/role, and the complete
selected catalog result (including availability). Tokens become invalid after
an auth identity change, lifecycle change, process restart, release change,
registry/schema update, role/request change, tool dependency change or plane
transition. Session nonces use weak ServerSession ownership and are not persisted as
global client registrations. Without a live, weakrefable MCP session, the
response is always uncacheable, even if a lifecycle context exists.

If auth context or lifecycle identity is missing, Gateway is not ready,
release SHA is unknown, or its health request fails, the server returns the
**full** schema with `schema_cache.state="uncacheable"` and no reusable ETag.
No `not_modified` outcome may be based on an unverified cache claim.
Normal discovery with `include_schema=false` keeps its original response
shape. Every call remains subject to the original MCP authentication, scope
enforcement, and server policy. A cache hit never authorizes tool execution.

## What this does not establish

External clients must actually save ETags and cached schema snapshots and
refetch/evict on `notifications/tools/list_changed`, process or session
changes, or a mismatched ETag. The server cannot evict a client-side cache.
Issue #876 separately tracks independent external catalog relisting,
schema-lease attestation and real calls of newly registered tools. Closing
P3-06 #896 additionally requires that client integration and cold-client
negative tests. This server contract is a scoped enabling increment, not
complete end-to-end client cache acceptance.
