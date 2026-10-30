# Agent SSH Gateway — TODO

**Reconciled:** 2026-10-30; authoritative Gitea `master@f10b014ed85819ffe974f55be76463d5db71a02f` after merged PR #728. Exact PR CI #21331 and post-merge CI #21342 both SUCCESS. Earlier reconciliation baseline: 2026-10-25, `master@9f7baa48760fba85f52c77fe4e055a5f7ac42a34`, exact-release CI #19743.

**Open: 31 (P1 8 / P2 14 / P3 9).** One checkbox = one independently closeable outcome; completed items are removed from the active checklist. A merged partial fix is not sufficient for closeout. OpenCode security remains a live prerequisite, not a green-CI assumption. Completed deploy LKG migration evidence is recorded in `docs/operations/SSH_GATEWAY_DEPLOY_STATE_MIGRATION.md`.

**2026-10-30 closeout evidence (not active TODO):** P2-15 — PR #728, commit `f10b014e`, CI #21331 and post-merge #21342 SUCCESS: both `docker_ps` and `docker_images` expose configured-daemon inventory scope, endpoint identity hash and truncation/unknown host-wide completeness; this does **not** establish exhaustive host/multi-daemon discovery or real pagination. P2-16 — verified guarded `git_fetch_ref` + `git_update_branch_by_merge` on the existing #730 and #728 candidates against exact `origin/master` SHA, followed by published current-base PR heads and successful CI/merge. Remaining independent edge cases should be new scoped findings rather than re-opening accepted outcomes.

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

## P2 — Reliability and engineering throughput (14)

- [ ] **P2-01: Wire the production `astro-sites` named root into `mcp-oauth` after the host path is provisioned.** (legacy #13)
  Named-root registration is now supported, but production still has no `MCP_OAUTH_ASTRO_SITES_ROOT` setting and the intended host directory is not provisioned. Once the operator supplies an existing absolute host path, add the `registry_roots.astro-sites` entry, the explicit writable `mcp-oauth` bind with host-path auto-creation disabled, the documented env contract, and deploy seam coverage. Deploy must fail closed when the configured path is absent; do not silently create an empty root.

- [ ] **P2-02: Make verification helpers honor project environments and CI-equivalent toolchains.** (legacy #14)
  `run_pytest`, `run_ruff`, `run_mypy` and verifier containers should not fail falsely on unreadable `.venv`, missing `uv.lock`, unwritable caches, monorepo collection collisions, or verifier/CI tool-version drift. A 2026-10-08 Kojo verified-push attempt additionally proved the trusted verifier image could use `/usr/bin/python` without `pytest`, while the repository verifier could execute the targeted tests. Expose candidate-verifier versus Actions-plane capability provenance explicitly; advertised checks must either run in the selected plane or return a typed environment/bootstrap diagnostic instead of code failure.
  **New confirmed cases:** two `deep-researcher` pip-only candidates fail `run_pytest`/`run_ruff` on mandatory `uv --frozen` with missing `uv.lock`; treat as *one* contract alongside existing env/cache/interpreter parity, with pip/requirements support and CI-equivalent checks.

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

- [ ] **P2-08: Add Gitea Actions artifact listing and bounded download.** (legacy #30)
  Operators can inspect runs/jobs/logs, but need first-class artifact metadata and safe archive/file retrieval with run/head provenance, size limits, checksum where available, path traversal protection and expiry diagnostics.

- [ ] **P2-09: Support managed read-only agent tasks over non-Git workspaces.** (legacy #34)
  Non-Git registered script/config/data workspaces need immutable snapshot source contracts for read-only analysis tasks, with explicit delivery/mutation disabled unless a separate trusted write contract exists.

- [ ] **P2-10: Expose command-plane degradation state separately from connector/auth failure.** (legacy #43)
  Long-lived agent sessions can temporarily lose callable tools during gateway restart, image boot, host overload or session expiry. Today these distinct states can all look like "connector gone" and provoke unnecessary re-auth or duplicate retries. Expose a cheap bounded plane status such as `ready|starting|overloaded|session_expired` with optional `retry_after_s`, build identity and start time; document expected transient gaps across deploys.

- [ ] **P2-11: Prevent candidate worktree drift after verified delivery.** (legacy #44)
  A verified candidate must remain byte-for-byte consistent with its Git HEAD unless an explicit write operation mutates it. In a JS Chat Engine delivery, remote exact head `ac66a850673cc0383163298e337fe2e3f0b96ec1` retained `_MIGRATION_LOCK_WAIT_SECONDS = 5.0`, while the registered candidate later showed that tracked line deleted without a session write call, producing false local test failures until restored. Add post-delivery worktree==HEAD proof, identify/remove the mutation path, and fail closed with attributed evidence if drift is detected.
  **Acceptance:** exact HEAD/worktree integrity across process boundaries; unexpected tracked edits block delivery/cleanup and get audit provenance.

- [ ] **P2-12: Fail project-scoped Compose mutations before confirmation when allowed roots are not configured.** (legacy #48)
  A registered `docker_compose_up` can return `confirmation_required`, but the subsequent `confirm_operation` then fails before Docker execution when `MCP_ALLOWED_PROJECT_ROOTS` is unset. Availability/preflight must reject this configuration before creating a one-time pending action, or safely resolve the registered project root without requiring callers to know host paths. Acceptance covers unset-root fail-fast behavior, successful registered-root execution, and proves confirmations are not consumed by impossible actions.

- [ ] **P2-13: Expose bounded destructive-audit health and recovery evidence.** (legacy #49)
  Guarded Gitea close/delete correctly fail closed with `AUDIT_UNAVAILABLE` before mutation, but callers cannot distinguish transient audit transport or storage outage from malformed cleanup input. Add a safe read-only audit subsystem status/recovery signal with retryability and correlation metadata; keep destructive mutations impossible unless intent persistence succeeds. Acceptance covers zero mutation during outage and exactly-once audited success after recovery for both PR close and exact-SHA branch deletion.

- [ ] **P2-17: Reconcile ambiguous merge attempts when CI evidence read fails but PR becomes merged.** (legacy #4)
  Father UI PR #48 returned `CI_EVIDENCE_INCOMPLETE` while later observed merged; resolve stale/ambiguous responses by exact post-state/head/base/job audit before retries.

## P3 — Optional capabilities and ergonomics (9)

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

- [ ] **P3-07: Expose guarded Gitea issue-state mutation.** (legacy #52)
  The external surface can list/read Gitea issues but cannot reconcile an issue to closed after its fix is independently proven. Add a same-repository issue-state mutation with fresh expected-state/version fencing, narrow field allowlisting, audit evidence and post-write verification. Acceptance covers close success, already-closed idempotence, concurrent-state mismatch, missing issue and remote failure with zero unintended mutation.

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