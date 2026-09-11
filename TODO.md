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

2. ⬜ **[CI-004] Browser E2E CI must fail closed when Selenium cannot execute.**
   Current Gitea runs report the `E2E (Selenium)` job as `success` while the
   actual `E2E tests` step is `skipped` when Chrome/Chromedriver is unavailable.
   A green aggregate workflow therefore does not prove browser coverage. Closure:
   reproducible browser-capable runner/provisioning plus an assertion that the
   Selenium tests actually ran; missing browser tooling must not aggregate as a
   passing E2E gate.

3. ⬜ **[AO-015] `gitea_push_verified_commit` needs auditable per-check execution evidence.**
   Cross-project delivery has shown `checks_verified=true` can disagree with
   canonical CI on the same SHA because verifier toolchain/execution differs.
   Each required check must return evidence bound to the exact workspace/head:
   normalized command identity, cwd/project, duration, exit code, bounded output,
   resolved tool/version provenance, and worktree mutation state. A verifier that
   cannot faithfully execute the requested invariant must fail closed and must
   not push.

4. ⬜ **[AL-020] Reconcile ambiguous Gitea merge mutation outcomes before allowing retry.**
   A merge can succeed server-side while timeout/HTTP 5xx is returned to the
   operator. Blind retry risks duplicate/contradictory mutation semantics.
   Closure: bounded fresh postcondition reads after timeout/5xx; return
   `completed_after_ambiguous_response` with exact merge SHA when proven, or a
   distinct `MUTATION_OUTCOME_UNKNOWN` verdict that forbids blind retry. PR reads
   should expose first-class `merged`/`merge_commit_sha` evidence. Cover lost
   responses and write-succeeded/HTTP-500 cases.

5. ⬜ **[AL-021] `gitea_get_file` must bind content to the requested ref and expose the resolved commit.**
   Cross-project evidence showed symbolic `branch="main"` reads returning bytes
   from a stale feature head while exact-SHA/candidate reads returned the correct
   base content. Closure: resolve branch/tag/SHA first, fetch by exact resolved
   commit, return `requested_ref` + `resolved_commit_sha`, key caches by exact
   commit+path, and fail closed on ref/content mismatch. Regress with alternating
   branches containing different blobs at the same path.

6. ⬜ **[AL-022] `git_commit` lease must bind staged bytes, not only porcelain status shape.**
   `expected_status_sha256` can remain unchanged when already-staged file bytes
   change but path/status letters do not. It is therefore not proof that the
   reviewed staged tree is the tree being committed. Closure: expose/enforce a
   content-bound index lease (for example Git tree object id or canonical cached
   diff/index digest) covering blob bytes, mode and rename target; stale lease
   must reject with zero commit mutation.

7. ⬜ **[AL-005] Eliminate operator tool exposure/catalog/authorization divergence.**
   The server-local `tools_manifest` and the ChatGPT-visible invokable schema
   catalog have repeatedly diverged: advertised tools/required parameters can be
   absent externally, prerequisite tools can be referenced by another tool but
   not invokable, and discovery/invocation permission can change from advertised
   schema to `FORBIDDEN`/missing namespace in one conversation. Closure: one
   permission/schema decision across discovery and invocation, exact contract
   parity, explicit `REQUIRED_PREFLIGHT_UNAVAILABLE`/namespace-revoked diagnostics,
   and byte-exact/hash-safe file-read/CAS contracts. Never infer external
   invokability solely from the server-local manifest.

8. ⬜ **[AL-006] Unify Git namespace identity across SSH/project/control-plane tools.**
   `repo_status(project=...)` can read a registered candidate while command-plane
   `execute_argv git ...` remains outside that repository; trusted Git helpers can
   also operate in a different ref/object namespace and fail with
   `GIT_LOCAL_REF_MISSING`. This directly blocked refreshing stale PR #252 on
   2026-09-11. Closure: host-path-free resolved workspace/ref/object metadata,
   typed `WORKSPACE_NAMESPACE_MISMATCH`, and safe bounded fetch/sync semantics
   that operate on the same registered workspace as trusted mutations.

9. ⬜ **[AL-007] Provide restart-safe command-session recovery and guarded local branch refresh.**
   Gateway deploy/reconnect can invalidate a known session while project tools
   remain usable. Clean workspaces can also be stranded on deleted/stale feature
   branches with no safe local switch/fetch/sync helper. Closure: bounded
   project-level existing-branch switch/default-branch checkout/fetch-prune/local
   cleanup with exact current/head/target guards, plus consistent retryable
   transport classification and recovery guidance. Do not generalize read retries
   to timed-out mutations.

10. ⬜ **[AL-010] Route writes around non-writeable or falsely-writeable production roots.**
    Registered projects have produced both false-negative and false-positive
    `filesystem_writeable` observations relative to the actual workspace/Git
    write plane. Handoff/task/file/Git mutation paths must either use a writable
    candidate automatically or fail early with typed
    `WORKSPACE_NOT_WRITEABLE`/`CANDIDATE_REQUIRED`; metadata must describe the
    exact write plane rather than a coarse directory boolean.

11. ⬜ **[AL-014] Cooldown admission must use the real `CooldownEntry` schema without crashing.**
    The active cooldown path formats diagnostics through non-existent
    `c.backend` while the typed entry exposes `provider`; a real cooldown can
    raise `AttributeError` before any worker starts. Closure: use the typed field,
    tolerate persisted/legacy entries safely, return sanitized provider/until and
    retry guidance, and regression-test an actual cooldown through
    `project_run_agent` without a backend field.

12. ⬜ **[AL-015] Finish live OpenCode output/startup semantic observability.**
    This consolidates the old startup/proxy-rotation, bounded-log, useful-work,
    and proxy-sidecar TODOs into one delivery finding. Private runner output must
    remain the authoritative raw classifier source while a bounded atomic,
    redacted attempt-local tail is published live; previous proxy attempts must
    not contaminate current diagnostics; zero-byte placeholders/heartbeats must
    not count as semantic progress; `running` should be emitted only after real
    model/tool activity. Implementation is in-flight in PR #252 but is not closed
    until rebuilt on current master, independently reviewed, CI-gated, deployed,
    and exercised live.

13. ⬜ **[AL-016] Single-agent outer envelopes must not advertise submission success for blocked/non-submitted results.**
    Current normalization converts raw `status="error"` to an outer failure, but
    `status="blocked"` can still pass through `run_tool_async` and inherit
    `success_text="Submitted agent task via router."` even when no new attempt is
    created. Closure: every pre-submit/terminal replay outcome binds outer status,
    nested status, job/attempt presence and actual submission occurrence; blocked
    or terminal non-submission is never `ok=true` submission success.

14. ⬜ **[AL-024] Do not replay ambiguous CodeIntelligence adapter generation requests.**
    Current `CodeIntelligence.generate_code()` retries the same POST up to three
    times after timeout, transport failure, non-200, adapter error and short
    response. Once `/api/generate` begins, execution is ambiguous and replay can
    duplicate work. PR #251 contains a candidate single-dispatch fix and targeted
    tests but is stale. Closure: current-base implementation, exactly one POST for
    ambiguous outcomes, safe local fallback, full call-site review and CI.

15. ⬜ **[AO-006] Verification must use a trustworthy isolated project/CI-equivalent environment.**
    Verification helpers have failed on unreadable/broken `.venv`, unwritable
    caches, missing lockfiles, monorepo collection semantics, and toolchain drift;
    in one case trusted verification passed while canonical CI failed the same
    formatter invariant on the same SHA. Closure: typed bootstrap/capability
    errors, isolated writable caches/env, repository-declared test profiles,
    pinned toolchain provenance, and no claim of CI-equivalence when execution
    differs. Fresh OpenCode-adapter evidence from PR #257 belongs here, not in a
    duplicate item.

16. ⬜ **[AL-018] Make the candidate verifier concurrency-safe.**
    `candidate_verifier.py` still defines one global container name
    `mcp-candidate-verifier`; parallel deliveries can collide and fail with Docker
    exit 125. Closure: unique request/job-scoped verifier identity (or equivalent
    isolated primitive), deterministic cleanup, cancellation safety, and a
    concurrent regression proving two independent verified deliveries cannot
    reuse/collide with the same verifier container.

## P2 — important reliability and capability gaps

17. ⬜ **[AL-011] Dirty-worktree review must be explicit, reproducible and hard to misuse.**
    A dirty snapshot source mode exists, but real reviews have still checked out
    only clean `base_ref` when the operator intended to review uncommitted
    changes. Closure: source contract clearly distinguishes committed ref vs
    dirty snapshot, records immutable snapshot/tree evidence, and returns typed
    mismatch/recovery guidance rather than allowing stale clean-source review to
    masquerade as the requested target.

18. ⬜ **[AL-017] Workspace path tools must accept safe framework dynamic-route brackets.**
    Current path validation still rejects literal `[`/`]`, blocking valid files
    such as Astro `[slug].astro` or Next/Svelte `[id].tsx` even inside the
    registered root. Closure: permit brackets while preserving absolute path,
    traversal and root-escape defenses; regress read and relevant write surfaces.

19. ⬜ **[AL-019] Task schema validation and tiny task writes must not enter a slow unrelated control-plane path.**
    Invalid local enum input has taken minutes to return despite being detectable
    at the API boundary, while the underlying task write itself is milliseconds.
    Closure: synchronous local validation, bounded small-write latency, and tests
    for both invalid enum and valid small task creation without weakening
    diagnostics.

20. ⬜ **[AO-001] First-class bounded internal service health probe.**
    Add an allowlisted read-only HTTP/TCP health capability for internal services
    with explicit host/port/path, finite timeout/body limits, provenance and typed
    DNS/refused/timeout/non-2xx/healthy outcomes; never expose env/secrets.

21. ⬜ **[AO-005] Finish typed Gitea repository/PR cleanup/settings operations.**
    Close-without-merge and branch deletion now exist, but repository settings
    such as guarded default-branch change still lack the same first-class exact-
    state mutation contract. Closure: exact expected-state guards, post-read
    verification, idempotency and catalog parity for the remaining cleanup/
    settings operations.

22. ⬜ **[AO-010] Improve command/Docker policy recovery ergonomics without weakening policy.**
    Operators still encounter denied shell wrappers/destructive cleanup, missing
    safe scratch cleanup, and Docker builds that fail before Dockerfile execution
    because the builder/control-plane path is read-only. Closure: typed denial
    with recommended safe alternate tool, bounded cleanup for Gateway-created
    scratch workspaces, and Docker diagnostics identifying context vs HOME/buildx
    cache vs daemon namespace with a sanctioned writable-cache recovery path.

23. ⬜ **[AO-014] Atomic secret-file key update without reading/resubmitting unrelated secrets.**
    Add a guarded `dotenv_key_update`/equivalent for allowlisted secret files:
    exact file/key, expected-current guard, atomic single-key update preserving
    unrelated bytes/mode/ownership, duplicate/missing-key refusal, and responses
    containing only non-secret hashes/receipt metadata. Tests must prove old/new
    values and neighboring secret lines never leak.

24. ⬜ **[AO-016] First-class bounded Gitea Actions artifact retrieval.**
    Job logs are not a transport for generated files. Add artifact listing and
    bounded/file-scoped retrieval bound to repo/run/job/head SHA with path-safe
    extraction, size/digest/provenance/truncation metadata, expiry handling and no
    log-based serialization workaround.

25. ⬜ **[AO-017] Typed registered-project → Gitea repository bootstrap/publish.**
    Add idempotent guarded repository creation/binding/default-branch setup and
    exact-head initial publication for an allowlisted owner/project. Incompatible
    existing remotes/settings or expected-head mismatch must fail before mutation.

26. ⬜ **[AO-018] First-class guarded Gitea Actions rerun.**
    Gitea supports run/failed-job rerun APIs but Gateway exposes only reads.
    Operators currently need an admin Docker/API fallback for transient CI
    infrastructure failures. Add exact run/head/attempt guards, terminal-failed
    precondition, ambiguity reconciliation, idempotency and post-read evidence;
    never require a fake no-op commit just to retrigger CI.

27. ⬜ **[AO-019] One-shot Docker/Compose execution with secret-safe env/network binding and read-only service SQL preflight.**
    `docker_run` cannot safely inherit selected Compose secrets/networks and there
    is no `docker_compose_run`. Add exact project/service/image binding, selected
    environment names inherited only inside the execution boundary, `--rm`, no
    ports by default, timeout and structured logs. Also provide a read-only
    Compose-service DB inventory mode restricted to one bounded parsed
    `SELECT`/`WITH`, so operators can inspect an application Postgres without
    accidentally querying Gateway control-plane Postgres or exposing a DSN.

28. ⬜ **[AO-020] Managed read-only agent review for non-Git workspaces.**
    Script/config/data workspaces that intentionally are not Git repos cannot be
    bound to the current immutable `base_ref` task contract. Support an immutable
    `source_kind=workspace_snapshot` digest + allowlist for analysis/review, keep
    mutation/delivery disabled without a separate trusted write contract, and
    return `NON_GIT_WRITE_DELIVERY_UNSUPPORTED` instead of demanding a fake SHA.

29. ⬜ **[AO-021] Make bounded-agent roles and supervisor-only closure first-class.**
    The API/task contract should model agents as implementer/reviewer/call-site
    auditor/adversarial tester/CI repair lanes with exact scope/source/check
    receipts. Agent reports may declare their own work complete but must not
    authoritatively close a finding/project or declare readiness. Make isolated
    parallel role-separated agents, evidence comparison, cancellation/retry and
    failed-review preservation cheap for the architect. The 2026-09-11 inventory
    agent losing its already-read task context is further evidence that prompt
    wording alone is not a reliable orchestration contract.

30. ⬜ **[FD-001] Route frontend verification through an approved Astro/frontend build capability.**
    Registered frontend projects should not depend on ad-hoc SSH `npm`. Add a
    typed frontend/Astro build helper that binds exact project/ref, package
    manager/lockfile and allowlisted script to an approved builder/runtime and
    returns toolchain/artifact/output evidence plus typed unavailable/failed
    outcomes.

31. ⬜ **[FD-002] Bounded managed HTTP smoke for public sites and internal services.**
    Expose safe `GET`/`HEAD` smoke (prefer the existing relay-curl substrate where
    appropriate) with explicit target/path, redirect/status/content-type, bounded
    body, timeout, optional XML validation and typed network/HTTP/XML failures.
    Do not weaken raw shell/curl policy.

32. ⬜ **[FD-003] Deploy-contract bridge for Astro/Compose frontend projects.**
    Bind a registered immutable source/candidate to the repo's approved deploy
    contract and exact Compose project/service/image sequence. Support read-only
    Compose render/preflight, exact namespace verification, dependency-only
    rollout and backup/snapshot prerequisites for DB-major migrations, then
    return build/recreate/log/health evidence without arbitrary deploy scripts.

33. ⬜ **[FD-004] CI-gated merges need typed no-workflow/no-run/trigger-incompatible states.**
    `CI_NOT_GREEN` conflates zero workflows, no run for the exact head, stale runs
    and repositories whose canonical workflow is push-only while merge policy
    accepts only `pull_request`. Add `CI_NOT_CONFIGURED`,
    `NO_REQUIRED_RUN_FOUND`/`CI_TRIGGER_INCOMPATIBLE` and an explicit repository
    policy for acceptable exact-head evidence without weakening fail-closed
    defaults.

34. ⬜ **[CI-001] Explain and eliminate correlated Docker-runner wall-clock failures.**
    Historical build/deploy phases failed near a shared ~18-minute boundary.
    Network retries and the Python-suite timeout fix improved adjacent symptoms,
    but the shared runner/resource/registry/storage/supervisor question is not
    proven closed. Closure requires controlled repeated build+deploy evidence and
    diagnostics that distinguish runner-specific degradation from shared limits.

35. ⬜ **[CI-002] Reconcile and safely clean superseded/orphaned Gitea Actions task containers.**
    Old Action containers/runs can remain active after newer PR heads become
    authoritative and continue consuming runner capacity. Add read-only mapping
    of run/job/container/head state plus cleanup guarded by exact run id, job id,
    container id and head SHA; ambiguous ownership must fail closed.

36. ⬜ **[CI-005] Remove the WebSocket TestClient contention quarantine or make it observable.**
    Unit CI currently reruns only `WebSocketDisconnect` because Starlette's
    in-process WebSocket TestClient can drop a pending response under CI resource
    contention. Before this consolidation the workflow still referenced obsolete
    `TODO.md T80.5`; that stale reference showed the underlying debt had fallen out
    of the authoritative backlog. Closure: reproduce
    or instrument the contention sufficiently to fix it, or establish a bounded
    deterministic test harness; until then the narrow retry must remain limited
    to that exact failure signature and expose retry occurrence in CI evidence.

## P3 — efficiency, ergonomics, productization and cosmetic debt

37. ⬜ **[AO-012] Filter and paginate the registered project catalog.**
    A one-project lookup has returned hundreds of historical candidates (679 in
    2026-09-11 evidence). Add exact/prefix/text and project-type/tag/parent filters,
    stable ordering, total count, `limit`/`offset`, and compact projection. Keep a
    bounded backwards-compatible unfiltered mode.

38. ⬜ **[AO-013] Async progress/cancellation for long candidate preparation.**
    Fresh candidate preparation has taken roughly 63–116 seconds in prior work
    and tens of seconds in current work while remaining synchronous/opaque. Add a
    durable job id, fetch/checkout/register/verify phases, cancellation before
    final registration, safe retry/idempotent recovery and optional bounded sync
    wait.

39. ⬜ **[AO-023] Define a supported public API/versioning/deprecation contract.**
    This is the still-relevant residue of the old `docs/roadmap.md` “stabilize the
    public API” item. Document which HTTP/WebSocket/MCP surfaces are stable,
    versioning/deprecation guarantees, compatibility window and migration path;
    bind breaking-change CI/release checks to that policy.

40. ⬜ **[AO-024] Produce versioned release artifacts in addition to continuously published container images.**
    Container images are already built, smoke-tested and pushed by CI, so that
    half of the old roadmap item is closed. No release workflow currently
    produces a formal versioned release artifact/changelog/provenance bundle.
    Define whether such releases are a supported product contract; if yes, add a
    reproducible tagged-release path with checksums/provenance, otherwise close
    this item explicitly as out of scope.

41. ⬜ **[CI-003] Avoid duplicate heavy post-merge CI when an exact PR head is already green.**
    Current master pushes rerun the full Python matrix after an exact PR has
    already passed it. Add a fail-closed fast path only when the merge commit is
    proven to be a clean merge of the exact green PR head with no extra code;
    direct pushes/ambiguous ancestry retain full CI. Build, deploy and host-smoke
    still run for the delivered master SHA.
