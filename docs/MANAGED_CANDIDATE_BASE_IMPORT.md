# Importing the current base into an existing bundle candidate (#660)

Use the existing `git_fetch_ref(project=<managed-candidate>, remote="origin", branch=<source-default-branch>, expected_remote_head=<full-current-SHA>)`.

The bundle fallback accepts only a registered candidate with its original deterministic source/feature/base identity and exact managed bundle origin. The registered source must resolve to one trusted Gitea repository. A clean local feature branch must match the pushed Gitea feature SHA; the requested branch must be that repository's current default branch and match the caller's exact SHA.

The existing trusted source publisher fetches into private staging, with credentials confined to that publisher. A self-contained exact-commit bundle is imported locally without credentials or FETCH_HEAD changes. The candidate's feature ref is verified in the same Git transaction that compare-and-swaps its remote-tracking ref. Source files/objects/refs, candidate worktree, index, feature history and origin are not rewritten. Candidate lineage locking also serializes preparation/cleanup, and temporary refs are removed before releasing that lock.

The result records source/repository identity, observed feature SHA, previous and imported base SHA. It imports a base only: subsequently call `git_update_branch_by_merge` with the expected feature HEAD, review conflicts, rerun checks and push without force. Import success is not merge, CI or deployment success.

Changed/missing lineage, non-default branches, missing exact base, dirty/unpushed work, SHA drift and tracking-ref races fail closed. A failed postverification restores only the exact tracking value written by this operation; a different concurrent value is preserved. If ref cleanup or storage fails after publication, the typed failure reports that a tracking update was attempted: inspect state before retrying. Immutable imported objects can remain after a rejected import, as with ordinary Git fetch.

Tests use isolated real Git repositories, bundles and ref transactions with controlled Gitea/source-publication boundaries. They cover exact import/repeat/explicit merge, identity tampering, missing registration, malformed metadata, dirty/unpushed work, SHA/local-ref races, partial transfer/timeout and token redaction. Live MCP publication must be verified separately before reporting consumers unblocked.
