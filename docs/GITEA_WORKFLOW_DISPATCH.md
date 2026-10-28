# Explicit Gitea workflow dispatch

`gitea_dispatch_workflow` and `gitea_workflow_dispatch_status` belong only to
`mcp_client_write` and require both `mcp:repo` and `mcp:admin`. The target must
be a registered source project under the configured Gitea owner; the service
account must also have repository admin and push permission. Merge and green
CI do not invoke this tool or authorize an operator workflow automatically.

Supply the reviewed workflow path, `refs/heads/<branch>`, the exact current
40-character commit SHA, schema-typed `inputs`, a non-secret stable
`operation_key`, and the explicit operator `reason` (20–500 characters).
`dry_run=true` checks the complete preflight without creating a dispatch
receipt or sending POST. String, boolean, finite number and choice inputs
are validated against the workflow at that immutable commit. Unknown keys,
wrong types, secret-bearing input names and unsupported input schemas fail
closed. Booleans become `true`/`false` strings only at Gitea's API boundary.

The required audit and receipt are fsynced before POST. The receipt lives in
`workflow-dispatch/` beside `MCP_AUDIT_LOG_PATH`; production OAuth already mounts
that parent on its persistent audit volume. Files are private, owner-checked,
size-bounded and opened without following symlinks. OS locks coordinate MCP
processes. Receipts retain request fingerprints and public target identity,
never input values, credentials or raw API error bodies.

Reusing an operation key reads and reconciles its receipt; it never sends POST
again. A changed request or different caller cannot reuse it. After a timeout,
disconnect or restart, call `gitea_workflow_dispatch_status` with the same
owner/repo/key. Do not change the key to retry an uncertain operation. A death
between the durable receipt and POST may leave an unexecuted operation unknown;
this is the deliberate at-most-once tradeoff. Do not delete or replace the audit
volume to make a key reusable. `not_found` refers only to that configured journal.

The client requests Gitea's `return_run_details=true`. A directly returned run
ID is then checked against this workflow's run list and its exact repository,
commit, actor, branch, event and attempt. `verified=true` attests identity;
the separate `status` and `conclusion` say whether the job finished successfully.
HTTP 204 is accepted but unbound. A lost/invalid POST response is unknown; even
one matching candidate from a run list is not proof of which operation created
it. These results have `verified=false`, no bound run ID and no automatic retry.
Preflight and post-read checks detect branch drift, but Gitea has no atomic
expected-SHA dispatch precondition: a branch can advance during the HTTP window.

Qualification uses `.github/workflows/gateway-dispatch-probe.yml`, merged on the
default branch. Explicitly supply `approved=true` and a unique bounded `probe_id`;
the job validates them and prints a small non-secret receipt. It does not check
out code, use secrets, access production state or run deployment commands.

After qualification, the authorized MarxMind host promotion is a separate
operator invocation in `infra-quart`, using its reviewed
`.gitea/workflows/marxmind-host-script-promotion.yml` at exact current main SHA
and its two declared inputs. Verify the exact installed script hash and workflow
conclusion before a bot release. Gateway #676 does not itself imply book/corpus
or Telegram delivery acceptance.
