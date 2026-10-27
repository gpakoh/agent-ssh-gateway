# ADR-2026-10-27: Gateway execution ownership is caller-neutral

## Status

Architecture boundary, confirmed by platform owner (2026-10-27). Applies to all
new Gateway work. Legacy field names require explicit compatibility review before
renaming; this ADR is not a declaration that those fields have been migrated.

## Responsibility

**Gateway is the hands.** It owns policy-scoped SSH/Git/files/CI/Docker tools,
agent-task execution, durable attempts, bounded automated recovery, verification
artifacts, cancellation/fencing, and its **own** OpenCode CLI workers.

**The caller is the brain.** A human, ChatGPT, CI, an internal Gateway procedure,
or a separate orchestration service may request permitted work. The Gateway
must not need to know how that caller reasons, sees the browser, plans releases,
or selects engineering priorities. A caller's approval and project readiness
remain outside the execution plane.

**Engineering Supervisor is a separate product.** Its browser/architect cognition,
project decisions and any OpenCode Adapter agents belong outside Gateway.
Supervisor may use the same public, scoped Gateway capabilities as any other
authorized caller; it is neither Gateway's parent nor its default reviewer,
runtime dependency, fallback backend, or owner of Gateway's OpenCode workers.

## Invariants

1. `write_agent_task -> run_opencode -> agent_status/read_agent_report/read_agent_diff`
   works for an authorized caller without Supervisor, OpenCode Adapter or a
   browser transport. A caller may opt into additional review; that must not
   prevent worker start, collection of a terminal result, or explicit cancellation.
2. Gateway's local automation may select/retry a proxy, launch its worker,
   run bounded required checks, persist receipts and reconcile an ambiguous
   attempt. These are **hands remembering a practiced action**, not planning
   product priorities. Safety policy, no-blind-replay and resource ownership
   remain mandatory regardless of caller.
3. A worker's exit 0 + passed machine checks means *execution evidence*
   is available; it does **not** mean a user's finding, release or PR is
   accepted. The original caller/authorized project reviewer makes those
   decisions. No specific external orchestrator is required.
4. Do not introduce imports, authentication, readiness conditions, role names,
   retry gates or service-to-service calls that require Engineering Supervisor
   for a standalone Gateway operation. Cross-service coordination is optional
   and outward-facing, not embedded in the execution core.
5. OpenCode Adapter agents are a separate runtime. Gateway's own OpenCode
   workers use its executor and artifacts; neither service silently inherits
   the other's session ownership, model policy or lifecycle.
6. Judge changes using *real* standalone OpenCode tasks: useful output,
   independent checks, terminal worker/runner/job/proxy agreement, bounded
   latency and safe failure/cancellation. Green CI alone is not proof.

## Existing historical coupling to retire deliberately

The October 2026 audit found these **real names/contracts**, not evidence that
Gateway presently invokes Engineering Supervisor:

- `examples/mcp_server/agent_tasks.py`: `supervisor-review`,
  `supervisor_review`, `pending_supervisor`, `needs_supervisor`, owner
  `supervisor`, and `supervisor_recreate*` recovery hints.
- `examples/mcp_server/agent_tools.py`: `supervisor-verdict.json`,
  `_supervisor_postrun_script_lines` and similarly named private artifacts.
- `examples/mcp_server/mcp_infra/adapters/supervisor.py`,
  `supervisor_integration.py`, `project_registry_control.py`,
  `server.py`, tool modes/scopes: three admin-only
  `supervisor_*` MCP integration/registration tools.
- `projects.yaml`: registered historical `.supervisor-workspaces`
  entries for pinned/temporary source workspaces.
- Historical notes and old tests repeat these names.

These are misleading ownership signals. Review consumers, stored receipts,
idempotency/recovery data and tool-mode/auth contracts before any rename,
removal or schema migration. In particular **do not** relabel a user approval
as automatically completed, delete persisted artifacts, or break existing
client tools merely to make the vocabulary look cleaner.

Target language for new Gateway contracts: `caller`, `reviewer`,
`review_pending`, `gateway`, `executor`, `task`, `attempt`.
No new `supervisor_*` execution features. Document legacy aliases as
compatibility-only, with a separate reviewed deprecation plan.

## Acceptance for future boundary cleanup

- Trace every currently exported `supervisor_*` tool and legacy gate value to
  real clients; determine whether each is generic Gateway functionality or
  obsolete application-specific coupling.
- Add versioned, caller-neutral replacements and compatibility/negative tests
  **before** removing old schemas or persisted keys.
- Demonstrate direct standalone `run_opencode` with no Supervisor configured,
  including success, provider failure, cancellation/reconciliation and report
  retrieval on a real deployed release. Distinguish tested from deployed.
