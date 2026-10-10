# Agent SSH Gateway — TODO

Last reconciled: 2026-10-10.

This file is the active backlog only. Completed audit notes, fixed CI blockers,
merged PR evidence, and superseded diagnostics should live in PR history,
changelogs, or dedicated audit archives — not in TODO.

On 2026-10-10 the current committed `master` backlog was reconciled again with
unique evidence from the long-lived dirty canonical `TODO.md`. The three dirty
runtime/tooling intake entries were not promoted as duplicate roadmap items:
their still-open substance was folded into existing #14, #2/#51 and #6/#45;
fresh cancellation reproductions were folded into #15. Historical duplicates,
completed subcases and superseded diagnostics were not reintroduced.

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

4. ⬜ **Reconcile stale unbound fleet leases without duplicate work.**
   Historical `attempted` / `legacy_unknown` lease rows can consume all fleet
   capacity even when no lease has a recent heartbeat. Add fleet status that
   separates live workers, reclaimable never-attempted leases and ambiguous rows;
   require explicit evidence/acknowledgement before deleting ambiguous leases;
   tombstone every reclaimed lease exactly once.

   **2026-10-10 implementation status:** PR #506 (`fix(fleet): reconcile stale
   unbound leases safely`) implements the intended status/reconciliation/tombstone
   contract and reports targeted local verification, but its current head
   `eaf2e2a64886...` is behind `master@7044f0e96054...` by two commits. Treat #4
   as implemented/locally verified on a branch only; it remains open until the
   branch is refreshed to current base, exact-head CI is green, the PR is merged,
   post-merge deploy/host-smoke succeeds, and the live operator surface is
   acceptance-tested without duplicate work or ambiguous lease deletion.

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

    **2026-10-09 reconnect acceptance:** PRs #491/#492 plus successful post-merge
    run #15114 now prove fresh bearer/public-OAuth MCP reconnects after a full
    redeploy accept the current schema surface. `tools_manifest` still reports
    `gitea_rerun_action_job` enabled/available, but the attached ChatGPT resource
    catalog still cannot call it. Therefore no additional Gateway rerun code is
    required here; the only remaining closure step is external catalog exposure
    followed by one real failed-job rerun acceptance from that external caller.

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

    **2026-10-10 bundle-delivery timeout evidence:** during the NOD bundle-backed
    candidate incident, an initial staging failure was followed by a client
    timeout; fresh Gitea reads after the unknown outcome proved the destination
    branch absent before any retry. Keep the deterministic `clone_bundle` staging
    defect under #2, but retain the timeout half here: `gitea_push_verified_commit`
    must reconcile remote state itself after response loss and report proven
    success, proven no-mutation, or typed unknown outcome without requiring the
    caller to infer safety from an exception.

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
