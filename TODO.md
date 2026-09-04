# Agent SSH Gateway — TODO

This file intentionally keeps only current open agent/operator wishes and live bugs.
Historical completed audit notes were pruned from TODO; they should live in a
separate changelog/audit archive if needed.

## 🧭 Architect/OpenCode agent-loop findings — 2026-09-03

PR #138 has merged; keep this list as the remaining close-out checklist until
items are explicitly verified as implemented, documented and safe in production.

1. ⬜ **OpenCode startup/proxy rotation close-out.** `inspect_agent_task` must
   expose startup/proxy dead time as first-class structured status instead of
   generic `running`: `phase=startup`, elapsed time, proxy attempt/max, last
   startup message, `useful_agent_activity_seen=false`, and verdicts such as
   `startup_stalled` / `startup_timeout`. Closure requires regression coverage
   proving repeated proxy rotation is diagnosable without raw-log reading.

2. ⬜ **Stable bounded log access for agent tasks.** Operator diagnostics must
   not depend on arbitrary shell/log reads that can fail safety checks. Provide a
   path-safe, redacted, bounded log/artifact surface for fixed task files such as
   `opencode-output.log`, `agent-status.md`, `agent-report.md`,
   `implementation-diff.patch`, `agent-heartbeat.json`, and proxy sidecars.
   Closure requires a regression that returns sanitized tail or structured
   `log_unavailable`, not a tool-level false-positive.

3. ⬜ **Separate useful agent work from startup dead time.** Source-bundle and
   checkout success are not proof that OpenCode read the plan, wrote a report,
   produced a diff, or ran checks. Count useful work only from semantic artifacts
   and meaningful status transitions. Heartbeat/proxy keepalive must not reset
   semantic staleness. Closure: fresh heartbeat plus stale semantic artifacts
   returns `likely_hung` / `startup_stalled`, not ordinary `running`.

4. ⬜ **Proxy rotation feedback needs a durable sidecar.** Normalize a redacted
   `proxy-status.json`-style artifact with attempt, max_attempts, provider kind,
   last_error_class, timestamps and final outcome. Never store proxy URLs or
   secrets. Closure requires tests asserting concise startup/proxy data is
   surfaced and the no-secret invariant is preserved.

5. ⬜ **Residual tool exposure/catalog mismatch.** #140 added repo-side catalog
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
   did not surface invokable schemas. Closure requires advertised tools to be
   either invokable with the exact implementation contract or explicitly marked
   unavailable with a reason at the same surface the operator uses.

6. ⬜ **Git namespace mismatch between SSH tools and control-plane git tools.**
   `execute_argv` / `repo_status` can observe one branch/ref/HEAD while trusted
   git tools act from another namespace or fail with `GIT_LOCAL_REF_MISSING`.
   Closure requires host-path-free metadata showing exact resolved project root,
   branch and head for trusted git operations, and fail-closed detection such as
   `WORKSPACE_NAMESPACE_MISMATCH` when namespaces diverge.

7. ⬜ **Command-plane/session recovery gap after transient reconnect/cooldown.**
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

8. ⬜ **OpenCode worker `UnknownError` needs structured failure reason and
   server-log correlation.** Managed delivery tasks can fail after source-bundle
   verification and clean clone setup but before useful work, with
   `Failure reason: none` and only opaque OpenCode `UnknownError` refs. Closure
   requires mapping such failures to typed phase/verdict values such as
   `opencode_server_error`, `provider_error` or `proxy_error`, preserving the
   upstream ref, and surfacing a redacted server-log correlation hint.

9. ⬜ **Agent job state is not durable across Gateway restart/deploy.** An
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

10. ⬜ **Handoff/write tools must route around non-writeable production roots.**
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
   the same diff passed in a clean verification clone. Verification tools should
   detect unreadable/broken project virtualenvs, create or select a safe isolated
   environment, or fail with a typed `VERIFICATION_ENV_UNREADABLE` diagnostic and
   a recovery path instead of treating environment bootstrap as code failure.

7. ⬜ **Bounded Gitea Actions job/step log retrieval.** During Astra C.2.5-C5/C6
   recovery on 2026-09-04, `gitea_list_action_run_jobs` exposed the failing
   step name (`Release integrity checks`) but no ChatGPT-visible tool exposed a
   bounded, redacted log tail for that job/step. The operator had to infer the
   cause by reproducing scripts locally. Add a `gitea_get_action_job_log` or
   `gitea_get_action_step_log` helper keyed by owner/repo/run/job/step with
   max-bytes/tail limits, redaction, typed `LOG_UNAVAILABLE`, and regression
   coverage that CI failures can be diagnosed without container/server log access.

8. ⬜ **Minimize `gitea_get_action_run` response payload.** During Astra PR #71
   polling on 2026-09-04, `gitea_get_action_run` returned raw nested
   actor/trigger_actor/repository/user payloads including fields unrelated to
   CI gating, while `gitea_list_action_runs` already exposes a compact shape.
   Normalize the single-run endpoint to the same minimized contract or a strict
   allowlist, redact unnecessary identity/contact fields, and add regression
   coverage that single-run reads cannot reintroduce raw Gitea API payloads.

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
