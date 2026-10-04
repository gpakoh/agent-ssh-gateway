# Agent SSH Gateway — TODO

Last cleaned: 2026-10-04.

This file is the active backlog only. Completed audit notes, fixed CI blockers,
merged PR evidence, and superseded diagnostics should live in PR history,
changelogs, or dedicated audit archives — not in TODO.

Open backlog count after cleanup: **32**

- **P1 / critical:** 10
- **P2 / important:** 15
- **P3 / capability / wishlist:** 7

## P1 / Critical blockers


2. ⬜ **Provide a canonical write-plane recovery path.**
   Some projects report candidate fallback while task policy requires canonical
   non-candidate execution. Others have handoff writes failing while archive/task
   maintenance succeeds. Expose exact write capability for code, Git and handoff
   surfaces, and return `CANONICAL_WRITE_PLANE_UNAVAILABLE` with safe recovery
   steps when a clean writeable canonical workspace cannot be provisioned.

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

   **GPT Browser Bridge production evidence, 2026-09-30:** the current green
   release was published from `main@f28e49763e58d69421a05cc05172870c6a50aede`
   as image ID
   `sha256:e8668bd0189c282467f2b8c2986f2600a6887de81dabd6740e708675aa9b2fe3`
   and registry digest
   `sha256:c5f6554ab94fc100f41e32f5309412de240d33a723e194e88fcee04447aec223`,
   while production still ran old digest
   `sha256:7a543f4be9e12208b17eaef8d2975e57cd993ad02c3a3e284cbee3dc878f72e5`
   with `RestartCount=25`, Docker health `unhealthy`, and a large failing
   streak. The repository's official `deploy/deploy-gpt-browser-bridge.sh`
   owns the required deployment transaction: release-image binding, lock and
   contract preflight, stale profile-lock cleanup, replay-stable Compose
   invocation, health smoke, deploy-state/LKG write and rollback. The available
   execution surfaces could not run that transaction safely: the default SSH
   session was an ephemeral `/home/mcpuser` environment with no checkout and
   no `docker`; the managed OpenCode workspace likewise had no Docker or
   `infra-quart` operator root and hit rootless/userns denial; Docker/Compose
   primitives could see the production daemon and exact image but did not inherit
   the operator-owned Compose environment, with raw Compose preflight failing on
   missing `BROWSER_SERVICE_INTERNAL_API_KEY`. Calling raw
   `docker_compose_up` / restart would also bypass the repository's LKG/state
   and rollback contract.

   Extend this item with a guarded `run_deploy_contract`-style capability:
   accept a registered repository plus exact source/ref, allowlisted deploy
   script identity/content hash, exact immutable release digest and expected
   Compose project/service; execute on the Docker-capable operator plane with
   operator-owned env available only inside the execution boundary; require
   confirmation for mutation; and return bounded preflight, image identity,
   health/readiness, restart stability, deploy-state/LKG and rollback evidence.
   Fail closed when the caller resolves only to an ephemeral non-Docker session,
   required Compose env cannot be proven, the release digest does not match the
   published/local artifact, or execution would fall back to raw Docker/Compose
   outside the declared deploy contract. Acceptance must include the Browser
   Bridge case above: production rebinds to digest `c5f6554a...`, the deploy
   state records the new LKG, unrelated services are not recreated, and an
   induced failed health/readiness gate proves rollback remains available.

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

30. ⬜ **Add Gitea Actions artifact listing and bounded download.**
    Operators can inspect runs/jobs/logs, but need first-class artifact metadata
    and safe archive/file retrieval with run/head provenance, size limits,
    checksum where available, path traversal protection and expiry diagnostics.

31. ⬜ **Add guarded Gitea Actions rerun primitives.**
    Exact-head failed-job/full-run reruns should be possible without fake commits
    or PR recreation. Require expected run id/head SHA/terminal failed state,
    reconcile ambiguous mutation responses, and return previous/new attempt plus
    selected jobs.

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

    **Zalesskiy SUP evidence, 2026-10-04:** two independent prerequisites
    remain. The live Gateway build `b9ffe1f5c4d71d9d352fa2a2d885f34ee1600568`
    advertises `docker_deploy_contract` as enabled/available in
    `mcp_client_write` with `mcp:docker:admin`, but this ChatGPT session's
    callable tool registry has no matching tool. An authorized read-only
    `docker_exec` plus `confirm_operation` succeeds, so this is not evidence
    of a general Docker-admin access failure. Track client exposure under
    existing item 6; PR #446 measures parity and explicitly leaves external
    exposure unverified. Separately, `_DEPLOY_CONTRACTS` in
    `examples/mcp_server/mcp_infra/adapters/docker.py` at Gateway
    `c63fda43604fb49afdc8b8bca62c2cd3243d31f9` allowlists only
    `gpt-browser-bridge`. Exposing that schema alone will not authorize Astra.

    Scope the frontend adapter to the existing reviewed `gpakoh/astra`
    `deploy.sh astro` transaction (item 8), not a generic shell runner.
    Bind both infrastructure HEAD and the independent
    `gpakoh/zalesskiy-sup` source HEAD, reviewed script/Compose content and
    build inputs; revalidate them at confirmation and capture the resulting
    immutable image ID. Preserve the script's lock, clean/main/remote-SHA
    gates, Directus read-only preflight, cache-busted build, frontend-only
    `up --no-deps --no-build --wait`, stability window and image rollback.
    Use the Docker-capable operator plane and keep operator secrets inside it;
    the frontend Compose path must not acquire the full stack's
    `LICENSE_KEY` requirement. Never use `--remove-orphans`.

    Acceptance: a client-invokable preflight and confirmation publish only
    Astro; exact source/build/runtime evidence agrees; Directus, PostgreSQL,
    Redis, Lana and Minio container IDs/StartedAt stay unchanged; public smoke
    and isolated health-failure rollback pass. Reconcile ambiguous execution
    before retry. The completed local-agent publication is reference evidence,
    not a Gateway deploy smoke: Astra `ad26f77`, site merge `c231ffa`
    (post-merge CI 13784 success), runtime image
    `sha256:1c029fc24639258d51a493c777237de00158675bc3981b06f98636de908e5356`,
    running/healthy with zero restarts. A Gateway read-only public probe
    independently confirmed the Yandex verification file returned HTTP 200,
    162 bytes and the expected verification body. Do not redeploy that release
    merely to collect evidence.

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
