# Verifier cache lifecycle (#882)

The SSH executor's verifier root is a private, server-owned, shared volume.
Its environments and tool caches are reproducible, not source or task state.
The incident on 2026-11-27 found 305 GiB of verifier data across 2,974 roots,
including 1,990 older than seven days. The offline operator recovery reclaimed
59.38 GiB; age alone is not a cleanup lease.

## New managed-candidate behavior

Primary, readonly fallback, standalone Ruff and compileall writers derive
one stable project root from the resolved source path. All use its original
.sync.lock inode through the owned child execution; never explicitly unlock
an inherited descriptor or unlink the lock. User-specific fallback venvs
remain isolated under fallback-u<uid>-<user> inside that same project root.
A queued verifier checks the source still exists after locking and before
writing dependency state. New directories use a private umask.

Normal guarded candidate_cleanup retains its Git/registry/delivery checks.
It attempts an exact verifier lease inside the existing lineage and registry
locks. For an exact registered candidate or a bound pending cleanup journal,
the lock marker is established even before the first verification starts.
An observed busy or unavailable lease blocks source removal. After proven
terminal source absence it removes only the recognized .venv and tool-cache
subtrees under that lease. An absent configured verifier base permits no
verifier writers; a late observation is still checked without creating a
marker. Unsafe post-delivery cache observations defer reclamation rather than
authorizing removal. The verifier root, lock inode, source
metadata, unknown trees and canonical project environments are preserved.

The result and success audit include verifier_cache_cleanup with state and
removed_trees. reclaimed means exact generated trees were removed (zero is
idempotent); absent means the configured cache was not present; busy or
deferred_unsafe means reclaiming was not established. Source delivery remains
valid on a cache-only failure. Repeat the same exact terminal cleanup
transaction for a deferred result, not a new candidate. Counts are not
physical freed bytes. No global directory walk or automatic volume prune
is introduced.

## Rollout and existing history

Before enabling the new lifecycle on the real shared volume, drain prior
verification jobs and restart the relevant Gateway/MCP/SSH executor
components together: old fallback writers used a different lock and must
not overlap the new cleanup. Confirm equal resolved source identities and
private verifier-root ownership across the execution/control planes.
Qualify with an isolated real candidate: perform checks, observe the
environment, complete guarded cleanup, observe source/registry absence,
generated cache absence, stable lock inode and the audit/result receipt.

Historical sibling fallback directories with the old 8-character path hash
and user suffix are not removed by this change. Existing unknown or unsafe
trees, caches for candidates that never complete cleanup, and physical
history from prior releases still require separate bounded reconciliation.
Use the operator-approved offline cleanup with drained consumers for that
backlog; never infer inactivity from directory age alone. Disk-pressure
monitoring remains necessary. This change is not a global TTL janitor.
