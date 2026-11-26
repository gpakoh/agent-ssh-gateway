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

When execution reaches terminal handling, Gateway attempts an append-required
`mcp.gitea_verified_push_outcome` audit event with the same correlation ID and
one of `remote_exact_head_verified`, `local_pre_mutation_failure`, or
`remote_postcondition_unproven`. Responses containing `audit_intent_id` also
report `audit_outcome_persisted`: true only after the required append (including
fsync) succeeds, false when persistence is unproven. Failure of this terminal
write does not undo a verified publication, convert unknown into a retry
lease, or change an independently proven pre-transport failure. A successful
terminal append records the observation, not an atomic receive-pack receipt.

For an unkeyed request whose entire response is lost, only an authorized
operator may inspect Gateway audit records by exact owner/ref/SHA and incident
time. Missing terminal evidence or a present intent **does not prove the
remote write failed or succeeded**. This audit evidence is prospective; it
cannot invent a receipt for Supervisor #248 or earlier ambiguous requests.

## Opt-in operation receipt

On a new reviewed mutation, preserve a unique caller-chosen `operation_key`
(8–128 characters, letters/digits and `_.:-`, beginning with a letter or
digit) together with the exact delivery contract. Pass the key to
`gitea_push_verified_commit`. The key is bound to the authenticated caller
and a hash of the normalized project, repo, branch, base/head SHAs, allowed
files and required checks. The existing verification and durable intent
guard still apply. Raw checks, tokens and paths are not written to the
operation receipt.

The private journal under the configured audit directory fsyncs a
single-use record before verification, and advances it to `dispatching`
with the audit intent ID before the Git subprocess is scheduled. Its
per-key lock prevents a concurrent duplicate invocation. A used key
cannot start another push, even after cancellation or process restart.

After an ambiguous response, call the same tool with the **same key and
exact contract**, `receipt_only=true` and `reconcile_only=false` (default).
This mode performs no verifier or remote Git/API operation. It returns
the following observed receipt states:

- `not_found`: no record in this configured journal, not proof of no push.
- `started`: verification-phase record, no terminal observation.
- `dispatching`: pre-transport intent was recorded, actual remote effect
  remains unknown without terminal evidence.
- `terminal`: the saved typed result, including the audit intent ID,
  exact observed head if any, and audit outcome persistence flag.

The receipt is historical, not a fresh remote-ref attestation:
`current_remote_state_verified=false`, `authorizes_mutation=false` and
`replay_allowed=false` apply to every state. Do not infer safe-negative
evidence from any missing or unfinished receipt. A terminal result can
itself contain an unknown remote effect. Confirm publication through the
independent exact-ref procedure above (omit the operation key for the
explicit `reconcile_only=true` call).

An ordinary keyed response reports `operation_receipt_persisted=true`
only after the terminal record append/replace and fsync succeed. A failed
terminal write is reported separately and never reverses the observed
remote outcome. A later read observes the available journal record; it
cannot prove that a previously failed fsync succeeded. The same-key
mutation is refused with `OPERATION_KEY_ALREADY_USED` or `OPERATION_BUSY`;
a changed contract with `OPERATION_KEY_CONFLICT`; a different caller
with `AUTH_ERROR`; invalid or unavailable journal evidence with
`RECEIPT_UNAVAILABLE`. None is a retry lease. Do not choose a new key to
bypass an ambiguous operation or delete its receipt.

Keep journal records as recovery evidence; there is no automatic expiry
or receipt-deletion operation. This is a caller-bound observation journal,
not an atomic Gitea receive-pack transaction. Legacy unkeyed requests
remain compatible and have no caller-keyed recovery record. Client schema
refresh may be necessary to expose `operation_key`, `receipt_only` and
`reconcile_only` before using these modes.

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
