# Agent SSH Gateway — TODO

Last cleaned: 2026-10-09.

This file is the active backlog only. Completed audit notes, fixed CI blockers,
merged PR evidence, and superseded diagnostics should live in PR history,
changelogs, or dedicated audit archives — not in TODO.

On 2026-10-09 the cleaned `master` backlog and the long-lived dirty canonical
`TODO.md` were reconciled into this single view. Historical duplicates and
superseded diagnostics were not reintroduced; unique still-open findings and
useful current evidence were folded into the corresponding active items.

Open backlog count after cleanup: **42**

- **P1 / critical:** 11
- **P2 / important:** 20
- **P3 / capability / wishlist:** 11

## P1 / Critical blockers


2. ⬜ **Provide canonical and candidate write-plane recovery paths.**
   Some projects report candidate fallback while task policy requires canonical
   non-candidate execution. Others have handoff writes failing while archive/task
   maintenance succeeds. Expose exact write capability for code, Git and handoff
   surfaces, and return `CANONICAL_WRITE_PLANE_UNAVAILABLE` with safe recovery
   steps when a clean writeable canonical workspace cannot be provisioned.

   **Candidate lineage recovery — observed during the RAG audit, 2026-10-06:**
   `prepare_candidate_clone` for `quart-core`, branch
   `fix/rag-citations-book-delivery-20261007`, returned
   `CANDIDATE_LINEAGE_SCAN_FAILED` / "candidate lineage metadata is unavailable"
   because of the older, different-branch candidate
   `candidate-quart-core-feat-supervisor-read-knowledge-capabilities-20261001`.
   Request: `b974b196-6532-49af-8b7e-772eb8bc762c`. The diagnostic directs
   the caller to a guarded cleanup workflow, but no such candidate-record
   recovery operation was exposed in this caller's callable tool catalog.
   This blocked an independent branch of the same project; it does not prove
   that candidate creation is broken for every project.

   Provide a typed inspect/reconcile/remove-record-and-recreate workflow:
   - Scope lineage lookup to the requested project/branch and isolate unrelated
     corrupt entries. Quarantine/report an invalid record without treating it as
     a valid candidate or disabling duplicate/path/ownership checks.
   - Inspect the exact registry/lineage identity and revision, workspace path,
     Git state, active jobs/leases and retained artifacts. Missing or corrupt
     metadata must remain unknown until recovery establishes those facts.
   - Allow guarded tombstoning/removal of a proven stale or irrecoverable
     registration/lineage record, with an audit snapshot and CAS/fencing against
     concurrent use. Preserve the directory, dirty files, commits and agent
     artifacts; metadata cleanup must not implicitly delete workspace contents.
     Active or ambiguous ownership requires an explicit fenced recovery decision.
   - Recreate a fresh registered candidate from a verified exact remote base
     after reconciliation, using a new directory when the old path is retained.
     Record predecessor/replacement identities; recover trustworthy metadata
     where possible instead of blindly fabricating or rewriting it.
   - Make retries and crash recovery idempotent: no duplicate candidates,
     orphan registrations, lost work or partially removed registry/lineage pairs.
     Return the replacement id, exact base/head, recovery receipt and next steps.

   Acceptance: cover missing/truncated/malformed lineage, unrelated stale rows,
   dirty retained work, live/expired/unknown leases, concurrent prepare/recovery,
   crash between cleanup and registration, and retry after an ambiguous timeout.
   An unrelated damaged record must not block a new branch; guarded cleanup and
   recreation of the damaged candidate must succeed without deleting its code.
   Exercise the reproducer above through the client-exposed recovery tools.

   **Operator recovery report, 2026-10-07:** no task/lease registry was found;
   removing the old directory broke
   `candidate-quart-core-fix-ci-greenlet-test-job-20261001`, whose Git alternates
   and origin depended on it. The operator restored the dependent clone's
   objects/remote and reported successful `git fsck` with dirty work preserved.
   A subsequent exact-base prepare at `main@2bbb8dd` still returned
   `CANDIDATE_LINEAGE_SCAN_FAILED`, now naming that repaired dependent clone
   (request `9726cf27-73c4-4f6d-b92a-784d327bc0ac`). Git integrity alone
   therefore does not establish valid registration/lineage metadata.
   Cleanup must inspect inbound Git alternates, origin and worktree dependencies
   before directory removal. Preserve the source or first make dependants
   self-contained against a trusted source, retaining local-only objects/refs;
   verify them before deletion. Missing registry/leases is unknown, not proof of
   inactivity. Add a regression with chained shared clones and unpushed commits:
   recovery must preserve every dependent HEAD, ref and dirty file and leave
   `git fsck` clean. Report metadata recovery and directory GC separately.

4. ⬜ **Reconcile stale unbound fleet leases without duplicate work.**
   Historical `attempted` / `legacy_unknown` lease rows can consume all fleet
   capacity even when no lease has a recent heartbeat. Add fleet status that
   separates live workers, reclaimable never-attempted leases and ambiguous rows;
   require explicit evidence/acknowledgement before deleting ambiguous leases;
   tombstone every reclaimed lease exactly once.

6. ⬜ **Keep ChatGPT-visible tool schemas in parity with server manifests.**
   Tools can appear available in `tools_manifest` while missing or having a
   different callable schema in the ChatGPT resource catalog. PR-A measurement
   now accepts a bounded client-reported tool list, binds retained attestations to
   the server-created MCP lifecycle + opaque auth identity + live toolset hash,
   emits complete server/client diff diagnostics, and keeps incomplete omissions
   explicitly unknown. Client reports remain unverified and do not gate existing
   Git mutations. This item stays open until a real external connector submits a
   complete report and identifies the filtering/caching/allowlist mechanism; do
   not add a curated catalog workaround before that evidence exists.

   **2026-10-09 external-session evidence:** after PR #489 was merged and
   post-merge run #14985 completed build-and-push, deploy and host-smoke on
   `master@0e357599a6aa...`, the authoritative live manifest reported
   `gitea_rerun_action_job` enabled/available and included it in the server
   toolset hash. The same already-attached ChatGPT `api_tool` catalog still
   omitted the tool and still exposed older schemas that omit newer optional
   arguments such as `closed_unmerged_cleanup_reason` and the client-observation
   parameters on `tools_manifest`. Supplying those hidden manifest parameters
   through the transport still succeeded and produced a lifecycle/auth/hash-bound
   incomplete attestation; because the report was intentionally incomplete, the
   missing job-rerun guard correctly remained `unknown`, not falsely absent.

   This narrows the remaining investigation to external catalog refresh/filtering
   rather than FastMCP registration. The same dirty-backlog evidence also covered
   server-visible/client-hidden `closed_unmerged_cleanup_reason`, `git_fetch_ref`
   and `docker_deploy_contract`; keep them under this single parity finding rather
   than opening one ticket per missing schema. Obtain a **complete** external
   catalog report after refresh/reconnect and identify which client cache/allowlist
   layer retains the old schema. Keep mutation behavior independent of this
   diagnostic signal.

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
    jobs, ordinary red CI, and repositories whose branch policy explicitly has no
    required status checks. When checks are disabled, either allow the exact-head
    merge or return a typed product-policy requirement that Actions are mandatory;
    do not collapse that state into `NO_REQUIRED_RUN_FOUND`. Exact-head acceptable
    evidence must be explicit in repository policy and never silently weakened.

42. ⬜ **Make Docker inventory scope explicit and machine-readable.**
   `docker_ps(all=true)` still returns only the containers visible through the
   Gateway's configured Docker endpoint, but the response does not identify that
   endpoint/context or state whether the inventory is host-complete. Historical
   operator evidence showed a 39-vs-151 mismatch; a fresh 2026-10-07 call still
   returned only 38 rows with no endpoint/scope/completeness metadata. Absence
   from this view must not be treated as proof of system-wide absence.

   Acceptance: Docker inventory responses expose a safe endpoint/context identity
   and an explicit scope/completeness field; documentation defines what `count`
   means; configured multiple endpoints are selectable or explicitly reported as
   unavailable; regression coverage prevents a scoped daemon view from being
   presented as system-wide inventory.

47. ⬜ **Provide redacted secret propagation/provisioning across approved deployment and CI scopes.**
    Production deploys and cross-repository CI can require an existing root-only
    credential without any safe way to copy/provision it to the approved target.
    Browser Service was blocked on `PROXY_REGISTRY_API_KEY`; work-session-service
    could not pull the private `agent-memory-service` image because its repository
    `REGISTRY_TOKEN` was absent. Add an audited primitive that accepts only
    allowlisted source/target identities and variable names, never returns or logs
    the value, preserves target ownership/mode, and reports presence/equality or
    opaque hash evidence only. It may either synchronize an approved env/secret
    target or inject the value only inside one guarded deploy/Actions boundary.
    Acceptance must prove source/target identity, no plaintext exposure, exact
    postcondition verification, clean-runner private-image pull, and fail-closed
    behavior for missing/ambiguous credentials.

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
    collection collisions, or verifier/CI tool-version drift. A 2026-10-08 Kojo
    verified-push attempt additionally proved the trusted verifier image could use
    `/usr/bin/python` without `pytest`, while the repository verifier could execute
    the targeted tests. Expose candidate-verifier versus Actions-plane capability
    provenance explicitly; advertised checks must either run in the selected plane
    or return a typed environment/bootstrap diagnostic instead of code failure.

15. ⬜ **Make agent cancellation and submission atomic/reconcilable.**
    Cancellation can mark a local job ambiguous while the remote runner keeps
    heartbeating, and rate-limited submission can return failure after a runner
    has already started unbound. The 2026-10-08 infra-quart pilot reproduced the
    cancellation side again: the Gateway job became ambiguous while the same
    remote runner PID continued fresh heartbeats with no terminal artifacts.
    Preserve a cancellable/fenceable remote handle or prove no runner/proxy
    acquisition occurred before returning failure; replacement work must remain
    blocked until the old writer is terminal or fenced.

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

    **2026-10-07 update:** PR #468 removed `setup-python`'s unused pip-cache
    post-action from uv-driven test/E2E jobs, eliminating the concrete teardown
    path that broke run #14371. Exact rerun #14371 attempt 2 and post-merge run
    #14458 both completed successfully. Keep this item open for the broader
    controller-level guarantee: an unrelated future teardown timeout must still
    terminalize or reconcile explicitly rather than strand the workflow.

30. ⬜ **Add Gitea Actions artifact listing and bounded download.**
    Operators can inspect runs/jobs/logs, but need first-class artifact metadata
    and safe archive/file retrieval with run/head provenance, size limits,
    checksum where available, path traversal protection and expiry diagnostics.

31. ⬜ **Add guarded Gitea Actions rerun primitives.**
    Exact-head failed-job/full-run reruns should be possible without fake commits
    or PR recreation. Require expected run id/head SHA/terminal failed state,
    reconcile ambiguous mutation responses, and return previous/new attempt plus
    selected jobs.

    **2026-10-09 implementation/deploy evidence:** full-run rerun was already
    live; PR #489 added `gitea_rerun_action_job` with exact run/job/head/current-
    attempt fencing, failed/cancelled-only policy, logical-job uniqueness and
    bounded ambiguous-response reconciliation. Exact head `6d932cc09f5b...`
    passed PR run #14967; merge `0e357599a6aa...` passed post-merge run #14985,
    including build-and-push, deploy and host-smoke. The live authoritative
    `tools_manifest` now reports both rerun tools enabled/available with
    `mcp:repo` + `mcp:admin`, and a session-bound incomplete client attestation
    correctly leaves `gitea_rerun_action_job` guard coverage `unknown`.

    Keep this item open only for caller-visible acceptance: the current external
    ChatGPT resource catalog still omits the new job-rerun tool even though the
    deployed server surface contains it. Do not add another rerun primitive or
    bypass the catalog; finish exposure/parity under item 6 (and cache invalidation
    under item 45), then prove a real external caller can invoke one failed job
    without rerunning the whole workflow.

33. ⬜ **Align outer `run_agent` success envelopes with non-submission results.**
    Pre-submit validation failures, blocked terminal replays and other
    non-submitted outcomes must not return outer `ok=true` with submission
    success text. Bind outer status, nested status, job/attempt presence and
    submission occurrence.

34. ⬜ **Support managed read-only agent tasks over non-Git workspaces.**
    Non-Git registered script/config/data workspaces need immutable snapshot
    source contracts for read-only analysis tasks, with explicit delivery/mutation
    disabled unless a separate trusted write contract exists.

43. ⬜ **Expose command-plane degradation state separately from connector/auth failure.**
    Long-lived agent sessions can temporarily lose callable tools during gateway
    restart, image boot, host overload or session expiry. Today these distinct
    states can all look like "connector gone" and provoke unnecessary re-auth or
    duplicate retries. Expose a cheap bounded plane status such as
    `ready|starting|overloaded|session_expired` with optional `retry_after_s`,
    build identity and start time; document expected transient gaps across deploys.

44. ⬜ **Prevent candidate worktree drift after verified delivery.**
    A verified candidate must remain byte-for-byte consistent with its Git HEAD
    unless an explicit write operation mutates it. In a JS Chat Engine delivery,
    remote exact head `ac66a850673cc0383163298e337fe2e3f0b96ec1` retained
    `_MIGRATION_LOCK_WAIT_SECONDS = 5.0`, while the registered candidate later
    showed that tracked line deleted without a session write call, producing false
    local test failures until restored. Add post-delivery worktree==HEAD proof,
    identify/remove the mutation path, and fail closed with attributed evidence if
    drift is detected.

48. ⬜ **Fail project-scoped Compose mutations before confirmation when allowed roots are not configured.**
    A registered `docker_compose_up` can return `confirmation_required`, but the
    subsequent `confirm_operation` then fails before Docker execution when
    `MCP_ALLOWED_PROJECT_ROOTS` is unset. Availability/preflight must reject this
    configuration before creating a one-time pending action, or safely resolve
    the registered project root without requiring callers to know host paths.
    Acceptance covers unset-root fail-fast behavior, successful registered-root
    execution, and proves confirmations are not consumed by impossible actions.

49. ⬜ **Expose bounded destructive-audit health and recovery evidence.**
    Guarded Gitea close/delete correctly fail closed with `AUDIT_UNAVAILABLE`
    before mutation, but callers cannot distinguish transient audit transport or
    storage outage from malformed cleanup input. Add a safe read-only audit
    subsystem status/recovery signal with retryability and correlation metadata;
    keep destructive mutations impossible unless intent persistence succeeds.
    Acceptance covers zero mutation during outage and exactly-once audited success
    after recovery for both PR close and exact-SHA branch deletion.

50. ⬜ **Allow guarded workspace parent-directory creation.**
    `workspace_file_write` cannot bootstrap a new package/workflow tree when a
    parent directory does not already exist, forcing placeholder commits or
    manual scaffolding. Add either explicit `create_parents=true` or a bounded
    `workspace_mkdir`/scaffold primitive with project-relative paths, depth/name
    limits, symlink/root-escape protection and receipts. Default behavior must
    remain fail-closed with a typed `PARENT_MISSING` diagnostic.

51. ⬜ **Reconcile `gitea_push_verified_commit` after ambiguous client/transport timeout.**
    A caller timeout can occur after the remote feature branch has already moved
    to the exact expected head, creating a false-negative delivery result and
    making a blind retry unsafe. After timeout/5xx, perform bounded fresh remote
    reads: return reconciled success when the exact head/postcondition is proven;
    return definite no-mutation only when the old tip is proven; otherwise return
    `MUTATION_OUTCOME_UNKNOWN` with no blind-retry guidance. Regression coverage
    must include response-lost-after-success and timeout-before-write cases.

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

45. ⬜ **Add session-scoped tool-schema caching with explicit invalidation.**
    Long-lived clients currently rediscover callable tool schemas repeatedly, and
    a partial catalog during gateway boot can look like a permanent capability
    loss. Define an etag/build-id based cache contract so schemas may be reused
    while the plane is stable and are invalidated on build or plane-state change;
    do not weaken authentication or server-side authorization checks.

    **2026-10-09 reproducer:** after master `0e357599a6aa...` completed
    build-and-push, deploy and host-smoke in run #14985, the live server
    `tools_manifest` advertised `gitea_rerun_action_job` as enabled/available
    and emitted toolset hash `sha256:c88900df0e1e...`, while this same long-lived
    ChatGPT session's external `api_tool` resource catalog still exposed the
    pre-deploy surface and omitted that tool. The server can accept bounded,
    lifecycle/auth/toolset-hash-bound `client_visible_tool_names` attestations,
    but the external catalog also hides those optional `tools_manifest`
    parameters from its callable schema. This proves a real cache/filtering
    invalidation gap outside the authoritative FastMCP tool manager, not a
    missing server registration.

    Acceptance should include a long-lived client attached before a deploy:
    once the server toolset hash/build identity changes, its next schema lookup
    must refresh to the new `mcp.tools/list` surface (including optional argument
    additions), or return an explicit stale-catalog state with a bounded refresh
    action. A successful server deploy must not leave the old callable catalog
    indefinitely authoritative in that session.

46. ⬜ **Make `info(project)` verification commands executable verbatim.**
    Verification guidance must include any project-specific mypy targets/options,
    not just a generic `uv run --extra dev mypy`. A JS Chat Engine delivery
    observed the advertised bare mypy command fail with `Missing target module,
    package, files, or command`, while `uv run --extra dev mypy app scripts tests`
    was the actual project contract. Add a round-trip contract test that every
    command advertised by `info(project).verification.commands` can be executed
    verbatim from the registered project root.

52. ⬜ **Expose guarded Gitea issue-state mutation.**
    The external surface can list/read Gitea issues but cannot reconcile an issue
    to closed after its fix is independently proven. Add a same-repository
    issue-state mutation with fresh expected-state/version fencing, narrow field
    allowlisting, audit evidence and post-write verification. Acceptance covers
    close success, already-closed idempotence, concurrent-state mismatch, missing
    issue and remote failure with zero unintended mutation.

53. ⬜ **Add guarded bulk tree / multi-file candidate bootstrap.**
    Greenfield packages currently require many sequential single-file workspace
    writes and pre-created directories. Add a bounded `workspace_apply_tree` or
    equivalent patchset/materialization primitive with file-count/byte limits,
    project-relative path validation, parent creation under policy, aggregate and
    per-file hashes, dry-run support and atomic/fail-closed semantics. Acceptance
    includes a ≥10-file bootstrap plus traversal/oversize/partial-failure tests.
