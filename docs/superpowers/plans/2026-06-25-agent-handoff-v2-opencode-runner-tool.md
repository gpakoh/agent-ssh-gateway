# OpenCode Runner MCP Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `gateway_project_run_opencode` MCP tool that executes an existing handoff task via OpenCode CLI on the SSH target.

**Architecture:** New `examples/mcp_server/opencode_tools.py` module with a single public function `project_run_opencode(run_cmd, *, project, task_id, model=None)`. Follows `agent_tasks.py` pattern — receives a `run_cmd` callable injected by `server.py`. The function builds a multi-step shell script that: (1) sets status=running, (2) invokes opencode, (3) captures git diff, (4) writes agent-report.md, (5) sets final status. `server.py` wraps it as MCP tool with `run_tool()` error handling and write-mode guard via `write_modes.assert_handoff_write_allowed()`.

**Tech Stack:** Python 3.11, MCP Gateway (FastMCP), GatewayClient (run_project_command)

## Global Constraints

- No auto-commit/push — executor must not commit
- Tool requires `MCP_GATEWAY_WRITE_MODE=handoff` or `full`
- Only accepts `project + task_id` — no free-form prompts
- Task must already exist (task.json + current-plan.md in .ai-bridge/tasks/<task_id>/)
- OpenCode runs on SSH target through `run_project_command`, not local subprocess
- All output files written to `.ai-bridge/tasks/<task_id>/` via SSH commands
- Follows `agent_tasks.py` pattern: `run_cmd` injection, `validate_task_id`, shell-safe heredocs
- MCP tool name: `gateway_project_run_opencode`

---

### Task 1: opencode_tools.py — core function

**Files:**
- Create: `examples/mcp_server/opencode_tools.py`
- Test: `tests/test_mcp_opencode.py`

**Interfaces:**
- Consumes: `agent_tasks.validate_task_id`
- Produces: `project_run_opencode(run_cmd, *, project, task_id, model=None) -> dict[str, Any]`

- [ ] **Step 1: Write failing test**

```python
"""Tests for OpenCode runner MCP tool — opencode_tools module."""
from __future__ import annotations

import pytest

from opencode_tools import project_run_opencode


def _fake_run_cmd(project: str, command: str) -> dict:
    return {"stdout": "", "stderr": "", "exit_code": 0}


class TestProjectRunOpencode:
    def test_invalid_task_id_raises(self):
        with pytest.raises(ValueError, match="Invalid task_id"):
            project_run_opencode(
                _fake_run_cmd,
                project="test",
                task_id="bad",
            )

    def test_accepted_task_id(self):
        result = project_run_opencode(
            _fake_run_cmd,
            project="test",
            task_id="2026-06-25-fix-auth-opencode",
        )
        assert "task_id" in result
        assert result["task_id"] == "2026-06-25-fix-auth-opencode"

    def test_returns_structured_result(self):
        result = project_run_opencode(
            _fake_run_cmd,
            project="test",
            task_id="2026-06-25-fix-auth-opencode",
        )
        for key in ("task_id", "status", "exit_code", "stdout", "stderr", "started_at", "finished_at"):
            assert key in result, f"missing key: {key}"
        assert result["status"] == "needs-review"

    def test_failed_run_returns_failed_status(self):
        def _failing_run_cmd(project: str, command: str) -> dict:
            return {"stdout": "", "stderr": "error", "exit_code": 1}
        result = project_run_opencode(
            _failing_run_cmd,
            project="test",
            task_id="2026-06-25-fix-auth-opencode",
        )
        assert result["status"] == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_opencode.py -v 2>&1`
Expected: FAIL — ImportError (module not found) + ModuleNotFoundError

- [ ] **Step 3: Create opencode_tools.py**

```python
"""OpenCode runner MCP tool — execute handoff tasks via OpenCode CLI.

Follows the same run_cmd injection pattern as agent_tasks.py.
All commands run on the SSH target through run_project_command.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable

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
    from agent_tasks import validate_task_id

    validate_task_id(task_id)

    td = f"{TASKS_REL_DIR}/{task_id}"
    started_at = _now_iso()

    opencode_flags = "--never-ask"
    if model:
        opencode_flags += f" --model {_shell_escape(model)}"

    parts = [
        f"td='{td}'",
        f"mkdir -p \"$td\"",
        f"echo 'Status: running' > \"$td/agent-status.md\"",
        f"OPCODE_BIN=$(command -v opencode 2>/dev/null || echo '/root/.opencode/bin/opencode')",
        f"if [ -f \"$td/current-plan.md\" ]; then",
        f"  $OPCODE_BIN run {opencode_flags} \"Read the plan at $td/current-plan.md and execute it fully. Save the implementation diff to $td/implementation-diff.patch. Update $td/agent-status.md as you complete each step. Do not commit, do not push, do not create branches, do not edit files outside Allowed files.\"",
        f"  RC=$?",
        f"else",
        f"  echo 'Error: current-plan.md not found in $td'",
        f"  RC=1",
        f"fi",
        f"git diff --no-color > \"$td/implementation-diff.patch\" 2>/dev/null",
    ]
    # Set final status based on exit code
    parts.extend([
        f"if [ $RC -eq 0 ]; then",
        f"  echo 'Status: needs-review' > {td}/agent-status.md",
        f"else",
        f"  echo 'Status: failed' > {td}/agent-status.md",
        f"fi",
    ])
    # Build agent-report.md via heredoc
    parts.append(
        f"cat > '{td}/agent-report.md' << 'REOF'\n"
        f"# OpenCode Runner Result — {task_id}\n\n"
        f"- Status: $(head -1 '{td}/agent-status.md' | cut -d' ' -f2)\n"
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_opencode.py -v 2>&1`
Expected: 3/3 PASS

- [ ] **Step 5: Commit**

```bash
git add examples/mcp_server/opencode_tools.py tests/test_mcp_opencode.py
git commit -m "feat(opencode-tools): add project_run_opencode function"
```

---

### Task 2: Register MCP tool in server.py and tool_modes.py

**Files:**
- Modify: `examples/mcp_server/server.py` — import and register tool
- Modify: `examples/mcp_server/tool_modes.py` — add to chatgpt set

**Interfaces:**
- Consumes: `project_run_opencode` from `opencode_tools.py`, `write_modes.assert_handoff_write_allowed`, `run_project_command` from `chatgpt_tools.py`
- Registers: `gateway_project_run_opencode` MCP tool (prefix `gateway_project_*`)

- [ ] **Step 6: Write failing test for tool registration**

Add to `tests/test_mcp_opencode.py`:

```python
class TestToolRegistration:
    def test_registered_in_chatgpt_mode(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        import importlib
        import sys
        from pathlib import Path
        example_dir = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
        monkeypatch.syspath_prepend(str(example_dir))
        sys.modules.pop("tool_modes", None)
        tm = importlib.import_module("tool_modes")
        assert tm.should_register_tool("gateway_project_run_opencode") is True
```

- [ ] **Step 7: Verify test fails**

Run: `python -m pytest tests/test_mcp_opencode.py::TestToolRegistration -v 2>&1`
Expected: FAIL — `gateway_project_run_opencode` not in chatgpt set

- [ ] **Step 8: Add tool to tool_modes.py chatgpt set**

In `examples/mcp_server/tool_modes.py`, add `"gateway_project_run_opencode"` to the `chatgpt` set:

```python
"gateway_project_run_opencode",
```

Place it after the `"gateway_project_archive_agent_task"` line to keep agent-handoff tools grouped.

- [ ] **Step 9: Verify test passes**

Run: `python -m pytest tests/test_mcp_opencode.py::TestToolRegistration -v 2>&1`
Expected: PASS

- [ ] **Step 10: Write failing test — tool runs through server**

Add to `tests/test_mcp_opencode.py`:

```python
class TestServerTool:
    def test_tool_function_can_be_imported(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        import importlib, sys
        from pathlib import Path
        example_dir = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
        monkeypatch.syspath_prepend(str(example_dir))
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "handoff")
        monkeypatch.setenv("GITEA_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        for name in list(sys.modules):
            if "mcp_server" in name or "tool_modes" in name or "opencode_tools" in name:
                sys.modules.pop(name, None)
        server = importlib.import_module("server")
        tool = getattr(server, "gateway_project_run_opencode", None)
        assert tool is not None, "gateway_project_run_opencode not found in server module"
```

- [ ] **Step 11: Verify test fails**

Run: `python -m pytest tests/test_mcp_opencode.py::TestServerTool -v 2>&1`
Expected: FAIL — `gateway_project_run_opencode` not in server module

- [ ] **Step 12: Import and register tool in server.py**

Add import in `examples/mcp_server/server.py` after the `agent_tasks` imports block:

```python
from opencode_tools import (
    project_run_opencode as _project_run_opencode,
)
```

Add the tool registration function in the "Agent Handoff v2 tools" section (after `gateway_project_archive_agent_task`):

```python
@register_tool("gateway_project_run_opencode")
def gateway_project_run_opencode(
    project: str,
    task_id: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Execute an existing handoff task via OpenCode CLI on the SSH target.
    Requires MCP_GATEWAY_WRITE_MODE=handoff or full."""
    from write_modes import assert_handoff_write_allowed
    assert_handoff_write_allowed()
    return run_tool(
        tool="gateway_project_run_opencode",
        title="Run OpenCode",
        fn=lambda: _project_run_opencode(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            model=model,
        ),
        success_text="Submitted OpenCode task.",
    )
```

- [ ] **Step 13: Verify test passes**

Run: `python -m pytest tests/test_mcp_opencode.py -v 2>&1`
Expected: 6/6 PASS (3 from Task 1, 1 from Task 2 Step 9, 2 from Task 2)

- [ ] **Step 14: Commit**

```bash
git add examples/mcp_server/server.py examples/mcp_server/tool_modes.py examples/mcp_server/opencode_tools.py tests/test_mcp_opencode.py
git commit -m "feat: register gateway_project_run_opencode MCP tool
- New opencode_tools.py module with project_run_opencode()
- Registered in server.py with write-mode guard
- Added to tool_modes.py chatgpt set
- 6 tests covering validation, structure, registration, import"
```

---

### Task 3: Full verification

- [ ] **Step 15: Run make check**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway && make check 2>&1
```

Expected: ruff clean, compileall OK, all tests pass (existing 84 + new 6 = 90)

- [ ] **Step 16: Push to GitHub + Gitea**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway
git push origin master
git push gitea master
```
