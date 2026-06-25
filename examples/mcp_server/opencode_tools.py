"""OpenCode runner MCP tool — execute handoff tasks via OpenCode CLI.

Follows the same run_cmd injection pattern as agent_tasks.py.
All commands run on the SSH target through run_project_command.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

TASKS_REL_DIR = ".ai-bridge/tasks"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def project_run_opencode(
    run_cmd: Callable[[str, str], dict[str, Any]],
    *,
    project: str,
    task_id: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Execute an existing handoff task via OpenCode CLI on the SSH target.

    Args:
        run_cmd: callable(project, command) that executes a shell command
        project: project name under MCP_GATEWAY_PROJECT_ROOT
        task_id: validated .ai-bridge task ID (must exist in tasks/)
        model: optional model override (e.g., "gpt-4o")

    Returns:
        dict with keys: task_id, status, exit_code, stdout, stderr,
        started_at, finished_at
    """
    from examples.mcp_server.agent_tasks import validate_task_id

    validate_task_id(task_id)

    td = f"{TASKS_REL_DIR}/{task_id}"
    started_at = _now_iso()

    opencode_flags = "--never-ask"
    if model:
        opencode_flags += f" --model {_shell_escape(model)}"

    parts = [
        f"td='{td}'",
        'mkdir -p "$td"',
        "echo 'Status: running' > \"$td/agent-status.md\"",
        "OPCODE_BIN=$(command -v opencode 2>/dev/null || echo '/root/.opencode/bin/opencode')",
        'if [ -f "$td/current-plan.md" ]; then',
        f'  $OPCODE_BIN run {opencode_flags} "Read the plan at $td/current-plan.md and execute it fully. Save the implementation diff to $td/implementation-diff.patch. Update $td/agent-status.md as you complete each step. Do not commit, do not push, do not create branches, do not edit files outside Allowed files."',
        "  RC=$?",
        "else",
        "  echo 'Error: current-plan.md not found in $td'",
        "  RC=1",
        "fi",
        'git diff --no-color > "$td/implementation-diff.patch" 2>/dev/null',
    ]
    # Set final status based on exit code
    parts.extend([
        "if [ $RC -eq 0 ]; then",
        "  echo 'Status: needs-review' > \"$td/agent-status.md\"",
        "else",
        "  echo 'Status: failed' > \"$td/agent-status.md\"",
        "fi",
    ])
    # Build agent-report.md via heredoc
    parts.append(
        f'cat > "$td/agent-report.md" << \'REOF\'\n'
        f"# OpenCode Runner Result — {task_id}\n\n"
        f"- Status: $(head -1 \"$td/agent-status.md\" | cut -d' ' -f2)\n"
        f"- Exit code: $RC\n"
        f"- Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        f"REOF"
    )
    parts.append("exit $RC")

    cmd = "\n".join(parts)
    result = run_cmd(project, cmd)

    return {
        "task_id": task_id,
        "status": "needs-review" if result.get("exit_code") == 0 else "failed",
        "exit_code": result.get("exit_code"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "started_at": started_at,
        "finished_at": _now_iso(),
    }


def _shell_escape(text: str) -> str:
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"
