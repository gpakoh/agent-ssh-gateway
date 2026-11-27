# Managed candidate: single untracked file discard (#651)

The new candidate_discard_untracked_file action clears ONE explicitly named
untracked regular file from a registered managed candidate, preserving its
bytes first. It is not generic rm, does not remove source repos, and does not
replace the independent candidate_cleanup delivery/provenance guards.

The action is mcp_client_write only and requires mcp:repo AND mcp:admin.
Arguments: exact project_id, expected_source_project, expected_branch,
full lowercase expected_head_sha, safe project-relative ASCII relative_path
(maximum six components), full lowercase SHA-256 expected_sha256, and
expected_size (0 through 1 MiB).

## Safety guarantees

- Under source+branch lineage AND registry mutation locks, verify exact
  candidate clone registration, metadata, current HEAD and branch. Git status
  must contain exactly the one untracked file and no other changes.
- Refuse active task/delivery evidence, inbound clone dependants and any
  open Gitea PR reference. Verify Gitea references twice. Unknown state
  refuses mutation; required destructive-intent audit must be durable.
- Walk relative parents by no-follow directory handles. Reject symlinks,
  foreign ownership, hardlinks, nonregular files, unsafe path components,
  wrong size, SHA mismatch and changes during a second read.
- Write and fsync exact bytes to owner-only 0600 persistent archive under
  the server-controlled workspace-registry journal root, keyed by immutable
  archive_id, before attempting removal. Verify archive contents.
- Atomically quarantine the exact entry within its own directory; verify
  inode, size and SHA again before unlinking. If a concurrent modification
  is detected, restore original pathname when safe. No shell delete and
  no recursive deletion; archive is retained after successful discard.
- No successful file discard authorizes future candidate_cleanup or
  cleanup of unrelated dirty candidates. Normal guards remain independent.

## Recovery / acceptance

The receipt provides archive_id, archived_sha256 and size. If the operation
is interrupted, inspect the persistent archive, audit and candidate status
under operator authority. A pre-existing archive (even when its SHA and bytes
match) denies repeat discard: a new file at the same path could otherwise be
removed by replay. A leftover quarantined untracked file is evidence;
do not rm -rf or blindly retry an unknown outcome. Archives are not pruned
automatically.

Contract tests include real Git/filesystem candidate fixture success and
refusals for other work, symlink, hardlink, wrong digest/size/branch,
unsafe path, live reference/audit failure, archive collision and concurrent
post-quarantine writer. MCP adapter coverage includes admin scopes and
write-only registration.

A server tool registry update alone does not guarantee an older MCP client
can call the new action. Refresh the client tools/list and separately obtain
live tool and archive acceptance after successful CI, merge and deployment.


## Exact untracked batch (#1012)

The separate `candidate_discard_untracked_files` action accepts 2–8 files
(up to 8 MiB each, 16 MiB combined). The original single-file action, including
its 1 MiB limit and legacy handoff exception, is unchanged. The new action is
write-mode only and requires BOTH `mcp:repo` and `mcp:admin`.

Supply the registered candidate ID, source project, branch, full HEAD SHA,
`expected_index_sha256` and `expected_status_sha256` from a fresh
`info(project)`, and a list of objects with exactly `relative_path`,
`expected_sha256`, and `expected_size`. The NUL-delimited status must match
the full requested untracked set; duplicates, tracked changes (including
hidden tracked edits), unsafe paths, extra untracked files, active evidence,
inbound dependencies, and unknown or open PR references deny the operation.

Under the existing lineage and registry locks, the batch additionally holds
Git's own exclusive `index.lock` for its real managed index. An existing lock
or alternate index path refuses the operation before archival. Read-only Git
observations disable optional index updates. Ordinary Git index writers are
excluded throughout archive/quarantine/removal; the lock identity is checked
before mutations and only the operation's own lock is removed. A lost or stale
lock requires explicit reconciliation, never blind deletion or replay.
A required audit binds every path fingerprint, size and digest. An exclusive durable operation plan records
the exact candidate/index/status lease, all archive IDs and quarantine names.
Every file is archived and verified, including directory fsync, before any
original entry is moved. The operation rechecks identity and references, then
quarantines and verifies the entire set before the first final removal.
Quarantine and archive operations do not constitute a multi-file atomic
transaction. The final status, index and HEAD are rechecked and a durable
completion receipt is written before reporting success.

On interruption or failure after the plan is prepared, the error includes
`operation_id`, observed removed paths, and `partial_or_unknown`. Recover
only under operator authority from the persistent plan, archive bytes and
actual path state; never replay an ambiguous operation. Restoration attempts
never overwrite a newly created original pathname. An archive/plan collision
refuses replay even when content is identical. Archives remain private and
are not automatically pruned.

**Scope boundary:** the open-PR prohibition remains in force for this first
batch implementation. Exact file-only quiescence for an active PR needs a
separately reviewed lease that excludes concurrent writers and delivery.
Neither the batch interface nor a successful archive constitutes that lease.
