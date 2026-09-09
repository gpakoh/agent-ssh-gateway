# Agent SSH Gateway — TODO

This file intentionally keeps only current open agent/operator wishes and live bugs.
Historical completed audit notes were pruned from TODO; they should live in a
separate changelog/audit archive if needed.

Stable finding IDs such as `AL-005` and `AO-011` are permanent cross-project
references. Do not renumber or reuse them when an item is closed; ordinal list
numbers are presentation only. All Gateway findings reported by sibling projects
must be deduplicated into this authoritative `TODO.md` on `master` rather than
kept only in a dirty canonical checkout, chat transcript, or stale feature branch.

## 🧭 Architect/OpenCode agent-loop findings — 2026-09-03

PR #138 has merged; keep this list as the remaining close-out checklist until
items are explicitly verified as implemented, documented and safe in production.

1. ⬜ **[AL-005] Residual tool exposure/catalog mismatch.** #140 added repo-side catalog
   consistency reporting, but the end-to-end operator problem remains until the
   ChatGPT-visible resource catalog and MCP `tools/list` cannot diverge silently.
   Live symptoms included missing invokable schemas for advertised/expected tools
   such as branch/PR cleanup helpers. During Supervisor RAG delivery on
   2026-09-04 against build `5f78241d6c50ea3b52fc0ea74544e21700205b90`, the
   visible `gitea_push_local_ref` schema omitted implementation-required
   `task_id`; after supplying the hidden field, delivery still failed on
   undocumented required artifacts such as `delivery-contract.json` /
   `candidate-receipt.json`. During Browser delivery on 2026-09-04,
   `list_resources(query="gitea_push_local_ref")` again exposed a schema without
   `task_id`, but invoking it for verified commit
   `c6df59ee894d3376c7c025c9ca9c53a1292797b8` failed validation with
   `task_id Field required`. During NOD delivery on 2026-09-04,
   `tools_manifest` advertised `gitea_materialize_task_candidate` /
   `prepare_candidate_clone`, but `list_resources(query="materialize" | "candidate")`
   did not surface invokable schemas. During the #195 TODO-writer rollout on
   2026-09-07, post-merge deploy/host-smoke proved live `tools_manifest` on
   master `4f74014632ffbb1e89369932a53768eed6ef06e0` advertised
   `todo_backlog_upsert` as enabled/available, while the ChatGPT-visible
   `api_tool.list_resources(paths=["SSH_Gateway"])` catalog in the same
   conversation still exposed only 127 schemas and omitted the new writer.
   Closure requires advertised tools to be
   either invokable with the exact implementation contract or explicitly marked
   unavailable with a reason at the same surface the operator uses.

   JS Chat Engine audit, 2026-09-08 (P2; open; owner: Gateway; review before
   the next operator-tooling release): the active `mcp_client_write` server
   manifest at build `736d09d4279a3439998323b7fe25435e5141dcee` advertised
   `todo_backlog_upsert` and `read_agent_artifact` as enabled/available, but
   neither exact callable name was present in the ChatGPT tool registry
   (`ALL_TOOLS`). This prevented use of the specialized deduplicating TODO
   writer for this audit. Existing `read_file` plus hash-guarded
   `supervisor_integrate_file` provide a permitted fallback for this existing
   TODO only. Reproduce by comparing those two manifest entries with the actual
   client-visible callable names; do not infer client availability from the
   server-local manifest. Acceptance: expose matching callable schemas or mark
   the client-side absence explicitly. Not a blocker for the JS engine audit
   because a guarded existing-file update is available.

   The same audit also exposed an opaque CAS-input diagnostic. An initial
   `supervisor_integrate_file` call supplied a bare hexadecimal
   `expected_sha256` (client input error) and returned only
   `TOOL_EXECUTION_FAILED: Supervisor integration failed` (request
   `9b85945e-d5af-47a0-9a9d-2a40c74850a9`). Reading the implementation showed
   that the required format is `sha256:<64 hex>`, which the callable field
   description did not state. Independently, full `read_file(TODO.md)` reported
   size 26398 and `truncated=false`, but returned 26397 UTF-8 bytes without
   the final LF: hashing returned content gave `7995ea6f...`, while
   `workspace_verify` returned `sha256:d5911a11...`; adding exactly one LF
   reproduced that raw-file hash. No new audit notes appeared after the failed
   call. Desired contract: document the prefixed hash format, return an
   authoritative raw-file SHA-256 with reads (or byte-exact content), and
   distinguish invalid hash format from current-hash mismatch with safe typed
   diagnostics. The recovery used the verified raw-file hash, preserved the
   final LF and retained all existing TODO content; it did not drop the CAS
   guard.

2. ⬜ **[AL-006] Git namespace mismatch between SSH tools and control-plane git tools.**
   `execute_argv` / `repo_status` can observe one branch/ref/HEAD while trusted
   git tools act from another namespace or fail with `GIT_LOCAL_REF_MISSING`.
   Closure requires host-path-free metadata showing exact resolved project root,
   branch and head for trusted git operations, and fail-closed detection such as
   `WORKSPACE_NAMESPACE_MISMATCH` when namespaces diverge.

3. ⬜ **[AL-010] Handoff/write tools must route around non-writeable production roots.**
   GPT RAG orchestration on 2026-09-04 reported `Permission denied` when trying
   to create parallel `.ai-bridge` handoffs for `quart-core`, and `index.lock`
   permission failures when attempting writes/staging in root project checkouts.
   Read-only Gateway metadata confirms the same class for `quart-core` and
   `rag-router-service`: both are Git worktrees but `filesystem_writeable=false`
   and recommend `writeable_candidate_clone`, while `marx-mind` is writeable.
   Browser delivery on 2026-09-04 reproduced the same class in
   `gpt-browser-bridge`: `info` reported `filesystem_writeable=false`, focused
   verification had to run in a writable candidate, and canonical `git add
   app/loops.py tests/unit/test_loops.py` failed with `.git/index.lock` permission
   denied even though `git diff --check` passed. Closure requires
   handoff/task/write/git tools to fail fast with a typed `WORKSPACE_NOT_WRITEABLE`
   / `CANDIDATE_REQUIRED` diagnostic or automatically create/use a writable
   candidate clone, instead of allowing late `.ai-bridge` or `.git/index.lock`
   permission failures in production roots.

4. ⬜ **[AL-011] OpenCode review clones must support dirty-worktree review targets.**
   During NOD verification on 2026-09-04, a read-only OpenCode review task
   materialized only repository `HEAD 40d026f8` instead of the current dirty
   worktree, so the review surface was not suitable for checking uncommitted NOD
   changes. A related task also stalled with `trailing_colon_stall`, leaving the
   operator without a usable agent review result. Closure requires the handoff /
   review clone contract to explicitly distinguish committed-HEAD review from
   dirty-worktree snapshot review, include the exact snapshot/source evidence in
   task metadata, and surface typed stall state plus recovery guidance instead
   of presenting the stale clone as a valid review target.

5. ⬜ **[AL-007] Command-plane/session recovery gap after transient reconnect/cooldown.**
   Project-level tools can remain usable while a previously known command session
   becomes unavailable. Cross-project evidence also shows clean workspaces can be
   stranded on deleted feature branches with no safe local switch/sync helper.
   Closure requires restored command-plane recovery or bounded project-level tools
   for existing-branch switch, default-branch checkout/sync and local-branch
   cleanup, with exact current/head/target guards and structured before/after
   evidence. During Zalesskiy SUP cleanup on 2026-09-08, remote branch
   `seo-soften-about-claims-20260908` was already deleted while the registered
   workspace remained locally checked out at `f09120737fa1e1e1930d4d6a70cc110cd018d387`;
   `origin/main` had advanced to `7ca51cfaf9ab335ac1bd2eedb19381f805fea4f5`
   and no exposed safe branch-switch helper existed. The desired helper must be
   local-only, fail closed on dirty or stale expected state, and return typed
   diagnostics such as `WORKTREE_DIRTY`, `TARGET_REF_MISSING`,
   `EXPECTED_HEAD_MISMATCH` and `COMMAND_SESSION_UNAVAILABLE`.

   JS Chat Engine audit on 2026-09-08 added transport evidence: native `info`,
   `tree` and `read_file` stayed usable while `git_status` and `recent_commits`
   returned inner `REMOTE_UNAVAILABLE` / retryable=true but outer
   `INVALID_ARGUMENT`; after the Gateway build transition the unchanged reads
   recovered. Acceptance includes bounded read-call completion during rollout,
   consistent retryable transport classification at every envelope level and
   explicit recovery guidance. Do not generalize read retries to blind retries of
   timed-out mutations.

6. ⬜ **[AL-012] `run_agent` outer success envelope must agree with pre-submit terminal failure.**
   During Zalesskiy SUP Tailwind migration on 2026-09-08, a managed task was
   rejected before submission because `task.json` supplied an explicit
   `worktree_path`. The nested result correctly reported `status="error"`, no
   job/attempt id and actionable recovery guidance, while the outer tool envelope
   simultaneously returned `ok=true`, `error=null` and submission success text.
   Closure requires pre-submit terminal validation failures to return a typed
   outer non-success outcome, never emit submission success text, and regression
   coverage binding outer status, nested status, job/attempt presence and actual
   submission/mutation occurrence.

## 🧩 Architect/operator wanted capabilities — 2026-09-03

1. ⬜ **[AO-001] First-class bounded internal service health probe.** Add a read-only
   `http_get_health` / `tcp_connect_check` capability for internal endpoints such
   as `http://agent-memory-service:8070/health`, with allowlisted method,
   explicit host/port/path, timeout, max response bytes, no secrets/env exposure,
   provenance, and clear DNS/refused/timeout/non-2xx/healthy distinctions.

2. ⬜ **[AO-004] Project-level branch creation must not depend on root-owned `.git` refs.**
   A clean registered child repository can be readable and PR-verifiable while
   `git_create_branch` fails on `.git/refs/heads/<branch>.lock` permission
   errors. During Supervisor delivery on 2026-09-04, attempting to import a
   verified local candidate branch into the canonical checkout via `git fetch
   <temp-clone> branch:refs/heads/...` failed with `insufficient permission for
   adding an object to repository database .git/objects`; no target branch ref
   was created and the dirty working tree remained unchanged. Managed checkouts
   must have coherent ownership for refs/index/objects,
   or branch creation must fail with a typed ownership diagnostic and recovery
   path such as `GIT_OWNERSHIP_BLOCKED`.

   NOD re-verification, 2026-09-08: registered child `gateway_client` at detached
   HEAD `a83e31b40d4fc464d71a369dda478ad636f7a009` rejected
   `git_create_branch` because `.git/refs/heads/...lock` was not writable, while
   the same workspace immediately accepted `git_add` and `git_commit`, producing
   detached commit `607376528dc23cddfdac751646dd83a9506970e4`. This proves a
   mixed-permission state where index/objects/HEAD writes succeed but refs/heads
   does not. Parent `nod-gateway` showed the opposite severity: `info` described
   the workspace as writable, but guarded `git_add` failed with insufficient
   permission to add an object to `.git/objects`, and no partial staging occurred.
   Acceptance therefore requires per-component Git write-capability preflight for
   index, objects, refs and HEAD; a single filesystem writeability boolean is not
   sufficient evidence.

3. ⬜ **[AO-005] Typed Gitea repo/PR cleanup tools for architect-controlled delivery.**
   Add first-class, ChatGPT-visible tools for safe repository cleanup operations
   that currently require manual UI/API fallback: close a Gitea PR without
   merge, update repository settings such as default branch, and verify
   `merged=false` / exact head/base state after cleanup. These tools must be
   surfaced as invokable schemas wherever `tools_manifest` advertises them,
   fail closed on ambiguous repo/PR identity, require explicit expected state
   inputs, and return structured audit evidence. Closure requires regression
   coverage for PR close-without-merge, default-branch switch, already-closed
   idempotency, and catalog/resource visibility parity.

4. ⬜ **[AO-006] Verification tools must not be pinned to an unreadable project `.venv`.**
   During Supervisor RAG verification on 2026-09-04, project-level
   `run_pytest`, `run_ruff` and `run_mypy` all failed before collection because
   the registered checkout had `.venv/bin/python3` with permission denied, while
   the same diff passed in a clean verification clone. During Browser recovery
   on 2026-09-04, a registered writable candidate workspace was clean at
   `b7b78df1888f207c5a79af7e5c8cd6a1d163e6ef`, but project-level `run_pytest`
   failed before collection because the runner used `uv --frozen` and the
   workspace had no `uv.lock`. Verification tools should detect unreadable,
   broken, or layout-incomplete environments, create or select a safe isolated
   environment, or fail with a typed `VERIFICATION_ENV_UNREADABLE` /
   `VERIFICATION_LOCKFILE_MISSING` diagnostic and a recovery path instead of
   treating environment bootstrap as code failure.

   NOD re-verification, 2026-09-08: after the child candidate was committed and
   clean at exact HEAD `607376528dc23cddfdac751646dd83a9506970e4`, supervisor
   `run_pytest` still failed before collection. `uv` found the existing
   `.venv/bin/python3` pointed to a non-existent interpreter, selected CPython
   3.12.14, then failed removing `./.venv/.lock` with permission denied. The same
   focused behavior had passed through an alternate execution lane; a managed
   OpenCode candidate also reported worker pytest `75 passed` and Ruff green while
   Gateway required-check bootstrap failed. Acceptance must include this
   broken-interpreter + unwritable-lock case and automatically select a safe
   isolated verifier instead of reporting project-test failure.

5. ⬜ **[AO-010] Command-plane policy and verification ergonomics need typed operator guidance.**
    Several 2026-09-04 delivery sessions exposed rough edges that overlap with
    existing namespace/session/environment findings but are not yet captured as a
    single operator contract: raw SSH `git push` / `git clone` can time out while
    the specialized `git_push` path succeeds; common diagnostic wrappers such as
    `sh -lc` and `python3 -c` are blocked by policy; destructive cleanup such as
    `rm` is correctly denied but leaves operators without a safe cleanup helper
    for failed temporary clones; and registered temporary workspaces can fail
    `run_pytest` / `run_compileall` before useful verification because the runner
    is pinned to `uv --frozen` / missing or incompatible `uv.lock` state. During
    Supervisor launch on 2026-09-04, `docker_compose_build` and build-enabled
    `docker_compose_up` failed before image build with `mkdir [PATH] read-only
    file system` for both a verified `.supervisor-workspaces` checkout and a
    separate `/media/1TB/Python/...` deploy context, while no-build compose could
    create networks/containers from an existing image. Closure requires typed
    Docker build-context diagnostics that identify whether the read-only path is
    the compose project dir, Docker builder state, HOME/cache, or daemon-side
    mount namespace, plus a safe recovery path such as read-only build context
    with writable builder cache. Also requires typed policy denials with
    recommended safe alternate tools, first-class bounded cleanup for
    Gateway-created scratch workspaces, and verification helpers that distinguish
    environment/bootstrap failure from project test failure.

6. ⬜ **[AO-011] Trusted candidate preparation/publication must handle Git safe-directory ownership preflight.**
   `prepare_candidate_clone` is the intended escape hatch from read-only or
   cross-owned canonical workspaces, so it must itself read an approved registered
   source without requiring mutable global Git configuration. Every trusted
   verifier/materializer that clones an approved registered source must use the
   same scoped/non-global `safe.directory` policy or fail before partial
   registration/mutation with typed `GIT_SAFE_DIRECTORY_REQUIRED` /
   `SOURCE_REPO_OWNERSHIP_BLOCKED` guidance. Wildcard `safe.directory=*` is
   forbidden.

   NOD re-verification, 2026-09-08, on build
   `00728408fc5ae1e8e89a076210846baaaf067a13`: `prepare_candidate_clone` for
   `nod-gateway` at exact base `40d026f888ff2e373fc83410e4a358bafa95589e`
   failed before candidate creation with `fatal: detected dubious ownership`.
   After `gateway_client` was committed cleanly at exact HEAD
   `607376528dc23cddfdac751646dd83a9506970e4`, `gitea_push_verified_commit`
   passed exact-base/head and allowed-files checks, entered source resolution,
   then failed cloning the approved registered source with the same ownership
   class. It returned `CANDIDATE_SOURCE_UNAVAILABLE`, `retryable=true`,
   `mutation_occurred=false`. Acceptance must cover both paths and all shared
   source-cloning verifier/materializer call sites.

7. ⬜ **[AO-012] Project catalog reads need filtering and pagination.**
   `project_list()` currently returns the entire registry, including historical
   candidate clones, when an architect often needs one project id. Add exact,
   prefix or text query plus project type/tag/parent filters, stable ordering,
   `limit`/`offset`, total count and a compact projection. Exact-id lookup should
   not require returning unrelated projects. Preserve a bounded backwards-
   compatible unfiltered mode.

8. ⬜ **[AO-013] Long-running candidate preparation needs async progress and cancellation.**
   Fresh `prepare_candidate_clone` calls have taken roughly 63-116 seconds in
   cross-project work while exposing no intermediate phase or cancellation.
   Desired contract: optional async submission with durable job id; cheap status
   phases such as fetch/checkout/register/verify; cancellation before final
   registration; safe retry; and an optional bounded synchronous wait. Preserve
   exact-base, clean-workspace and idempotent recovery guarantees.

9. ⬜ **[AO-014] Secret-bearing dotenv files need a targeted atomic key-update primitive.**
   A production `.env` key sometimes must be changed without reading, previewing,
   diffing, returning or resubmitting unrelated secrets. Add a bounded
   `dotenv_key_update` / `secret_file_key_update` capability for allowlisted
   project-relative secret files. Inputs: exact project, relative file, key,
   sensitive replacement value and an expected-current guard (prefer file/value
   hash or opaque version). Update exactly one existing key atomically; preserve
   unrelated bytes, mode and ownership; fail closed on stale state or ambiguous
   duplicate/missing keys. No response, log, receipt, preview, audit metadata or
   exception may contain old/new secret values, full lines, neighboring lines or
   other file contents. Return only non-secret evidence such as key name,
   before/after file hash, changed flag and atomic-write receipt id. Regression
   coverage must include spaces, `#`, `=`, quotes and UTF-8 values, stale guards,
   duplicate/missing keys, crash/interruption safety and proof that no output
   surface leaks secrets.

10. ⬜ **[AO-015] `gitea_push_verified_commit` needs auditable per-check execution evidence.**
    Astra Builder C6 delivery on 2026-09-08 showed the helper can return
    `checks_verified=true` and push an exact head even though independent CI on
    the same SHA immediately disproves one of the declared required invariants.
    Each required check must therefore return structured evidence bound to the
    exact workspace/head: normalized command/argv identity, resolved cwd/project,
    duration, exit code, bounded stdout/stderr evidence and whether the worktree
    changed. `checks_verified=true` must be derivable from these per-check results.
    If the selected verification lane cannot execute a check faithfully, fail
    closed with a typed verification-capability error and do not push.

11. ⬜ **[AO-016] Gitea Actions need first-class bounded generated-artifact retrieval.**
    Job logs are diagnostic evidence, not a transport for generated files. During
    the same Astra Builder recovery, CI generated the canonical ~52 KiB widget
    runtime but Gateway could only expose log output; the operator had to rebuild
    the artifact in another lane and compare hashes. Add read-only
    `gitea_list_action_artifacts` / `gitea_get_action_artifact_file` or equivalent
    bounded job-workspace snapshot APIs bound to repo, run, job and exact head
    SHA. Return path, size, digest, provenance and truncation state; small text may
    be returned bounded, larger artifacts should use a file reference. Fail closed
    on run/job/head mismatch, path escape or missing artifact.

12. ⬜ **[AO-017] Registered local projects need a typed Gitea repository bootstrap/publish primitive.**
    During `site-audit-platform` bootstrap on 2026-09-09, Gateway had a registered
    project and valid local Git baseline but no typed flow to create the matching
    remote Gitea repository, bind it as the allowed origin, set/verify the default
    branch and publish the exact guarded baseline. Add a bounded idempotent
    `gitea_create_repository` / `bootstrap_gitea_repository` flow for an exact
    registered project and allowlisted owner/namespace. Inputs must bind expected
    local branch/head and desired repository identity/settings; incompatible
    existing remotes must fail with zero writes. Return structured evidence for
    local project/head, created-or-existing remote identity, default branch and
    mutation occurrence, plus the exact guarded publish step when publication is
    separate. Acceptance includes create-missing, compatible idempotency,
    incompatible refusal, expected-head mismatch before mutation and a full
    registered-project-to-remote bootstrap without manual UI/API fallback.

## 🧩 Frontend/Astro delivery findings — 2026-09-05

1. ⬜ **Route frontend verification through the existing Astro delivery
   capability, not ad-hoc SSH `npm`.** During Zalesskiy SUP homepage SEO delivery
   on 2026-09-05, the repository clearly declared an Astro frontend
   (`package.json`, `npm run build`, Dockerfile based on `node:20-alpine`), but
   the command-plane verification environment had no `npm` binary. A later host
   container audit showed the platform already has an Astro/frontend execution
   lane: `astro-builder-runtime-1`, `astro-builder-control-plane-1`,
   `astro-builder-studio-1`, `astro-builder-cache-1`, and live
   `astro-sites-astro` running `npm run build && npm run preview -- --host` on
   port `4321` with Astro v5.18.2 and generated sitemap output. Therefore the
   missing capability is not simply "install Node in SSH"; it is Gateway routing
   from a registered frontend project to the existing Astro Builder / astro-sites
   delivery path. Closure requires a typed `frontend_build` / `astro_build`
   helper that detects project metadata and builder affiliation, selects an
   approved existing builder/runtime or bounded Node container, binds exact
   project id, working directory, lockfile/package manager and allowlisted script,
   and returns structured build evidence: toolchain versions, command identity,
   artifact path, stdout/stderr tail, exit code and typed
   `FRONTEND_BUILD_CAPABILITY_UNAVAILABLE` / `FRONTEND_BUILD_FAILED` outcomes.

2. ⬜ **Bounded live HTTP smoke via a managed fetch capability, preferably backed
   by `relay-curl-worker`.** The Zalesskiy SUP delivery needed to verify
   `robots.txt`, `sitemap-index.xml` and public landing-page headers live, but
   raw `curl` through `execute_argv` was denied by command policy. This is a
   sibling of the internal service health probe, but it must support public-site
   verification and should share bounded fetch primitives rather than duplicate
   network policy. A later host audit found a long-running `relay-curl-worker`
   container (`alpine`, `sleep 86400`, `restart: unless-stopped`), which looks
   like an existing
   substrate for safe smoke checks but is not exposed as a typed Gateway tool.
   Do not weaken the raw shell/curl denylist. Implement a bounded `http_smoke` /
   `public_http_smoke` capability with allowlisted `GET`/`HEAD`, explicit
   host/path, redirect-chain capture, status, content-type, bounded body sample,
   timeout, optional XML validation for sitemap files, no cookies/secrets/env
   exposure, and typed DNS/refused/timeout/non-2xx/invalid-XML outcomes. Closure
   requires provenance showing the execution substrate used (`relay-curl-worker`
   or another approved fetch runner), plus regression coverage that operators can
   verify public `robots.txt`, sitemap and landing-page headers without shelling
   out.

3. ⬜ **Deploy-contract bridge for Astro/Compose frontend projects.** The
   Zalesskiy SUP repository has a `deploy.sh` that runs `docker compose up -d
   --build astro`, but `execute_argv` cannot run it because the SSH session has
   no `docker` binary, while Docker mutations are intentionally available only
   through Gateway Docker tools. Treat this as an orchestration gap between repo
   deploy intent and Gateway capabilities, not as a reason to run arbitrary
   scripts. Operators need a typed deployment adapter that reads an allowlisted
   deploy contract, resolves the registered frontend project to the existing
   Astro Builder / astro-sites / Compose service path, confirms exact compose
   project/service/image/build sequence, executes through Gateway Docker tools,
   and reports build/up/restart/smoke evidence. Closure requires avoiding both
   bad choices: raw deploy scripts that cannot run in SSH and ad-hoc Docker tool
   calls that bypass the repo's documented deploy path. This item deliberately
   does not duplicate the existing Docker `read-only file system` build-context
   finding; it is about the missing bridge between frontend delivery contracts
   and Gateway Docker actions.

4. ⬜ **CI-gated merge tools need an explicit no-workflow/no-run state.** During
   the same PR flow, `gitea_list_workflows` and `gitea_list_action_runs` returned
   no workflows/runs for `gpakoh/zalesskiy-sup`, while the protected merge helper
   refused with `CI_NOT_GREEN`. That is fail-closed, but the diagnostic should be
   more precise and actionable: `CI_NOT_CONFIGURED` / `NO_REQUIRED_RUN_FOUND`,
   including whether manual verification artifacts can satisfy a configured
   override policy. Closure requires tests for repos with zero workflows, repos
   with workflows but no run for the exact head SHA, and repos with stale runs
   from another head.

## 🆕 Runtime/CI findings — 2026-08-19

1. ⬜ **Correlated ~18-minute CI failures across Docker runners/phases.** Prior
   runs showed different Docker runners failing around the same wall-clock
   boundary while building/deploying images. A port-collision fix was merged, but
   the broader shared timeout/resource/registry/network/storage/supervisor
   question remains open. Closure requires distinguishing runner-specific
   degradation from a shared infrastructure limit and demonstrating a controlled
   build+deploy path that does not hit the hidden deadline.

2. ⬜ **Superseded Gitea Action task containers can keep consuming runner capacity.**
   During Astra C.2.5-C5/C6 recovery on 2026-09-04, newer PR heads existed and
   newer runs were authoritative, but older superseded `nod-ci-node22` action
   task containers still appeared as running with no useful CPU and their Gitea
   runs still reported `in_progress`. Operators need a safe read-only
   reconciliation surface showing whether an Action container belongs to the
   current PR head, a superseded head, or an orphaned run, plus a guarded cleanup
   path that fails closed unless run id, job id, container id, and head SHA all
   match the stale/superseded state.

3. ⬜ **Avoid duplicate heavy CI after already-green PR heads.** Direct default-
   branch pushes are now allowed, but the workflow still repeats the full heavy
   Python matrix on `master` after a PR has already passed the same code gate.
   Add a safe CI/CD fast path: PRs keep the full matrix; the post-merge
   `master` push should run only quick sanity checks plus build, deploy, and
   host-smoke when the merge commit is a clean merge of an exact green PR head
   with no additional code changes. Closure requires explicit evidence binding
   the green PR run to the merged head, a fallback to full CI for direct pushes
   or ambiguous histories, and tests/docs proving fail-closed behavior rather
   than silently weakening the deployment gate.

## Runtime/tooling intake — 2026-09-07

These entries are deduplicated against the existing Gateway TODO backlog. They record concrete new failure modes from the #188/#189/#191 recovery and a cross-project JS_chat-engine report, so the evidence does not live only in chat.

1. ⬜ **SSH_Gateway discovery and invocation permissions can diverge inside one conversation.** This extends, but does not duplicate, the existing residual catalog mismatch finding. The earlier NOD mode was advertised schemas followed by `Resource not found`; this JS_chat-engine report is advertised schemas followed by `FORBIDDEN: This conversation does not support developer MCPs`, then later discovery no longer exposing `SSH_Gateway` at all.

   **Severity:** P1 for agent/supervisor workflows.

   **Reproduction:** start a project conversation where `SSH_Gateway` is expected; call `api_tool.list_resources(paths=["SSH_Gateway"], query="workspace_file_edit")` and observe Gateway schemas / dynamic namespace exposure. Invoke `SSH_Gateway.workspace_file_edit(...)` or read-only `SSH_Gateway.health()` and observe `FORBIDDEN: This conversation does not support developer MCPs`. Repeat `api_tool.list_resources(paths=["SSH_Gateway"])` and observe `SSH_Gateway` missing while unrelated namespaces remain.

   **Expected behavior:** discovery and invocation authorization must use the same permission decision. If developer MCPs are forbidden, discovery must not advertise `SSH_Gateway`; if advertised, invocation must either reach Gateway or return one stable typed permission error such as `TOOL_NAMESPACE_PERMISSION_REVOKED` with namespace, toolset/permission version, and recovery hint.

   **Acceptance:** tests cover forbidden namespace not advertised; advertised namespace can invoke read-only health/tool-list probe or receives deterministic typed permission error; revoked-after-discovery does not alternate between stale schemas, `FORBIDDEN`, and missing namespace.


