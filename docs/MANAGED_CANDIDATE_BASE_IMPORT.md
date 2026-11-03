# Importing the current base into an existing bundle candidate (#660)

Use the existing `git_fetch_ref(project=<managed-candidate>, remote="origin", branch=<source-default-branch>, expected_remote_head=<full-current-SHA>)`.

The bundle fallback accepts only a registered candidate with its original deterministic source/feature/base identity and exact managed bundle origin. The registered source must resolve to one trusted Gitea repository. For **default branch** imports, the clean local feature HEAD must match the published Gitea feature SHA; the requested default branch must match the caller's exact SHA. A separately pinned **feature branch** import is supported only with the fast-forward proof described below.

The existing trusted source publisher fetches into private staging, with credentials confined to that publisher. A self-contained exact-commit bundle is imported locally without credentials or FETCH_HEAD changes. The candidate's feature ref is verified in the same Git transaction that compare-and-swaps its remote-tracking ref. Source files/objects/refs, candidate worktree, index, feature history and origin are not rewritten. Candidate lineage locking also serializes preparation/cleanup, and temporary refs are removed before releasing that lock.

Symbolic tracking references are refused. Publication, rollback and temporary-ref cleanup use no-deref operations so a symbolic alias cannot redirect writes or deletions into a local branch.

The result records source/repository identity, observed feature SHA, previous and imported base SHA. It imports a base only: subsequently call `git_update_branch_by_merge` with the expected feature HEAD, review conflicts, rerun checks and push without force. Import success is not merge, CI or deployment success.

Changed/missing lineage, unrecognized branches, missing exact base, dirty/unpushed work for the default import, non-fast-forward feature history, SHA drift and tracking-ref races fail closed. A failed postverification restores only the exact tracking value written by this operation; a different concurrent value is preserved. If ref cleanup or storage fails after publication, the typed failure reports that a tracking update was attempted: inspect state before retrying. Immutable imported objects can remain after a rejected import, as with ordinary Git fetch.

Tests use isolated real Git repositories, bundles and ref transactions with controlled Gitea/source-publication boundaries. They cover exact import/repeat/explicit merge, identity tampering, missing registration, malformed metadata, dirty/unpushed work, SHA/local-ref races, partial transfer/timeout and token redaction. Live MCP publication must be verified separately before reporting consumers unblocked.

## Recover an existing candidate whose remote feature HEAD advanced (#768)

If the feature was updated remotely after candidate creation (for example by
review or an earlier worker), do **not** force-push, rewrite the bundle origin,
or clone a second candidate. Confirm the exact current feature/default SHAs
in the trusted Gitea repository, then perform these three separate operations:

1. `git_fetch_ref(project=<existing-candidate>, remote="origin", branch=<exact-feature-branch>, expected_remote_head=<full-remote-feature-SHA>)` imports the exact published feature commit into `refs/remotes/origin/<feature>`. The tool checks that the remote commit **descends from the current clean local feature HEAD**. It rechecks remote default/feature leases and candidate identity before an atomic tracking-ref update. It does **not** move the local feature branch or index/worktree.
2. `git_refresh_branch_to_head(project=<existing-candidate>, branch=<feature>, expected_current_head=<old-local-feature-SHA>, target_head=<verified-remote-feature-SHA>)` applies the already-imported fast-forward through its own write preflight and postconditions. A divergent remote feature or local worktree change is not eligible.
3. Only after the feature HEAD matches the remote, call the original `git_fetch_ref(..., branch=<default-branch>, expected_remote_head=<full-current-default-SHA>)`, followed by the guarded `git_update_branch_by_merge(..., source_branch="origin/<default-branch>", expected_head=<exact-current-feature-SHA>)` and normal tests/CI.

The feature-import response identifies `import_mode=managed_candidate_feature_fast_forward`.
A Gitea branch not equal to the repo's default or the exact candidate feature
is refused. Local feature commits not contained in the remote feature are
never discarded. A rejected or interrupted transfer may add immutable Git
objects but must not silently update `refs/remotes/origin/<feature>` or the
working branch. Every operation must be verified independently; an imported
commit is not an accepted PR or permission to merge/deploy.

## Creation and recovery after a stale registered source (#699)

For `prepare_candidate_clone`, `base_ref="origin/main"` and
`base_ref="refs/remotes/origin/main"` refer to `refs/heads/main` in the
registered source's trusted Gitea repository. This interpretation applies
only to base selection, not generic remote probes or delivery-branch cleanup.
Use `refs/heads/origin/main` to name a literal remote branch `origin/main`.
An absent or unknown tracking target never falls back to the cached local
tracking ref. Explicit immutable SHA bases and local-only named bases retain
their existing contracts.

Creation records the actual disposable bundle origin in candidate metadata.
The importer accepts either the original managed content-addressed origin or
an exact recorded factory staging origin, bound to the deterministic source,
branch, base and initial HEAD. It never fetches from the disposable origin,
which may no longer exist. Recovery preserves this record; it never invents
one from a legacy clone's current Git configuration. The existing exact
default/feature SHA leases and ref transactions remain required.

Failures now identify a bounded `stage` (identity, candidate state, remote
lease, source publication, bundle import, recheck, tracking publication or
postverification), without exposing credentials or arbitrary exception text.
Legacy staging origins without creation proof remain refused. For a
never-modified legacy clone, use `candidate_cleanup` with the exact project,
source, branch and HEAD and `abandon_unmodified=true`, then prepare the same
feature from a freshly verified base. Do not manually rewrite metadata,
origin or history to satisfy a guard.

Unmodified cleanup uses the recorded full `base_sha`, original `head` and
deterministic project ID, rather than requiring the descriptive `base_ref`
to be a SHA. It checks clean/current state, local refs and unreachable Git
objects, remote feature absence, active evidence, inbound dependencies and
external references before deletion; all existing cleanup journal and
idempotency guards remain. Any unique work or ambiguous ownership must be
preserved for review. A source's symbolic ref may have advanced since
creation without invalidating the immutable original base proof.

Neither path needs a writable source `FETCH_HEAD`: trusted remote objects are
materialized in private staging. `git_write_capabilities` covers its advertised
index/objects/refs/HEAD components, not permission to overwrite arbitrary
source metadata. Do not chmod/chown the registered source to repair this flow.
