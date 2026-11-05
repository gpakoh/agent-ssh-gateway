# Agent SSH Gateway — TODO

**Reconciled:** 2026-11-05 against authoritative Gitea and exact deployed `master@dbe254dd7541ab137c351739f45df527f6c935b1` (PR #791, post-merge CI #22833 SUCCESS: build, deploy, host-smoke; live Gateway and MCP SHA verified; `gitea_close_issue` verified in the real `mcp_client_write` catalog). Earlier 2026-11-02 release: PR #758, CI #22178 SUCCESS, `master@e1456dbd`. Previous reconciliations: 2026-11-01 `master@5deb30c3fa464cfb108a5db9e5e4fbe387936d09`, CI #21914/#21917 SUCCESS, and 2026-10-30 `master@f10b014ed85819ffe974f55be76463d5db71a02f`, CI #21331/#21342 SUCCESS.

**Open: 29 (P1 8 / P2 13 / P3 8).** Gitea Issues: **30 open**; PRs: **0 open**. These are distinct, overlapping inventories; **do not add them into 59 tasks**. Each local checkbox is an independently closeable outcome and remains open until actual accepted behavior, including production evidence when needed. The separate Gitea Issues index below is authoritative for cross-project coordination and ownership. OpenCode security remains a live prerequisite, not a green-CI assumption. Completed deploy LKG migration evidence is recorded in `docs/operations/SSH_GATEWAY_DEPLOY_STATE_MIGRATION.md`.

**2026-10-30 closeout evidence (not active TODO):** P2-15 — PR #728, commit `f10b014e`, CI #21331 and post-merge #21342 SUCCESS: both `docker_ps` and `docker_images` expose configured-daemon inventory scope, endpoint identity hash and truncation/unknown host-wide completeness; this does **not** establish exhaustive host/multi-daemon discovery or real pagination. P2-16 — verified guarded `git_fetch_ref` + `git_update_branch_by_merge` on the existing #730 and #728 candidates against exact `origin/master` SHA, followed by published current-base PR heads and successful CI/merge. Remaining independent edge cases should be new scoped findings rather than re-opening accepted outcomes.

**2026-11-01 closeout evidence (not active TODO):** P2-12 — PR #749, merge `master@5deb30c3fa464cfb108a5db9e5e4fbe387936d09`. Fail-fast `docker_compose_up`/related project-scoped confirmation now rejects unconfigured `MCP_ALLOWED_PROJECT_ROOTS` and explicitly disallowed directories before allocating an unusable one-shot pending action; registered-root resolution stays supported. Exact PR HEAD `8965c8ecaddb2509657c707ce59074e5c3f077e2` passed CI #21914 (20/20 checks); post-merge CI #21917 on exact master passed build, deploy and host-smoke. All three changed file blobs were independently compared between PR head and master. No live Compose mutation was used to qualify the preflight; historical dirty source candidate remains preserved separately pending ownership review.

**2026-11-05 closeout evidence (not active TODO):** P3-07 — PR #790, exact merge `master@9b7d4d57e07c38965cefbe2999faf4fda697d31a`, PR CI #22796 SUCCESS (20 required), post-merge #22815 SUCCESS (build/deploy/host-smoke). Its initial live tool registry omitted `gitea_close_issue`; PR #791 restored the missing `mcp_client_write` allowlist entry, RED→GREEN real FastMCP `tools/list` regression, exact merge `master@dbe254dd7541ab137c351739f45df527f6c935b1`, PR CI #22821 attempt 2 SUCCESS (20 required checks), post-merge #22833 SUCCESS. Independently verified live Gateway/MCP exact SHA and `tools_manifest` / `gateway_catalog` exposing typed `gitea_close_issue` only in write mode with `mcp:repo`/`mcp:admin`. The current ChatGPT attachment still needs client catalog refresh (P1-02), which does not undo server-side acceptance. No production Issue state mutation was used as a smoke test.

## P1 — Security, deploy and release blockers (8)

- [ ] **P1-01: Provide canonical and candidate write-plane recovery paths.** (legacy #2)
  Some projects report candidate fallback while task policy requires canonical non-candidate execution. Others have handoff writes failing while archive/task maintenance succeeds. Expose exact write capability for code, Git and handoff surfaces, and return `CANONICAL_WRITE_PLANE_UNAVAILABLE` with safe recovery steps when a clean writeable canonical workspace cannot be provisioned.
  **Remaining acceptance:** typed canonical vs candidate Git/handoff capability; safe recovery of missing/corrupt lineage; fenced terminal-evidence candidate cleanup with preserved artifacts. `WORKSPACE_CONTENDED` case #634, cleanup #640; PR #643 diagnostic fingerprint is not clearance.

- [ ] **P1-02: Keep ChatGPT-visible tool schemas in parity with server manifests.** (legacy #6)
  Tools can appear available in `tools_manifest` while missing or having a different callable schema in the ChatGPT resource catalog. PR-A measurement now accepts a bounded client-reported tool list, binds retained attestations to the server-created MCP lifecycle + opaque auth identity + live toolset hash, emits complete server/client diff diagnostics, and keeps incomplete omissions explicitly unknown. Client reports remain unverified and do not gate existing Git mutations. This item stays open until a real external connector submits a complete report and identifies the filtering/caching/allowlist mechanism; do not add a curated catalog workaround before that evidence exists.
  **Remaining acceptance:** exact external callable manifest parity/lease and invalidation. Fold in the `write_agent_task.source_mode` incident: callable schema must enumerate `committed_head` and `dirty_worktree_snapshot`; runtime-only validation is insufficient.

- [ ] **P1-03: Reconcile Gitea Actions lifecycle, stranded jobs and cancellable runs.** (legacy #7)
  CI runs/jobs can remain `waiting` / `in_progress` after all useful work is done, after cleanup timeouts, or after old workflow jobs later become runnable. Add exact-run/head cancellation, stale/orphaned container reconciliation, terminalization of false-condition/dependency-blocked jobs, and safe rerun eligibility diagnostics.

- [ ] **P1-04: Add a repo-owned deploy-script / deploy-contract runner.** (legacy #8)
  Some repositories intentionally encode safety in a reviewed deploy script rather than raw Compose primitives: preflight, locks, immutable image checks, replay-stable snapshots, health smoke, LKG writes and rollback. Gateway needs a controlled runner that binds exact script path/content/ref, image authority, env names and Compose project, while redacting secrets and reconciling ambiguous Docker outcomes.

- [ ] **P1-05: Add one-shot Compose/Docker execution with secret-safe env inheritance.** (legacy #9)
  Operators need to run migrations, readiness commands and read-only database inventory against Compose-backed services without exposing DSNs or mutating long-running service definitions. Support exact image/service/network guards, inherited env names, `--rm`, timeouts, no port publication by default, and a read-only SQL/catalog mode for Compose PostgreSQL services.

- [ ] **P1-07: Isolate PR Docker jobs from the shared host daemon or document the trust boundary.** (legacy #11)
  PR-controlled Docker build/smoke steps can mutate the shared runner Docker daemon. Prefer disposable per-job daemon/VM isolation; otherwise enforce and document that every same-repo PR writer is intentionally trusted as host admin and prevent untrusted/fork PRs from privileged labels.

- [ ] **P1-08: Provide redacted secret propagation/provisioning across approved deployment and CI scopes.** (legacy #47)
  Production deploys and cross-repository CI can require an existing root-only credential without any safe way to copy/provision it to the approved target. Browser Service was blocked on `PROXY_REGISTRY_API_KEY`; work-session-service could not pull the private `agent-memory-service` image because its repository `REGISTRY_TOKEN` was absent. Add an audited primitive that accepts only allowlisted source/target identities and variable names, never returns or logs the value, preserves target ownership/mode, and reports presence/equality or opaque hash evidence only. It may either synchronize an approved env/secret target or inject the value only inside one guarded deploy/Actions boundary. Acceptance must prove source/target identity, no plaintext exposure, exact postcondition verification, clean-runner private-image pull, and fail-closed behavior for missing/ambiguous credentials.

- [ ] **P1-09: OpenCode scheduled upgrade failure prevents bounded agent jobs before implementation.** (legacy #1)
  <!-- gateway-todo-key: opencode-upgrade-failed --> **Severity:** P1
  **Still blocked:** historical exit 80 / upgrade-blocked job reached zero required checks and zero diff. PR #630 only documented incident, did not fix runtime; new bounded qualified executor canary and Node 22 prerequisite needed. Coordination issues #639 and #642.

## P2 — Reliability and engineering throughput (13)

- [ ] **P2-01: Wire the production `astro-sites` named root into `mcp-oauth` after the host path is provisioned.** (legacy #13)
  Named-root registration is now supported, but production still has no `MCP_OAUTH_ASTRO_SITES_ROOT` setting and the intended host directory is not provisioned. Once the operator supplies an existing absolute host path, add the `registry_roots.astro-sites` entry, the explicit writable `mcp-oauth` bind with host-path auto-creation disabled, the documented env contract, and deploy seam coverage. Deploy must fail closed when the configured path is absent; do not silently create an empty root.

- [ ] **P2-02: Make verification helpers honor project environments and CI-equivalent toolchains.** (legacy #14)
  `run_pytest`, `run_ruff`, `run_mypy` and verifier containers should not fail falsely on unreadable `.venv`, missing `uv.lock`, unwritable caches, monorepo collection collisions, or verifier/CI tool-version drift. A 2026-10-08 Kojo verified-push attempt additionally proved the trusted verifier image could use `/usr/bin/python` without `pytest`, while the repository verifier could execute the targeted tests. Expose candidate-verifier versus Actions-plane capability provenance explicitly; advertised checks must either run in the selected plane or return a typed environment/bootstrap diagnostic instead of code failure.
  **New confirmed cases:** two `deep-researcher` pip-only candidates fail `run_pytest`/`run_ruff` on mandatory `uv --frozen` with missing `uv.lock`; treat as *one* contract alongside existing env/cache/interpreter parity, with pip/requirements support and CI-equivalent checks.
  **2026-10-31 scoped progress (not closure):** isolated requirements-only projects without `pyproject.toml` now receive a verifier-only temporary metadata file under the guarded external verifier root; original source remains unchanged, `uv.lock` is never fabricated. Projects with neither supported requirements nor `pyproject.toml` fail with typed `VERIFIER_BOOTSTRAP_FAILED`, not a false pytest failure. Gateway regression covers requirements-only and pyproject-without-lock cases and missing metadata. Full P2-02 remains open for native NOD/Deep Researcher runtime acceptance, CI interpreter/version parity and reliable bootstrap diagnostics; related narrower Gitea Issue #654 was closed on 2026-11-04, which is not acceptance of all P2-02 criteria.

- [ ] **P2-03: Make agent cancellation and submission atomic/reconcilable.** (legacy #15)
  Cancellation can mark a local job ambiguous while the remote runner keeps heartbeating, and rate-limited submission can return failure after a runner has already started unbound. The 2026-10-08 infra-quart pilot reproduced the cancellation side again: the Gateway job became ambiguous while the same remote runner PID continued fresh heartbeats with no terminal artifacts. Preserve a cancellable/fenceable remote handle or prove no runner/proxy acquisition occurred before returning failure; replacement work must remain blocked until the old writer is terminal or fenced.

- [ ] **P2-04: Reconcile agent jobs durably across Gateway restarts/deploys.** (legacy #16)
  Restart/deploy can make returned job ids disappear while artifacts still show stale `running` status. Inspect/status APIs should derive terminal states such as `lost_after_restart`, `orphaned_attempt` or `artifact_incomplete`, including last useful activity and safe recovery guidance.
  **Observed:** preserved agent_events/fleet outcome can prove failed terminal state despite transient API `JOB_NOT_FOUND`; do not conflate.

- [ ] **P2-05: Fail schema validation quickly before slow control-plane work.** (legacy #24)
  Pure local/schema errors such as invalid enum values should return near the API boundary with allowed values and typed diagnostics, not after long router or backend latency.

- [ ] **P2-06: Add retry metadata and coalescing for rate-limit responses.** (legacy #26)
  Rate-limit errors should expose retry-after, bucket identity and lower-cost alternate read guidance. Repeated schema/status reads should be coalesced where possible to avoid making contention worse.

- [ ] **P2-07: Diagnose Docker build read-only path failures precisely.** (legacy #27)
  Docker/Compose build failures should identify whether the read-only path is the compose context, HOME/buildx/cache, builder state or daemon namespace, and offer a sanctioned writable-builder-cache recovery path.

- [ ] **P2-08: Add Gitea Actions artifact listing and bounded download.** (legacy #30; open Issue #792)
  Operators can inspect runs/jobs/logs, but need first-class artifact metadata and safe archive/file retrieval with run/head provenance, size limits, checksum where available, path traversal protection and expiry diagnostics. **2026-11-05:** XLOUD #46 raised #792 for two real private Actions preview ZIPs. Require exact live PR/run/head/attempt binding, bounded streaming to approved ephemeral storage, strict redirect host allowlist without forwarding Gitea PAT, SHA256, TTL revoke and negative tests. Source contract/handoff recorded in #792; not shipped.

- [ ] **P2-09: Support managed read-only agent tasks over non-Git workspaces.** (legacy #34)
  Non-Git registered script/config/data workspaces need immutable snapshot source contracts for read-only analysis tasks, with explicit delivery/mutation disabled unless a separate trusted write contract exists.

- [ ] **P2-10: Expose command-plane degradation state separately from connector/auth failure.** (legacy #43)
  Long-lived agent sessions can temporarily lose callable tools during gateway restart, image boot, host overload or session expiry. Today these distinct states can all look like "connector gone" and provoke unnecessary re-auth or duplicate retries. Expose a cheap bounded plane status such as `ready|starting|overloaded|session_expired` with optional `retry_after_s`, build identity and start time; document expected transient gaps across deploys.

- [ ] **P2-11: Prevent candidate worktree drift after verified delivery.** (legacy #44)
  A verified candidate must remain byte-for-byte consistent with its Git HEAD unless an explicit write operation mutates it. In a JS Chat Engine delivery, remote exact head `ac66a850673cc0383163298e337fe2e3f0b96ec1` retained `_MIGRATION_LOCK_WAIT_SECONDS = 5.0`, while the registered candidate later showed that tracked line deleted without a session write call, producing false local test failures until restored. Add post-delivery worktree==HEAD proof, identify/remove the mutation path, and fail closed with attributed evidence if drift is detected.
  **Acceptance:** exact HEAD/worktree integrity across process boundaries; unexpected tracked edits block delivery/cleanup and get audit provenance.

- [ ] **P2-13: Expose bounded destructive-audit health and recovery evidence.** (legacy #49; open Issue #788)
  **Shipped 2026-11-05:** PR #787, CI #22740 and post-merge #22750 SUCCESS added bounded process-local `health().mcp.audit` evidence (unproven / last_write_succeeded / unavailable, no probe writes, no mutation authorization); live Gateway/MCP verified. **Remaining:** #788 requires controlled isolated-staging audit outage/recovery for guarded PR close and exact-SHA branch delete, proving zero mutation while audit storage fails and one attributable audit-backed success after recovery. Do not simulate an outage against production.

- [ ] **P2-17: Reconcile ambiguous merge attempts when CI evidence read fails but PR becomes merged.** (legacy #4)
  Father UI PR #48 returned `CI_EVIDENCE_INCOMPLETE` while later observed merged; resolve stale/ambiguous responses by exact post-state/head/base/job audit before retries.

## P3 — Optional capabilities and ergonomics (8)

- [ ] **P3-01: Make candidate preparation observable and cancellable.** (legacy #36)
  Slow `prepare_candidate_clone` calls should support async mode, progress phases, cancellation before registration, and idempotent retry while keeping the current synchronous path for small clones.

- [ ] **P3-02: Expose first-class bounded health and HTTP smoke probes.** (legacy #37)
  Add internal and public HTTP/TCP probes with allowlisted method/host/path, bounded response bytes, timeout, provenance, redirect/status/body metadata, and clear DNS/refused/timeout/non-2xx diagnostics.

- [ ] **P3-03: Bridge frontend/Astro delivery to the existing builder/runtime path.** (legacy #38)
  Frontend repos need typed build/smoke/deploy adapters that detect package manager/lockfile/service identity, use the approved Astro/builder substrate or bounded Node container, and return build/up/smoke evidence without raw `npm` or ad-hoc deploy scripts.

- [ ] **P3-04: Model agent roles and authoritative closeout explicitly.** (legacy #40)
  Agent reports may say implementation/review/checks are complete, but should not self-close findings or mark a project ready. Gateway should preserve role, scope, exact source/diff/check evidence and allow multiple isolated implementer/reviewer/adversarial lanes without workspace/proxy collisions.

- [ ] **P3-05: Improve operator-safe command-plane recovery helpers.** (legacy #41)
  Provide typed local branch switch/sync, safe scratch cleanup, project-level Docker/build diagnostics and policy-denial guidance so operators do not need raw shell fallbacks for common recovery actions.

- [ ] **P3-06: Add session-scoped tool-schema caching with explicit invalidation.** (legacy #45)
  Long-lived clients currently rediscover callable tool schemas repeatedly, and a partial catalog during gateway boot can look like a permanent capability loss. Define an etag/build-id based cache contract so schemas may be reused while the plane is stable and are invalidated on build or plane-state change; do not weaken authentication or server-side authorization checks.

- [ ] **P3-08: Add guarded bulk tree / multi-file candidate bootstrap.** (legacy #53)
  Greenfield packages currently require many sequential single-file workspace writes and pre-created directories. Add a bounded `workspace_apply_tree` or equivalent patchset/materialization primitive with file-count/byte limits, project-relative path validation, parent creation under policy, aggregate and per-file hashes, dry-run support and atomic/fail-closed semantics. Acceptance includes a ≥10-file bootstrap plus traversal/oversize/partial-failure tests.

- [ ] **P3-09: Recover and review the local Selenium session reaper before worktree cleanup.** (legacy #54)
  <!-- gateway-todo-key: local-selenium-session-reaper-unreviewed --> **Severity:** P3
  **Acceptance:** determine if retained local unique commit `4fc863ef` is safe and still applicable; qualify isolated regressions and deliver via reviewed PR or preserve/archive it with provenance before guarded cleanup.

## External tracking (not counted twice)

- Coordination browser [#636](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/636): MCP board/inbox already released; operator-only read-only PAT and live browser acceptance are outstanding. Former 500+ Issues blanket read failure was addressed by merged PR #633; do not restore the closed intake.
- Candidate terminal clearance: [#634](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/634), [#640](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/640), PR #643 — covered by P1-01; diagnostic-only PR is not cleanup approval.
- Executor: [#639](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/639), [#642](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/642) — covered by P1-09; failed historical canary must not be rerun blindly.

**Reconciled/omitted as already closed on master:** local stale former #23 (candidate-verifier concurrency), #33 (agent envelope), #35 (project catalog), #46 (`info` command), #50 (guarded parent creation), old coordination 500-row blanket issue. Two pip-only verifier reports were merged into former #14; `source_mode` callable enum into former #6. No user source files or other repositories are modified by this TODO update.

**Closure rule:** Remove an item only with exact-revision acceptance evidence in PR/Issue history and, for deploy/security behavior, verified running-state evidence. Keep historical test logs and finished steps out of the active TODO.

## Gitea Issues — authoritative coordination index (2026-11-05)

Gitea returned **30 OPEN Issues** (not Pull Requests) as of 2026-11-05, with **0 open PRs**; this is a separate, overlapping view of the 29 local engineering TODO outcomes above, **not 59 separate tasks**. Gitea state is the authoritative open/closed signal; coordination workflow state remains separate. Five previously indexed Issues (#753, #724, #717, #697, #654) are Gitea-closed and omitted below; five newer open Issues (#792, #788, #784, #768, #761) have been inserted. An Issue with no P0/P1/P2 title prefix is unclassified, not implicitly P3. Read exact current Issue/last comments before acting; do not automatically close `waiting_review` entries #672/#669.

### P0 operator/safety (1)

- [ ] [#652](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/652) **P0 Site Audit — операторская квалификация исторического LKG перед выпуском**
  Next: P0 operator LKG / protected-stack NO_GO until explicit authorized immutable preflight and rollback proof.

### P1 explicitly marked (4)

- [ ] [#684](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/684) **P1 OpenCode standalone: быстрый надёжный старт, завершение и безопасное восстановление**
  Next: Repeated useful standalone OpenCode runs, provider retry diagnostics and cancellation fencing; one green worker is insufficient.
- [ ] [#679](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/679) **P1-07: аттестация admission policy для shared Docker runner**
  Next: Runner-side admission and fork-negative Docker-socket access attestation, not repo-YAML gate alone.
- [ ] [#656](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/656) **P1: NOD Gateway clone_bundle blocks guarded P1 delivery**
  Next: Diagnostic classification already released in PR #782; original intermittent staging/reproduction and NOD-owned qualification are outstanding. Do not claim fixed from an isolated passing probe.
- [ ] [#761](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/761) **P1 XLOUD: guarded post-accepted fallback unavailable; design exact-LKG image-bound rollback**
  Next: XLOUD source/operator must attest immutable accepted predecessor site+intake image IDs/digests, protected LKG, approval and replay-safe restore before any governed Gateway fallback. No production rollback or unscoped Docker.

### P2 explicitly marked (11)

- [ ] [#788](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/788) **P2-49: staging acceptance for mandatory audit outage/recovery after shipped health evidence**
  Next: Controlled isolated fail-closed + recovery test for PR close and exact-SHA branch delete; no production audit outage.
- [ ] [#784](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/784) **P2 verifier: unbounded per-candidate flock after timed-out pytest masks test results**
  Next: Bounded flock wait already shipped via PR #785; remaining source-of-contention evidence and phase-correlated receipts required before incident closure.
- [ ] [#768](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/768) **P2: existing Kojo #687 managed candidate cannot import verified current Gitea refs**
  Next: Qualify exact provenance/remote-lease import for the SAME registered Kojo candidate; no duplicate clone, unsafe remote rewrite or changing Kojo source.
- [ ] [#742](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/742) **P2: read-only host port/PID diagnostics via SSH Gateway (ss, docker-proxy owner, docker port)**
  Next: Read-only host-aware port/PID, docker-proxy ownership and UNKNOWN states; sshd namespace is not host.
- [ ] [#688](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/688) **P2 [BACKLOG] SSH через HTTP CONNECT + ключи/пароли: полный контракт MCP и маршрутизации**
  Next: DEFERRED by operator; HTTP CONNECT / credentials / host-key routing requires separate go-ahead after priority bugs.
- [ ] [#672](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/672) **P2: bounded UTF-8 input for read-only MarxMind model qualification** [coord: waiting_review]
  Next: Bounded UTF-8 read-only classifier invocation with exact model and secret/scope limits.
- [ ] [#669](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/669) **P2 Gitea coordination: add comment-only action without issue state mutation** [coord: waiting_review]
  Next: coord_reply/idempotency on waiting_review verified live; original quart-core requester acceptance remains.
- [ ] [#650](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/650) **P2: read-only Docker network inventory for bounded Site Audit CI orphan cleanup**
  Next: Read-only bounded Docker network inventory, endpoint completeness and attachment IDs.
- [ ] [#649](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/649) **P2 MAIster cross-project dependencies: typed Gitea coord links blocks/depends_on**
  Next: Typed scoped cross-project coordination dependencies and ACL/cycle tests.
- [ ] [#648](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/648) **P2 MAIster external_check: exact-SHA CI evidence и stale/unknown receipts для Py review**
  Next: Typed immutable exact-SHA CI evidence; stale/unknown if head or attempt drift, do not conflate deploy.
- [ ] [#647](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/647) **P2 Harness² / SSH Gateway: безопасные изолированные candidate probes и точная provenance для Quality**
  Next: Bounded isolated candidate probes with holder separation, provenance, immutable cleanup receipts.

### Priority not explicit in Issue title (14)

- [ ] [#792](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/792) **XLOUD #46: безопасное чтение Gitea Actions private preview artifacts по точным PR/SHA/run**
  Next: Native Gitea private artifact metadata and exact-run/PR-HEAD gated bounded ZIP staging; enforce HTTPS redirect provenance with no PAT forwarding, opaque private handles, SHA256 and TTL/revocation. Owner contract recorded 2026-11-05; no code/production acceptance yet (P2-08).
- [ ] [#744](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/744) **Candidate safe abandonment: XLOUD duplicate Chromium experiment blocked by unreachable Git objects**
  Next: Exact-ID unreachable Git objects and XLOUD owner-specific safe abandonment; no generic git GC/rm.
- [ ] [#733](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/733) **Replace embedded site-media-worker with minimal configurable client for standalone Media API**
  Next: Private standalone Media API client, SHA/CAS/HTTP negative tests and staged smoke; Gateway must not own embedded worker deployment.
- [ ] [#692](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/692) **Architecture debt: убрать supervisor-owned контракты из автономного Gateway без разрыва совместимости**
  Next: Standalone caller-neutral Gateway/OpenCode contracts with verified legacy Supervisor consumers.
- [ ] [#651](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/651) **Gateway cleanup: safely discard a single untracked candidate file**
  Next: Owner/branch/head/SHA-fenced discard of one untracked candidate file; no raw rm.
- [ ] [#646](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/646) **Shared Docker runner: NOD Production check used unscoped prune --volumes**
  Next: Shared runner protection against unscoped docker prune --volumes.
- [ ] [#645](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/645) **Action cancel remains queued with cleanup pending; subsequent PR CI not dispatched**
  Next: Gitea Actions cancel/queued-cleanup terminal reconciliation, no blind retries.
- [ ] [#642](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/642) **OpenCode Alpine executor missing pinned Node22 prevents canonical Astro widget qualification**
  Next: Isolated unprivileged Node22+ pinned build toolchain for Astro widgets.
- [ ] [#641](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/641) **Gateway SSH transport REMOTE_UNAVAILABLE while Gitea adapter remains responsive**
  Next: SSH plane health/reconnect after REMOTE_UNAVAILABLE while Gitea still works.
- [ ] [#640](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/640) **Gateway candidate_cleanup: чистый неизменённый candidate нельзя безопасно abandon**
  Next: Exact immutable base proof for clean unmodified candidate abandonment.
- [ ] [#639](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/639) **Supervisor Stage B: подтвердить восстановление OpenCode после upgrade-blocked (PR #630)**
  Next: Bounded executor/upgrade qualification and safe handoff to external Supervisor, no replay of old job.
- [ ] [#638](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/638) **OpenCode cancellation ambiguous: remote worker termination cannot be proven**
  Next: Prove remote worker termination/fencing before replacement.
- [ ] [#636](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/636) **Py Coordination: браузерная Kanban-доска поверх Gitea Issues**
  Next: Authorized browser Kanban over Gitea Issues/Comments, no second store, CSRF/RBAC tests.
- [ ] [#634](https://git.xloud.ru/gpakoh/agent-ssh-gateway/issues/634) **SSH Gateway: завершённый Analytics PR #1 удерживается WORKSPACE_CONTENDED (legacy source.bundle)**
  Next: Merged Analytics PR #1 legacy source.bundle candidate cleanup, retained task evidence.

### Delivery gates and deduplication

- **Latest deployed:** PR #791 (`gitea_close_issue` active tool registration), exact `master@dbe254dd7541ab137c351739f45df527f6c935b1`, PR Actions #22821 attempt 2 SUCCESS (20 mandatory checks), post-merge Actions #22833 SUCCESS (build-and-push, deploy and host-smoke). Live Gateway/MCP exact SHA and `tools_manifest(mode=mcp_client_write)` / `gateway_catalog` registration independently verified on 2026-11-05. PR #790 underlying durable-audit Issue-close was previously released via #22796/#22815. Historic #724 now Gitea-closed; do not infer anything about unrelated retained candidate bytes.
- **P1-01** overlaps open #634/#640/#656/#744 (historical #697/#724 Gitea-closed); **P1-03** overlaps #645; **P1-07** overlaps #646/#679; **P1-09/P2-03** overlap #638/#639/#642/#684; **P2-02** includes historical closed #654 but has broader remaining acceptance; **P2-10** overlaps #641; **P3-04** overlaps #692.
- **Client/server tools:** live server registers **183** tools in `mcp_client_write`; this ChatGPT session still exposes **179** callable names. Four server-registered names lack client bindings: `candidate_discard_untracked_file`, `docker_network_ls`, `docker_network_inspect`, `gitea_close_issue`. The live MCP catalog is authoritative for server availability, **not** proof of this client's callable schemas. A client catalog relist/refresh is still required before the new Issue close can be invoked from this session; no ad-hoc shell/HTTP fallback (P1-02/P3-06; also #650/#651).
- **Coordination comments:** #669 actual `coord_reply` preserved `waiting_review` and original Gitea state/labels, exact-key replay idempotent, changed text rejected, 64 focused native tests PASS; independent requester acceptance remains outstanding.
- **Safety:** external P0 #652 (LKG) and P1 #679 (runner-side admission) still require operator/host evidence and cannot be declared healthy from an SSH-container read. Gitea #753 is closed as of 2026-11-04; this does not authorize touching the operator-owned dirty `projects.yaml`. Never skip release guards or delete historical candidate data in two unjournaled steps.
- **Cost:** one owned candidate/PR at a time; locally verify first, rerun only a provably failed exact-SHA job where authorized, merge after green CI, verify post-merge production, then SHA-guarded cleanup.
- **Closeout:** record the exact merged code/CI/deploy/acceptance in the Issue and remove a local TODO checkbox only when its own full requirements are satisfied. A closed PR or cleaned directory alone is not completion.
