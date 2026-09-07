# Agent SSH Gateway — TODO

This file intentionally keeps only current open agent/operator wishes and live bugs.
Historical completed audit notes were pruned from TODO; they should live in a
separate changelog/audit archive if needed.

## 🧭 Architect/OpenCode agent-loop findings — 2026-09-03

PR #138 has merged; keep this list as the remaining close-out checklist until
items are explicitly verified as implemented, documented and safe in production.

1. ⬜ **Separate useful agent work from startup dead time.** Source-bundle and
   checkout success are not proof that OpenCode read the plan, wrote a report,
   produced a diff, or ran checks. Count useful work only from semantic artifacts
   and meaningful status transitions. Heartbeat/proxy keepalive must not reset
   semantic staleness. Closure: fresh heartbeat plus stale semantic artifacts
   returns `likely_hung` / `startup_stalled`, not ordinary `running`.

2. ⬜ **Proxy rotation feedback needs a durable sidecar.** Normalize a redacted
   `proxy-status.json`-style artifact with attempt, max_attempts, provider kind,
   last_error_class, timestamps and final outcome. Never store proxy URLs or
   secrets. Closure requires tests asserting concise startup/proxy data is
   surfaced and the no-secret invariant is preserved.

3. ⬜ **Residual tool exposure/catalog mismatch.** #140 added repo-side catalog
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

4. ⬜ **Git namespace mismatch between SSH tools and control-plane git tools.**
   `execute_argv` / `repo_status` can observe one branch/ref/HEAD while trusted
   git tools act from another namespace or fail with `GIT_LOCAL_REF_MISSING`.
   Closure requires host-path-free metadata showing exact resolved project root,
   branch and head for trusted git operations, and fail-closed detection such as
   `WORKSPACE_NAMESPACE_MISMATCH` when namespaces diverge.

5. ⬜ **Command-plane/session recovery gap after transient reconnect/cooldown.**
   After a transient 429 or reconnect cooldown, project-level tools such as
   `git_status` and `current_branch` can continue to work while `execute_argv`
   against the previously known session returns `SESSION_NOT_FOUND` or becomes
   unavailable. During Supervisor delivery on 2026-09-04, `session_health`
   reported a fresh connected session `56858908-245f-434b-bfbb-8fac04bec94b`,
   but a subsequent `execute_argv` using the same id immediately returned
   `SESSION_NOT_FOUND`. Closure requires restored command-plane session recovery
   or safe project-level tools for existing-branch switch and local-branch
   deletion, with regression coverage proving operators do not need probe refs
   to recover.

6. ⬜ **OpenCode worker `UnknownError` needs structured failure reason and
   server-log correlation.** Managed delivery tasks can fail after source-bundle
   verification and clean clone setup but before useful work, with
   `Failure reason: none` and only opaque OpenCode `UnknownError` refs. Closure
   requires mapping such failures to typed phase/verdict values such as
   `opencode_server_error`, `provider_error` or `proxy_error`, preserving the
   upstream ref, and surfacing a redacted server-log correlation hint.

7. ⬜ **Agent job state is not durable across Gateway restart/deploy.** An
   OpenCode corrective task had useful work in `opencode-output.log` and had
   already run targeted tests, but the Gateway restart during CI deploy made the
   returned `job_id` disappear with `JOB_NOT_FOUND` while `agent-status.md`
   still reported stale `Status: running` and no `agent-report.md` or
   `implementation-diff.patch` existed. Closure requires restart-safe task/job
   reconciliation: after transport restart, an operator must get a typed
   `lost_after_restart` / `orphaned_attempt` / `artifact_incomplete` state with
   last useful activity and recovery instructions, not a vanished job plus stale
   running status. During Supervisor GatewayAdapter R5 on 2026-09-04,
   `agent-status.md` still showed startup/proxy rotation while `job_status` for
   the returned job id reported `JOB_NOT_FOUND`, so reconciliation must also
   preserve or derive terminal state for still-visible task artifacts.

8. ⬜ **Handoff/write tools must route around non-writeable production roots.**
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

9. ⬜ **OpenCode review clones must support dirty-worktree review targets.**
   During NOD verification on 2026-09-04, a read-only OpenCode review task
   materialized only repository `HEAD 40d026f8` instead of the current dirty
   worktree, so the review surface was not suitable for checking uncommitted NOD
   changes. A related task also stalled with `trailing_colon_stall`, leaving the
   operator without a usable agent review result. Closure requires the handoff /
   review clone contract to explicitly distinguish committed-HEAD review from
   dirty-worktree snapshot review, include the exact snapshot/source evidence in
   task metadata, and surface typed stall state plus recovery guidance instead
   of presenting the stale clone as a valid review target.

## 🧩 Architect/operator wanted capabilities — 2026-09-03

1. ⬜ **First-class bounded internal service health probe.** Add a read-only
   `http_get_health` / `tcp_connect_check` capability for internal endpoints such
   as `http://agent-memory-service:8070/health`, with allowlisted method,
   explicit host/port/path, timeout, max response bytes, no secrets/env exposure,
   provenance, and clear DNS/refused/timeout/non-2xx/healthy distinctions.

2. ⬜ **Workspace-local git identity bootstrap for supervisor delivery clones.**
   Fresh managed/supervisor workspaces can reach `git commit` and fail with
   `Author identity unknown`. This repeated during manual Supervisor
   GatewayAdapter commit on 2026-09-04: the staged candidate was valid, but
   project-level `git_commit` failed until command-plane git was run with
   per-command `user.name`/`user.email`. Managed Git workspaces should either
   receive a safe local-only committer identity at creation time or expose a
   bounded commit helper that sets per-command identity without touching global
   config.

3. ⬜ **Trusted delivery path for externally prepared/local-agent workspaces.**
   A verified isolated forward-port workspace should be deliverable without
   mutating the canonical checkout. Desired path: `register_delivery_workspace`
   or `push_verified_commit` accepting an allowlisted workspace root, expected
   base/head SHA, clean-tree proof, allowed-files proof and gate evidence, then
   pushing exactly that commit through the trusted credential boundary. During
   Supervisor delivery on 2026-09-04, two clean registered verification projects
   held main-based commits with passing focused/full checks, but `git_push`
   returned `GIT_REMOTE_NOT_ALLOWED` even for the visible local-path `origin`,
   raw SSH pushes to Gitea remotes failed with `Host key verification failed` in one path and with BatchMode/ConnectTimeout as `ssh: connect to host git.example.com port 2222: Operation timed out`,
   and `gitea_push_local_ref` required unavailable task-bound artifacts
   (`implementation-diff.patch`, `delivery-contract.json`/candidate receipt);
   the operator had no safe publication route from the verified workspace.

4. ⬜ **Project-level branch creation must not depend on root-owned `.git` refs.**
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

5. ⬜ **Typed Gitea repo/PR cleanup tools for architect-controlled delivery.**
   Add first-class, ChatGPT-visible tools for safe repository cleanup operations
   that currently require manual UI/API fallback: close a Gitea PR without
   merge, update repository settings such as default branch, and verify
   `merged=false` / exact head/base state after cleanup. These tools must be
   surfaced as invokable schemas wherever `tools_manifest` advertises them,
   fail closed on ambiguous repo/PR identity, require explicit expected state
   inputs, and return structured audit evidence. Closure requires regression
   coverage for PR close-without-merge, default-branch switch, already-closed
   idempotency, and catalog/resource visibility parity.

6. ⬜ **Verification tools must not be pinned to an unreadable project `.venv`.**
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

7. ⬜ **Rate-limit responses need operator-safe retry metadata and coalescing.**
   During Supervisor/browser orchestration on 2026-09-04, repeated schema/tool
   reloads plus normal verification calls hit `RATE_LIMITED: 180 per 1 minute`,
   temporarily blocking `inspect_agent_task` while an agent's terminal report
   was needed for review. The error was retryable but did not expose a bounded
   `retry_after_seconds`, bucket identity, or suggested lower-cost alternate
   read path. Add structured retry metadata and consider coalescing repeated
   schema discovery/status reads so operators can recover without guessing the
   cooldown window or accidentally increasing contention.

8. ⬜ **Command-plane policy and verification ergonomics need typed operator guidance.**
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

1. ⬜ **`gitea_push_verified_commit` / isolated verifier must preserve useful diagnostics and support the prepared-candidate happy path.** This extends, but does not duplicate, the existing `Trusted delivery path for externally prepared/local-agent workspaces` item. #188 fixed a `str` vs `Path` adapter bug and #191 added structured candidate-delivery verification diagnostics, but the prepared candidate clone + trivial-check happy path still needs an explicit close-out proof.

   **Severity:** P1 for safe architect-controlled delivery.

   **Observed behavior before #191:** clean candidate `candidate-agent-ssh-gateway-fix-host-smoke-timeout-master-20260906-087f6ceaeb46-1e9bbd054c61` had local checks passing and clean git status. `gitea_push_verified_commit` returned `CHECK_FAILED` with `isolated delivery verification failed: CandidateVerificationError`. The same failure occurred with real `uv` checks, simple `grep` checks, and `required_checks=true`, so the verifier failed before candidate-controlled checks could matter.

   **Reproduction:** prepare a clean candidate clone under `.mcp-candidate-clones`, commit a small allowed-file change, then call `gitea_push_verified_commit` with exact base/head SHAs, allowed files, and `required_checks` containing only `true`. Expected: verifier passes or fails with a typed, bounded, phase-specific contract error. Pre-#191 observed: generic `CandidateVerificationError`.

   **Expected behavior:** if staging root and mount are valid, `true` must pass. If root/mount/env validation fails, return a stable typed reason such as `CANDIDATE_VERIFIER_ROOT_MISMATCH`, `CANDIDATE_VERIFIER_SOURCE_UNAVAILABLE`, or `CANDIDATE_VERIFIER_VOLUME_SUBPATH_INVALID`, with redacted root/subpath diagnostics and recovery hint.

   **Acceptance:** regression for prepared candidate clone + `required_checks=["true"]`; invalid source-root cases fail before Docker with typed diagnostics; tool either supports prepared candidate clones or explicitly rejects them with a typed contract error.

2. ⬜ **SSH_Gateway discovery and invocation permissions can diverge inside one conversation.** This extends, but does not duplicate, the existing residual catalog mismatch finding. The earlier NOD mode was advertised schemas followed by `Resource not found`; this JS_chat-engine report is advertised schemas followed by `FORBIDDEN: This conversation does not support developer MCPs`, then later discovery no longer exposing `SSH_Gateway` at all.

   **Severity:** P1 for agent/supervisor workflows.

   **Reproduction:** start a project conversation where `SSH_Gateway` is expected; call `api_tool.list_resources(paths=["SSH_Gateway"], query="workspace_file_edit")` and observe Gateway schemas / dynamic namespace exposure. Invoke `SSH_Gateway.workspace_file_edit(...)` or read-only `SSH_Gateway.health()` and observe `FORBIDDEN: This conversation does not support developer MCPs`. Repeat `api_tool.list_resources(paths=["SSH_Gateway"])` and observe `SSH_Gateway` missing while unrelated namespaces remain.

   **Expected behavior:** discovery and invocation authorization must use the same permission decision. If developer MCPs are forbidden, discovery must not advertise `SSH_Gateway`; if advertised, invocation must either reach Gateway or return one stable typed permission error such as `TOOL_NAMESPACE_PERMISSION_REVOKED` with namespace, toolset/permission version, and recovery hint.

   **Acceptance:** tests cover forbidden namespace not advertised; advertised namespace can invoke read-only health/tool-list probe or receives deterministic typed permission error; revoked-after-discovery does not alternate between stale schemas, `FORBIDDEN`, and missing namespace.

3. ⬜ **First-class TODO/backlog writer with dedupe, repro template and safe delivery.**

   **Severity:** P2 operator capability, P1 during multi-project recovery when defects otherwise remain only in chat history.

   **Need:** operators/architects must be able to record bugs, safety misbehavior and missing capabilities in `TODO.md` without ad-hoc Docker-admin or manual Gitea Contents API calls. The writer should deduplicate by normalized title/failure mode and either append a new checkbox or update an existing item with a dated sub-finding.

   **Required fields:** severity, observed behavior, reproduction steps, expected behavior, impact, acceptance criteria, related commits/runs/PRs, and whether the entry is new or an extension of an existing backlog item.

   **Acceptance:** one safe tool call creates/updates a TODO entry, reports `created` vs `updated`, refuses duplicate checkbox creation for the same failure mode, and supports TODO-only delivery without Docker-admin or secret-bearing command construction.
