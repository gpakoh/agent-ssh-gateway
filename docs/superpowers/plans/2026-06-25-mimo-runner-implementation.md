# Mimo Runner MCP Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `gateway_project_run_mimo` MCP tool — execute handoff tasks via Mimo CLI inside disposable git worktrees only.

**Architecture:** New `examples/mcp_server/mimo_tools.py` with `project_run_mimo()` function (injected `run_cmd` callable, same pattern as `opencode_tools.py`). Builds a shell script with 11 pre-flight guards, then executes `mimo run --dangerously-skip-permissions` inside the worktree. No separate CLI wrapper.

**Tech Stack:** Python 3.11+, MCP (FastMCP), Mimo CLI, git worktrees

## Global Constraints

- `model` parameter: regex `^[A-Za-z0-9._:/@+-]{1,80}$`, validated in Python before any shell construction
- `project_run_mimo()` must NOT call `find_mimo_bin()` — binary discovery is inside shell script on SSH target only
- `TASK_ID` must be explicitly declared as shell variable in the generated script
- No `scripts/mimo_runner_wrapper.py` — only MCP tool
- All paths to `.ai-bridge/tasks/` use `$PROJECT_REAL` (absolute), not `$td` alone
- `git diff` is captured from worktree path, not project root
- Tool name: `gateway_project_run_mimo`, visibility: chatgpt mode only
- `MCP_GATEWAY_WORKTREE_ROOT` env var required for runtime guards
- Write mode guard: `assert_handoff_write_allowed()` required

---

### Task 1: `mimo_tools.py` — model validation + shell script builder

**Files:**
- Create: `examples/mcp_server/mimo_tools.py`
- Test: `tests/test_mcp_mimo.py` (written first)

**Interfaces:**
- Consumes: `run_cmd: Callable[[str, str], dict[str, Any]]` — injected by server.py
- Produces: `project_mimo_run(run_cmd, *, project, task_id, model=None) -> dict[str, Any]`
  - Returns: `task_id, status, exit_code, stdout, stderr, started_at, finished_at`
- Produces: `validate_model(model: str | None) -> str | None` — raises ValueError on bad input

- [ ] **Step 1: Write the failing tests in `tests/test_mcp_mimo.py` — model validation**

```python
"""Tests for Mimo runner MCP tool — mimo_tools module."""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
from pathlib import Path

import pytest

from examples.mcp_server.mimo_tools import project_run_mimo, validate_model

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
TASK_ID = "2026-06-25-mimo-task-opencode"


def _fake_run_cmd(project: str, command: str) -> dict:
    return {"stdout": command, "stderr": "", "exit_code": 0}


class TestModelValidation:
    def test_valid_models(self):
        for m in ["big-pickle", "zen/big-pickle", "claude-sonnet-4", "provider:model", "a:b@1.2+c-d"]:
            assert validate_model(m) == m

    def test_none_passes_through(self):
        assert validate_model(None) is None

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="Invalid model name"):
            validate_model("Big Pickle")

    def test_rejects_shell_chars(self):
        for bad in ["x; rm -rf /", "$(whoami)", "`id`", "foo && bar", "foo|bar"]:
            with pytest.raises(ValueError, match="Invalid model name"):
                validate_model(bad)

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="Invalid model name"):
            validate_model("")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="Invalid model name"):
            validate_model("x" * 81)
```

- [ ] **Step 2: Run model validation tests — expect failures**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway && python3 -m pytest tests/test_mcp_mimo.py::TestModelValidation -v 2>&1
```
Expected: ImportError / NameError — `mimo_tools.py` does not exist yet.

- [ ] **Step 3: Create `examples/mcp_server/mimo_tools.py` — model validation + helpers**

```python
"""Mimo runner MCP tool — execute handoff tasks via Mimo CLI inside disposable worktrees.

All guards execute inside the shell script on the SSH target.
Binary discovery ($MIMO_BIN, command -v, /root/.mimocode/bin/mimo)
is handled by the shell script, not by Python.

Follows the same run_cmd injection pattern as agent_tasks.py and opencode_tools.py.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

TASKS_REL_DIR = ".ai-bridge/tasks"

MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,80}$")


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
        # Guard 1
        'if [ ! -f "$td/task.json" ]; then',
        '  echo "Error: task.json not found in $td" >&2; exit 1;',
        "fi",
        # Guard 2
        """AGENT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("agent",""))' "$td/task.json" 2>/dev/null)""",
        'if [ "$AGENT" != "mimo" ]; then',
        '  echo "Error: task.json agent is not mimo" >&2; exit 1;',
        "fi",
        # Guard 3
        """WORKTREE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("worktree_path",""))' "$td/task.json" 2>/dev/null)""",
        'if [ -z "$WORKTREE" ]; then',
        '  echo "Error: worktree_path not set in task.json" >&2; exit 1;',
        "fi",
        # Guard 4
        'if [ -z "$MCP_GATEWAY_WORKTREE_ROOT" ]; then',
        '  echo "Error: MCP_GATEWAY_WORKTREE_ROOT not set" >&2; exit 1;',
        "fi",
        # Guard 5
        'if [ ! -d "$WORKTREE" ]; then',
        '  echo "Error: worktree_path does not exist or is not a directory" >&2; exit 1;',
        "fi",
        # Guard 6 — canonical paths
        "PROJECT_REAL=$(realpath .)",
        "WORKTREE_REAL=$(realpath \"$WORKTREE\")",
        "WORKTREE_ROOT_REAL=$(realpath \"$MCP_GATEWAY_WORKTREE_ROOT\")",
        # Guard 7
        'if [ "$WORKTREE_REAL" = "$PROJECT_REAL" ]; then',
        '  echo "Error: worktree_path equals project root" >&2; exit 1;',
        "fi",
        # Guard 8
        'case "$WORKTREE_REAL" in',
        '  "$WORKTREE_ROOT_REAL"/*) ;;',
        '  *) echo "Error: worktree_path outside MCP_GATEWAY_WORKTREE_ROOT" >&2; exit 1 ;;',
        "esac",
        # Guard 9
        'if ! git -C "$WORKTREE_REAL" rev-parse --is-inside-work-tree >/dev/null 2>&1; then',
        '  echo "Error: worktree_path is not a git worktree" >&2; exit 1;',
        "fi",
        # Guard 10
        """GIT_TOP=$(git -C "$WORKTREE_REAL" rev-parse --show-toplevel 2>/dev/null)""",
        'if [ "$(realpath "$GIT_TOP")" != "$WORKTREE_REAL" ]; then',
        '  echo "Error: worktree_path is not the top-level of its worktree" >&2; exit 1;',
        "fi",
        # Guard 11
        """GIT_DIR=$(git -C "$WORKTREE_REAL" rev-parse --git-dir 2>/dev/null)""",
        """GIT_COMMON_DIR=$(git -C "$WORKTREE_REAL" rev-parse --git-common-dir 2>/dev/null)""",
        'if [ "$GIT_DIR" = "$GIT_COMMON_DIR" ]; then',
        '  echo "Error: worktree_path is a main git checkout, not a linked disposable worktree" >&2; exit 1;',
        "fi",
        # Binary discovery inside shell
        'MIMO_BIN="${MIMO_BIN:-$(command -v mimo 2>/dev/null || true)}"',
        'if [ -z "$MIMO_BIN" ] && [ -x "/root/.mimocode/bin/mimo" ]; then',
        '  MIMO_BIN="/root/.mimocode/bin/mimo"',
        "fi",
        'if [ -z "$MIMO_BIN" ] || [ ! -x "$MIMO_BIN" ]; then',
        '  echo "Error: Mimo binary not found" >&2; exit 127;',
        "fi",
        # Status: running
        'echo "Status: running" > "$PROJECT_REAL/$td/agent-status.md"',
        # Change to worktree
        'cd "$WORKTREE_REAL"',
        # Execute mimo run
        f'"$MIMO_BIN" run --dangerously-skip-permissions{model_flag} \\',
        f'  "Read $PROJECT_REAL/$td/current-plan.md in the parent repo at $PROJECT_REAL. '
        f'Execute the plan fully inside worktree $WORKTREE_REAL. '
        f'Do not commit, do not push, do not create branches. '
        f'Save the implementation diff to $PROJECT_REAL/$td/implementation-diff.patch. '
        f'Update $PROJECT_REAL/$td/agent-status.md as you go. '
        f'Work only inside $WORKTREE_REAL."',
        "RC=$?",
        # Diff capture from worktree
        'git -C "$WORKTREE_REAL" diff --no-color > "$PROJECT_REAL/$td/implementation-diff.patch" 2>/dev/null',
        # Final status
        "if [ $RC -eq 0 ]; then",
        '  echo "Status: needs-review" > "$PROJECT_REAL/$td/agent-status.md"',
        "else",
        '  echo "Status: failed" > "$PROJECT_REAL/$td/agent-status.md"',
        "fi",
        # agent-report
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
```

- [ ] **Step 4: Run model validation tests — expect green**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway && python3 -m pytest tests/test_mcp_mimo.py::TestModelValidation -v 2>&1
```
Expected: 6/6 passed.

- [ ] **Step 5: Add command construction tests to `tests/test_mcp_mimo.py`**

Add to the same test file:

```python
def _capture_command(project: str, cmd: str) -> dict:
    _capture_command.last_cmd = cmd
    return {"stdout": cmd, "stderr": "", "exit_code": 0}


class TestCommandConstruction:
    def test_contains_dangerously_skip_permissions(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert _capture_command.last_cmd is not None
        assert "--dangerously-skip-permissions" in _capture_command.last_cmd

    def test_contains_task_id(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert TASK_ID in _capture_command.last_cmd

    def test_contains_worktree_root_guard(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "MCP_GATEWAY_WORKTREE_ROOT" in _capture_command.last_cmd

    def test_contains_agent_mimo_guard(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert 'agent is not mimo' in _capture_command.last_cmd

    def test_contains_do_not_commit(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "Do not commit" in _capture_command.last_cmd

    def test_contains_do_not_push(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "do not push" in _capture_command.last_cmd

    def test_contains_work_only_inside_worktree(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "Work only inside" in _capture_command.last_cmd

    def test_contains_diff_from_worktree(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert 'git -C "$WORKTREE_REAL" diff' in _capture_command.last_cmd

    def test_contains_linked_worktree_check(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "git-common-dir" in _capture_command.last_cmd

    def test_contains_mimo_binary_discovery(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "MIMO_BIN" in _capture_command.last_cmd

    def test_contains_model_flag_when_provided(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID, model="big-pickle")
        assert "--model 'big-pickle'" in _capture_command.last_cmd

    def test_no_model_flag_when_none(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID, model=None)
        # Should not contain --model flag
        assert "--model " not in _capture_command.last_cmd

    def test_uses_absolute_paths(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert '$PROJECT_REAL/$td/' in _capture_command.last_cmd

    def test_worktree_path_guard_present(self):
        _capture_command.last_cmd = None
        project_run_mimo(_capture_command, project="test", task_id=TASK_ID)
        assert "worktree_path not set" in _capture_command.last_cmd
```

- [ ] **Step 6: Run command construction tests — expect green**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway && python3 -m pytest tests/test_mcp_mimo.py::TestCommandConstruction -v 2>&1
```
Expected: 14/14 passed.

- [ ] **Step 7: Add result mapping tests**

Add to `tests/test_mcp_mimo.py`:

```python
class TestProjectRunMimo:
    def test_invalid_task_id_raises(self):
        with pytest.raises(ValueError, match="Invalid task_id"):
            project_run_mimo(_fake_run_cmd, project="test", task_id="bad")

    def test_invalid_model_raises_before_run_cmd(self):
        call_log = []
        def tracking_run_cmd(p, c):
            call_log.append(c)
            return {"stdout": "", "stderr": "", "exit_code": 0}

        with pytest.raises(ValueError, match="Invalid model name"):
            project_run_mimo(tracking_run_cmd, project="test", task_id=TASK_ID, model="Big Pickle")
        assert len(call_log) == 0, "run_cmd should not be called with invalid model"

    def test_accepted_task_id_returns_structured_result(self):
        result = project_run_mimo(_fake_run_cmd, project="test", task_id=TASK_ID)
        assert "task_id" in result
        assert result["task_id"] == TASK_ID

    def test_returns_structured_result_keys(self):
        result = project_run_mimo(_fake_run_cmd, project="test", task_id=TASK_ID)
        for key in ("task_id", "status", "exit_code", "stdout", "stderr", "started_at", "finished_at"):
            assert key in result, f"missing key: {key}"

    def test_status_needs_review_on_zero_exit(self):
        def ok_run_cmd(p, c):
            return {"stdout": "", "stderr": "", "exit_code": 0}
        result = project_run_mimo(ok_run_cmd, project="test", task_id=TASK_ID)
        assert result["status"] == "needs-review"

    def test_status_failed_on_nonzero_exit(self):
        def fail_run_cmd(p, c):
            return {"stdout": "", "stderr": "error", "exit_code": 1}
        result = project_run_mimo(fail_run_cmd, project="test", task_id=TASK_ID)
        assert result["status"] == "failed"
```

- [ ] **Step 8: Run all unit tests**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway && python3 -m pytest tests/test_mcp_mimo.py -v 2>&1
```
Expected: All unit tests pass (no binary required).

- [ ] **Step 9: Commit**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway
git add examples/mcp_server/mimo_tools.py tests/test_mcp_mimo.py
git commit -m "feat: add mimo_tools.py with project_run_mimo() and 11 pre-flight guards"
```

---

### Task 2: Register `gateway_project_run_mimo` in server.py + tool_modes.py

**Files:**
- Modify: `examples/mcp_server/server.py`
- Modify: `examples/mcp_server/tool_modes.py`

**Interfaces:**
- Consumes: `project_run_mimo` from `mimo_tools.py` (Task 1), `should_register_tool` from `tool_modes.py`
- Produces: Registered MCP tool `gateway_project_run_mimo` (visible only in chatgpt mode)

- [ ] **Step 1: Import `project_run_mimo` in server.py**

After the opencode import (line 63-65), add:

```python
from mimo_tools import (
    project_run_mimo as _project_run_mimo,
)
```

- [ ] **Step 2: Add `gateway_project_run_mimo` tool to server.py**

After the `project_run_opencode` block (around line 1195), add:

```python
@register_tool("gateway_project_run_mimo")
def gateway_project_run_mimo(
    project: str,
    task_id: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Execute an existing handoff task via Mimo CLI inside a disposable git worktree.
    Requires write mode handoff or full. See spec for 11 pre-flight guards.
    Mimo runs with --dangerously-skip-permissions — only valid in disposable worktrees."""
    from write_modes import assert_handoff_write_allowed
    assert_handoff_write_allowed()
    return run_tool(
        tool="gateway_project_run_mimo",
        title="Run mimo task",
        fn=lambda: _project_run_mimo(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            model=model,
        ),
        success_text="Submitted mimo task.",
    )
```

- [ ] **Step 3: Add `gateway_project_run_mimo` to chatgpt tool set in tool_modes.py**

After `"project_run_opencode"` (line 130), add a comma and:

```python
        "gateway_project_run_mimo",
```

- [ ] **Step 4: Add registration tests and MCP import test**

Add to `tests/test_mcp_mimo.py`:

```python
MIMO_BIN = os.getenv("MIMO_BIN") or shutil.which("mimo") or "/root/.mimocode/bin/mimo"


class TestToolRegistration:
    def test_registered_in_chatgpt_mode(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        import importlib
        import sys
        example_dir = EXAMPLE_DIR
        monkeypatch.syspath_prepend(str(example_dir))
        sys.modules.pop("tool_modes", None)
        tm = importlib.import_module("tool_modes")
        assert tm.should_register_tool("gateway_project_run_mimo") is True

    def test_visible_in_tools_for_chatgpt(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        import importlib
        import sys
        example_dir = EXAMPLE_DIR
        monkeypatch.syspath_prepend(str(example_dir))
        sys.modules.pop("tool_modes", None)
        tm = importlib.import_module("tool_modes")
        tools = tm.tools_for_mode()
        assert "gateway_project_run_mimo" in tools


class TestServerTool:
    @pytest.mark.skipif(
        not importlib.util.find_spec("mcp"),
        reason="mcp package not installed; only available with optional dependencies",
    )
    def test_tool_function_can_be_imported(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        import importlib
        import sys
        example_dir = EXAMPLE_DIR
        monkeypatch.syspath_prepend(str(example_dir))
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "handoff")
        monkeypatch.setenv("GITEA_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        for name in list(sys.modules):
            if "mimo_tools" in name or "mcp_server" in name or "tool_modes" in name:
                sys.modules.pop(name, None)
        server = importlib.import_module("server")
        tool = getattr(server, "gateway_project_run_mimo", None)
        assert tool is not None, "gateway_project_run_mimo not found in server module"
```

- [ ] **Step 5: Run registration + server tests**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway && python3 -m pytest tests/test_mcp_mimo.py -v 2>&1
```
Expected: All tests pass.

- [ ] **Step 6: Run full lint and check**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway && python3 -m ruff check examples/mcp_server/ tests/ && python3 -m mypy examples/mcp_server/mimo_tools.py 2>&1 | tail -10
```

- [ ] **Step 7: Commit**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway
git add examples/mcp_server/server.py examples/mcp_server/tool_modes.py tests/test_mcp_mimo.py
git commit -m "feat: register gateway_project_run_mimo MCP tool (chatgpt mode only)"
```

---

### Task 3: Push and verify CI

- [ ] **Step 1: Push to Gitea**

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway && git push gitea master
```

- [ ] **Step 2: Check CI status**

```bash
sleep 180 && curl --noproxy '*' -s "http://192.168.1.103:3005/api/v1/repos/gpakoh/agent-ssh-gateway/actions/runs?limit=1" -H "Authorization: token 46f10e23158e2da2ead68e5daed514c61b18af09"
```
Expected: success (all tests pass or known skips).

---

### Task 4: Push to GitHub (optional, for v0.1.0 tag)

- [ ] **Step 1: Push to GitHub master**

```bash
GIT_TOKEN=$(python3 -c "import yaml; print(yaml.safe_load(open('/root/.config/gh/hosts.yml'))['github.com']['oauth_token'])")
cd /media/1TB/Python/web_ssh/web-ssh-gateway
git push "https://gpakoh:${GIT_TOKEN}@github.com/gpakoh/agent-ssh-gateway.git" master
```
