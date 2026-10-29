# Legacy cleanup journal recovery (#697)

The `candidate_cleanup_abort_legacy_prepared` tool handles one narrowly
identified **pre-CAS legacy transaction**. It is available only in
`mcp_client_write` with `mcp:admin` authorization. This is not a generic
candidate remover, project unregister, or permission to change another
project's files.

## Contract

Call with `apply=false` and exact `project_id`, `expected_head_sha`,
`expected_branch`, `expected_source_project`, and
`expected_legacy_root`. The root must end in
`.candidates/<project_id>` and match a single registered `git-source`
entry. Gateway checks the persisted journal and returns a SHA-256 lease
**only** for `phase=prepared` with the exact original candidate cleanup
identity and no registered or physical managed candidate clone at that ID.

Apply **only after operator review** using `apply=true`, the returned
`expected_tombstone_sha256`, and a specific human reason (20–400
characters). The mandatory persistent audit must succeed before modification.
The action rechecks the journal, registry, root directory, and inode under
the source-lineage and registry locks, then moves only the original journal
into an adjacent `*.legacy-aborted-*.json` evidence archive. It does not
remove or rewrite the legacy Git-source, registry entry, project data, or
existing archives.

Fail-closed cases: missing/foreign/duplicate registry entry; symlinked or
missing legacy directory; managed clone occupying the ID; changed journal
SHA; any phase other than `prepared`; other journal fields, prior cleanup
progress, absent audit, or mismatched lease.

This repairs the **stranded journal** only. It does not imply the legacy
Git-source is disposable, that its PR was merged, or that its own cleanup
has been authorized. After archival, perform an independent source-project
review and use an appropriate, separately audited legacy ownership flow.
Do not emulate this tool with a manual `rm`, broad registry update, or
unsanctioned production rollout.

Deployment/runtime acceptance remains blocked by Issue #703's
`REGISTRY_CONFIG_DRIFT` until operator-approved exact-SHA registry
reconciliation and a successful host-smoke run.
