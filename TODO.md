# Agent SSH Gateway — authoritative open backlog

This file is the single authoritative backlog for open Gateway engineering work.
It is intentionally ordered by severity, not by discovery date or implementation
area. `docs/roadmap.md` points here and must not carry a second independent
backlog.

Stable IDs are permanent cross-project references. Never renumber or reuse an ID
when a finding is closed. Completed findings are removed from this file after
implementation, independent verification, current-base CI/delivery evidence where
applicable, and production evidence when the finding is runtime-facing.

Reconciliation sources used on 2026-09-11: current `master` TODO, the dirty
canonical TODO intake (43 historical/open entries), `docs/roadmap.md`, current
repository TODO/FIXME/HACK/TBD and unchecked-plan scans, CI comments, open Gitea
PRs/branches, and live runtime evidence. Historical implementation-plan checkboxes
and runbook operator checklists are not backlog items by themselves: they are
included only when the current tree or live system still demonstrates the gap.

Severity policy:
- **P0** — verified active security/data-loss or production-outage condition.
- **P1** — correctness, safety, delivery-integrity, or near-term availability risk.
- **P2** — important reliability/capability gap that is currently fail-closed or has a safe workaround.
- **P3** — efficiency, ergonomics, cost, documentation, or productization debt.

There are currently **no verified P0 findings**.


## Ownership / implementation lanes

Stable IDs, not list positions, define ownership. One finding has exactly one implementation owner at a time.

- **Architect / integration queue (6):** `AL-005`, `AL-006`, `AL-021`, `AL-010`, `AL-025`, `AO-016`.
  The architect owns current-base ports/rebases, stale-branch salvage, open-PR review,
  security/call-site/transactional acceptance, exact-head CI, guarded merge/close,
  post-merge deploy/exact-SHA host-smoke, branch cleanup and final closure/readiness.
  Current integration PR/branch lines include #270, #271, #280, #290, #313 and #320.
- **SOL GPT 1 — upper implementation half (15):** `AL-023`, `AO-015`, `AL-007`, `AL-014`, `AL-016`, `AO-006`, `AL-011`, `AL-017`, `AL-019`, `AO-001`, `AO-005`, `AO-010`, `AO-014`, `AO-017`, `AO-018`.
  It may implement/test and create bounded PR candidates only for these IDs; it does not
  merge/deploy/declare closure and does not take Architect/SOL GPT 2 IDs without handoff.
- **SOL GPT 2 — lower implementation half (16):** `AO-019`, `AO-020`, `AO-021`, `AO-022`, `FD-001`, `FD-002`, `FD-003`, `FD-004`, `CI-001`, `CI-002`, `CI-005`, `AO-012`, `AO-013`, `AO-023`, `AO-024`, `CI-003`.
  It may implement/test and create bounded PR candidates only for these IDs; it does not
  merge/deploy/declare closure and does not take Architect/SOL GPT 1 IDs without handoff.
- New P1 findings default to SOL GPT 1; new P2/P3 findings default to SOL GPT 2 unless
  explicitly marked integration-reserved. When an architect-owned integration PR exists,
  agents stop implementation on that ID unless explicitly asked for review/tests.

## P1 — correctness, safety, delivery integrity, availability

1. ⬜ **[AL-023] Bound OAuth token retention in memory and durable storage.**
   `GatewayOAuthProvider._tokens` retains expired access tokens: both
   `verify_access_token()` and `load_access_token()` return `None` on expiry but
   do not evict the entry. Refresh rotation removes the old refresh token but
   leaves the old access token and adds another access/refresh pair. `TokenStore`
   appends/marks revoked records but never compacts expired/revoked history, and
   startup `load_tokens()` registers non-revoked entries without excluding
   already-expired records. Live evidence on 2026-09-11: `mcp-oauth` was about
   501.5 MiB / 512 MiB before recreate and about 103.6 MiB immediately after
   recreate. This proves restart-reclaimable accumulation, but does not by itself
   prove token retention is the only contributor. Closure: bounded in-memory
   eviction, locked/atomic durable compaction policy, startup expiry filtering,
   repeated-refresh regression tests, no raw-token leakage, and a live soak/metric
   showing memory remains bounded under representative OAuth traffic.

2. ⬜ **[AO-015] `gitea_push_verified_commit` needs auditable per-check execution evidence.**
   Cross-project delivery has shown `checks_verified=true` can disagree with
   canonical CI on the same SHA because verifier toolchain/execution differs.
   Each required check must return evidence bound to the exact workspace/head:
   normalized command identity, cwd/project, duration, exit code, bounded output,
   resolved tool/version provenance, and worktree mutation state. A verifier that
   cannot faithfully execute the requested invariant must fail closed and must
   not push.

3. ⬜ **[AL-021] `gitea_get_file` must bind content to the requested ref and expose the resolved commit.**
   Cross-project evidence showed symbolic `branch="main"` reads returning bytes
   from a stale feature head while exact-SHA/candidate reads returned the correct
   base content. Closure: resolve branch/tag/SHA first, fetch by exact resolved
   commit, return `requested_ref` + `resolved_commit_sha`, key caches by exact
   commit+path, and fail closed on ref/content mismatch. Regress with alternating
   branches containing different blobs at the same path.

4. ⬜ **[AL-005] Eliminate operator tool exposure/catalog/authorization divergence.**
   The server-local `tools_manifest` and the ChatGPT-visible invokable schema
   catalog have repeatedly diverged: advertised tools/required parameters can be
   absent externally, prerequisite tools can be referenced by another tool but
   not invokable, and discovery/invocation permission can change from advertised
   schema to `FORBIDDEN`/missing namespace in one conversation. Closure: one
   permission/schema decision across discovery and invocation, exact contract
   parity, explicit `REQUIRED_PREFLIGHT_UNAVAILABLE`/namespace-revoked diagnostics,
   and byte-exact/hash-safe file-read/CAS contracts. Never infer external
   invokability solely from the server-local manifest.

5. ⬜ **[AL-006] Unify Git namespace identity across SSH/project/control-plane tools.**
   `repo_status(project=...)` can read a registered candidate while command-plane
   `execute_argv git ...` remains outside that repository; trusted Git helpers can
   also operate in a different ref/object namespace and fail with
   `GIT_LOCAL_REF_MISSING`. This directly blocked refreshing stale PR #252 on
   2026-09-11. Closure: host-path-free resolved workspace/ref/object metadata,
   typed `WORKSPACE_NAMESPACE_MISMATCH`, and safe bounded fetch/sync semantics
   that operate on the same registered workspace as trusted mutations.

6. ⬜ **[AL-007] Provide restart-safe command-session recovery and guarded local branch refresh.**
   Gateway deploy/reconnect can invalidate a known session while project tools
   remain usable. Clean workspaces can also be stranded on deleted/stale feature
   branches with no safe local switch/fetch/sync helper. Closure: bounded
   project-level existing-branch switch/default-branch checkout/fetch-prune/local
   cleanup with exact current/head/target guards, plus consistent retryable
   transport classification and recovery guidance. Do not generalize read retries
   to timed-out mutations.

7. ⬜ **[AL-010] Route writes around non-writeable or falsely-writeable production roots.**
    Registered projects have produced both false-negative and false-positive
    `filesystem_writeable` observations relative to the actual workspace/Git
    write plane. Handoff/task/file/Git mutation paths must either use a writable
    candidate automatically or fail early with typed
    `WORKSPACE_NOT_WRITEABLE`/`CANDIDATE_REQUIRED`; metadata must describe the
    exact write plane rather than a coarse directory boolean.

8. ⬜ **[AL-014] Cooldown admission must use the real `CooldownEntry` schema without crashing.**
    The active cooldown path formats diagnostics through non-existent
    `c.backend` while the typed entry exposes `provider`; a real cooldown can
    raise `AttributeError` before any worker starts. Closure: use the typed field,
    tolerate persisted/legacy entries safely, return sanitized provider/until and
    retry guidance, and regression-test an actual cooldown through
    `project_run_agent` without a backend field.

9. ⬜ **[AL-016] Single-agent outer envelopes must not advertise submission success for blocked/non-submitted results.**
    Current normalization converts raw `status="error"` to an outer failure, but
    `status="blocked"` can still pass through `run_tool_async` and inherit
    `success_text="Submitted agent task via router."` even when no new attempt is
    created. Closure: every pre-submit/terminal replay outcome binds outer status,
    nested status, job/attempt presence and actual submission occurrence; blocked
    or terminal non-submission is never `ok=true` submission success.
    Current live evidence: Father UI verification on 2026-09-14 reproduced this live at fleet
    capacity 64/64: `run_agent` returned nested `status="blocked"`,
    `error_code="FLEET_CAPACITY_EXHAUSTED"`, `queued=false`, with no attempt
    submitted, while the outer envelope was still `ok=true` with misleading
    submission success text.

10. ⬜ **[AO-006] Verification must use a trustworthy isolated project/CI-equivalent environment.**
    Verification helpers have failed on unreadable/broken `.venv`, unwritable
    caches, missing lockfiles, monorepo collection semantics, and toolchain drift;
    in one case trusted verification passed while canonical CI failed the same
    formatter invariant on the same SHA. Closure: typed bootstrap/capability
    errors, isolated writable caches/env, repository-declared test profiles,
    pinned toolchain provenance, and no claim of CI-equivalence when execution
    differs. Fresh OpenCode-adapter evidence from PR #257 belongs here, not in a
    duplicate item.
    RAG Router evidence, 2026-09-12: on a clean writable candidate, `run_ruff` and
    `run_compileall` both failed before lint/compile because the Gateway wrapper invoked
    `uv --frozen` while the repository intentionally had no `uv.lock`. This is verifier
    bootstrap failure, not project-code failure. Closure must include lockfile/tooling
    contract detection and typed `VERIFICATION_LOCKFILE_MISSING` /
    `VERIFICATION_ENV_UNSUPPORTED` (or a supported safe non-frozen lane), with
    Ruff/pytest/mypy/compileall regressions on a pyproject repository without `uv.lock`.

11. ⬜ **[AL-025] Started command cancellation must preserve unknown remote outcome.**
    `SSHSessionManager.execute_stream()` cancellation closes the local SSH channel;
    a synthetic/unknown exit (`-1`/`None`) does not prove that the remote process or
    its side effects stopped. The durable path already preserved that uncertainty,
    while the legacy/non-durable path could classify the same started execution as
    `cancelled`. Supervisor live evidence reproduced Gateway `ambiguous` together
    with a fresh runner `running` heartbeat; merged PR #318 now correctly keeps that
    lifecycle non-terminal until runner-owned `finished/final` proof. Current-base
    implementation PR #320 is based on `11ec27a8a6335cf3471e78dd1464572333576d77`
    with exact head `cffb8b8059736825a385d1b2948a90ab50df9d9b`; canonical CI #11492
    failed on three stale event-contract tests plus one unrelated transient Git
    maintenance-lock probe and is being corrected before merge. Closure: pending or
    pre-execution cancellation remains `cancelled`; after execution starts a
    synthetic/unknown local-interrupt result is `ambiguous`; factual non-negative
    remote exit wins as completed/failed; wait/serialization/events preserve the
    ambiguous outcome; no unfenced `runner_pid` kill; exact-head CI plus post-merge
    exact-SHA deploy/host-smoke/live evidence.

## P2 — reliability and capability gaps with fail-closed behavior/workarounds

12. ⬜ **[AL-011] Dirty-worktree review must be explicit, reproducible and hard to misuse.**
    A dirty snapshot source mode exists, but real reviews have still checked out
    only clean `base_ref` when the operator intended to review uncommitted
    changes. Closure: source contract clearly distinguishes committed ref vs
    dirty snapshot, records immutable snapshot/tree evidence, and returns typed
    mismatch/recovery guidance rather than allowing stale clean-source review to
    masquerade as the requested target.

13. ⬜ **[AL-017] Workspace path tools must accept safe framework dynamic-route brackets.**
    Current path validation still rejects literal `[`/`]`, blocking valid files
    such as Astro `[slug].astro` or Next/Svelte `[id].tsx` even inside the
    registered root. Closure: permit brackets while preserving absolute path,
    traversal and root-escape defenses; regress read and relevant write surfaces.

14. ⬜ **[AL-019] Task schema validation and tiny task writes must not enter a slow unrelated control-plane path.**
    Invalid local enum input has taken minutes to return despite being detectable
    at the API boundary, while the underlying task write itself is milliseconds.
    Closure: synchronous local validation, bounded small-write latency, and tests
    for both invalid enum and valid small task creation without weakening
    diagnostics.

15. ⬜ **[AO-001] First-class bounded internal service health probe.**
    Add an allowlisted read-only HTTP/TCP health capability for internal services
    with explicit host/port/path, finite timeout/body limits, provenance and typed
    DNS/refused/timeout/non-2xx/healthy outcomes; never expose env/secrets.

16. ⬜ **[AO-005] Finish typed Gitea repository/PR cleanup/settings operations.**
    Close-without-merge and branch deletion now exist, but repository settings
    such as guarded default-branch change still lack the same first-class exact-
    state mutation contract. Closure: exact expected-state guards, post-read
    verification, idempotency and catalog parity for the remaining cleanup/
    settings operations.

17. ⬜ **[AO-010] Improve command/Docker policy recovery ergonomics without weakening policy.**
    Operators still encounter denied shell wrappers/destructive cleanup, missing
    safe scratch cleanup, and Docker builds that fail before Dockerfile execution
    because the builder/control-plane path is read-only. Closure: typed denial
    with recommended safe alternate tool, bounded cleanup for Gateway-created
    scratch workspaces, and Docker diagnostics identifying context vs HOME/buildx
    cache vs daemon namespace with a sanctioned writable-cache recovery path.
    Current evidence: Father UI verification on 2026-09-14 found `execute_argv`
    rejected syntax-only `bash -n` with `PERMISSION_DENIED` and no first-class
    syntax-check alternative, while `gitea_push_verified_commit` later accepted and
    verified the same `bash -n scripts/deploy-father-ui.sh` invariant as a required check.

18. ⬜ **[AO-014] Atomic secret-file key update without reading/resubmitting unrelated secrets.**
    Add a guarded `dotenv_key_update`/equivalent for allowlisted secret files:
    exact file/key, expected-current guard, atomic single-key update preserving
    unrelated bytes/mode/ownership, duplicate/missing-key refusal, and responses
    containing only non-secret hashes/receipt metadata. Tests must prove old/new
    values and neighboring secret lines never leak.

19. ⬜ **[AO-016] First-class bounded Gitea Actions artifact retrieval.**
    Job logs are not a transport for generated files. Add artifact listing and
    bounded/file-scoped retrieval bound to repo/run/job/head SHA with path-safe
    extraction, size/digest/provenance/truncation metadata, expiry handling and no
    log-based serialization workaround.

20. ⬜ **[AO-017] Typed registered-project → Gitea repository bootstrap/publish.**
    Add idempotent guarded repository creation/binding/default-branch setup and
    exact-head initial publication for an allowlisted owner/project. Incompatible
    existing remotes/settings or expected-head mismatch must fail before mutation.

21. ⬜ **[AO-018] First-class guarded Gitea Actions rerun.**
    Gitea supports run/failed-job rerun APIs but Gateway exposes only reads.
    Operators currently need an admin Docker/API fallback for transient CI
    infrastructure failures. Add exact run/head/attempt guards, terminal-failed
    precondition, ambiguity reconciliation, idempotency and post-read evidence;
    never require a fake no-op commit just to retrigger CI.

22. ⬜ **[AO-019] One-shot Docker/Compose execution with secret-safe env/network binding and read-only service SQL preflight.**
    `docker_run` cannot safely inherit selected Compose secrets/networks and there
    is no `docker_compose_run`. Add exact project/service/image binding, selected
    environment names inherited only inside the execution boundary, `--rm`, no
    ports by default, timeout and structured logs. Also provide a read-only
    Compose-service DB inventory mode restricted to one bounded parsed
    `SELECT`/`WITH`, so operators can inspect an application Postgres without
    accidentally querying Gateway control-plane Postgres or exposing a DSN.

23. ⬜ **[AO-020] Managed read-only agent review for non-Git workspaces.**
    Script/config/data workspaces that intentionally are not Git repos cannot be
    bound to the current immutable `base_ref` task contract. Support an immutable
    `source_kind=workspace_snapshot` digest + allowlist for analysis/review, keep
    mutation/delivery disabled without a separate trusted write contract, and
    return `NON_GIT_WRITE_DELIVERY_UNSUPPORTED` instead of demanding a fake SHA.

24. ⬜ **[AO-021] Make bounded-agent roles and supervisor-only closure first-class.**
    The API/task contract should model agents as implementer/reviewer/call-site
    auditor/adversarial tester/CI repair lanes with exact scope/source/check
    receipts. Agent reports may declare their own work complete but must not
    authoritatively close a finding/project or declare readiness. Make isolated
    parallel role-separated agents, evidence comparison, cancellation/retry and
    failed-review preservation cheap for the architect. The 2026-09-11 inventory
    agent losing its already-read task context is further evidence that prompt
    wording alone is not a reliable orchestration contract.

25. ⬜ **[AO-022] Candidate clones need first-class lifecycle cleanup / unregister.**
    Live operator evidence on 2026-09-17 found dozens of historical
    `candidate-agent-ssh-gateway-*` workspaces still present under the
    server-owned `.mcp-candidate-clones` area after merged/closed deliveries,
    superseded bases, probes and review passes. `project_list()` simultaneously
    still exposes historical candidates as registered `candidate-clone` projects
    in the runtime overlay. Creation is therefore durable in both filesystem and
    registry state, but the supervisor surface exposes `prepare_candidate_clone`
    / `supervisor_register_project` with no matching `candidate_cleanup` /
    `supervisor_unregister_project`. Raw deletion is intentionally unavailable,
    and deleting only the directory would leave registry/cache state pointing at
    a missing root while unregistering only the project would leak disk state.

    Closure: add an admin-only typed candidate lifecycle primitive restricted to
    server-owned candidate roots. Require exact project id, expected HEAD SHA and
    expected branch/source identity; fail closed for dirty worktrees, active
    task/job/delivery references, open-PR candidates, metadata mismatch,
    symlink/root escape, pending supervisor integration journals or registered
    descendants. Cleanup must CAS/journal the runtime-registry removal, reset both
    REST and MCP registry caches, and remove the exact candidate directory as one
    reconciled lifecycle operation. Partial failure between unregister and
    filesystem removal must be observable and idempotently recoverable, with a
    bounded audit tombstone containing candidate/source/base/head/branch identity
    and cleanup reason but no host paths or secrets.

    Also provide bounded `plan -> apply` GC for accumulated candidates. Planning
    classifies each candidate with explicit keep/delete evidence; apply revalidates
    exact HEAD, clean status and active-reference state immediately before
    mutation. Age alone never authorizes deletion, and open PRs, active tasks /
    deliveries and current supervisor review workspaces are kept by default.
    Acceptance covers stale-clean, dirty, open-PR, active-task,
    missing-directory registry entries, orphan directories without registry
    entries, registered descendants and a crash between unregister/delete. Only
    provably stale clean candidates disappear; `project_list()` drops successfully
    cleaned entries after cache reset; repeated cleanup is idempotent; canonical
    and source projects cannot be deleted through this primitive. This is
    lifecycle correctness, distinct from AO-010 generic command-policy cleanup
    ergonomics and AO-012 project-catalog pagination.
    Current cleanup-core state: merged code has exact identity guards,
    dirty/active-evidence/open-delivery fail-closed checks, CAS unregister plus registry
    reset, dev/inode re-check before removal, `prepared -> registry_removed -> complete`
    tombstones, and idempotent/crash-recovery/concurrency regressions. Keep this finding
    open: the current-master audit still does not evidence plan -> apply GC, a first-class
    supervisor/MCP cleanup tool surface, or dedicated orphan-directory GC classification.

26. ⬜ **[FD-001] Route frontend verification through an approved Astro/frontend build capability.**
    Registered frontend projects should not depend on ad-hoc SSH `npm`. Add a
    typed frontend/Astro build helper that binds exact project/ref, package
    manager/lockfile and allowlisted script to an approved builder/runtime and
    returns toolchain/artifact/output evidence plus typed unavailable/failed
    outcomes.

27. ⬜ **[FD-002] Bounded managed HTTP smoke for public sites and internal services.**
    Expose safe `GET`/`HEAD` smoke (prefer the existing relay-curl substrate where
    appropriate) with explicit target/path, redirect/status/content-type, bounded
    body, timeout, optional XML validation and typed network/HTTP/XML failures.
    Do not weaken raw shell/curl policy.

28. ⬜ **[FD-003] Deploy-contract bridge for Astro/Compose frontend projects.**
    Bind a registered immutable source/candidate to the repo's approved deploy
    contract and exact Compose project/service/image sequence. Support read-only
    Compose render/preflight, exact namespace verification, dependency-only
    rollout and backup/snapshot prerequisites for DB-major migrations, then
    return build/recreate/log/health evidence without arbitrary deploy scripts.

29. ⬜ **[FD-004] CI-gated merges need typed no-workflow/no-run/trigger-incompatible states.**
    `CI_NOT_GREEN` conflates zero workflows, no run for the exact head, stale runs
    and repositories whose canonical workflow is push-only while merge policy
    accepts only `pull_request`. Add `CI_NOT_CONFIGURED`,
    `NO_REQUIRED_RUN_FOUND`/`CI_TRIGGER_INCOMPATIBLE` and an explicit repository
    policy for acceptable exact-head evidence without weakening fail-closed
    defaults.

30. ⬜ **[CI-001] Explain and eliminate correlated Docker-runner wall-clock failures.**
    Historical build/deploy phases failed near a shared ~18-minute boundary.
    Network retries and the Python-suite timeout fix improved adjacent symptoms,
    but the shared runner/resource/registry/storage/supervisor question is not
    proven closed. Closure requires controlled repeated build+deploy evidence and
    diagnostics that distinguish runner-specific degradation from shared limits.

31. ⬜ **[CI-002] Reconcile and safely clean superseded/orphaned Gitea Actions task containers.**
    Old Action containers/runs can remain active after newer PR heads become
    authoritative and continue consuming runner capacity. Add read-only mapping
    of run/job/container/head state plus cleanup guarded by exact run id, job id,
    container id and head SHA; ambiguous ownership must fail closed.

32. ⬜ **[CI-005] Remove the WebSocket TestClient contention quarantine or make it observable.**
    Unit CI currently reruns only `WebSocketDisconnect` because Starlette's
    in-process WebSocket TestClient can drop a pending response under CI resource
    contention. Before this consolidation the workflow still referenced obsolete
    `TODO.md T80.5`; that stale reference showed the underlying debt had fallen out
    of the authoritative backlog. Closure: reproduce
    or instrument the contention sufficiently to fix it, or establish a bounded
    deterministic test harness; until then the narrow retry must remain limited
    to that exact failure signature and expose retry occurrence in CI evidence.

## P3 — efficiency, ergonomics, productization and cosmetic debt

33. ⬜ **[AO-012] Filter and paginate the registered project catalog.**
    A one-project lookup has returned hundreds of historical candidates (679 in
    2026-09-11 evidence). Add exact/prefix/text and project-type/tag/parent filters,
    stable ordering, total count, `limit`/`offset`, and compact projection. Keep a
    bounded backwards-compatible unfiltered mode.

34. ⬜ **[AO-013] Async progress/cancellation for long candidate preparation.**
    Fresh candidate preparation has taken roughly 63–116 seconds in prior work
    and tens of seconds in current work while remaining synchronous/opaque. Add a
    durable job id, fetch/checkout/register/verify phases, cancellation before
    final registration, safe retry/idempotent recovery and optional bounded sync
    wait.

35. ⬜ **[AO-023] Define a supported public API/versioning/deprecation contract.**
    This is the still-relevant residue of the old `docs/roadmap.md` “stabilize the
    public API” item. Document which HTTP/WebSocket/MCP surfaces are stable,
    versioning/deprecation guarantees, compatibility window and migration path;
    bind breaking-change CI/release checks to that policy.

36. ⬜ **[AO-024] Produce versioned release artifacts in addition to continuously published container images.**
    Container images are already built, smoke-tested and pushed by CI, so that
    half of the old roadmap item is closed. No release workflow currently
    produces a formal versioned release artifact/changelog/provenance bundle.
    Define whether such releases are a supported product contract; if yes, add a
    reproducible tagged-release path with checksums/provenance, otherwise close
    this item explicitly as out of scope.

37. ⬜ **[CI-003] Avoid duplicate heavy post-merge CI when an exact PR head is already green.**
    Current master pushes rerun the full Python matrix after an exact PR has
    already passed it. Add a fail-closed fast path only when the merge commit is
    proven to be a clean merge of the exact green PR head with no extra code;
    direct pushes/ambiguous ancestry retain full CI. Build, deploy and host-smoke
    still run for the delivered master SHA.

