# Candidate cleanup: physical plan fingerprint (read-only)

## Purpose

`candidate_cleanup` refuses removal when `.ai-bridge/current-plan.md`
exists, even when task directories are absent. The error
`WORKSPACE_CONTENDED` includes *physical evidence*, not a verified statement
that an OpenCode task or fleet lease is still active.

Issue #634 records a Business Analytics candidate that is Git-clean and
preserved in an archive branch but remains blocked by this plan file.
The diagnostic enhancement does **not** authorize removal, archive, renaming,
or manual mutation of the candidate.

## New typed diagnostic fields

When `current-plan.md` is an ordinary regular file, the Gateway attempts to
read at most 1 MiB through an anchored directory descriptor, using
`O_NOFOLLOW` and bounded reads. It returns:

- `plan_fingerprint_status=verified`, `plan_sha256` and
  `plan_size_bytes` when the before/after file identity and metadata are
  consistent.
- `plan_fingerprint_status=oversize` for an oversized file;
  `unsafe` for a link or unexpected file identity;
  `unstable` for concurrent replacement/change; or
  `unavailable` when a secure read is not possible.

The plan's content is never included in the tool response. An SHA-256 value
only identifies a **specific physical snapshot**, not whether the plan was
completed. Never interpret a matching hash, an empty `tasks/`, a successful
Git merge, or a missing ephemeral `source.bundle` as proof of terminated
jobs, workers, task attempts, or leases.

## Required before any later terminal-plan clearance

Future code would need a separate audited and explicitly authorized workflow:
prove the exact candidate HEAD, source, branch and preservation ref; obtain
authoritative, fresh evidence that workers, tasks, leases, PR references and
delivery attempts are terminal; bind approval to the exact physical plan
fingerprint; preserve the plan's contents in a safe operator-owned archive;
recheck all evidence within the lineage lock immediately before any mutation;
and fail closed on inconsistency, races, unsafe paths or missing proof.

This diagnostic change implements **none** of those destructive operations.
The existing guarded `candidate_cleanup` behavior remains unchanged.
