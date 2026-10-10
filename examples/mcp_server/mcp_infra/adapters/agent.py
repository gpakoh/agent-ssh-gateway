"""Agent Handoff v2 adapter.

client and _agent_router are resolved through the server module at call
time: tests patch examples.mcp_server.server.client and expect the
patched client here. _split_lines is imported from the gateway adapter.

Tools are registered explicitly via register_all() (called by server.py
after runtime.set_mcp) instead of import-time decorator side effects.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from agent_tasks import (
    agent_task_status as _agent_task_status,
)
from agent_tasks import (
    archive_agent_task as _archive_agent_task,
)
from agent_tasks import (
    cancel_agent_task as _cancel_agent_task,
)
from agent_tasks import (
    claim_agent_attempt_state as _claim_agent_attempt_state,
)
from agent_tasks import (
    inspect_agent_task as _inspect_agent_task,
)
from agent_tasks import (
    list_agent_tasks as _list_agent_tasks,
)
from agent_tasks import (
    prepare_agent_task_retry as _prepare_agent_task_retry,
)
from agent_tasks import (
    read_agent_artifact_tail as _read_agent_artifact_tail,
)
from agent_tasks import (
    read_agent_attempt_hint_by_state_key as _read_agent_attempt_hint_by_state_key,
)
from agent_tasks import (
    read_agent_attempt_state as _read_agent_attempt_state,
)
from agent_tasks import (
    read_agent_log_tail as _read_agent_log_tail,
)
from agent_tasks import (
    read_agent_task_file as _read_agent_task_file,
)
from agent_tasks import (
    validate_source_mode as _validate_source_mode,
)
from agent_tasks import validate_task_id as _validate_task_id
from agent_tasks import (
    write_agent_attempt_state as _write_agent_attempt_state,
)
from agent_tasks import (
    write_agent_task as _write_agent_task,
)
from agent_tools import project_run_agent as _project_run_agent
from gateway_client import GatewayClientError
from mcp_audit import AuditWriteError, McpAuditEvent, redact_secrets
from mcp_client_tools import run_project_command
from opencode_tools import project_run_opencode as _project_run_opencode
from tool_results import tool_error

from examples.mcp_server.agent_paths import managed_workspace_path
from examples.mcp_server.agent_sources import (
    ensure_dirty_worktree_review_bundle,
    ensure_managed_source_bundle,
)
from examples.mcp_server.fleet_runtime import (
    ExecutionPlaneRecoveryBoundary,
    get_fleet_runtime,
)
from examples.mcp_server.fleet_state import (
    ATTEMPTED,
    LEGACY_UNKNOWN,
    LeaseConflictError,
    LeaseNotFoundError,
)
from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.adapters.gateway import _split_csv_or_lines, _split_lines
from examples.mcp_server.mcp_infra.tool_registry import register_tool, run_tool, run_tool_async
from examples.mcp_server.task_candidate import (
    bind_task_attempt_job,
    read_task_attempt_identity_by_state_key,
    record_task_delivery_contract,
    resolve_task_attempt_identity,
)


def _server_client():
    return server_attr("get_gateway_client")()


def _server_agent_client():
    return server_attr("get_agent_client")()


def _wait_job_contract(job_id: str) -> dict[str, Any]:
    """Wait on a durable job and normalize the outcome for the sync path.

    wait_job() raises GatewayClientError with body {"job_id", "status":
    "running", "wait_timed_out": True} when the bounded wait expires while
    the job is still running; translate it into the durable receipt the
    sync submission path returns to the caller so they can poll
    job_status/job_result with the embedded job_id instead of seeing an
    opaque timeout.
    """
    try:
        return _server_client().wait_job(job_id)
    except GatewayClientError as exc:
        if exc.body and exc.body.get("wait_timed_out"):
            return exc.body
        raise


def _server_agent_router():
    return server_attr("_agent_router")


def _trusted_fleet_job_resolver() -> Callable[[str], str | None]:
    """Resolve a fleet task id from trusted durable execution identities.

    Current submissions persist an immutable attempt id in the MCP control
    plane *before* Gateway dispatch, then bind ``job_id`` only after the ACK.
    If the coordinator dies in that window, reconstruct the exact durable
    Gateway key ``task:<project_state_key>:<task_id>:attempt:<attempt_id>``
    from that trusted pre-submit identity. Never guess/scan attempt ids.

    Only when no trusted attempt identity exists at all may historical
    pre-attempt leases fall back to the older exact task-scoped submission key.
    Missing exact claims stay unresolved; age or timestamps never release a
    lease.
    """

    def _resolve(durable_task_id: str) -> str | None:
        if not isinstance(durable_task_id, str):
            return None
        project_key, separator, task_id = durable_task_id.partition(":")
        if not separator or not project_key or not task_id:
            return None
        single_task_id = True
        try:
            _validate_task_id(task_id)
        except ValueError:
            # Historical run_agents releases used one comma-joined fleet key
            # for a batch before per-task durable identities existed. That
            # aggregate is not a valid single task_id and therefore must not be
            # passed into single-task artifact/control-plane readers. Do not
            # split or guess children: only the authoritative Gateway family /
            # exact durable submission keys below may recover such a row.
            single_task_id = False

        if single_task_id:
            identity = read_task_attempt_identity_by_state_key(
                project_key=project_key,
                task_id=task_id,
            )
            if identity is not None:
                attempt_id, current_job_id = identity
                if current_job_id:
                    return current_job_id
                return _server_agent_client().resolve_submission_job(
                    f"task:{durable_task_id}:attempt:{attempt_id}"
                )

            # The executor coordination record is worker-writable evidence, not a
            # trust anchor. Use only its attempt_id as a lookup hint; Gateway's
            # exact submission key remains authoritative because it embeds this
            # durable fleet task identity. A present hint never falls back to the
            # legacy base key after an exact miss.
            attempt_hint = _read_agent_attempt_hint_by_state_key(
                project_key=project_key,
                task_id=task_id,
            )
            if attempt_hint is not None:
                return _server_agent_client().resolve_submission_job(
                    f"task:{durable_task_id}:attempt:{attempt_hint}"
                )

        # The crash window between mark_submit_attempted() and submit_sync()
        # creates neither a control-plane binding nor attempt-state.json.  In
        # that narrow case, let Gateway recover only a UNIQUE retained member
        # of the strict attempt family. Gateway validates ownership and
        # authoritative job state; ambiguity/backend inconsistency propagates
        # fail-closed. Only a positive family miss may fall back to the older
        # pre-attempt exact task-scoped key.
        client = _server_agent_client()
        family_job_id = client.resolve_submission_job_family(
            f"task:{durable_task_id}:attempt:"
        )
        if family_job_id:
            return family_job_id
        return client.resolve_submission_job(f"task:{durable_task_id}")

    return _resolve


def _parse_generation_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


async def _execution_plane_recovery_boundary() -> ExecutionPlaneRecoveryBoundary | None:
    """Attest the current replacement generation for historical fleet recovery.

    This path is disabled unless the deployment explicitly opts in. The live
    Compose profile enables it only for the MCP service that starts after the
    Gateway and dedicated ``agent-sshd`` dependencies are healthy. Any missing,
    malformed, custom, or unhealthy evidence fails closed and leaves ambiguous
    leases consuming capacity rather than guessing liveness.
    """
    enabled = os.environ.get("MCP_AGENT_FLEET_GENERATION_RECOVERY", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    try:
        executor_host = str(server_attr("_agent_executor_host") or "").strip()
        if executor_host != "agent-sshd":
            return None

        gateway = await asyncio.to_thread(_server_client().health)
        if not isinstance(gateway, dict) or gateway.get("ready") is not True:
            return None
        # In-memory submission identity is authoritative only when every live
        # JobRecord belongs to this one process. Multi-worker deployments keep
        # generation recovery disabled until they provide a shared active-job
        # registry rather than risking a cross-worker false miss.
        if gateway.get("gateway_workers") != 1:
            return None
        gateway_started_at = _parse_generation_datetime(gateway.get("started_at"))
        gateway_generation = str(gateway.get("build_sha") or "").strip()
        if gateway_started_at is None or not gateway_generation:
            return None

        container_name = os.environ.get(
            "MCP_AGENT_EXECUTOR_CONTAINER_NAME", "ssh-gateway-agent-sshd"
        ).strip()
        if not container_name:
            return None
        docker_client = server_attr("DockerClient")()
        inspected = await docker_client.inspect(container_name, max_lines=2)
        entries = inspected if isinstance(inspected, list) else [inspected]
        if len(entries) != 1 or not isinstance(entries[0], dict):
            return None
        executor = entries[0]
        state = executor.get("State")
        config = executor.get("Config")
        if not isinstance(state, dict) or not isinstance(config, dict):
            return None
        labels = config.get("Labels")
        health_state = state.get("Health")
        if (
            state.get("Running") is not True
            or not isinstance(health_state, dict)
            or health_state.get("Status") != "healthy"
            or not isinstance(labels, dict)
            or labels.get("com.docker.compose.service") != "agent-sshd"
        ):
            return None
        executor_started_at = _parse_generation_datetime(state.get("StartedAt"))
        executor_generation = str(executor.get("Id") or "").strip()
        if executor_started_at is None or not executor_generation:
            return None

        raw_mcp_started_at = float(server_attr("_mcp_started_at"))
        mcp_started_at = datetime.fromtimestamp(raw_mcp_started_at, tz=UTC)
        # The opt-in contract is specifically for the deployment topology where
        # this coordinator starts only after both upstream dependencies are
        # healthy. Refuse evidence that contradicts that ordering.
        if mcp_started_at < gateway_started_at or mcp_started_at < executor_started_at:
            return None
        observed_at = datetime.now(UTC)
        return ExecutionPlaneRecoveryBoundary(
            gateway_started_at=gateway_started_at,
            executor_started_at=executor_started_at,
            mcp_started_at=mcp_started_at,
            observed_at=observed_at,
            gateway_generation=gateway_generation,
            executor_generation=executor_generation,
            mcp_generation=f"mcp-start:{raw_mcp_started_at:.6f}",
        )
    except Exception:
        # Recovery evidence is optional and must be fail-closed: an unexpected
        # inspection/health/runtime failure may suppress historical reclaim,
        # but must never block a normal durable agent submission.
        return None


def _agent_router_terminal_observer() -> Callable[[str, dict[str, Any]], None] | None:
    """Build the eventual-result observer for durable async agent jobs.

    ``project_run_agent`` currently executes only the OpenCode backend.  Keep
    that backend identity explicit here until additional executable backends
    are introduced; the observer is absent when router health tracking is not
    enabled.  Gateway job results are already redacted before reaching this
    callback.
    """
    router = _server_agent_router()
    if router is None or not getattr(router, "enabled", False):
        return None

    def _observe(_job_id: str, result: dict[str, Any]) -> None:
        terminal_status = str(result.get("status") or "")
        if terminal_status in {"cancelled", "ambiguous"}:
            # User cancellation and fleet liveness reconciliation are not
            # evidence that the OpenCode backend itself failed. Feeding either
            # into record_result would incorrectly cool/FAIL the backend.
            return
        raw_exit_code = result.get("exit_code")
        exit_code = (
            raw_exit_code
            if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool)
            else -1
        )
        stdout = str(result.get("stdout", ""))
        if exit_code == 77:
            # Exit 77 is wrapper-owned and normalized only after the runner
            # positively detected rate limiting.  Preserve that semantic even
            # when redacted/truncated gateway output no longer contains the
            # original provider text that AgentBackendRouter pattern-matches.
            stdout = f"{stdout}\nrate limit".strip()
        router.record_result(
            "opencode",
            exit_code=exit_code,
            stdout=stdout,
            stderr=str(result.get("stderr", "")),
        )

    return _observe


def _normalize_single_agent_submission(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    """Fail honestly when a single-agent pre-submit path returns status=error.

    project_run_agent/project_run_opencode use raw result dicts for execution
    receipts. A raw ``status=error`` means no successful submission contract
    exists and must not be wrapped by run_tool_async as ``ok=true`` merely
    because no Python exception was raised. Batch submission intentionally has
    different semantics and does not use this helper: one failed item can
    coexist with other successfully submitted items.
    """
    if "ok" in result or result.get("status") != "error":
        return result
    raw_message = result.get("error")
    message = (
        raw_message.strip()
        if isinstance(raw_message, str) and raw_message.strip()
        else "Agent submission failed before a runnable job was accepted."
    )
    return tool_error(
        tool=tool,
        code="AGENT_SUBMISSION_FAILED",
        message=message,
        result=result,
        source="agent",
    )


def _split_scope_patterns(value: str | None) -> list[str] | None:
    """Parse allowed/forbidden file patterns from the MCP string surface.

    Newlines are canonical, but a comma-separated single line is accepted as
    a convenience because the public MCP schema exposes these fields as strings
    rather than arrays. Keep this parser scope-only: required_checks may
    legitimately contain commas and must remain newline-separated.
    """
    lines = _split_lines(value)
    if lines is None:
        return None
    patterns: list[str] = []
    for line in lines:
        patterns.extend(part.strip() for part in line.split(",") if part.strip())
    return patterns


# ── Agent Handoff v2 tools ──────────────────────────────────────────


def gateway_write_agent_task(
    project: str,
    task_id: str,
    agent: str,
    task: str,
    scope: str = "",
    allowed_files: str | None = None,
    forbidden_files: str | None = None,
    required_checks: str | None = None,
    acceptance_criteria: str | None = None,
    commit_message: str | None = None,
    constraints: str | None = None,
    worktree_path: str | None = None,
    base_ref: str | None = None,
    source_mode: str | None = None,
    workflow_phase: str | None = None,
) -> dict[str, Any]:
    """Write task.json + current-plan.md to .ai-bridge/tasks/<task_id>/."""

    def _fn() -> dict[str, Any]:
        # Publish exact committed source in the trusted control plane before
        # the task becomes runnable. The executor only consumes this root RO.
        #
        # The returned publication binds a SHA-256 that the control plane
        # computed over the SAME private snapshot bytes it just fully
        # proved (single head == base_ref, bundle verify, scratch clone).
        # Any failure here propagates: no runnable task without a bound
        # digest, and no supervisor-time recapture fallback exists.
        normalized_source_mode = _validate_source_mode(source_mode)
        source_ref = base_ref.strip() if isinstance(base_ref, str) and base_ref.strip() else None
        source_tree_sha = None
        managed_source_sha256: str | None = None
        if normalized_source_mode == "dirty_worktree_snapshot":
            if worktree_path and worktree_path.strip():
                raise ValueError(
                    "dirty_worktree_snapshot uses a managed review clone and does not accept worktree_path"
                )
            if managed_workspace_path(project, task_id) is None:
                raise ValueError(
                    "dirty_worktree_snapshot requires MCP_AGENT_WORKSPACE_ROOT managed execution"
                )
            dirty_publication = ensure_dirty_worktree_review_bundle(project, base_ref)
            if dirty_publication is None:
                raise ValueError(
                    "dirty_worktree_snapshot requires MCP_AGENT_SOURCE_ROOT managed source storage"
                )
            managed_source_sha256 = dirty_publication.sha256
            source_ref = dirty_publication.snapshot_ref
            source_tree_sha = dirty_publication.tree_sha
        else:
            publication = ensure_managed_source_bundle(project, base_ref)
            managed_source_sha256 = publication.sha256 if publication else None
        parsed_allowed = _split_scope_patterns(allowed_files) or []
        parsed_forbidden = _split_scope_patterns(forbidden_files) or []
        parsed_checks = _split_lines(required_checks) or []
        if base_ref:
            record_task_delivery_contract(
                project=project,
                task_id=task_id,
                base_ref=base_ref,
                allowed_files=parsed_allowed,
                forbidden_files=parsed_forbidden,
                required_checks=parsed_checks,
            )

        return _write_agent_task(
            # Script transport (sh + stdin), NOT run_project_command: the
            # generated command is a multi-line heredoc script that
            # shlex.split() would shred (live: mkdir saw 'cat', '>', 'JEOF'
            # as separate argv entries). execute_project_script pipes it
            # verbatim to a bare `sh` -- same shape, correct semantics.
            lambda p, s: _server_client().execute_project_script(p, s),
            project=project,
            task_id=task_id,
            agent=agent,
            task=task,
            scope=scope,
            allowed_files=parsed_allowed,
            forbidden_files=parsed_forbidden,
            required_checks=parsed_checks,
            acceptance_criteria=_split_lines(acceptance_criteria),
            commit_message=commit_message,
            constraints=constraints,
            worktree_path=worktree_path,
            base_ref=base_ref,
            managed_source_sha256=managed_source_sha256,
            source_mode=normalized_source_mode,
            source_ref=source_ref,
            source_tree_sha=source_tree_sha,
            workflow_phase=workflow_phase,
        )

    return run_tool(
        tool="write_agent_task",
        title="Write agent task",
        fn=_fn,
        success_text="Wrote agent task.",
    )


def gateway_read_agent_status(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-status.md."""
    return run_tool(
        tool="read_agent_status",
        title="Read agent status",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            filename="agent-status.md",
        ),
        success_text="Read agent status.",
    )


def gateway_read_agent_report(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-report.md."""
    return run_tool(
        tool="read_agent_report",
        title="Read agent report",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            filename="agent-report.md",
        ),
        success_text="Read agent report.",
    )


def gateway_read_agent_diff(project: str, task_id: str) -> dict[str, Any]:
    """Read the review diff and return the SHA-256 of those exact returned bytes."""

    def _fn() -> dict[str, Any]:
        result = _read_agent_task_file(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            filename="implementation-diff.patch",
        )
        if result.get("exit_code") == 0 and result.get("stdout") != "(not found)":
            text = str(result.get("stdout", ""))
            result["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return result

    return run_tool(
        tool="read_agent_diff",
        title="Read agent diff",
        fn=_fn,
        success_text="Read agent diff.",
    )


def gateway_read_agent_log(
    project: str,
    task_id: str,
    tail_lines: int = 200,
) -> dict[str, Any]:
    """Read a bounded tail of a running OpenCode agent's stdout/stderr log."""

    def _fn() -> dict[str, Any]:
        result = _read_agent_log_tail(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            tail_lines=tail_lines,
        )
        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        redacted_stdout = str(redact_secrets(stdout))
        redacted_stderr = str(redact_secrets(stderr))
        result["stdout"] = redacted_stdout
        result["stderr"] = redacted_stderr
        result["redacted"] = redacted_stdout != stdout or redacted_stderr != stderr
        return result

    return run_tool(
        tool="read_agent_log",
        title="Read agent live log",
        fn=_fn,
        success_text="Read agent live log.",
    )


def gateway_read_agent_artifact(
    project: str,
    task_id: str,
    artifact: str,
    tail_lines: int = 200,
    max_bytes: int = 65536,
) -> dict[str, Any]:
    """Read a bounded, redacted tail of one fixed agent task artifact."""

    def _fn() -> dict[str, Any]:
        result = _read_agent_artifact_tail(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            artifact=artifact,
            tail_lines=tail_lines,
            max_bytes=max_bytes,
        )
        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        redacted_stdout = str(redact_secrets(stdout))
        redacted_stderr = str(redact_secrets(stderr))
        result["stdout"] = redacted_stdout
        result["stderr"] = redacted_stderr
        result["redacted"] = bool(result.get("redacted")) or redacted_stdout != stdout or redacted_stderr != stderr
        return result

    return run_tool(
        tool="read_agent_artifact",
        title="Read agent artifact tail",
        fn=_fn,
        success_text="Read agent artifact tail.",
    )



def gateway_agent_status(
    project: str,
    task_id: str,
    stale_after_seconds: int = 600,
) -> dict[str, Any]:
    """Lightweight agent task status: no log tail, cheap polling first."""

    return run_tool(
        tool="agent_status",
        title="Read agent status snapshot",
        fn=lambda: _agent_task_status(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            stale_after_seconds=stale_after_seconds,
            job_status=lambda jid: _server_client().job_status(jid),
        ),
        success_text="Read agent status snapshot.",
    )



def gateway_inspect_agent_task(
    project: str,
    task_id: str,
    tail_lines: int = 120,
    stale_after_seconds: int = 600,
    reasoning_loop_after_seconds: int = 120,
) -> dict[str, Any]:
    """Inspect one agent task: status, job, artifact mtimes, stale verdict, and log tail."""

    def _fn() -> dict[str, Any]:
        result = _inspect_agent_task(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            tail_lines=tail_lines,
            stale_after_seconds=stale_after_seconds,
            reasoning_loop_after_seconds=reasoning_loop_after_seconds,
            job_status=lambda jid: _server_client().job_status(jid),
        )
        log = result.get("log")
        if isinstance(log, dict):
            stdout = str(log.get("stdout", ""))
            stderr = str(log.get("stderr", ""))
            redacted_stdout = str(redact_secrets(stdout))
            redacted_stderr = str(redact_secrets(stderr))
            log["stdout"] = redacted_stdout
            log["stderr"] = redacted_stderr
            result["redacted"] = redacted_stdout != stdout or redacted_stderr != stderr
        status_text = result.get("status_text")
        if isinstance(status_text, str):
            redacted_status = str(redact_secrets(status_text))
            result["status_text"] = redacted_status
            result["redacted"] = bool(result.get("redacted")) or redacted_status != status_text
        return result

    return run_tool(
        tool="inspect_agent_task",
        title="Inspect agent task",
        fn=_fn,
        success_text="Inspected agent task.",
    )


def gateway_list_agent_tasks(project: str) -> dict[str, Any]:
    """List task directories under .ai-bridge/tasks/."""
    return run_tool(
        tool="list_agent_tasks",
        title="List agent tasks",
        fn=lambda: _list_agent_tasks(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
        ),
        success_text="Listed agent tasks.",
    )



def gateway_cancel_agent_task(project: str, task_id: str) -> dict[str, Any]:
    """Cancel the gateway job bound to an agent task's durable attempt record."""

    return run_tool(
        tool="cancel_agent_task",
        title="Cancel agent task",
        fn=lambda: _cancel_agent_task(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            cancel_job=lambda job_id: _server_client().cancel_job(job_id),
        ),
        success_text="Requested agent task cancellation.",
    )


def gateway_retry_agent_task(
    project: str,
    source_task_id: str,
    retry_task_id: str,
    continuation_prompt: str | None = None,
) -> dict[str, Any]:
    """Refuse retry cloning until trusted never-submitted proof is available."""

    return run_tool(
        tool="retry_agent_task",
        title="Prepare agent task retry",
        fn=lambda: _prepare_agent_task_retry(
            lambda p, c: run_project_command(_server_client(), p, c),
            lambda p, s: _server_client().execute_project_script(p, s),
            project=project,
            source_task_id=source_task_id,
            retry_task_id=retry_task_id,
            job_status=lambda job_id: _server_client().job_status(job_id),
            continuation_prompt=continuation_prompt,
            trusted_never_submitted=False,
            trusted_retry_seed=None,
        ),
        success_text="Prepared agent task retry.",
    )


def gateway_archive_agent_task(project: str, task_id: str) -> dict[str, Any]:
    """Move .ai-bridge/tasks/<task_id>/ -> .ai-bridge/archive/<task_id>/."""
    return run_tool(
        tool="archive_agent_task",
        title="Archive agent task",
        fn=lambda: _archive_agent_task(
            lambda p, s: _server_client().execute_project_script(p, s),
            project=project,
            task_id=task_id,
        ),
        success_text="Archived agent task.",
    )


async def gateway_fleet_status() -> dict[str, Any]:
    """Inspect fleet capacity and classify lease safety without mutation."""

    async def _fn() -> dict[str, Any]:
        fleet = await get_fleet_runtime()
        if fleet is None:
            return {"enabled": False, "reason": "fleet_disabled"}
        snapshot = await fleet.fleet_status(
            job_status_fn=lambda job_id: _server_client().job_status(job_id)
        )
        return {"enabled": True, **snapshot}

    return await run_tool_async(
        tool="fleet_status",
        title="Fleet status",
        fn=_fn,
        success_text="Read fleet status.",
    )


async def gateway_fleet_reconcile_unbound(
    task_id: str,
    lease_fence: str,
    expected_submit_state: str,
    acknowledge: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Explicitly reconcile one exact historical unbound fleet lease."""

    async def _fn() -> dict[str, Any]:
        if acknowledge is not True:
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="POLICY_DENIED",
                message="Explicit acknowledge=true is required for ambiguous fleet reconciliation.",
                retryable=False,
            )
        normalized_reason = reason.strip() if isinstance(reason, str) else ""
        if not normalized_reason or len(normalized_reason) > 500 or "\x00" in normalized_reason:
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="INVALID_INPUT",
                message="reason must be 1..500 non-NUL characters.",
                retryable=False,
            )
        if expected_submit_state not in {ATTEMPTED, LEGACY_UNKNOWN}:
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="INVALID_INPUT",
                message="expected_submit_state must be attempted or legacy_unknown.",
                retryable=False,
            )
        if not isinstance(lease_fence, str):
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="INVALID_INPUT",
                message="lease_fence must be a UUID string.",
                retryable=False,
            )
        try:
            lease_token = str(uuid.UUID(lease_fence.strip()))
        except (ValueError, AttributeError):
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="INVALID_INPUT",
                message="lease_fence must be a valid UUID.",
                retryable=False,
            )

        fleet = await get_fleet_runtime()
        if fleet is None:
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="CHECK_FAILED",
                message="Fleet admission is disabled; there is no active fleet runtime to reconcile.",
                retryable=False,
            )

        lease = await fleet.state.get_lease(task_id)
        if lease is None:
            try:
                return await fleet.reconcile_unbound_lease(
                    task_id=task_id,
                    lease_token=lease_token,
                    expected_submit_state=expected_submit_state,
                    resolved_job_id=None,
                    recovery_boundary=None,
                )
            except LeaseNotFoundError:
                return tool_error(
                    tool="fleet_reconcile_unbound",
                    code="FILE_NOT_FOUND",
                    message="No matching active or previously reconciled fleet lease exists.",
                    retryable=False,
                )
        if lease.pool != fleet.pool_name:
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="WORKSPACE_CONTENDED",
                message="Fleet lease belongs to a different pool.",
                retryable=True,
            )
        if lease.lease_token != lease_token or lease.submit_state != expected_submit_state:
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="WORKSPACE_CONTENDED",
                message="Fleet lease token or submission state changed.",
                retryable=True,
            )

        resolver = _trusted_fleet_job_resolver()
        try:
            resolved_job_id = await asyncio.to_thread(resolver, task_id)
        except Exception as exc:
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="CHECK_FAILED",
                message="Trusted Gateway submission identity could not be resolved.",
                retryable=True,
                details={"error_class": type(exc).__name__},
            )

        normalized_resolved_job_id = (
            resolved_job_id.strip()
            if isinstance(resolved_job_id, str) and resolved_job_id.strip()
            else None
        )
        if lease.job_id is not None:
            if normalized_resolved_job_id == lease.job_id:
                return {
                    "action": "already_bound_existing_job",
                    "task_id": task_id,
                    "job_id": lease.job_id,
                    "released": False,
                }
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="WORKSPACE_CONTENDED",
                message="Bound fleet lease does not match trusted submission identity.",
                retryable=True,
            )

        recovery_boundary = None
        if normalized_resolved_job_id is None:
            recovery_boundary = await _execution_plane_recovery_boundary()
            if recovery_boundary is None:
                return tool_error(
                    tool="fleet_reconcile_unbound",
                    code="CHECK_FAILED",
                    message="Replacement-generation evidence is unavailable; ambiguous lease retained.",
                    retryable=True,
                )

        correlation_id = uuid.uuid4().hex
        audit_metadata = {
            "task_id": task_id,
            "lease_fence_sha256": hashlib.sha256(lease_token.encode()).hexdigest(),
            "expected_submit_state": expected_submit_state,
            "reason": normalized_reason,
            "acknowledged": True,
        }
        try:
            server_attr("get_audit_logger")().append_required(
                McpAuditEvent(
                    event_type="mcp.fleet_reconcile_intent",
                    tool="fleet_reconcile_unbound",
                    action="reconcile_unbound_lease",
                    decision="allow",
                    reason=normalized_reason,
                    request_id=correlation_id,
                    metadata=audit_metadata,
                )
            )
        except AuditWriteError:
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="AUDIT_UNAVAILABLE",
                message="Audit log unavailable; fleet reconciliation refused.",
                retryable=True,
            )

        try:
            result = await fleet.reconcile_unbound_lease(
                task_id=task_id,
                lease_token=lease_token,
                expected_submit_state=expected_submit_state,
                resolved_job_id=resolved_job_id,
                recovery_boundary=recovery_boundary,
            )
        except (LeaseConflictError, LeaseNotFoundError) as exc:
            return tool_error(
                tool="fleet_reconcile_unbound",
                code="WORKSPACE_CONTENDED",
                message="Fleet lease changed during reconciliation.",
                retryable=True,
                details={"error_class": type(exc).__name__},
            )

        try:
            server_attr("get_audit_logger")().append(
                McpAuditEvent(
                    event_type="mcp.fleet_reconcile_result",
                    tool="fleet_reconcile_unbound",
                    action="reconcile_unbound_lease",
                    decision="allow" if result.get("action") != "retained" else "block",
                    reason=str(result.get("reason") or result.get("action") or "reconciled"),
                    request_id=correlation_id,
                    metadata={**audit_metadata, "result_action": result.get("action")},
                )
            )
        except Exception:
            pass
        result["correlation_id"] = correlation_id
        return result

    return await run_tool_async(
        tool="fleet_reconcile_unbound",
        title="Fleet reconcile unbound lease",
        fn=_fn,
        success_text="Reconciled fleet lease.",
    )


_OPENCODE_FLEET_ADMISSION_ENV = "MCP_OPENCODE_FLEET_ADMISSION_ENABLED"


def _opencode_fleet_admission_enabled() -> bool:
    """Whether direct run_opencode submissions opt into global fleet admission."""
    return os.environ.get(_OPENCODE_FLEET_ADMISSION_ENV, "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_OPENCODE_HELP = {
    "recommended_model": "big-pickle",
    "profile": (
        "Use for narrow engineering tasks with explicit boundaries; strong at concrete fixes, "
        "reading existing code, regression tests, refactoring, and CI/staging feedback. "
        "Do not delegate architecture, broad consequence discovery, security semantics, or readiness decisions."
    ),
    "prompt_contract": (
        "State the exact finding/invariant, allowed and forbidden files, acceptance criteria, "
        "required checks, and where the agent must stop."
    ),
    "supervisor_rule": (
        "The agent may implement and test; the supervisor decides whether a finding is closed or the project is ready. "
        "If the agent appears to exceed scope, reread the exact task prompt before blaming agent initiative."
    ),
}


async def gateway_run_opencode(
    project: str,
    task_id: str,
    model: str | None = None,
    async_submit: bool = False,
) -> dict[str, Any]:
    """Execute an existing handoff task directly via OpenCode CLI.

    This path deliberately bypasses the backend router. It also bypasses the
    global Postgres fleet admission layer by default because the dedicated
    executor already enforces proxy leases and cgroup headroom admission.
    Set MCP_OPENCODE_FLEET_ADMISSION_ENABLED=true to opt this tool back into
    the fleet layer. Durable attempt identity and isolated workspace handling
    remain inside project_run_opencode regardless of this setting.
    """
    from write_modes import assert_handoff_write_allowed

    assert_handoff_write_allowed()

    def _submit(
        before_gateway_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        return _project_run_opencode(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            model=model,
            run_script=lambda _p, s: _server_agent_client().execute_script(s),
            run_script_async=lambda _p, s, k: _server_agent_client().execute_script_async(
                s, k
            ),
            run_script_wait=lambda jid: _wait_job_contract(jid),
            read_attempt_state=lambda p, t: _read_agent_attempt_state(
                lambda _p, c: run_project_command(_server_client(), _p, c), project=p, task_id=t
            ),
            claim_attempt_state=lambda p, t, rec: _claim_agent_attempt_state(
                lambda _p, s: _server_client().execute_project_script(_p, s), project=p, task_id=t, record=rec
            ),
            write_attempt_state=lambda p, t, rec: _write_agent_attempt_state(
                lambda _p, s: _server_client().execute_project_script(_p, s), project=p, task_id=t, record=rec
            ),
            job_status=lambda jid: _server_client().job_status(jid),
            resolve_trusted_attempt=lambda p, t, f: resolve_task_attempt_identity(
                project=p, task_id=t, fingerprint=f
            ),
            record_trusted_attempt=lambda p, t, a, f, j: bind_task_attempt_job(
                project=p, task_id=t, attempt_id=a, fingerprint=f, job_id=j
            ),
            before_gateway_dispatch=before_gateway_dispatch,
            async_submit=async_submit,
        )

    async def _fn() -> dict[str, Any]:
        if _opencode_fleet_admission_enabled():
            result = await _submit_agent_with_fleet(
                project=project,
                task_id=task_id,
                submit_sync=_submit,
            )
        else:
            result = await asyncio.to_thread(_submit, None)
        return _normalize_single_agent_submission("run_opencode", result)

    response = await run_tool_async(
        tool="run_opencode",
        title="Run opencode task",
        fn=_fn,
        success_text="Submitted opencode task.",
    )
    response["help"] = dict(_OPENCODE_HELP)
    return response


def _build_agent_submit(
    *,
    project: str,
    task_id: str,
    model: str | None,
    async_submit: bool,
) -> Callable[[Callable[[], None] | None], dict[str, Any]]:
    """Build the synchronous gateway submit used by single and batch paths."""

    def _submit(
        before_gateway_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        return _project_run_agent(
            lambda p, c: run_project_command(_server_client(), p, c),
            project=project,
            task_id=task_id,
            model=model,
            router=_server_agent_router(),
            run_script=lambda _p, s: _server_agent_client().execute_script(s),
            run_script_async=lambda _p, s, k: _server_agent_client().execute_script_async(
                s, k
            ),
            run_script_wait=lambda jid: _wait_job_contract(jid),
            read_attempt_state=lambda p, t: _read_agent_attempt_state(
                lambda _p, c: run_project_command(_server_client(), _p, c), project=p, task_id=t
            ),
            claim_attempt_state=lambda p, t, rec: _claim_agent_attempt_state(
                lambda _p, s: _server_client().execute_project_script(_p, s), project=p, task_id=t, record=rec
            ),
            write_attempt_state=lambda p, t, rec: _write_agent_attempt_state(
                lambda _p, s: _server_client().execute_project_script(_p, s), project=p, task_id=t, record=rec
            ),
            job_status=lambda jid: _server_client().job_status(jid),
            resolve_trusted_attempt=lambda p, t, f: resolve_task_attempt_identity(
                project=p, task_id=t, fingerprint=f
            ),
            record_trusted_attempt=lambda p, t, a, f, j: bind_task_attempt_job(
                project=p, task_id=t, attempt_id=a, fingerprint=f, job_id=j
            ),
            before_gateway_dispatch=before_gateway_dispatch,
            async_submit=async_submit,
        )

    return _submit


async def _submit_agent_with_fleet(
    *,
    project: str,
    task_id: str,
    submit_sync: Callable[[Callable[[], None] | None], dict[str, Any]],
    terminal_observer: Callable[[str, dict[str, Any]], None] | None = None,
    observe_submitted_job: bool = True,
    sweep_before_submit: bool = True,
) -> dict[str, Any]:
    """Use durable fleet admission when enabled, otherwise submit directly."""
    fleet = await get_fleet_runtime()
    if fleet is None:
        return await asyncio.to_thread(submit_sync, None)
    trusted_job_resolver = _trusted_fleet_job_resolver()
    job_result_fn = (
        (lambda jid: _server_client().job_result(jid))
        if terminal_observer is not None
        else None
    )
    return await fleet.submit(
        project=project,
        task_id=task_id,
        submit_sync=lambda: submit_sync(None),
        submit_with_dispatch_guard=submit_sync,
        job_status_fn=lambda jid: _server_client().job_status(jid),
        job_result_fn=job_result_fn,
        terminal_observer=terminal_observer,
        observe_submitted_job=observe_submitted_job,
        retry_attempted_unbound=True,
        trusted_job_resolver=trusted_job_resolver,
        recovery_boundary=None,
        sweep_before_submit=sweep_before_submit,
    )


async def gateway_run_agent(
    project: str,
    task_id: str,
    model: str | None = None,
    async_submit: bool = False,
) -> dict[str, Any]:
    """Execute a handoff task via the backend router with optional fleet admission."""
    from write_modes import assert_handoff_write_allowed

    assert_handoff_write_allowed()
    submit_sync = _build_agent_submit(
        project=project,
        task_id=task_id,
        model=model,
        async_submit=async_submit,
    )
    terminal_observer = _agent_router_terminal_observer()

    async def _fn() -> dict[str, Any]:
        result = await _submit_agent_with_fleet(
            project=project,
            task_id=task_id,
            submit_sync=submit_sync,
            terminal_observer=terminal_observer,
            observe_submitted_job=async_submit,
        )
        return _normalize_single_agent_submission("run_agent", result)

    return await run_tool_async(
        tool="run_agent",
        title="Run agent task (router)",
        fn=_fn,
        success_text="Submitted agent task via router.",
    )


_MAX_AGENT_BATCH = 64


async def gateway_run_agents(project: str, task_ids: str) -> dict[str, Any]:
    """Submit multiple prepared agent tasks in one MCP call."""
    from write_modes import assert_handoff_write_allowed

    assert_handoff_write_allowed()

    async def _fn() -> dict[str, Any]:
        ids = _split_csv_or_lines(task_ids) or []
        if not ids:
            raise ValueError("task_ids must contain at least one task ID")
        if len(ids) > _MAX_AGENT_BATCH:
            raise ValueError(f"task_ids may contain at most {_MAX_AGENT_BATCH} items")
        if len(ids) != len(set(ids)):
            raise ValueError("task_ids must not contain duplicates")

        fleet = await get_fleet_runtime()
        terminal_observer = _agent_router_terminal_observer()
        trusted_job_resolver = _trusted_fleet_job_resolver() if fleet is not None else None

        def status_fn(job_id: str) -> dict[str, Any]:
            return _server_client().job_status(job_id)

        def result_fn(job_id: str) -> dict[str, Any]:
            return _server_client().job_result(job_id)

        detailed_result_fn = result_fn if terminal_observer is not None else None
        if fleet is not None:
            try:
                await fleet.reconcile(
                    status_fn,
                    job_result_fn=detailed_result_fn,
                    terminal_observer=terminal_observer,
                    trusted_job_resolver=trusted_job_resolver,
                    recovery_boundary=None,
                )
            except Exception:
                pass

        async def submit_one(task_id: str) -> dict[str, Any]:
            submit_sync = _build_agent_submit(
                project=project,
                task_id=task_id,
                model=None,
                async_submit=True,
            )
            try:
                if fleet is None:
                    result = await asyncio.to_thread(submit_sync, None)
                else:
                    result = await fleet.submit(
                        project=project,
                        task_id=task_id,
                        submit_sync=lambda: submit_sync(None),
                        submit_with_dispatch_guard=submit_sync,
                        job_status_fn=status_fn,
                        job_result_fn=detailed_result_fn,
                        terminal_observer=terminal_observer,
                        observe_submitted_job=True,
                        retry_attempted_unbound=True,
                        trusted_job_resolver=trusted_job_resolver,
                        recovery_boundary=None,
                        sweep_before_submit=False,
                    )
                return result
            except Exception as exc:
                return {
                    "task_id": task_id,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                }

        results = await asyncio.gather(*(submit_one(task_id) for task_id in ids))
        return {"items": results, "count": len(results)}

    return await run_tool_async(
        tool="run_agents",
        title="Run agent tasks",
        fn=_fn,
        success_text="Submitted agent tasks via router.",
    )


def register_all() -> None:
    register_tool("write_agent_task")(gateway_write_agent_task)
    register_tool("read_agent_status")(gateway_read_agent_status)
    register_tool("read_agent_report")(gateway_read_agent_report)
    register_tool("read_agent_diff")(gateway_read_agent_diff)
    register_tool("read_agent_log")(gateway_read_agent_log)
    register_tool("read_agent_artifact")(gateway_read_agent_artifact)
    register_tool("agent_status")(gateway_agent_status)
    register_tool("inspect_agent_task")(gateway_inspect_agent_task)
    register_tool("list_agent_tasks")(gateway_list_agent_tasks)
    register_tool("cancel_agent_task")(gateway_cancel_agent_task)
    register_tool("retry_agent_task")(gateway_retry_agent_task)
    register_tool("archive_agent_task")(gateway_archive_agent_task)
    register_tool("fleet_status")(gateway_fleet_status)
    register_tool("fleet_reconcile_unbound")(gateway_fleet_reconcile_unbound)
    register_tool("run_opencode")(gateway_run_opencode)
    register_tool("run_agent")(gateway_run_agent)
    register_tool("run_agents")(gateway_run_agents)
