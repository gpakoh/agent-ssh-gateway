"""Mimo runner MCP tool — execute handoff tasks via Mimo CLI inside disposable worktrees.

HARD BLOCKED: --dangerously-skip-permissions is not allowed.
This tool is disabled — no confirmation flow, no override.
Use project_run_pytest or project_run_ruff for safe command execution.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from command_policy import CommandPolicyError


def project_run_mimo(
    run_cmd: Callable[[str, str], dict[str, Any]],
    *,
    project: str,
    task_id: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Execute an existing handoff task via Mimo CLI inside a disposable git worktree.

    BLOCKED: --dangerously-skip-permissions is not allowed.
    This tool is hard-blocked — no confirmation flow, no override.

    All pre-flight guards (11 checks) execute inside the shell script.
    Binary discovery is handled by the shell script, NOT by Python.
    This function does NOT call find_mimo_bin() — unit tests never
    require a real Mimo binary.

    Args:
        run_cmd: callable(project, command) that executes a shell command
        project: project name under MCP_GATEWAY_PROJECT_ROOT
        task_id: validated .ai-bridge task ID (must exist in tasks/)
        model: optional model override (validated by regex)

    Returns:
        dict with keys: task_id, status, exit_code, stdout, stderr,
        started_at, finished_at
    """
    # Emit audit event at raise site for traceability
    try:
        from examples.mcp_server.mcp_audit import McpAuditEvent, get_audit_logger

        get_audit_logger().append(McpAuditEvent(
            event_type="mcp.tool_blocked",
            tool="project_run_mimo",
            action="execute_task",
            decision="block",
            reason="--dangerously-skip-permissions is not allowed",
            error_code="MIMO_BLOCKED",
        ))
    except Exception:
        pass  # audit failure must not change tool behavior

    raise CommandPolicyError(
        "project_run_mimo is blocked: --dangerously-skip-permissions is not allowed. "
        "Use project_run_pytest or project_run_ruff for safe command execution."
    )
