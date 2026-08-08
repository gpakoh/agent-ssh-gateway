"""Agent Backend Router MCP tool — routes task to OpenCode via router selection.

The router selects the backend based on availability and cooldown state.
When disabled, falls back to the task.json ``agent`` field.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from command_policy import CommandPolicyError

TASKS_REL_DIR = ".ai-bridge/tasks"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _shell_escape(text: str) -> str:
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"


def _read_task_json(
    run_cmd: Callable[[str, str], dict[str, Any]],
    project: str,
    task_id: str,
) -> dict[str, Any]:
    import shlex

    cmd = f"cat {shlex.quote(f'{TASKS_REL_DIR}/{task_id}/task.json')}"
    result = run_cmd(project, cmd)
    raw = result.get("stdout", "")
    if not raw.strip():
        return {}
    return json.loads(raw)


def _read_current_plan(
    run_cmd: Callable[[str, str], dict[str, Any]],
    project: str,
    task_id: str,
) -> str | None:
    import shlex

    td = f"{TASKS_REL_DIR}/{task_id}"
    cmd = f"cat {shlex.quote(td)}/current-plan.md"
    result = run_cmd(project, cmd)
    return result.get("stdout", "").strip() or None


def _build_opencode_script(td: str, task_id: str, model: str | None) -> str:
    opencode_flags = "--dangerously-skip-permissions"
    if model:
        opencode_flags += f" --model {_shell_escape(model)}"

    parts = [
        f"td='{td}'",
        'mkdir -p "$td"',
        'echo "Status: running" > "$td/agent-status.md"',
        "OPCODE_BIN=$(command -v opencode 2>/dev/null || echo '/root/.opencode/bin/opencode')",
        'if [ -f "$td/current-plan.md" ]; then',
        f'  $OPCODE_BIN run {opencode_flags} "Read the plan at $td/current-plan.md and execute it fully. Save the implementation diff to $td/implementation-diff.patch. Update $td/agent-status.md as you complete each step. Do not commit, do not push, do not create branches."',
        "  RC=$?",
        "else",
        '  echo "Error: current-plan.md not found in $td"',
        "  RC=1",
        "fi",
        'git diff --no-color > "$td/implementation-diff.patch" 2>/dev/null',
    ]
    parts.extend(
        [
            "if [ $RC -eq 0 ]; then",
            '  echo "Status: needs-review" > "$td/agent-status.md"',
            "else",
            '  echo "Status: failed" > "$td/agent-status.md"',
            "fi",
        ]
    )
    parts.append(
        f'cat > "$td/agent-report.md" << REOF\n'
        f"# Agent Runner Result — {task_id}\n\n"
        f"- Agent: opencode\n"
        f"- Status: $(head -1 \"$td/agent-status.md\" | cut -d' ' -f2)\n"
        f"- Exit code: $RC\n"
        f"- Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        f"REOF"
    )
    parts.append("exit $RC")
    return "\n".join(parts)


def project_run_agent(
    run_cmd: Callable[[str, str], dict[str, Any]],
    *,
    project: str,
    task_id: str,
    model: str | None = None,
    router: Any | None = None,
    run_script: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute a handoff task via the agent backend router.

    Reads ``task.json`` from the SSH target, validates the task contract
    (``agent``, ``allowed_backends``), selects the backend via the router
    (when enabled), and delegates to the appropriate execution path.

    Args:
        run_cmd: callable(project, command) that executes a shell command
        project: project name under ``MCP_GATEWAY_PROJECT_ROOT``
        task_id: validated ``.ai-bridge`` task ID
        model: optional model override
        router: optional ``AgentBackendRouter`` instance
        run_script: callable(project, script) for multi-line bash scripts

    Returns:
        dict with keys: task_id, status, exit_code, stdout, stderr,
        started_at, finished_at
    """
    from examples.mcp_server.agent_tasks import validate_task_id

    validate_task_id(task_id)

    started_at = _now_iso()
    td = f"{TASKS_REL_DIR}/{task_id}"

    task_json = _read_task_json(run_cmd, project, task_id)
    if not task_json:
        return {
            "task_id": task_id,
            "status": "error",
            "error": "task.json not found or empty",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    agent = task_json.get("agent", "auto")
    allowed = task_json.get("allowed_backends", [])
    if agent != "auto":
        allowed = allowed or [agent]
    if not allowed:
        return {
            "task_id": task_id,
            "status": "error",
            "error": "task.json missing allowed_backends",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    if router is not None and getattr(router, "enabled", False):
        preferred = agent if agent == "opencode" else None
        selected = router.select_backend(task_agent=preferred)
    else:
        # Router disabled: use task.json agent field if valid, else first allowed
        selected = agent if agent in allowed else allowed[0]

    if not selected:
        cooldowns = router.get_cooldowns() if router else []
        cooldown_info = "; ".join(f"{c.backend}: blocked until {c.until}" for c in cooldowns)
        return {
            "task_id": task_id,
            "status": "blocked",
            "error": f"all backends unavailable ({cooldown_info})"
            if cooldown_info
            else "no backend available",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    if selected == "opencode":
        # Emit audit event at raise site for traceability
        try:
            from examples.mcp_server.mcp_audit import McpAuditEvent, get_audit_logger

            get_audit_logger().append(McpAuditEvent(
                event_type="mcp.tool_blocked",
                tool="run_agent",
                action="select_backend",
                decision="block",
                reason=f"{selected} agent backend is not allowed",
                error_code="AGENT_BACKEND_BLOCKED",
                metadata={"command_root": selected},
            ))
        except Exception:
            pass  # audit failure must not change tool behavior

        raise CommandPolicyError(
            f"run_agent is blocked: {selected} agent backend is not allowed. "
            "Use the dedicated run_opencode tool instead."
        )

    if selected not in allowed:
        return {
            "task_id": task_id,
            "status": "error",
            "error": f"selected backend '{selected}' not in allowed_backends {allowed}",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    if selected == "opencode":
        plan = _read_current_plan(run_cmd, project, task_id)
        if not plan:
            return {
                "task_id": task_id,
                "status": "error",
                "error": "current-plan.md not found — write task plan first",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": _now_iso(),
            }
        cmd = _build_opencode_script(td, task_id, model)
    else:
        return {
            "task_id": task_id,
            "status": "error",
            "error": f"unsupported backend: {selected}",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    result = (run_script or run_cmd)(project, cmd)
    exit_code = result.get("exit_code")

    if router is not None and selected:
        router.record_result(
            selected,
            exit_code=exit_code if exit_code is not None else -1,
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
        )

    return {
        "task_id": task_id,
        "status": "needs-review"
        if exit_code == 0
        else "failed"
        if exit_code is not None
        else "error",
        "exit_code": exit_code,
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "started_at": started_at,
        "finished_at": _now_iso(),
    }
