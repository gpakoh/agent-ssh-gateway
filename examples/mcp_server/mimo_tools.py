"""Mimo runner MCP tool — execute handoff tasks via Mimo CLI inside disposable worktrees.

All guards execute inside the shell script on the SSH target.
Binary discovery ($MIMO_BIN, command -v, /root/.mimocode/bin/mimo)
is handled by the shell script, not by Python.

Follows the same run_cmd injection pattern as agent_tasks.py and opencode_tools.py.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

TASKS_REL_DIR = ".ai-bridge/tasks"

MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,80}$")

MIMO_EXTRA_NO_PROXY = os.getenv(
    "MIMO_EXTRA_NO_PROXY",
    "10.0.1.103,10.0.0.3,10.0.0.127,localhost,127.0.0.1,::1",
)


def validate_model(model: str | None) -> str | None:
    if model is None:
        return None
    if not MODEL_RE.fullmatch(model):
        raise ValueError(f"Invalid model name: {model!r}")
    return model


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _shell_escape(text: str) -> str:
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"


def _build_mimo_script(
    task_id: str,
    td: str,
    model: str | None,
) -> str:
    """Build the shell script that runs on the SSH target.

    Guards (11 checks) run before `mimo run`:
      1. task.json exists
      2. task.json agent is "mimo"
      3. worktree_path set in task.json
      4. MCP_GATEWAY_WORKTREE_ROOT is set
      5. worktree_path dir exists
      6. Canonical realpath variables
      7. worktree != project root
      8. worktree under MCP_GATEWAY_WORKTREE_ROOT
      9. git rev-parse --is-inside-work-tree
     10. git rev-parse --show-toplevel matches
     11. linked worktree check (git-dir != git-common-dir)

    Binary discovery inside shell, not in Python, so unit tests
    never require a real Mimo binary.
    """
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
        "# Guard 2: agent == mimo",
        """AGENT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("agent",""))' "$td/task.json" 2>/dev/null)""",
        'if [ "$AGENT" != "mimo" ]; then',
        '  echo "Error: task.json agent is not mimo" >&2; exit 1;',
        "fi",
        "",
        "# Guard 3: worktree_path exists in task.json",
        """WORKTREE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("worktree_path",""))' "$td/task.json" 2>/dev/null)""",
        'if [ -z "$WORKTREE" ]; then',
        '  echo "Error: worktree_path not set in task.json" >&2; exit 1;',
        "fi",
        "",
        "# Guard 4: MCP_GATEWAY_WORKTREE_ROOT is set",
        'if [ -z "$MCP_GATEWAY_WORKTREE_ROOT" ]; then',
        '  echo "Error: MCP_GATEWAY_WORKTREE_ROOT not set" >&2; exit 1;',
        "fi",
        "",
        "# Guard 5: worktree_path exists as directory",
        'if [ ! -d "$WORKTREE" ]; then',
        '  echo "Error: worktree_path does not exist or is not a directory" >&2; exit 1;',
        "fi",
        "",
        "# Guard 6: canonical realpath variables",
        "PROJECT_REAL=$(realpath .)",
        "WORKTREE_REAL=$(realpath \"$WORKTREE\")",
        "WORKTREE_ROOT_REAL=$(realpath \"$MCP_GATEWAY_WORKTREE_ROOT\")",
        "",
        "# Guard 7: worktree != project root",
        'if [ "$WORKTREE_REAL" = "$PROJECT_REAL" ]; then',
        '  echo "Error: worktree_path equals project root" >&2; exit 1;',
        "fi",
        "",
        "# Guard 8: worktree under MCP_GATEWAY_WORKTREE_ROOT",
        'case "$WORKTREE_REAL" in',
        '  "$WORKTREE_ROOT_REAL"/*) ;;',
        '  *) echo "Error: worktree_path outside MCP_GATEWAY_WORKTREE_ROOT" >&2; exit 1 ;;',
        "esac",
        "",
        "# Guard 9: valid git worktree",
        'if ! git -C "$WORKTREE_REAL" rev-parse --is-inside-work-tree >/dev/null 2>&1; then',
        '  echo "Error: worktree_path is not a git worktree" >&2; exit 1;',
        "fi",
        "",
        "# Guard 10: worktree top-level matches",
        """GIT_TOP=$(git -C "$WORKTREE_REAL" rev-parse --show-toplevel 2>/dev/null)""",
        'if [ "$(realpath "$GIT_TOP")" != "$WORKTREE_REAL" ]; then',
        '  echo "Error: worktree_path is not the top-level of its worktree" >&2; exit 1;',
        "fi",
        "",
        "# Guard 11: linked worktree (not main checkout)",
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
        "# NO_PROXY: ensure local targets bypass HTTP proxy",
        f"MIMO_EXTRA_NO_PROXY={_shell_escape(MIMO_EXTRA_NO_PROXY)}",
        'export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$MIMO_EXTRA_NO_PROXY"',
        'export no_proxy="${no_proxy:+$no_proxy,}$MIMO_EXTRA_NO_PROXY"',
        "",
        "# Mark running",
        'echo "Status: running" > "$PROJECT_REAL/$td/agent-status.md"',
        "",
        "# Change to worktree and execute",
        'cd "$WORKTREE_REAL"',
        "",
        f'"$MIMO_BIN" run --dangerously-skip-permissions{model_flag} \\',
        '  "Read $PROJECT_REAL/$td/current-plan.md in the parent repo at $PROJECT_REAL. '
        'Execute the plan fully inside worktree $WORKTREE_REAL. '
        'Do not commit, do not push, do not create branches. '
        'Save the implementation diff to $PROJECT_REAL/$td/implementation-diff.patch. '
        'Update $PROJECT_REAL/$td/agent-status.md as you go. '
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
        f"# Mimo Runner Result — {task_id}",
        "",
        "- Agent: mimo",
        '- Status: $(head -1 "$PROJECT_REAL/$td/agent-status.md" | cut -d" " -f2)',
        "- Exit code: $RC",
        "- Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)",
        '- Worktree: $WORKTREE_REAL',
        "REOF",
        "exit $RC",
    ]
    return "\n".join(parts)


def project_run_mimo(
    run_cmd: Callable[[str, str], dict[str, Any]],
    *,
    project: str,
    task_id: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Execute an existing handoff task via Mimo CLI inside a disposable git worktree.

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
    from examples.mcp_server.agent_tasks import validate_task_id

    validate_task_id(task_id)
    if model is None:
        model = os.getenv("MIMO_DEFAULT_MODEL", "ollama-gen/gemma4:26b")
    model = validate_model(model)

    td = f"{TASKS_REL_DIR}/{task_id}"
    started_at = _now_iso()

    cmd = _build_mimo_script(task_id, td, model)
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
