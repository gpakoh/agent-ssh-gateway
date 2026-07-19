"""OpenCode runner MCP tool — execute handoff tasks via OpenCode CLI.

HARD BLOCKED: --dangerously-skip-permissions is not allowed.
This tool is disabled — no confirmation flow, no override.
Use project_run_pytest or project_run_ruff for safe command execution.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from command_policy import CommandPolicyError


def project_run_opencode(
    run_cmd: Callable[[str, str], dict[str, Any]],
    *,
    project: str,
    task_id: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Execute an existing handoff task via OpenCode CLI on the SSH target.

    BLOCKED: --dangerously-skip-permissions is not allowed.
    This tool is hard-blocked — no confirmation flow, no override.

    Args:
        run_cmd: callable(project, command) that executes a shell command
        project: project name under MCP_GATEWAY_PROJECT_ROOT
        task_id: validated .ai-bridge task ID (must exist in tasks/)
        model: optional model override (e.g., "gpt-4o")

    Returns:
        dict with keys: task_id, status, exit_code, stdout, stderr,
        started_at, finished_at
    """
    # Emit audit event at raise site for traceability
    try:
        from examples.mcp_server.mcp_audit import McpAuditEvent, get_audit_logger

        get_audit_logger().append(McpAuditEvent(
            event_type="mcp.tool_blocked",
            tool="project_run_opencode",
            action="execute_task",
            decision="block",
            reason="--dangerously-skip-permissions is not allowed",
            error_code="OPENCODE_BLOCKED",
        ))
    except Exception:
        pass  # audit failure must not change tool behavior

    raise CommandPolicyError(
        "project_run_opencode is blocked: --dangerously-skip-permissions is not allowed. "
        "Use project_run_pytest or project_run_ruff for safe command execution."
    )
