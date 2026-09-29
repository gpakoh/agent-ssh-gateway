# Agent SSH Gateway — TODO

Last cleaned: 2026-09-29.

This file is the active backlog only. Completed audit notes, fixed CI blockers,
merged PR evidence, and superseded diagnostics should live in PR history,
changelogs, or dedicated audit archives — not in TODO.

Open backlog count after cleanup: **41**

- **P1 / critical:** 12
- **P2 / important:** 22
- **P3 / capability / wishlist:** 7

## P1 / Critical blockers


1. ⬜ **Add guarded `git_fetch` and exact-head workspace refresh.**
   Gateway can detect stale PR branches and outdated local refs, but cannot update
   remote-tracking refs or refresh a clean workspace to an exact Gitea SHA through
   a typed tool. Add an audited fetch/refresh path with clean-worktree and
   expected-state guards, no implicit checkout/merge by default, before/after SHA
   evidence, and typed errors for dirty trees, disallowed remotes, and network
   failures.

2. ⬜ **Provide a canonical write-plane recovery path.**
   Some projects report candidate fallback while task policy requires canonical
   non-candidate execution. Others have handoff writes failing while archive/task
   maintenance succeeds. Expose exact write capability for code, Git and handoff
   surfaces, and return `CANONICAL_WRITE_PLANE_UNAVAILABLE` with safe recovery
   steps when a clean writeable canonical workspace cannot be provisioned.

3. ⬜ **Create a trusted exact-source delivery/materialization path.**
   Operators need to review or deliver from an exact authorized Gitea commit or
   an externally prepared clean workspace without mutating a stale canonical
   checkout or requiring task-bound artifacts that are impossible to create from
   the visible surface. The path must bind owner/repo/SHA, allowed files, clean
   tree proof, checks, and push/PR evidence.

4. ⬜ **Reconcile stale unbound fleet leases without duplicate work.**
   Historical `attempted` / `legacy_unknown` lease rows can consume all fleet
   capacity even when no lease has a recent heartbeat. Add fleet status that
   separates live workers, reclaimable never-attempted leases and ambiguous rows;
   require explicit evidence/acknowledgement before deleting ambiguous leases;
   tombstone every reclaimed lease exactly once.

5. ⬜ **Expose OpenCode startup, proxy and useful-work state structurally.**
   Startup/proxy rotation, provider/server errors and semantic agent progress are
   still too dependent on raw log archaeology. Surface phase, elapsed time,
   proxy attempt/max, last startup message, final proxy outcome, upstream error
   class/ref, and `useful_agent_activity_seen`. Heartbeats must not reset
   semantic staleness.

6. ⬜ **Keep ChatGPT-visible tool schemas in parity with server manifests.**
   Tools can appear available in `tools_manifest` while missing or having a
   different callable schema in the ChatGPT resource catalog. Safety-required
   preflight tools must be invokable from the same client surface as the mutation
   they guard, or the mutation must fail early with a typed
   `REQUIRED_PREFLIGHT_UNAVAILABLE` diagnostic.

7. ⬜ **Reconcile Gitea Actions lifecycle, stranded jobs and cancellable runs.**
   CI runs/jobs can remain `waiting` / `in_progress` after all useful work is
   done, after cleanup timeouts, or after old workflow jobs later become runnable.
   Add exact-run/head cancellation, stale/orphaned container reconciliation,
   terminalization of false-condition/dependency-blocked jobs, and safe rerun
   eligibility diagnostics.

8. ⬜ **Add a repo-owned deploy-script / deploy-contract runner.**
   Some repositories intentionally encode safety in a reviewed deploy script
   rather than raw Compose primitives: preflight, locks, immutable image checks,
   replay-stable snapshots, health smoke, LKG writes and rollback. Gateway needs
   a controlled runner that binds exact script path/content/ref, image authority,
   env names and Compose project, while redacting secrets and reconciling
   ambiguous Docker outcomes.

9. ⬜ **Add one-shot Compose/Docker execution with secret-safe env inheritance.**
    Operators need to run migrations, readiness commands and read-only database
    inventory against Compose-backed services without exposing DSNs or mutating
    long-running service definitions. Support exact image/service/network guards,
    inherited env names, `--rm`, timeouts, no port publication by default, and a
    read-only SQL/catalog mode for Compose PostgreSQL services.

10. ⬜ **Expose guarded repository settings and branch-protection mutations.**
    Gateway can read branch protection/default branch state but lacks a typed
    way to set default branch, required checks, approvals or protection against
    exact expected current state. Add CAS-style settings updates with audit
    evidence and post-write verification.

11. ⬜ **Isolate PR Docker jobs from the shared host daemon or document the trust boundary.**
    PR-controlled Docker build/smoke steps can mutate the shared runner Docker
    daemon. Prefer disposable per-job daemon/VM isolation; otherwise enforce and
    document that every same-repo PR writer is intentionally trusted as host
    admin and prevent untrusted/fork PRs from privileged labels.

12. ⬜ **Make CI merge gating distinguish missing, incompatible and stale evidence.**
    Merge tools should distinguish no workflow, no exact-head run, stale run,
    trigger-incompatible workflow (for example push-only), intentionally skipped
    jobs, and ordinary red CI. Exact-head acceptable evidence must be explicit in
    repository policy and never silently weakened.

## P2 / Important bugs and reliability gaps

13. ⬜ **Wire the production `astro-sites` named root into `mcp-oauth` after the host path is provisioned.**
   Named-root registration is now supported, but production still has no
   `MCP_OAUTH_ASTRO_SITES_ROOT` setting and the intended host directory is not
   provisioned. Once the operator supplies an existing absolute host path, add
   the `registry_roots.astro-sites` entry, the explicit writable `mcp-oauth`
   bind with host-path auto-creation disabled, the documented env contract, and
   deploy seam coverage. Deploy must fail closed when the configured path is
   absent; do not silently create an empty root.


14. ⬜ **Make verification helpers honor project environments and CI-equivalent toolchains.**
    `run_pytest`, `run_ruff`, `run_mypy` and verifier containers should not fail
    falsely on unreadable `.venv`, missing `uv.lock`, unwritable caches, monorepo
    collection collisions, or verifier/CI tool-version drift. They should select
    the repository-declared verification profile or return a typed environment
    diagnostic with toolchain provenance.

15. ⬜ **Make agent cancellation and submission atomic/reconcilable.**
    Cancellation can mark a local job ambiguous while the remote runner keeps
    heartbeating, and rate-limited submission can return failure after a runner
    has already started unbound. Preserve a cancellable/fenceable remote handle
    or prove no runner/proxy acquisition occurred before returning failure.

16. ⬜ **Reconcile agent jobs durably across Gateway restarts/deploys.**
    Restart/deploy can make returned job ids disappear while artifacts still show
    stale `running` status. Inspect/status APIs should derive terminal states
    such as `lost_after_restart`, `orphaned_attempt` or `artifact_incomplete`,
    including last useful activity and safe recovery guidance.

17. ⬜ **Expose bounded agent logs and structured artifacts consistently.**
    Fixed artifacts such as `opencode-output.log`, `agent-status.md`,
    `agent-report.md`, `implementation-diff.patch`, heartbeat/proxy sidecars and
    OpenCode upgrade receipts should be readable through bounded, redacted,
    path-safe helpers that return structured `log_unavailable` / unsupported
    diagnostics rather than raw shell requirements.

18. ⬜ **Bootstrap local Git identity for managed delivery workspaces.**
    Fresh managed/supervisor workspaces can reach `git commit` and fail with
    `Author identity unknown`. Candidate creation or commit helpers should set a
    safe local-only committer identity without touching global config.

19. ⬜ **Preflight per-component Git write capability before mutations.**
    Workspaces may have index/object writes available while refs are unwritable,
    or vice versa. Branch creation, staging, commit and cleanup helpers must
    check index, objects, refs and HEAD separately and return typed ownership or
    permission diagnostics before partial mutation.

20. ⬜ **Handle Git safe-directory ownership in all source-clone paths.**
    `prepare_candidate_clone` and verifier/materializer source resolution can
    fail on Git dubious-ownership checks. Use scoped/non-global safe-directory
    configuration for approved registered sources or return a typed
    `SOURCE_REPO_OWNERSHIP_BLOCKED` before registering partial candidates.

21. ⬜ **Bind `gitea_get_file` content to the requested ref.**
    File reads must resolve branch/tag/SHA to an exact commit, fetch content from
    that exact commit, and return `requested_ref` plus `resolved_commit_sha`.
    Cache keys must include exact resolved commit and path, not only repo/path or
    symbolic branch.

22. ⬜ **Permit safe framework dynamic-route filenames in workspace paths.**
    Path validation should allow literal `[` and `]` in components such as
    `[slug].astro` or `[id].tsx` while retaining absolute-path, traversal and
    root-escape protections across read and write helpers.

23. ⬜ **Make candidate verifier containers concurrency-safe.**
    Parallel verified-push/delivery operations must not collide on a fixed Docker
    container name. Allocate per-request verifier identities, clean them up
    deterministically, and test concurrent success/failure/cancellation paths.

24. ⬜ **Fail schema validation quickly before slow control-plane work.**
    Pure local/schema errors such as invalid enum values should return near the
    API boundary with allowed values and typed diagnostics, not after long router
    or backend latency.

25. ⬜ **Minimize `gitea_get_action_run` payloads.**
    Single-run reads should use the same compact allowlisted shape as run lists,
    avoiding raw nested Gitea actor/repository/user payloads unrelated to CI
    gating.

26. ⬜ **Add retry metadata and coalescing for rate-limit responses.**
    Rate-limit errors should expose retry-after, bucket identity and lower-cost
    alternate read guidance. Repeated schema/status reads should be coalesced
    where possible to avoid making contention worse.

27. ⬜ **Diagnose Docker build read-only path failures precisely.**
    Docker/Compose build failures should identify whether the read-only path is
    the compose context, HOME/buildx/cache, builder state or daemon namespace,
    and offer a sanctioned writable-builder-cache recovery path.

28. ⬜ **Terminalize successful CI jobs after Docker teardown timeout.**
    If all declared job steps succeeded but final runner container cleanup times
    out, the CI controller should still reach a bounded terminal state and
    release downstream jobs, or fail/reconcile explicitly rather than leaving the
    workflow indefinitely `in_progress`.

29. ⬜ **Capture Selenium sidecar readiness diagnostics.**
    E2E infrastructure failures before tests should preserve sidecar container
    status/logs and distinguish startup crash, image/runtime failure and product
    E2E failure. Retrying infra failures must not require source changes.

30. ⬜ **Add Gitea Actions artifact listing and bounded download.**
    Operators can inspect runs/jobs/logs, but need first-class artifact metadata
    and safe archive/file retrieval with run/head provenance, size limits,
    checksum where available, path traversal protection and expiry diagnostics.

31. ⬜ **Add guarded Gitea Actions rerun primitives.**
    Exact-head failed-job/full-run reruns should be possible without fake commits
    or PR recreation. Require expected run id/head SHA/terminal failed state,
    reconcile ambiguous mutation responses, and return previous/new attempt plus
    selected jobs.

32. ⬜ **Allow audited cleanup of superseded closed-unmerged PR branches.**
    Branch deletion should support stale same-repo heads of closed-unmerged PRs
    when the caller supplies exact branch SHA, verifies no open PR uses it, and
    provides explicit superseding merged PR/ref evidence. Keep separate from the
    narrower tree-equivalent cleanup already implemented.

33. ⬜ **Align outer `run_agent` success envelopes with non-submission results.**
    Pre-submit validation failures, blocked terminal replays and other
    non-submitted outcomes must not return outer `ok=true` with submission
    success text. Bind outer status, nested status, job/attempt presence and
    submission occurrence.

34. ⬜ **Support managed read-only agent tasks over non-Git workspaces.**
    Non-Git registered script/config/data workspaces need immutable snapshot
    source contracts for read-only analysis tasks, with explicit delivery/mutation
    disabled unless a separate trusted write contract exists.

## P3 / Capability and ergonomics wishlist

35. ⬜ **Add project catalog filtering and pagination.**
    `project_list()` should support exact id, prefix/text filters, project type,
    tags, parent, limit/offset, total count and compact projections so operators
    do not retrieve hundreds of historical candidates to find one canonical id.

36. ⬜ **Make candidate preparation observable and cancellable.**
    Slow `prepare_candidate_clone` calls should support async mode, progress
    phases, cancellation before registration, and idempotent retry while keeping
    the current synchronous path for small clones.

37. ⬜ **Expose first-class bounded health and HTTP smoke probes.**
    Add internal and public HTTP/TCP probes with allowlisted method/host/path,
    bounded response bytes, timeout, provenance, redirect/status/body metadata,
    and clear DNS/refused/timeout/non-2xx diagnostics.

38. ⬜ **Bridge frontend/Astro delivery to the existing builder/runtime path.**
    Frontend repos need typed build/smoke/deploy adapters that detect package
    manager/lockfile/service identity, use the approved Astro/builder substrate
    or bounded Node container, and return build/up/smoke evidence without raw
    `npm` or ad-hoc deploy scripts.

39. ⬜ **Avoid duplicate heavy CI after already-green PR heads.**
    Post-merge `master` pushes should be able to fast-path to quick sanity plus
    build/deploy/host-smoke when the merge commit is a clean merge of an exact
    green PR head. Direct pushes and ambiguous histories must fall back to full
    CI.

40. ⬜ **Model agent roles and authoritative closeout explicitly.**
    Agent reports may say implementation/review/checks are complete, but should
    not self-close findings or mark a project ready. Gateway should preserve
    role, scope, exact source/diff/check evidence and allow multiple isolated
    implementer/reviewer/adversarial lanes without workspace/proxy collisions.

41. ⬜ **Improve operator-safe command-plane recovery helpers.**
    Provide typed local branch switch/sync, safe scratch cleanup, project-level
    Docker/build diagnostics and policy-denial guidance so operators do not need
    raw shell fallbacks for common recovery actions.
