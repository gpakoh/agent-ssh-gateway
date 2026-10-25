# Agent SSH Gateway — TODO

> **Public archive copy:** historical links to a private Git server have been
> replaced with a non-routable example hostname. The original intake is retained
> in the Git history; this copy is intentionally sanitized for public docs.

Last reconciled: 2026-10-12.

This file is the active backlog only. Completed audit notes, fixed CI blockers,
merged PR evidence, and superseded diagnostics should live in PR history,
changelogs, or dedicated audit archives — not in TODO.

On 2026-10-12 the active backlog was reconciled against current merged/runtime
evidence. Eight entries with completed acceptance were removed: #4 (fleet stale
lease reconciliation), #12 (typed CI merge-policy evidence), #21 (immutable
Gitea file ref binding), #28 (CI teardown terminalization), #31 (guarded job-level
Actions rerun), #39 (post-merge heavy-CI fast path), #42 (machine-readable
Docker inventory scope), and #51 (verified-push ambiguous-outcome reconciliation).
Partial progress on still-open items remains recorded under those items rather
than being treated as closure.

Open backlog count after cleanup: **34**

- **P1 / critical:** 8
- **P2 / important:** 16
- **P3 / capability / wishlist:** 10

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

   **Exact-remote-base drift evidence, 2026-10-09:** authoritative Gitea
   `gpakoh/infra-quart/main` advanced to `c396301a3a82912d4da87d8902fa2abe3ee028c9`
   after merged PR #334, while `prepare_candidate_clone(base_ref=main)` still
   materialized stale `dcec7ad7e5a069024e62fcad42d099a2565f9998`; requesting the exact
   current SHA returned `SOURCE_REF_NOT_AVAILABLE` repeatedly. The caller failed
   closed, did not open a stale-base PR, and exact-SHA deleted its temporary
   remote candidate ref after read-after-write proved a prior ambiguous push had
   actually succeeded. Acceptance for this item must also cover remote-main-ahead-
   of-local-source: symbolic `main` and exact current SHA must resolve to the
   authoritative remote commit or return an explicit refresh/recovery action,
   never silently create a candidate from stale local state.

   **2026-10-09 deployed stale-source acceptance:** PR #498 merged as
   `eaeb7d48ad81...`; post-merge run #15202 completed build-and-push, deploy and
   host-smoke successfully, and live `health` reported Gateway/MCP build SHA
   `eaeb7d48ad81...`. At acceptance time authoritative `gpakoh/infra-quart/main`
   was `e8bbef68af3f...`, while the registered source checkout remained at
   `HEAD=a38021c7890c...` with stale local `gitea/main=981df946b884...`.
   Calling the deployed `prepare_candidate_clone(project=infra-quart,
   base_ref=main)` returned typed retryable `SOURCE_REMOTE_STATE_UNKNOWN` with
   `recovery_action=refresh_or_restore_trusted_remote_access`; it did not create
   a candidate directory and did not materialize either stale local SHA. This
   closes the silent-stale-fallback subcase. Item #2 remains open for the broader
   canonical write-plane and lineage recovery contract described above.

   **2026-10-10 deployed inbound-dependency fence acceptance:** PR #501 merged as
   `71b1204b82d5...`; post-merge run #15307 completed build-and-push, deploy and
   host-smoke successfully, and live `health` reported both Gateway and MCP on
   exact SHA `71b1204b82d5...`. A disposable `/tmp` fixture executed inside that
   deployed `mcp-server` image created a candidate plus a sibling `git clone
   --shared`, then invoked the real `candidate_cleanup` core. Cleanup failed
   closed with non-retryable `WORKSPACE_CONTENDED`, identified the dependant as
   `candidate-live-shared-dependent` with dependency types `alternates` and
   `local_origin`, and returned
   `recovery_action=make_dependants_self_contained_then_retry_cleanup`. The target
   candidate directory and registry entry remained intact, the dependant HEAD
   remained readable, and `git fsck --full` returned 0. This closes the unsafe
   inbound-dependency deletion subcase through the deployed core. The live MCP
   manifest still exposes `prepare_candidate_clone` but no `candidate_cleanup`
   operator tool, so item #2 remains open for caller-visible inspect/reconcile/
   cleanup and dependant-self-containment recovery rather than for this fence.

   **2026-10-10 bundle-backed delivery evidence:** standard
   `prepare_candidate_clone` candidates in `gpakoh/nod-gateway` use an immutable
   local bundle as their trusted source. Multiple clean, scoped candidates then
   failed `gitea_push_verified_commit` before remote mutation during the
   `clone_bundle` staging phase (`GIT_PUSH_FAILED` / later
   `CANDIDATE_SOURCE_UNAVAILABLE`); fresh Gitea reads proved the destination refs
   absent for the definite pre-mutation cases. Fold this into #2 rather than a
   new P2 item: the candidate Git write plane must reliably materialize exact
   bundle-backed heads, or return a bounded typed recovery action without
   requiring candidate reconstruction. The response-lost/timeout ambiguity of
   the same incident is tracked under existing #51.

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

   **2026-10-09 divergent closed-unmerged cleanup reproducer:** `infra-quart`
   still has superseded closed-unmerged branches whose exact heads are proven but
   whose trees diverge from current default. The live server manifest documents
   optional `closed_unmerged_cleanup_reason` on `gitea_delete_branch`, while this
   ChatGPT-facing callable schema still exposes only owner/repo/branch/
   `expected_head_sha`. An exact-SHA delete therefore fails closed with
   `POLICY_DENIED` and `mutation_occurred=false`, and the caller cannot supply the
   audited explicit cleanup reason without a raw-API workaround. Keep this as
   concrete caller-impact evidence under #6, not as a separate cleanup feature:
   once external schema parity is restored, re-run one superseded infra-quart
   branch cleanup and verify exact-SHA deletion plus absent ref while all existing
   open-PR/protected/default/audit guards remain fail-closed.

   **2026-10-09 terminal reconnect evidence:** PR #491 added fresh-session bearer
   and public OAuth schema acceptance; its first post-merge deploy #15036 exposed
   an EOF-SSE parser defect and correctly rolled back. PR #492 fixed that parser,
   and post-merge #15062 completed successfully. The later workflow change #494
   then redeployed exact `master@9c4bc3ff677c...`; run #15114 completed
   build-and-push, deploy and host-smoke successfully, with host-smoke proving
   `web-ssh-gateway`, `mcp-server` and `mcp-oauth` all running that exact SHA and
   a fresh public OAuth black-box flow passing. After that full restart/reconnect,
   authoritative `tools_manifest` still exposes `gitea_rerun_action_job`, while
   this attached ChatGPT `api_tool.list_resources(..., query="gitea_rerun_action_job")`
   still does not expose the callable tool. This isolates the remaining defect to
   the external ChatGPT resource-catalog refresh/filter/cache layer rather than
   Gateway deployment, FastMCP registration or public MCP/OAuth reconnect behavior.

   **2026-10-10 MCP notification boundary evidence:** PR #504 merged as
   `4955470c1f8a...`; post-merge run #15403 completed build-and-push, deploy and
   host-smoke successfully, and live Gateway/MCP health reported exact build SHA
   `4955470c1f8a...` with 139 registered tools. The deployed `tools_manifest`
   emitted the standard MCP `notifications/tools/list_changed` signal successfully
   for toolset hash `sha256:88b62a9a5c4c...`, but the attached ChatGPT
   `api_tool` catalog still omitted both newly exposed `candidate_cleanup` and
   the older `gitea_rerun_action_job`. A second `tools_manifest` call again
   reported `catalog_refresh_signal.status=sent` rather than `already_sent`,
   proving these tool calls are bound to separate short-lived MCP lifecycles from
   the external schema-catalog snapshot. Server-side list-change notification is
   therefore implemented and deployed, but it cannot invalidate this caller's
   catalog. Keep #6 open at the external client/platform boundary; do not add a
   Gateway call-by-name bypass. Closure requires the external catalog itself to
   refresh/rebind (or expose an explicit stale/refresh control) and then prove the
   real callable schemas for `candidate_cleanup` and `gitea_rerun_action_job`.

   **2026-10-10 closed-unmerged cleanup evidence:** agent-memory-service PR #13
   and opencode-adapter PRs #82/#84 were independently proven superseded by
   current-base merged replacements, including byte-identical useful blobs and
   green post-merge evidence, yet their stale exact-SHA branches could not be
   driven to terminal cleanup from this caller. The server already advertises
   the guarded cleanup/create-at-SHA capabilities needed to record supersession,
   while the attached external catalog hides the relevant callable schema. Fold
   these reproductions into #6/#45 rather than creating a separate cleanup item;
   once external schema parity is restored, re-run one proven-superseded branch
   through the audited exact-SHA cleanup path without force/reset or dummy commit.

   **2026-10-12 fresh-caller progress:** this newly attached caller now exposes
   `candidate_cleanup`, `gitea_rerun_action_job`, `gitea_create_branch_at_sha`,
   `gitea_set_default_branch`, guarded deploy tools and Actions-secret provisioning.
   Live `tools_manifest` reports 147 registered tools and no names missing from the
   supplied client observation, but the observation remains explicitly incomplete
   and not independently verified. Keep #6 open for complete schema parity,
   including optional arguments and a proven long-lived refresh/rebind path.

7. ⬜ **Reconcile Gitea Actions lifecycle, stranded jobs and cancellable runs.**
   CI runs/jobs can remain `waiting` / `in_progress` after all useful work is
   done, after cleanup timeouts, or after old workflow jobs later become runnable.
   Add exact-run/head cancellation, stale/orphaned container reconciliation,
   terminalization of false-condition/dependency-blocked jobs, and safe rerun
   eligibility diagnostics.

   **2026-10-12 progress:** PR #518 merged guarded exact-run cancellation with
   run/head/attempt fencing, audit-before-mutation and bounded postcondition
   reconciliation. Keep #7 open for stale/orphaned runner-container cleanup and
   terminalization/reconciliation of stranded dependency-blocked jobs.

   **2026-10-23 Site Audit CI evidence:** `site-audit-platform` post-merge
   run #19150 failed after a bounded Docker create timeout in Nginx ingress
   smoke; a uniquely named late-created Nginx container remained in
   `Created` state and was later absent from read-only inventory. PR #186
   added exact-name fixture removal/absence verification. Its run #19168
   passed Python and PostgreSQL jobs but Docker qualification failed twice
   on distinct daemon-pressure symptoms: ambiguous `docker run` completion
   in API/DB smoke (attempt 1) and a 120-second pinned PostgreSQL image
   pull timeout (attempt 2). These are CI runner/daemon lifecycle failures,
   not evidence that the source contract passed. Extend #7 with bounded
   runner-state/temporary-container and network reconciliation, clear job
   resource-pressure diagnostics, and isolation of heavy Docker jobs; preserve
   exact-SHA fail-closed release gates and never blanket-prune shared Docker.
   A subsequent read-only `docker_compose_ps(project_dir=xloud)` also timed out
   after 60 seconds via the Gateway Compose execution plane, so isolate
   runner overload from operator read-path degradation before authorizing
   further CI retries or production action.
   **2026-10-23 additional read-only evidence:** the configured Docker daemon
   reported 51 containers (49 running, two Created) including 26 running
   Gitea Actions job containers. `docker stats --no-stream` timed out after
   60 seconds; `docker_compose_ps(project_dir=xloud)` also repeatedly timed
   out after 60 seconds while `docker_ps` still returned. The two unowned
   Created Gateway containers must not be treated as disposable without
   exact-ID ownership/lease proof. Acceptance should demonstrate bounded
   Docker statistics/Compose read paths alongside job container isolation,
   without blanket cleanup or weakened Site Audit release checks.
   Site Audit PR #186 (merged into main@047b4a7ebfffd4f3c9a8fb4f7fa00a2ae526d6f7)
   adds source-side exact-digest cache inspection and fail-closed reconciliation
   for the API/DB smoke PostgreSQL pull (10 mock-Docker cases plus workflow
   contract checked), ingress fixture cleanup verification, and a bounded
   Gotenberg Chromium startup timeout of 60s after 3×20s websocket-start
   failures. Exact PR-head CI #19199 passed verify, PostgreSQL and build-images;
   post-merge CI #19201 had successful verify/PostgreSQL but font inventory
   scanner failed to exit within 120s attached + 90s same-container reconcile;
   one guarded build-images rerun then ceased logging at DNS/Squid smoke for
   over 15 minutes and was SHA/attempt-fenced cancelled. The matching Actions
   runner container transitioned through Docker `removing` and disappeared
   without blanket cleanup. Two protected verified-push attempts on an
   independent registered Site Audit candidate timed out under this load,
   initially leaving no remote branch; once runner cancellation completed,
   the same CAS push succeeded. Site Audit PR #187 then added source-side
   font-only find output and exact target/error attribution, passed all three
   jobs in CI #19216 and merged into main@d0e634f6b9fac1def7ef013cb4492d1ee2aea208;
   its feature branch and candidate were removed. Post-merge CI #19235
   passed verify, but PostgreSQL and build-images both stalled at Actions
   runner `docker create` from 09:55 UTC, with neither exact job container
   present in read-only `docker_ps` after four minutes; the exact run/HEAD/
   attempt was guarded-cancelled to stop further runner contention.
   On 2026-10-23, exact job reruns in #19235 proved PostgreSQL SUCCESS but
   `build-images` FAILURE at `site-audit-browser` font inventory: the scanner
   later had observed exit=0, while its first 15-second `docker inspect`
   timed out and the source reconciler prematurely treated that one read
   timeout as a terminal unknown state. Site Audit PR #188
   (head `2a19f5823b844147fe83bc0e944ae29056f37585`) corrects bounded
   same-container inspection retries with fail-closed regression tests;
   CI #19254 finished FAILURE: verify and PostgreSQL SUCCESS, but two
   functional ingress tests timed out after 45s in bounded `docker create`
   and refused unknown outcomes; Actions container volume removal then hit
   `context deadline exceeded`. These failures are independent of PR #188's
   font reconciler and must not be waived. During this same run, a trivial
   Dockerfile source-SHA `RUN printf|grep` step took 205 seconds despite
   cached layers, consistent with execution-plane resource contention.
   At an early read the ingress probes were absent, but a later exact
   read-only `docker_inspect` proved *both* late-created upstream containers
   persisted in `Created` state: `sa-probe-up-3f346b61e0b0`
   (ID `c755223bd228afa0a4b467397ecd6a0c714917f98a15d2b822af0ffc1499a1fb`,
   created 13:13:07 UTC) and `sa-probe-up-7c1a71536159`
   (ID `d89d696aee1493aa1574448d2ad3f99edcb416549de31def8c11440e3ea0c16d`,
   created 13:14:15 UTC). Both use the pinned Nginx digest
   `sha256:892bb2152464f0c5d8972593a5f5afaf8054552580b699d95f8ff4af08c2d33d`,
   distinct `sa-ingress-probe-*` networks, and have never started.
   No exact-ID Docker removal was authorized or attempted; preserve these
   provenance anchors for protected operator cleanup. In the same interval,
   `docker_compose_ps(project_dir=xloud)` again exceeded 60 seconds while
   18 Gitea Actions containers were visible in read-only `docker stats`;
   protected rerun was withheld. Additional read-only checks saw a 30-second
   timeout on `docker_inspect(xloud-xloud-intake-1)` and Site Audit deploy
   status refused with `DEPLOY_CONTRACT_VERIFICATION_FAILED`: durable deploy
   state could not be read safely. In contrast, the XLOUD site remained
   `running/healthy` with zero restarts. A timed-out read is unknown evidence,
   not proof of intake presence/absence or authorization for deployment.
   Acceptance for #7 must reconcile stalled create/inspect operations with
   container/lease state and prove bounded terminalization before retries.
   Source mitigations do not by themselves restore shared runner isolation
   or production LKG.

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

   **2026-10-12 progress:** the live callable surface now includes generic
   `docker_deploy_contract` plus dedicated Site Audit and Content Harvester
   guarded deploy paths; Content Harvester also has a guarded migration path.
   Site Audit's candidate Git-trust edge case was fixed by PR #523, merged as
   `0588fdcfa86f...`; post-merge run #16188 attempt 2 completed successfully.
   Keep #8 open until guarded Site Audit is re-accepted live after that deploy
   and the generic rollback contract has final cross-project acceptance rather
   than capability-presence evidence alone.

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

    **2026-10-12 progress:** guarded exact-SHA branch creation and leased default-
    branch mutation are merged and callable as `gitea_create_branch_at_sha` and
    `gitea_set_default_branch`. Keep #10 open for typed required-checks, approval
    and branch-protection mutation against exact expected current policy state.

    **2026-10-13 deployed branch-protection progress / external-schema blocker:**
    PR #538 merged the guarded CAS branch-protection lifecycle as
    `master@44e05b529e71...` after exact current-base PR run #16427 completed
    successfully. Post-merge run #16431 then completed build-and-push, deploy and
    host-smoke successfully on that exact merge SHA; host-smoke proved
    `web-ssh-gateway`, `mcp-server` and `mcp-oauth` all running
    `BUILD_SHA=44e05b529e71...`, the OAuth->SSH black-box smoke passed, and live
    `health` reported both Gateway and MCP ready on the same exact build SHA.
    The authoritative live `tools_manifest` now reports 151 tools and exposes
    `gitea_get_branch_protection` plus admin-only
    `gitea_set_branch_protection`/`gitea_delete_branch_protection` as enabled and
    available, with toolset hash
    `sha256:f9ee11f3310547f8f54f192364f55d6b819eb4405178608086444128e2b85f79`.
    However, this long-lived attached ChatGPT resource catalog still omits all
    three callable schemas even after the server emitted
    `notifications/tools/list_changed` and the catalog was queried again. Keep
    #10 open only for external live create/read/(update)/delete acceptance once
    #6/#45 refresh/rebind exposes the real schemas; do not bypass the guarded
    contract with raw Gitea/HTTP mutation.

11. ⬜ **Isolate PR Docker jobs from the shared host daemon or document the trust boundary.**
    PR-controlled Docker build/smoke steps can mutate the shared runner Docker
    daemon. Prefer disposable per-job daemon/VM isolation; otherwise enforce and
    document that every same-repo PR writer is intentionally trusted as host
    admin and prevent untrusted/fork PRs from privileged labels.

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

    **2026-10-09 agent-memory production evidence:** `agent-memory-service`
    promotion is integrated and its current-main image `aefd07d016ba...` was
    published at registry digest `sha256:9c906bdae1d1...`, but no live
    `agent-memory-service` container exists. The reviewed promotion contract
    requires operator-owned `AGENT_MEMORY_DATABASE_URL`,
    `AGENT_MEMORY_INTERNAL_API_KEY`, `AGENT_MEMORY_INFRA_COMPOSE_DIR` and a
    digest-pinned `AGENT_MEMORY_PSQL_IMAGE`; the active deployment execution
    plane cannot read the root-owned infra environment and no guarded primitive
    can consume those values without exposing them. Keep this under the shared
    secret-propagation finding rather than opening a service-specific blocker.
    Evidence: agent-memory-service PR #7 merge `e09040ec...`, PR #8 merge
    `aefd07d01...`, post-merge run #15197 SUCCESS; live inventory still shows no
    agent-memory-service container.

    **2026-10-09 Actions/private-registry evidence:** `work-session-service`
    proved that the built-in `GITEA_TOKEN` is non-empty but is not accepted for
    Docker package-registry authentication: exact-head run #15200 reached
    `docker login` and failed unauthorized. The branch was corrected back to a
    fail-closed explicit `REGISTRY_TOKEN` contract at `565cd8a0...`; no callable
    Actions secret-management primitive can bind an operator-owned read-only PAT
    to that repository. Closure for this item must therefore cover both guarded
    deploy-time secret consumption and audited Actions-secret provisioning, with
    no plaintext returned or logged. Evidence: run #15200 quality/postgres green,
    agent-memory-e2e failed only at registry login; corrected head
    `565cd8a0eb77...` retains explicit `REGISTRY_TOKEN`.

    **2026-10-12 progress:** PR #517 merged guarded Actions-secret provisioning
    for the approved `registry_token` source into `work-session-service` as
    `REGISTRY_TOKEN`, with source-hash leasing, audit-before-write, redacted
    results and ambiguous-response reconciliation. Keep #47 open for the second
    half of the contract: secret-safe deploy-time consumption/injection and its
    production acceptance without plaintext exposure.

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

   **Update 2026-10-09:**
   **Observed behavior:** 2026-10-10 NOD gateway_client clean candidate based on main@c4a634b50f2c...: targeted run_pytest for tests/unit/test_vless_subscription_uri.py did not reach collection because the helper selected CPython 3.14.7 musl, attempted a source build of grpcio==1.68.0 and failed because c++ was absent. Targeted run_compileall for only app/bounded/network/vless_uri.py and its test likewise bootstrapped the full uv project environment, attempted grpcio-tools/grpcio builds and failed before syntax checking. In contrast, exact gateway_client main@c4a634b5 post-merge Gitea Actions runs 15292-15295 completed successfully on the repository CI toolchain including full pytest. This makes local helper failure a false product signal and makes the nominally lightweight syntax checker unnecessarily depend on full dependency bootstrap.
   **Reproduction:** Prepare a clean candidate clone of gpakoh/gateway_client main@c4a634b50f2c...; call run_pytest(target=tests/unit/test_vless_subscription_uri.py) and run_compileall(target=[app/bounded/network/vless_uri.py, tests/unit/test_vless_subscription_uri.py]). Observe uv selecting CPython 3.14.7 musl and build failure on grpcio/grpcio-tools due missing c++, before test collection or compileall. Compare with Gitea Actions runs 15292-15295 on the same main SHA, which are SUCCESS.
   **Expected behavior:** Verification helpers should honor the project's declared/CI-equivalent Python/toolchain or return a typed bootstrap/toolchain diagnostic distinct from code/test failure. run_compileall should be able to perform a dependency-free stdlib syntax compilation of requested Python files without resolving/installing the entire project dependency graph unless the project explicitly requires that behavior.
   **Impact:** False red verification blocks supervisor acceptance, encourages unnecessary retries, and makes a cheap syntax check expensive and coupled to unrelated native dependencies. It also creates provenance drift between Gateway helper evidence and authoritative CI evidence.
   **Acceptance:** Existing item #14 acceptance plus: on gateway_client (or a fixture with pinned native dependency unsupported on a newer interpreter), targeted pytest selects the declared/CI-supported interpreter or reports typed environment incompatibility before test status; targeted compileall compiles requested files without full dependency bootstrap; helper result distinguishes bootstrap/toolchain failure from product code/test failure; returned metadata identifies interpreter/toolchain plane; regression demonstrates CI-equivalent Python 3.11 succeeds while accidental Python 3.14 selection is not reported as a code failure.
   **Related evidence:** gateway_client main c4a634b50f2c01f3cb8f4f5a69075eaa2a4cd16d; post-merge Gitea runs 15292, 15293, 15294, 15295 SUCCESS. Gateway run_pytest job 01eca165-e930-4c52-ba97-280ca0d33a55; run_compileall job c05f6a3a-ced2-4136-98c5-ace8f9ff9ee6.

   **2026-10-10 registered-subproject evidence:** targeted `run_pytest` for the
   registered NOD `master_server` service failed before pytest collection because
   the helper assumed a project-root `pyproject.toml`; targeting the nested test
   through the parent candidate instead built the parent environment and failed
   collection on missing service dependency `pydantic`. This is the same #14
   environment-resolution defect, not a separate roadmap item. Acceptance must
   cover requirements-based registered subprojects with their configured cwd and
   dependency contract, and return an explicit unsupported-environment/bootstrap
   diagnostic rather than a misleading product-test failure when resolution is
   impossible.

15. ⬜ **Make agent cancellation and submission atomic/reconcilable.**
    Cancellation can mark a local job ambiguous while the remote runner keeps
    heartbeating, and rate-limited submission can return failure after a runner
    has already started unbound. The 2026-10-08 infra-quart pilot reproduced the
    cancellation side again: the Gateway job became ambiguous while the same
    remote runner PID continued fresh heartbeats with no terminal artifacts.
    Preserve a cancellable/fenceable remote handle or prove no runner/proxy
    acquisition occurred before returning failure; replacement work must remain
    blocked until the old writer is terminal or fenced.

    **2026-10-10 live split-state evidence:** two independent pilots reproduced
    the same failure mode. Browser task `port-provider-challenge-currentbase-20261010`
    became locally `ambiguous` after cancellation while remote runner PID 1498
    continued fresh heartbeats. Supervisor task `pilot-gateway-dispatch` likewise
    became locally ambiguous while remote PID 33878 kept heartbeating and was later
    classified `likely_hung/reasoning_loop`; a second cancellation was rejected
    solely because the control-plane job was already ambiguous. Both candidate
    worktrees remained clean, so no late write was observed, but remote termination
    was not proven. Keep #15 open until an ambiguous local cancellation retains a
    durable remote stop/fence path and replacement admission is impossible until
    that remote writer is terminal or fenced.

16. ⬜ **Reconcile agent jobs durably across Gateway restarts/deploys.**
    Restart/deploy can make returned job ids disappear while artifacts still show
    stale `running` status. Inspect/status APIs should derive terminal states
    such as `lost_after_restart`, `orphaned_attempt` or `artifact_incomplete`,
    including last useful activity and safe recovery guidance.

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

30. ⬜ **Add Gitea Actions artifact listing and bounded download.**
    Operators can inspect runs/jobs/logs, but need first-class artifact metadata
    and safe archive/file retrieval with run/head provenance, size limits,
    checksum where available, path traversal protection and expiry diagnostics.

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

    **2026-10-13 deployed progress / external-schema blocker:** PR #533 merged
    guarded `create_parents` support as `master@0cf0c0b651ce...`; post-merge run
    #16365 completed build-and-push, deploy and host-smoke successfully, and live
    Gateway/MCP health reported exact build SHA `0cf0c0b651ce...`, ready=true.
    The deployed server manifest documents `create_parents` on
    `workspace_file_write` and emits toolset hash `sha256:f250b44e2878...`.
    A live external call through the currently attached client proved the default
    path now returns typed non-retryable `PARENT_MISSING` with zero filesystem
    mutation (`acceptance-parent/**` remained absent). However, even after the
    server sent `notifications/tools/list_changed` and the external catalog was
    queried again, this attached callable schema still omitted the new optional
    `create_parents` argument. Do not close #50 until a fresh/correct external
    schema can invoke `create_parents=true` and prove nested creation plus the
    returned parent/post-write verification receipt. Track the stale optional-
    argument refresh defect under #6/#45; do not bypass it with raw transport.

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

   **Update 2026-10-23:**
   **Observed behavior:** 2026-10-23 Father UI candidate `fix/father-ui-edit-draft-refresh-20261023`: gitea_push_verified_commit with required_checks `npm ci --no-audit --no-fund\nnpm test\nnpm run build\nnpm run lint` failed before any mutation at check 0, exit 127, because the trusted ssh-gateway-sshd verifier image has no npm (`sh: npm: not found`). The task-runner could install isolated Node and reported tests passing, but its gate also emitted 127; the trusted delivery verifier therefore cannot attest a frontend candidate's actual quality gates.
   **Reproduction:** Prepare a registered clean Father UI candidate at known SHA and commit two-file React/Vitest change; invoke `gitea_push_verified_commit` with `required_checks='npm ci --no-audit --no-fund\nnpm test\nnpm run build\nnpm run lint'`. It rejects `npm` as unresolved on the trusted verifier PATH, with immutable check receipt; push using only `git diff --check HEAD^ HEAD` succeeds but delegates full npm coverage to Gitea CI.
   **Expected behavior:** Provide a first-class bounded, lockfile-verified Node 22 frontend candidate verification substrate with npm or project-declared package manager. Do not silently skip requested required checks. Keep exact-head, allowed-file, clean-worktree and immutable result attestations; use pinned image digest and approved proxy for dependency install.
   **Impact:** Frontend review cannot get full source-verification attestation before Gitea push; operators either fail closed, or use weaker checks while relying on exact-head PR CI, increasing iteration latency.
   **Acceptance:** Proof on a registered Father UI candidate that npm ci, npm audit, Vitest, TS/Vite build, lint, Father→Kojo proxy contract all run in a pinned trusted verifier without unreviewed host npm installation; canonical gateway push refuses tampered/dirty candidates, records check receipts, preserves secrets/server-side constraints and makes Gitea CI a separate mandatory release gate.
   **Related evidence:** Father UI PR #47 head 113435077e8d1a27f70e448ad2234159f8b9bc94; 2026-10-23 gitea_push_verified_commit CANDIDATE_CHECK_FAILED exit 83 at `npm ci` with primary_tool kind unresolved and check exit 127; separate git diff --check HEAD^ HEAD accepted.
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

    **2026-10-09 isolation evidence:** post-merge #15114 redeployed
    `master@9c4bc3ff677c...` successfully. Fresh public OAuth host-smoke passed and
    all three live application containers reported that exact BUILD_SHA, proving
    the Gateway/public MCP reconnect path is current after restart. Yet the same
    attached ChatGPT catalog still omits `gitea_rerun_action_job` while live
    `tools_manifest` exposes it. The remaining invalidation responsibility is
    therefore outside the Gateway process/session implementation: close this item
    only when the external resource catalog either refreshes on server build/toolset
    change or surfaces an explicit stale state plus bounded refresh action.

    **2026-10-10 notification/lifecycle isolation evidence:** PR #504 deployed
    exact `master@4955470c1f8a...` in successful post-merge run #15403. The live
    server toolset hash changed to `sha256:88b62a9a5c4c...`; invoking the already
    caller-visible `tools_manifest` twice caused two successful standard
    `notifications/tools/list_changed` emissions, but each call reported
    `status=sent` rather than the second reporting `already_sent`. The attached
    `api_tool` catalog remained unchanged after both emissions and still omitted
    `candidate_cleanup` and `gitea_rerun_action_job`. This proves the schema
    snapshot is not scoped to, or revalidated by, the MCP lifecycle that executes
    tools. A Gateway session-cache change cannot close #45 by itself: acceptance
    requires the external catalog owner to bind cache identity/invalidation to
    connector/build/toolset identity or expose an explicit refresh/reconnect
    action whose postcondition is a freshly fetched `tools/list`.

    **2026-10-10 typed refresh-contract progress:** PR #507 merged to
    `master@7044f0e96054...`; post-merge run #15518 completed successfully. The
    server now exposes a typed catalog state plus bounded `tools/list` refresh
    guidance keyed to the live server toolset hash. This satisfies the Gateway
    side of explicit stale-catalog diagnostics, but does not close #45: the
    external ChatGPT resource catalog still owns its separate schema snapshot and
    must prove that it consumes the refresh/rebind contract and fetches the new
    callable schema in a long-lived caller.

    **2026-10-12 fresh-attachment evidence:** a newly attached caller now exposes
    the previously missing guarded schemas, including `candidate_cleanup` and
    `gitea_rerun_action_job`, against live toolset hash
    `sha256:50b7ce5e8ea37ad31d68e3bafce99df230342b5c2d9a82c206f3dffa5a26be4b`.
    This proves fresh attachment can converge, but not the required in-place
    invalidation of a client that remained attached across a toolset/build change;
    keep #45 open for that long-lived acceptance only.

    **2026-10-13 in-place optional-argument reproducer:** PR #533 deployed exact
    `master@0cf0c0b651ce...`; live MCP toolset hash changed to
    `sha256:f250b44e2878a8de141b5b6d8da773c02c4a42315d0d7ba5008377f6d2e724ef`,
    and authoritative `tools_manifest` immediately documented the new optional
    `create_parents` argument on `workspace_file_write`. The same long-lived
    attached external catalog still returned the pre-deploy schema without that
    argument. `tools_manifest` emitted `notifications/tools/list_changed` with the
    new hash and explicitly instructed an MCP `tools/list` refresh; querying the
    external resource catalog again still returned the stale schema. This is a
    direct argument-level (not merely tool-name) acceptance failure for #45/#6
    and currently blocks external `create_parents=true` acceptance for #50.

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

## Security and executor isolation — 2026-10-22

1. ⬜ **Disable password-based SSH login and eliminate fixed credential in executor image.**
   <!-- gateway-todo-key: sshd-fixed-password-authentication-2026-10-22 -->
   **Severity:** P1

   **Observed behavior:** At master d70b9d18b751782cf69870618237c09d29c4d5c1, docker/sshd/Dockerfile appends PasswordAuthentication yes to sshd_config and assigns a fixed password to mcpuser during image build; both general sshd and agent-sshd derive from the same image. Read-only Docker inspection found no host-published port 2222, but the containers attach to shared internal_net and proxy_parser_default networks. In-network exploitability has not been tested; treat as a credential exposure risk, not a confirmed compromise.

   **Reproduction:** Review docker/sshd/Dockerfile at the pinned SHA for PasswordAuthentication and account provisioning instructions; inspect docker/docker-compose.yml sshd and agent-sshd networks and the effective sshd -T configuration in an isolated controlled environment. Do not log or repeat the credential. Review image history and the production image digest for inherited configuration.

   **Expected behavior:** Production and CI SSH executors reject password authentication and accept only explicitly authorized public keys; no static password is provisioned in image layers or production runtime; trusted access patterns continue to work.

   **Impact:** Any workload with network reachability to the SSH executor could potentially authenticate using a predictable account credential and gain access to workspaces or agent state; internal Docker network placement does not eliminate lateral movement risk.

   **Acceptance:** Remove fixed credential provisioning from the image and disable PasswordAuthentication and KbdInteractiveAuthentication, with explicit effective sshd -T checks; verify authorized-keys access and deny password login in automated isolation tests; review/rotate any reused credentials and test both sshd services; complete exact-head PR CI, image publication and guarded host-smoke/deployment, checking unchanged authorized execution; preserve production until approved release.

   **Related evidence:** Gitea agent-ssh-gateway master d70b9d18b751782cf69870618237c09d29c4d5c1 docker/sshd/Dockerfile; docker/docker-compose.yml; read-only docker_inspect ssh-gateway-sshd and ssh-gateway-agent-sshd 2026-10-22.

2. ⬜ **Require TLS certificate verification by default in Python SDK.**
   <!-- gateway-todo-key: sdk-python-requests-tls-verify-false-2026-10-22 -->
   **Severity:** P1

   **Observed behavior:** At Gitea master d70b9d18b751782cf69870618237c09d29c4d5c1, sdk/ssh_gateway.py lines 33-55 constructs requests.Session and sets self.session.verify = False; module also invokes urllib3.disable_warnings(). The client methods transmit API credentials and may send SSH passwords/private keys to the gateway. This is a confirmed insecure SDK default; no production use or network exploitation has been established.

   **Reproduction:** Read sdk/ssh_gateway.py SSHGatewayClient.__init__, auth(), ssh_connect() and static workspace client helper constructors; instantiate client with an HTTPS URL and inspect session.verify (currently False). In isolated tests, use an HTTPS endpoint with an untrusted/self-signed certificate and verify the default client rejects the handshake. Never test against production credentials or disable TLS globally.

   **Expected behavior:** SDK enforces CA/hostname TLS verification for HTTPS by default. Optional explicit operator-configured custom CA bundle is supported; insecure mode is never silently enabled and may be prohibited or clearly quarantined for isolated development. Warnings remain enabled. No secrets transmitted when certificate validation fails.

   **Impact:** A network-positioned attacker could impersonate an HTTPS Gateway endpoint to intercept auth/API credentials and submitted SSH passwords or keys whenever this SDK is used over an untrusted channel. Finding is in source code; actual production SDK use and exploitability remain unverified.

   **Acceptance:** Remove urllib3.disable_warnings() and unconditional session.verify=False; default verification to True and support explicit CA path where appropriate; regression tests for trusted CA accepted, invalid/self-signed/wrong hostname rejected before credential submission, and SDK helper factory behavior; inspect callers and document migration and any explicit development-only opt-out; complete exact-head PR CI and protected merge, without altering production until authorized.

   **Related evidence:** gpakoh/agent-ssh-gateway: sdk/ssh_gateway.py lines 33-55, 59-85, 392-408 at master d70b9d18b751782cf69870618237c09d29c4d5c1; README.md and SECURITY.md warn against unaudited external exposure.

## Runtime/tooling intake — 2026-10-22

1. ⬜ **Git write-capability preflight misses FETCH_HEAD permission denial.**
   <!-- gateway-todo-key: git-fetch-fetch-head-permission-false-positive-capabilities -->
   **Severity:** P2

   **Observed behavior:** On 2026-10-22, registered clean js-chat-engine checkout main@cefbe556: git_write_capabilities reported all index/objects/refs/head writeable and fully_writeable=true, but git_fetch_ref(origin/main, expected_remote_head=ea62a6d80eb57082ce8a1446fe97b178561547a1) failed with `error: cannot open '.git/FETCH_HEAD': Permission denied` (exit 255). Candidate creation from exact SHA succeeded via the supported isolated candidate workflow; original checkout was not modified.

   **Reproduction:** Use the registered js-chat-engine canonical checkout at its clean stale HEAD; call git_write_capabilities(project=js-chat-engine), then git_fetch_ref(project=js-chat-engine,remote=origin,branch=main,expected_remote_head=ea62a6d80eb57082ce8a1446fe97b178561547a1). Inspect mismatch between preflight fully_writeable=true and FETCH_HEAD error.

   **Expected behavior:** Preflight and fetch should agree on required writable Git metadata, including FETCH_HEAD; fail closed with a precise permission diagnosis or use a supported managed write plane without granting extra rights.

   **Impact:** Misleading preflight causes failed source refresh, stale main workspace, and unnecessary debugging; guarded exact-base candidate path remains functional. No data loss observed.

   **Acceptance:** Regression with non-writable FETCH_HEAD and otherwise writable refs/index; capability/operation must surface a consistent actionable limitation. No implicit chmod, unsafe fallback, or remote rewriting. Verify allowed normal fetch still works.

   **Related evidence:** 2026-10-22 JS Chat Engine session, git_fetch_ref request_id=1e4f2b38-f68f-403e-80a8-d44ae7b90586, git_write_capabilities request_id=f954a01d-cace-460f-b122-17ec68bd4693

2. ⬜ **Docker inventory omits inspectable running container and its image; expose completeness clearly.**
   <!-- gateway-todo-key: docker-inventory-truncated-omits-inspectable-service-20261023 -->
   **Severity:** P2

   **Observed behavior:** On 2026-10-23, Gateway docker_inspect(name='opencode-adapter') returns a RUNNING, HEALTHY container Id=f8cf98141ddc38b21e16b426d6af1b6e7af6a3d3346a8ef55edc831951bb9602, Image=sha256:03d0b2be8eec4172533d950faa39398bf2a82bfcad5f302fded5e8b9bf179d26, compose project infra-quart. Yet docker_ps(all=true,limit=200) returns count=42 without opencode-adapter and inventory.daemon_completeness='truncated' despite count < requested limit. docker_images(limit=250) returns 165 images and no opencode-adapter image ID. docker_inspect('father-ui') resolves the same ID listed by docker_ps, proving at least one ordinary cross-tool match. This can be a legitimate partial inventory, but it is not sufficient for a 'no container' or 'image unavailable' claim.

   **Reproduction:** Read-only: docker_inspect('opencode-adapter'), docker_inspect('f8cf98141ddc38b21e16b426d6af1b6e7af6a3d3346a8ef55edc831951bb9602'); compare docker_ps({all:true,limit:200}) array and inventory metadata, then docker_images({limit:250}). Check configured Docker endpoint identity, ordering/filtering/pagination, returned result completeness, and image enumerator semantics.

   **Expected behavior:** On the same endpoint, running inspectable containers appear in exhaustive listing or the listing is demonstrably limited with actionable cursor/next-page metadata; image inventory similarly makes missing IDs/untagged images or truncation explicit. A caller cannot infer absence from an incomplete list.

   **Impact:** False negative production discovery can mislead rollback/release decisions. A prior agent incorrectly reported no OpenCode container because docker_ps omitted it, whereas docker_inspect proves its existence.

   **Acceptance:** Add regression fixture with 1+ container inspectable by ID but omitted from first page. Make inventory order/pagination/capacity diagnostics explicit and reproducible, show next-page mechanism or reliably discover all within declared scope, and test docker_ps vs docker_inspect consistency; document docker_images ID/index omissions or add exact read-only image lookup.

   **Related evidence:** opencode-adapter CI #18510 failed deploy job #90777, CI #18689, infra-quart issue #491; Gateway docker_inspect exact container f8cf9814... and docker_ps(all=true,limit=200) 42 rows reported truncated; no adjacent source modifications requested.
## Runtime/tooling intake — 2026-10-23

1. ⬜ **Py Coordination: inbox, Kanban and create fail on repositories with over 500 Gitea issues/PRs.**
   <!-- gateway-todo-key: py-coordination-500-issues-inbox-board-create-blocked-20261023 -->
   **Severity:** P1

   **Observed behavior:** Live Gateway/MCP build 89dd22be3f348e7e2a80125f4e9b79f52746f76e has seven enabled coordination tools and successful post-merge CI #19113 (build-and-push/deploy/host-smoke), but coord_board(project=agent-ssh-gateway) and coord_inbox(project=agent-ssh-gateway) both fail with MUTATION_OUTCOME_UNKNOWN: 'collection exceeds bounded 500-row reconciliation window'. coord_create(source_project=agent-ssh-gateway,target_project=agent-ssh-gateway,title=FAQ...,request_key=py-gpt-ownership-faq-20261023-v1) is rejected by the same pre-write scan; no target issue was created. coord_board(kojo-bot-service) reproduces the 500-row limit, while business-analytics-service and engineering-supervisor-service return valid boards. The authoritative owner project's own board cannot publish or read cross-GPT communications when its Gitea history is large.

   **Reproduction:** On current deployed 89dd22be..., call coord_board(project='agent-ssh-gateway',limit=25) and coord_inbox(project='agent-ssh-gateway',limit=25); both return nonretryable MUTATION_OUTCOME_UNKNOWN instead of bounded results. Attempt coord_create with target_project='agent-ssh-gateway' and a new stable request_key; read of existing history fails before POST (do NOT retry with a fresh key when outcome is unknown). Independently call coord_board(project='kojo-bot-service',limit=25) for another large project. Source: examples/mcp_server/coordination.py _pages uses page size 50 and max 10 pages and raises upon ten full pages; list_threads scans issues state=all (including ordinary issues and PRs) before filtering coordination markers. Follow adapters/coordination_adapter.py coord_board, coord_inbox and coord_create.

   **Expected behavior:** Project inbox and Kanban must remain readable and give explicit bounded/truncated results for large Gitea repositories without treating a read-only list overflow as an ambiguous mutation. Coordination create and idempotent replay must use a sound, bounded authoritative lookup/uniqueness strategy that can prove a stable request_key is absent or fail closed with specific actionable diagnostics (never silently create duplicates or blindly retry unknown POST outcomes). Preserve authenticated project scope, issue/comment provenance, and separation of normal issues/PRs.

   **Impact:** P1 core utility failure: GPT №1 cannot use its own coordination board or publish the owner-assigned FAQ; at least GPT №3's large repo is also affected. This blocks cross-agent dependency exchange in busy repositories despite successful server deployment and healthy MCP. Temporary Py GPT roster FAQ was published as verified coordination issue #221 in engineering-supervisor-service; it is NOT yet published on agent-ssh-gateway and is not a substitute for fixing the owner board.

   **Acceptance:** 1) Regression fixtures with >500 normal issues/PRs plus coordination issues at early and late pages: coord_inbox/coord_board provide valid scoped entries, accurate count/truncated metadata, no silent loss and predictable bounded cost; separate tested read-only failure classifications from write ambiguity. 2) With >500 history entries, coord_create can create one issue in target agent-ssh-gateway and same-key replay yields the exact same issue; wrong payload conflicts, uncertain network POST is reconciled/no blind repeat, and concurrent creation cannot falsely claim exactly-once. 3) Cross-session end-to-end smoke using registered source/target proves coord_create->coord_board/inbox->coord_accept/reply/update->coord_open, with correct Gitea issue URLs and untrusted content not granting authority. 4) Required CI on exact PR head, merge and post-merge deploy/host-smoke on exact SHA, then actual live coord_board/coord_inbox/coord_create success in the previously failing big repository. 5) Do not edit foreign repos, purge old issues, raise scan caps blindly or overwrite live state. Owner can subsequently move/pin FAQ from temporary engineering-supervisor-service Issue #221 to canonical owner board.

   **Related evidence:** PR #629 merged 6919ccbfda6b, PR #632 merged 89dd22be3f34; post-merge Actions #19113 SUCCESS (2026-10-23). Live health: MCP/Gateway build 89dd22be and ready=true. coord_board request_id=11e3907b-90ba-42c5-be3b-e0c7cb559c19; coord_inbox request_id=28166f76-704e-4846-838b-5407110c5895; blocked FAQ coord_create request_id=fd3b9319-a799-40e5-9810-02804a187652; second-project reproduction kojo-bot-service request_id=7d0928ed-a330-4830-9ea0-045fd0a243d8. Temporary shared FAQ: https://gitea.example.invalid/gpakoh/engineering-supervisor-service/issues/221.

2. ⬜ **Gateway pytest/ruff verifier assumes uv.lock for pip-only repositories.**
   <!-- gateway-todo-key: gateway-run-pytest-ruff-frozen-uv-lock-nonuv-project-20261023 -->
   **Severity:** P2

   **Observed behavior:** On 2026-10-23, in registered writable candidate of `deep-researcher` (base main@63561d9eb49ffd7182012d9f87e32e565b313794), both run_pytest(target=['tests/test_main.py','tests/test_research_limits.py']) and run_ruff(target=['client/main.py','client/research_runtime.py','tests/test_main.py','tests/test_research_limits.py']) refused before executing tests with `error: Unable to find lockfile at uv.lock, but --frozen was provided`. Project has pyproject.toml pytest/ruff settings but no uv.lock; its CI uses its own approved dependency workflow. run_compileall works.

   **Reproduction:** Prepare registered candidate clone of `gpakoh/deep-researcher`; call run_pytest and run_ruff with a valid target; observe TOOL_EXECUTION_FAILED exit=2 from verifier `uv --frozen` before tests/lint. Do not alter project to fabricate uv.lock merely to satisfy Gateway.

   **Expected behavior:** Gateway selects documented supported environment per repo or exposes a safe interpreter / preinstalled tool mode for pip/requirements-managed Python projects. If frozen uv is unsupported, report `unsupported verifier environment` distinctly from test failure, with actionable alternatives.

   **Impact:** Agents cannot locally validate deep-researcher tests/formatter; false test-failure classification and extra CI latency can delay security patches. Scope confined to Gateway tooling.

   **Acceptance:** Regression: registered pip/requirements-managed Python repository with no uv.lock can execute pytest and Ruff through approved toolchain, or tools accurately report unsupported environment without implying test failure; uv-locked repositories still remain frozen. No unapproved dependency/network modifications.

   **Related evidence:** Gateway requests on 2026-10-23: run_pytest job dbff5372-cbdd-40f7-a1c0-1eb149b0bced; run_ruff job 6503fedc-0d3d-45a3-9232-a77f0c3a0162; candidate-deep-researcher-fix-research-log-response-redaction-20261023-63561d9eb49f-32ffa371ce68. No Gateway code/commits by reporter.

3. ⬜ **Registered candidate cannot fetch new main when updating stale PR.**
   <!-- gateway-todo-key: gateway-candidate-trusted-fetch-current-main-ref-missing-20261023 -->
   **Severity:** P2

   **Observed behavior:** For registered candidate-deep-researcher-fix-research-log-response-redaction-20261023-63561d9eb49f-32ffa371ce68 (source deep-researcher, valid clean head 61692bae...), `git_fetch_ref(project=..., remote='origin', branch='main', expected_remote_head='4f6909d9e5c77cb802bd8c189bd2d197a7439162')` returned `GIT_REMOTE_NOT_ALLOWED: Could not resolve trusted remote branch for fetch`. `git_update_branch_by_merge(source_branch='main')` separately returns `GIT_LOCAL_REF_MISSING: Source branch/ref 'main' was not found`. Gitea main SHA verified by merged PR #44. Correct stale PR #42 cannot be updated with provided trusted Git tools despite registered candidate and known permitted base.

   **Reproduction:** Prepare a candidate clone for an own-project PR from old exact base, merge a different green PR into default main, attempt `git_fetch_ref(origin,main,expected_remote_head=<new main sha>)` then `git_update_branch_by_merge(source_branch='main')`. Verify registered source repo remote refs/allowed remotes and expose an approved way to merge current default branch into candidate. No arbitrary shell workaround.

   **Expected behavior:** An authorized registered candidate can fetch exactly authenticated Gitea default branch and merge it using a lease-guarded safe tool; or an explicit approved `refresh_candidate_base` operation with source_project/ref SHA; failed calls distinguish missing remote config versus policy block.

   **Impact:** Forces safe agents to preserve equivalent work via fresh candidate/new superseding PR and audited cleanup instead of updating original PR, adding CI delay and branch/PR accumulation.

   **Acceptance:** Regression on registered candidate whose main advanced: trusted fetch returns remote-tracking ref pointing to exact Gitea SHA, conflict-aware merge updates branch; unrelated remotes and repos remain denied. Candidate stale base replay remains fail-closed.

   **Related evidence:** Deep Researcher security PR #42 `61692bae...` base `63561d9...`; safety PR #44 merged main `4f6909d9...`. Gateway git_fetch_ref request b1d48398-2e7c-4cf4-a97c-d4598ef8dcf7 denied GIT_REMOTE_NOT_ALLOWED; git_update_branch_by_merge request 220c2de7-ef6f-4992-b775-47bcdac1a14d denied GIT_LOCAL_REF_MISSING. Security content preserved exactly in replacement PR #45, current CI #19249.

4. ⬜ **Reconcile ambiguous merge attempts when CI evidence read fails but PR becomes merged.**
   <!-- gateway-todo-key: merge-ci-evidence-incomplete-then-merged-state-observed -->
   **Severity:** P2

   **Observed behavior:** 2026-10-23 Father UI PR #48 at exact head 3cf6d6b29e3cf658a43f620dcdd1cfc61f6ab3cf: after exact-head Actions #19264/#19265 both completed SUCCESS and jobs were confirmed, `gitea_merge_pull_request` returned CI_EVIDENCE_INCOMPLETE with retryable=true. A following read then observed PR #48 already closed/merged into main at 197cfd2da1c332b739c0252e9a8280b0ecfbf373, remote feature branch absent, and registered candidate cleanup reported `already_cleaned=true`. The source of transition is unknown: concurrent actor or a tool side effect cannot be distinguished from this evidence.

   **Reproduction:** Reproduce under controlled concurrency on a disposable Gitea PR. Pin expected head/base, make Actions evidence briefly inconsistent/expensive to read during merge preflight, capture adapter's response and independent server-side PR state/merge commit plus candidate cleanup receipt before retry. Check whether tool returns CI_EVIDENCE_INCOMPLETE before mutation, after mutation, or alongside an external merger.

   **Expected behavior:** A failed merge preflight must be non-mutating, and any ambiguous timeout or reconciliation race must return an explicit outcome-unknown state and immutable correlation evidence. Support idempotent status reconciliation by exact head/base/merge SHA; never invite blind merge retry after a potentially completed operation.

   **Impact:** Operators cannot distinguish denied preflight from a completed concurrent merge and may retry or clean up unsafely. The observed run was reconciled safely by read-only Gitea checks, with no duplicate mutation.

   **Acceptance:** Add race/timeout tests with CI evidence changing during guarded merge; prove zero mutation for preflight denial; prove post-mutation outcome is reconciled or clearly ambiguous; provide identifiers for concurrent merge vs same-call merge, expected SHA guards, and branch/candidate cleanup provenance. Do not conflate preflight read failures with successful merges.

   **Related evidence:** Father UI PR #48 https://gitea.example.invalid/gpakoh/father-ui/pulls/48; exact successful Actions #19264/#19265 on 3cf6d6b29e3cf658a43f620dcdd1cfc61f6ab3cf; merge commit 197cfd2da1c332b739c0252e9a8280b0ecfbf373; post-merge CI #19267. Merge API returned CI_EVIDENCE_INCOMPLETE; later candidate_cleanup tombstone ab7ac2687f173ca755330b6c35e1e8a9600787d58ae8e9209a84ae4ab67b7faa reported already_cleaned=true.

5. ⬜ **Gateway run_pytest requires uv.lock for pip-only projects; expose supported test runner.**
   <!-- gateway-todo-key: gateway-run-pytest-uv-frozen-missing-lockfile-pip-project-20261023 -->
   **Severity:** P2

   **Observed behavior:** On registered deep-researcher candidate `candidate-deep-researcher-fix-consolidate-session-canary-and-ci-timing-202-a522ac01c557-1a08288a3d78`, `run_pytest(target=['tests/test_research_session_canary.py','tests/test_research_limits.py'])` fails before collecting tests: verifier runs `uv sync --frozen`, prints `Unable to find lockfile at uv.lock, but --frozen was provided` and exit 2. Repository is a supported pip requirements project (`docker/requirements.txt`), and normal Gitea CI installs via pip then pytest successfully (run #19291 217 passed). `run_compileall` works.

   **Reproduction:** On registered clean candidate of gpakoh/deep-researcher (no uv.lock), invoke Gateway structured run_pytest on tests/test_research_session_canary.py. Observe forced frozen-uv error, without tests. Do not bypass with unapproved shell commands.

   **Expected behavior:** Runner detects supported requirements-based projects, selects or offers a policy-approved isolated pip/pytest verifier, or returns explicit unsupported-runner status before trying to create .venv. Continue to forbid uncontrolled network and untrusted dependency installation.

   **Impact:** Agents cannot run targeted tests before pushing security-sensitive code and must wait for expensive full CI (200MB CPU torch and many pinned dependencies), increasing PR churn and runner contention.

   **Acceptance:** Regression for clean pip requirements project without uv.lock: targeted run_pytest either works in bounded isolated environment, or reports a precise alternative supported workflow; uv.lock projects still use frozen sync; no unsafe shell bypass.

   **Related evidence:** Gateway run_pytest request 77351191-3068-48ce-94d3-85b883ff4148, 2026-10-23. Deep Researcher PR #49, CI #19318. No Gateway commit requested.
## Agent handoff and callable schema ergonomics — 2026-10-23

1. ⬜ **Expose write_agent_task source_mode choices in callable tool schema.**
   <!-- gateway-todo-key: write-agent-task-source-mode-choices-not-discoverable -->
   **Severity:** P3

   **Observed behavior:** SSH Gateway write_agent_task presents source_mode as an unconstrained optional string in the caller-visible schema; valid values are not described. During a Father UI candidate handoff on 2026-10-23, source_mode='candidate' was accepted by argument parsing but rejected at runtime with INVALID_INPUT ('Must be one of: committed_head, dirty_worktree_snapshot'). Retrying with committed_head succeeded.

   **Reproduction:** Inspect caller-visible write_agent_task signature (source_mode?: string | null, no enumeration or documented values); submit a handoff with source_mode='candidate' to a prepared registered candidate clone. The validator rejects it despite the intuitive project kind. Valid mode committed_head succeeds.

   **Expected behavior:** The manifest/tool argument schema and docstring advertise exactly committed_head and dirty_worktree_snapshot (or an actual strict enum), with default meaning and examples for managed candidate clones. Invalid choices should be rejected before any writes with a clear, actionable message.

   **Impact:** Forces an avoidable failed call and retry when assembling safe isolated agent tasks; ambiguity risks accidental use of dirty snapshot when committed head is intended.

   **Acceptance:** 1. Caller-visible schema/doc explicitly enumerates valid source_mode values and default; 2. example for prepare_candidate_clone -> write_agent_task uses committed_head; 3. generated MCP manifest/tool contract test validates value discoverability; 4. valid/invalid modes preserve existing fail-closed handling without creating tasks on failure.

   **Related evidence:** Father UI candidate father-ui-edit-draft-refresh-20261023 initial write_agent_task returned INVALID_INPUT for source_mode='candidate'; retry committed_head succeeded at 2026-10-23; validator in examples/mcp_server/agent_tasks.py:130-141.
