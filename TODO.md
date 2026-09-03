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
   such as branch/PR cleanup helpers. Closure requires advertised tools to be
   either invokable or explicitly marked unavailable with a reason at the same
   surface the operator uses.

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
   unavailable. Closure requires restored command-plane session recovery or safe
   project-level tools for existing-branch switch and local-branch deletion, with
   regression coverage proving operators do not need probe refs to recover.

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
   running status.

## 🧩 Architect/operator wanted capabilities — 2026-09-03

1. ⬜ **First-class bounded internal service health probe.** Add a read-only
   `http_get_health` / `tcp_connect_check` capability for internal endpoints such
   as `http://agent-memory-service:8070/health`, with allowlisted method,
   explicit host/port/path, timeout, max response bytes, no secrets/env exposure,
   provenance, and clear DNS/refused/timeout/non-2xx/healthy distinctions.

2. ⬜ **Workspace-local git identity bootstrap for supervisor delivery clones.**
   Fresh managed/supervisor workspaces can reach `git commit` and fail with
   `Author identity unknown`. Managed Git workspaces should either receive a safe
   local-only committer identity at creation time or expose a bounded commit
   helper that sets per-command identity without touching global config.

3. ⬜ **Trusted delivery path for externally prepared/local-agent workspaces.**
   A verified isolated forward-port workspace should be deliverable without
   mutating the canonical checkout. Desired path: `register_delivery_workspace`
   or `push_verified_commit` accepting an allowlisted workspace root, expected
   base/head SHA, clean-tree proof, allowed-files proof and gate evidence, then
   pushing exactly that commit through the trusted credential boundary.

4. ⬜ **Project-level branch creation must not depend on root-owned `.git` refs.**
   A clean registered child repository can be readable and PR-verifiable while
   `git_create_branch` fails on `.git/refs/heads/<branch>.lock` permission
   errors. Managed checkouts must have coherent ownership for refs/index/objects,
   or branch creation must fail with a typed ownership diagnostic and recovery
   path such as `GIT_OWNERSHIP_BLOCKED`.

5. ⬜ **Docker exec denylist must avoid substring false positives.**
   `docker_exec` blocked a cleanup attempt because the ordinary branch name
   `chore/canonical-gitea-env-20260903` contains the substring `env`. Closure
   requires denylist checks to classify dangerous argument forms precisely
   enough to block environment/secret exfiltration attempts without rejecting
   safe identifiers, branch names, paths or command arguments that merely contain
   words such as `env` as a substring.

## 🆕 Runtime/CI findings — 2026-08-19

1. ⬜ **Correlated ~18-minute CI failures across Docker runners/phases.** Prior
   runs showed different Docker runners failing around the same wall-clock
   boundary while building/deploying images. A port-collision fix was merged, but
   the broader shared timeout/resource/registry/network/storage/supervisor
   question remains open. Closure requires distinguishing runner-specific
   degradation from a shared infrastructure limit and demonstrating a controlled
   build+deploy path that does not hit the hidden deadline.
