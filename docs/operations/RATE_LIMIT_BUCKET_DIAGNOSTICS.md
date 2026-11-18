# Opaque 429 rate-limit bucket diagnostics

The Gateway's ordinary slowapi request throttling already returns a typed
`RATE_LIMIT_EXCEEDED` response with `Retry-After`,
`details.retry_after_seconds`, `details.bucket_class` (`ip`/`master`),
and `details.operation_class`. A retry is not evidence that the bucket
was reset; preserve the server's cooldown.

For ordinary **request-rate** 429 responses, the Gateway also supplies:

- `details.bucket_id` in `bkt_<24 lowercase hex digits>` format.
- `X-RateLimit-Bucket-ID` containing the same opaque value.

This is a correlation handle **only**, not a reusable authorization token,
unique global user ID, cross-worker storage key or rate-limit waiver. It is
HMAC-derived from the same trusted-proxy-aware source-IP key as slowapi,
along with bucket class, HTTP method and route template. A secret random
key exists only in this Gateway process; source IP, bearer token, credential
and actual URL are never returned. An ID is repeatable for the same
source/route/lane **within one process lifetime**, and deliberately changes
across process restarts or workers. Distributed callers should use
`bucket_class` plus `Retry-After` for behavioral backoff, not assume
cross-instance equality or infer that two different IDs represent different
underlying Redis quota entries.

The MCP error adaptor copies `bucket_id` from a structured 429 only when
it matches the narrow generated format. Arbitrary diagnostic text, IPs,
headers or upstream bearer-like strings are discarded. Existing typed
`retry_after_seconds` and specific `RATE_LIMIT_EXCEEDED`/
`SESSION_LIMIT_EXCEEDED` error codes remain unchanged. SSH **session
capacity** errors do not claim an HTTP request-rate bucket ID.

This is a scoped slice of Gateway Issue #885. Other limiter sources,
alternative read guidance and broad end-to-end coalescing are not declared
complete. Repeated non-mutating MCP status/health reads already have
in-flight singleflight for the explicitly reviewed routes; do not retry
mutations to simulate coalescing.
