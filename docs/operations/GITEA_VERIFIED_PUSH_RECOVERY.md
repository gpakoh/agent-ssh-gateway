# Verified Gitea push: recovering a delayed or ambiguous result

Applies to `gitea_push_verified_commit` (Gateway Issue #973). This runbook
does not authorize any changes to the target project's branch or candidate.

## Why absence is not a retry lease

An API/client timeout or `MUTATION_OUTCOME_UNKNOWN` can occur after the trusted
Git receive-pack operation has started. Several immediate `get_branch` calls
can still report the previous SHA or HTTP 404. This is **not proof** that the
push never applied. During Gateway PR #976 a feature branch became visible
with the exact expected commit after earlier negative Gitea branch reads.

Do **not** re-run an ordinary `gitea_push_verified_commit`, force-push,
delete/recreate the branch, or recreate the candidate solely because the
branch is absent or stale. Preserve the exact candidate, commit, base SHA,
repo/ref, and Gateway request ID.

## Safe, non-mutating reconciliation

1. Independently read the exact destination ref, not a filtered history or
   cached branch list, and compare with the original `expected_head_sha`.
   A positive match may be qualified; absence, error, and conflicting SHA
   remain **unproven**.
2. After a positive observation, call the same verified-push tool with the
   **same registered project, owner, repo, destination branch, expected base
   and head, allowed files and required checks**, plus `reconcile_only=true`.
   The mode reruns the ordinary candidate checks, then performs read-only Gitea
   branch verification. It is explicitly forbidden from calling Git push,
   including when the branch is absent or changed.
3. Only `ok=true`, `outcome=already_published`, `verified=true`,
   `checks_verified=true` and exact `remote_observed_sha` attest the existing
   publication. `mutation_started=false` describes **this reconciliation
   call**, not whether the earlier ambiguous push created the branch.
4. When the state is missing, changed, protected, inaccessible, or the
   candidate proof drifts, preserve the candidate and return to the operator
   with `CHECK_FAILED` or the relevant typed denial. An uncertain or negative
   read must never authorize a second push. An authorized Gitea receive-pack
   / ref-update audit and an exact no-mutation receipt are required before
   reconsidering a new mutation in the absence of a positive HEAD.

## Durable Gateway intent evidence (new mutations)

Before starting the trusted Git subprocess, Gateway writes an append-required
`mcp.gitea_verified_push_intent` audit event with a random correlation ID,
exact owner/repository/ref, original and expected SHAs, and authenticated
Gitea username. Tokens, Git credential headers, raw stderr, verification
commands and host paths are **never** included. If the audit sink is
unavailable, Gateway returns `AUDIT_UNAVAILABLE`,
`mutation_started=false` and performs **no Git push**.

When the process survives the request, a best-effort
`mcp.gitea_verified_push_outcome` event records either
`remote_exact_head_verified`, `local_pre_mutation_failure`, or
`remote_postcondition_unproven`. The mutation response includes
`audit_intent_id` to match the durable event, including on an uncertain
postcondition. If the client loses the entire response, only an authorized
operator may inspect Gateway audit records by exact owner/ref/SHA and the
incident time. A missing terminal outcome or a present intent **does not
prove the remote write failed or succeeded**. Only a positive remote HEAD or
authoritative Gitea receive-pack/ref-update evidence can settle the mutation.
This audit change is prospective; it cannot invent a receipt for Supervisor
#248 or earlier ambiguous requests.

The default `reconcile_only=false` retains the regular verified-push
workflow. It also avoids a duplicate push when it independently observes the
exact already-published HEAD twice, but **do not use default mode to recover a
previously ambiguous push**: a changed or missing head could enter the normal
push path. Use the explicit read-only mode.

This is a bounded positive-outcome recovery slice, **not** a durable
receive-pack mutation journal. The original uncertain Supervisor #248 ref
requires its own authoritative server-side audit if it remains absent.
Clients may need to refresh their Gateway tool schema to expose the optional
`reconcile_only` argument.
