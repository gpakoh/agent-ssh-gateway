# Guarded Gitea Issue reopen

`gitea_reopen_issue` is the **Issue-only** counterpart of `gitea_close_issue`.
It repairs an accidentally closed Gitea Issue without treating a merged PR
or an unverified deployment as completion. It cannot reopen pull requests
or change a task's coordination status on its own.

## Operator workflow

1. Read the actual Issue through `gitea_get_issue(owner, repo, issue_number)`
   and confirm `item_type=issue`, `state=closed`, the original requirement,
   and why closure was premature. Verify the owner/repository and number.
2. Supply its **exact** `updated_at` to `gitea_reopen_issue` as
   `expected_updated_at`, plus a meaningful **20–500 character** reason.
   This is a write tool requiring both `mcp:repo` and `mcp:admin`
   and a durable private audit logger.
3. The Gateway performs a first Issue-only state/identity/version check,
   resolves the authenticated Gitea identity and opaque MCP caller fingerprint,
   and repeats the state/version/content check. It writes a required audit
   intent before a **single** Issue API PATCH with exactly `{"state":"open"}`.
4. It reads the Issue back and reports `open=true`, `verified=true`,
   `already_open=false`, expected/observed timestamps only after a confirmed
   Issue-only open state. A previously open Issue returns an explicit
   `already_open=true` **no-op**; no intent/PATCH is written.
5. A stale version, permission failure, PR number or missing private audit
   fails closed. If PATCH or post-PATCH readback times out, the tool returns
   `MUTATION_OUTCOME_UNKNOWN` (`retryable=false`). It attempts one read-only
   observation but **does not attribute a coincident open state to this call**.
   Re-read the Issue independently; never replay an ambiguous PATCH blindly.

**Race limitation:** Gitea's Issue PATCH does not have atomic
`If-Match`/version CAS. Two exact preflight reads reduce, but cannot
eliminate, a competing write between the final read and PATCH. The
audit records the actor, SHA-256 caller fingerprint, original version,
reason, requested transition and confirmed observed version/state without
recording bearer tokens or private content.

The tool's registration and JSON input schema are included in the active
`mcp_client_write` server mode and are excluded from read-only
`mcp_client`. The external ChatGPT client may cache older tools/list
responses; server registration and `tools/list_changed` do **not** prove
the refreshed client can call the new tool. Keep Issue #938 open for
post-deployment callable-client acceptance, and track external schema
refresh separately in Issue #876.
