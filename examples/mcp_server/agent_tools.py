"""Agent Backend Router MCP tool — routes task to OpenCode or Mimo via router selection.

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
    cmd = f"cat {TASKS_REL_DIR}/{task_id}/task.json 2>/dev/null || echo '{{}}'"
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
    cmd = f"cat {shlex.quote(td)}/current-plan.md 2>/dev/null || true"
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


def _build_mimo_script(td: str, task_id: str, model: str | None) -> str:
    model_flag = f" --model {_shell_escape(model)}" if model else ""

    parts = [
        f"TASK_ID='{task_id}'",
        f'td="{TASKS_REL_DIR}/$TASK_ID"',
        "",
        "# Guard 1: task.json exists",
        'if [ ! -f "$td/task.json" ]; then',
        '  echo "Error: task.json not found in $td" >&2; exit 1;',
        "fi",
        "",
        "# Guard 2: worktree_path exists in task.json",
        """WORKTREE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("worktree_path",""))' "$td/task.json" 2>/dev/null)""",
        'if [ -z "$WORKTREE" ]; then',
        '  echo "Error: worktree_path not set in task.json" >&2; exit 1;',
        "fi",
        "",
        "# Guard 3: MCP_GATEWAY_WORKTREE_ROOT is set",
        'if [ -z "$MCP_GATEWAY_WORKTREE_ROOT" ]; then',
        '  echo "Error: MCP_GATEWAY_WORKTREE_ROOT not set" >&2; exit 1;',
        "fi",
        "",
        "# Guard 4: worktree_path exists as directory",
        'if [ ! -d "$WORKTREE" ]; then',
        '  echo "Error: worktree_path does not exist or is not a directory" >&2; exit 1;',
        "fi",
        "",
        "# Guard 5: canonical realpath variables",
        "PROJECT_REAL=$(realpath .)",
        'WORKTREE_REAL=$(realpath "$WORKTREE")',
        'WORKTREE_ROOT_REAL=$(realpath "$MCP_GATEWAY_WORKTREE_ROOT")',
        "",
        "# Guard 6: worktree != project root",
        'if [ "$WORKTREE_REAL" = "$PROJECT_REAL" ]; then',
        '  echo "Error: worktree_path equals project root" >&2; exit 1;',
        "fi",
        "",
        "# Guard 7: worktree under MCP_GATEWAY_WORKTREE_ROOT",
        'case "$WORKTREE_REAL" in',
        '  "$WORKTREE_ROOT_REAL"/*) ;;',
        '  *) echo "Error: worktree_path outside MCP_GATEWAY_WORKTREE_ROOT" >&2; exit 1 ;;',
        "esac",
        "",
        "# Guard 8: valid git worktree",
        'if ! git -C "$WORKTREE_REAL" rev-parse --is-inside-work-tree >/dev/null 2>&1; then',
        '  echo "Error: worktree_path is not a git worktree" >&2; exit 1;',
        "fi",
        "",
        "# Guard 9: worktree top-level matches",
        """GIT_TOP=$(git -C "$WORKTREE_REAL" rev-parse --show-toplevel 2>/dev/null)""",
        'if [ "$(realpath "$GIT_TOP")" != "$WORKTREE_REAL" ]; then',
        '  echo "Error: worktree_path is not the top-level of its worktree" >&2; exit 1;',
        "fi",
        "",
        "# Guard 10: linked worktree (not main checkout)",
        """GIT_DIR=$(git -C "$WORKTREE_REAL" rev-parse --git-dir 2>/dev/null)""",
        """GIT_COMMON_DIR=$(git -C "$WORKTREE_REAL" rev-parse --git-common-dir 2>/dev/null)""",
        'if [ "$GIT_DIR" = "$GIT_COMMON_DIR" ]; then',
        '  echo "Error: worktree_path is a main git checkout, not a linked disposable worktree" >&2; exit 1;',
        "fi",
        "",
        "# Binary discovery inside shell",
        'MIMO_BIN="${MIMO_BIN:-$(command -v mimo 2>/dev/null || true)}"',
        'if [ -z "$MIMO_BIN" ] && [ -x "/root/.mimocode/bin/mimo" ]; then',
        '  MIMO_BIN="/root/.mimocode/bin/mimo"',
        "fi",
        'if [ -z "$MIMO_BIN" ] || [ ! -x "$MIMO_BIN" ]; then',
        '  echo "Error: Mimo binary not found" >&2; exit 127;',
        "fi",
        "",
        "# Mark running",
        'echo "Status: running" > "$PROJECT_REAL/$td/agent-status.md"',
        "",
        "# Change to worktree and execute",
        'cd "$WORKTREE_REAL"',
        "",
        f'"$MIMO_BIN" run --dangerously-skip-permissions{model_flag} \\',
        '  "Read $PROJECT_REAL/$td/current-plan.md in the parent repo at $PROJECT_REAL. '
        "Execute the plan fully inside worktree $WORKTREE_REAL. "
        "Do not commit, do not push, do not create branches. "
        "Save the implementation diff to $PROJECT_REAL/$td/implementation-diff.patch. "
        "Update $PROJECT_REAL/$td/agent-status.md as you go. "
        'Work only inside $WORKTREE_REAL."',
        "RC=$?",
        "",
        "# Capture diff from worktree",
        'git -C "$WORKTREE_REAL" diff --no-color > "$PROJECT_REAL/$td/implementation-diff.patch" 2>/dev/null',
        "",
        "# Final status",
        "if [ $RC -eq 0 ]; then",
        '  echo "Status: needs-review" > "$PROJECT_REAL/$td/agent-status.md"',
        "else",
        '  echo "Status: failed" > "$PROJECT_REAL/$td/agent-status.md"',
        "fi",
        "",
        "# agent-report",
        'cat > "$PROJECT_REAL/$td/agent-report.md" << REOF',
        f"# Agent Runner Result — {task_id}",
        "",
        "- Agent: mimo",
        '- Status: $(head -1 "$PROJECT_REAL/$td/agent-status.md" | cut -d" " -f2)',
        "- Exit code: $RC",
        "- Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)",
        "- Worktree: $WORKTREE_REAL",
        "REOF",
        "exit $RC",
    ]
    return "\n".join(parts)


def project_run_agent(
    run_cmd: Callable[[str, str], dict[str, Any]],
    *,
    project: str,
    task_id: str,
    model: str | None = None,
    router: Any | None = None,
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
        preferred = agent if agent in ("opencode", "mimo") else None
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

    if selected in ("opencode", "mimo"):
        raise CommandPolicyError(
            f"project_run_agent is blocked: {selected} backend is not allowed. "
            "Use dedicated project_run_opencode/project_run_mimo tools instead."
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
    elif selected == "mimo":
        worktree = task_json.get("worktree_path", "")
        if not worktree:
            return {
                "task_id": task_id,
                "status": "error",
                "error": "worktree_path required in task.json for mimo backend",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "started_at": started_at,
                "finished_at": _now_iso(),
            }
        cmd = _build_mimo_script(td, task_id, model)
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

    result = run_cmd(project, cmd)
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
