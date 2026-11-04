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
under operator authority. A leftover quarantined untracked file is evidence;
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
