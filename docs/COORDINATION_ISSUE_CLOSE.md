# Explicitly completing a coordination Issue (#694)

`resolved` is a proposed resolution with a durable report comment. It continues
to leave the Gitea Issue open. Merging a PR does not close a coordination Issue.

After reviewing the completed work, an authorized operator can explicitly call
`coord_close` in `mcp_client_write` mode with both `mcp:repo` and `mcp:admin`.
The tool accepts only a registered source project and a normal coordination
Issue, never a PR, arbitrary URL, candidate project or a batch of Issues.

1. Record the final report using `coord_update(..., state="resolved")`: describe
   changes, PR and merge SHA, actual tests and exact-head CI, deployment evidence
   where relevant, limitations and candidate/branch cleanup.
2. Read `coord_open`, review the report and subsequent discussion, and retain the
   latest trusted resolved comment ID and `thread.close_state_sha256`.
3. Explicitly call `coord_close(project, issue_number, resolution_event_id,
   expected_state_sha256, request_key, reason)`. The reason records the operator's
   decision; neither a green CI result nor a read call grants close authorization.
4. Require `closed=true, verified=true` and independently re-read the Issue's
   Gitea `state=closed`. The original report remains in the same thread.

The close action does not append or duplicate the report. It binds the existing
trusted resolved comment, verifies repository write permission and identity,
requires a fresh content/state fingerprint, persists an attributed required
audit intent and rechecks the thread immediately before a single fixed
`PATCH /repos/{owner}/{repo}/issues/{number}` with only `{"state":"closed"}`.
The generic Gitea PATCH allowlist is unchanged. Errors never serialize tokens or
upstream exception bodies.

Receipts contain operation/report hashes and identity metadata, not comment
bodies or credentials. They live in `coordination-close/` beside the configured
`MCP_AUDIT_LOG_PATH`, on the existing persistent MCP audit/data volume. Private
directories, regular files, fsync and nonblocking OS file locks serialize the
same Issue across MCP processes sharing that volume. Do not share one Gitea
writer across replicas with separate receipt volumes: distributed locking over
independent filesystems is not provided. Receipt storage failure refuses close.

Request keys are scoped to the registered repository and Issue. Reusing a key
with a different payload is rejected. Once dispatch is recorded, replays only
read Gitea and reconcile; they never resend PATCH, including after restart,
cancellation, an unreadable response or manual reopening. An unresolved attempt
also blocks a different key from bypassing it. A known permission rejection can
be reviewed again with a new operation key. No receipts are automatically pruned.

Gitea does not expose a proven Issue compare-and-swap contract in this path.
The fingerprint is a client-side precondition, not a server CAS guarantee. A
remote edit between the final read and PATCH can still coincide with a close;
post-write content checks then report an unknown outcome rather than claiming
verified completion. The tool never automatically reopens an Issue to roll back
another writer. Inspect the actual Issue after unknown/conflicting results.

The native tests exercise real private filesystem receipts/locks, concurrent
attempts, restart/replay, cancellation, changed reports, spoofed authors, PRs,
permission/audit refusal, response loss, remote-edit windows, fixed HTTP payload,
scope/mode registration and the real FastMCP input schema. Controlled transports
do not replace live qualification of the published tool after normal deployment.
