# Agent Handoff v2 — Gateway Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 6 Gateway MCP tools for the Parallel Agent Handoff v2 protocol — `write_agent_task`, `read_agent_*`, `list_agent_tasks`, `archive_agent_task`.

**Architecture:** A new `agent_tasks.py` module provides core functions (write `task.json` + `current-plan.md`, read task files, list tasks, archive tasks), all executing through `run_project_command` via the SSH gateway. Six new `@register_tool` entries in `server.py` expose them as MCP tools.

**Tech Stack:** Python 3.11+, FastMCP, existing `run_project_command` pattern.

**Working directory:** `examples/mcp_server/` within the agent-ssh-gateway project.

## Global Constraints

- All tools follow the existing `@register_tool` + `run_tool` + `run_project_command` pattern.
- New tool names use prefix `gateway_project_` for project-scoped operations.
- Every tool that reads file content returns it as `text/plain` via `run_project_command`.
- `task_id` must match `[a-z0-9][a-z0-9-]{10,120}` — validate in the tool function.
- All shell commands execute through the existing SSH gateway — no new transport needed.
- `archive_agent_task` moves (not deletes) tasks to `.ai-bridge/archive/`.

---
### Task 1: Data model and path guards

**Files:**
- Create: `examples/mcp_server/agent_tasks.py` (constants + validate_task_id + helpers)
- Test: `tests/test_agent_tasks.py` (validation tests)

**Interfaces:**
- Produces: `TASK_ID_RE`, `TASKS_REL_DIR`, `ARCHIVE_REL_DIR`, `validate_task_id()`, `_task_dir()`, `_archive_dir()`, `_shell_escape()`

- [ ] **Step 1: Create `agent_tasks.py` with validation and path helpers**

```python
"""Agent Handoff v2 — .ai-bridge task management for parallel agent execution."""

from __future__ import annotations

import re
from typing import Any

TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{10,120}$")

TASKS_REL_DIR = ".ai-bridge/tasks"
ARCHIVE_REL_DIR = ".ai-bridge/archive"


def validate_task_id(task_id: str) -> None:
    """Raise ValueError if task_id is malformed."""
    if not TASK_ID_RE.match(task_id):
        raise ValueError(
            f"Invalid task_id: {task_id!r}. Must match {TASK_ID_RE.pattern}"
        )


def _task_dir(task_id: str) -> str:
    return f"{TASKS_REL_DIR}/{task_id}"


def _archive_dir(task_id: str) -> str:
    return f"{ARCHIVE_REL_DIR}/{task_id}"


def _shell_escape(text: str) -> str:
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"
```

- [ ] **Step 2: Write validation tests**

```python
"""Tests for Agent Handoff v2 — agent_tasks module."""

from __future__ import annotations

import pytest

from examples.mcp_server.agent_tasks import validate_task_id


class TestValidateTaskId:
    def test_valid_ids(self):
        for tid in [
            "2026-06-24-stage-12-15a-rag-search-chunks-opencode",
            "a12345678901",
            "fix-test-flake-auth-mimo",
        ]:
            validate_task_id(tid)

    def test_invalid_ids(self):
        for tid in ["", "too-short", "UPPERCASE", "has spaces", "\xe4", None]:
            with pytest.raises((ValueError, TypeError)):
                validate_task_id(tid)  # type: ignore[arg-type]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_agent_tasks.py -v -x 2>&1 | head -20`
Expected: ModuleNotFoundError (no agent_tasks.py yet)

- [ ] **Step 4: Create the file, run tests to verify pass**

```bash
pytest tests/test_agent_tasks.py -v -x
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add examples/mcp_server/agent_tasks.py tests/test_agent_tasks.py
git commit -m "feat(agent-tasks): add data model and path guards"
```

---

### Task 2: write\_agent\_task function

**Files:**
- Modify: `examples/mcp_server/agent_tasks.py` (add `build_task_json`, `build_current_plan`, `write_agent_task`)
- Modify: `tests/test_agent_tasks.py` (add tests)

**Interfaces:**
- Produces: `build_task_json()`, `build_current_plan()`, `write_agent_task()`

- [ ] **Step 1: Add builder functions + `write_agent_task` to `agent_tasks.py`**

Add after the path helpers:

```python
import json
from datetime import UTC, datetime


def build_task_json(
    *,
    task_id: str,
    agent: str,
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    required_checks: list[str] | None = None,
    worktree_path: str | None = None,
    commit_allowed: bool = False,
    push_allowed: bool = False,
) -> str:
    """Build machine-readable task.json content."""
    validate_task_id(task_id)
    data: dict[str, Any] = {
        "task_id": task_id,
        "agent": agent,
        "allowed_files": allowed_files or [],
        "forbidden_files": forbidden_files or [],
        "required_checks": required_checks or [],
        "worktree_path": worktree_path or "",
        "commit_allowed": commit_allowed,
        "push_allowed": push_allowed,
        "created": datetime.now(UTC).isoformat(),
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def build_initial_status(agent: str, task_id: str) -> str:
    """Build initial agent-status.md with Status: created."""
    return (
        f"Status: created\n\n"
        f"## Task\n\n"
        f"- Task ID: {task_id}\n"
        f"- Agent: {agent}\n"
        f"- Started: {datetime.now(UTC).isoformat()}\n\n"
        f"## Progress\n\n"
        f"Task created, awaiting executor.\n"
    )


def build_current_plan(
    *,
    task_id: str,
    task: str,
    scope: str = "",
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    required_checks: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    commit_message: str | None = None,
    constraints: str | None = None,
) -> str:
    """Build human-readable current-plan.md content."""
    validate_task_id(task_id)
    allow = "\n".join(f"- {f}" for f in (allowed_files or []))
    forbid = "\n".join(f"- {f}" for f in (forbidden_files or []))
    checks = "\n".join(f"- `{c}`" for c in (required_checks or []))
    criteria = "\n".join(f"- {c}" for c in (acceptance_criteria or []))
    notes = f"\n## Constraints\n\n{constraints}\n" if constraints else ""

    return (
        f"# {task}\n\n"
        f"## Metadata\n\n"
        f"- Task ID: {task_id}\n"
        f"- Created: {datetime.now(UTC).isoformat()}\n\n"
        f"## Scope\n\n{scope}\n\n"
        f"## Allowed files\n\n{allow}\n\n"
        f"## Forbidden\n\n{forbid}\n\n"
        f"## Required checks\n\n{checks}\n\n"
        f"## Acceptance criteria\n\n{criteria}\n"
        + (f"\n## Commit message\n\n```\n{commit_message}\n```\n" if commit_message else "")
        + notes
        + "\n## Agent instructions\n\n"
        + "Read this plan and execute it in small, reviewable steps.\n"
        + "After each meaningful change, update `.ai-bridge/tasks/{task_id}/agent-status.md`.\n"
        + f"Save final diff to `.ai-bridge/tasks/{task_id}/implementation-diff.patch`.\n"
        + "Do not commit or push unless explicitly instructed.\n"
    )


def write_agent_task(
    run_cmd, *,
    project: str,
    task_id: str,
    agent: str,
    task: str,
    scope: str = "",
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    required_checks: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    commit_message: str | None = None,
    constraints: str | None = None,
    worktree_path: str | None = None,
) -> dict[str, Any]:
    """Write task.json + current-plan.md + agent-status.md to .ai-bridge/tasks/<task_id>/.

    Also writes worktree-path.txt if worktree_path is provided.
    """
    validate_task_id(task_id)

    task_json = build_task_json(
        task_id=task_id,
        agent=agent,
        allowed_files=allowed_files,
        forbidden_files=forbidden_files,
        required_checks=required_checks,
        worktree_path=worktree_path,
    )
    current_plan = build_current_plan(
        task_id=task_id,
        task=task,
        scope=scope,
        allowed_files=allowed_files,
        forbidden_files=forbidden_files,
        required_checks=required_checks,
        acceptance_criteria=acceptance_criteria,
        commit_message=commit_message,
        constraints=constraints,
    )
    initial_status = build_initial_status(agent=agent, task_id=task_id)

    td = _task_dir(task_id)
    parts = [
        f"mkdir -p {td}",
        f"cat > {td}/task.json << 'JEOF'\n{task_json}\nJEOF",
        f"cat > {td}/current-plan.md << 'PEOF'\n{current_plan}\nPEOF",
        f"cat > {td}/agent-status.md << 'SEOF'\n{initial_status}\nSEOF",
    ]
    if worktree_path:
        parts.append(
            f"cat > {td}/worktree-path.txt << 'WEOF'\n{worktree_path}\nWEOF"
        )
    cmd = " && ".join(parts)
    return run_cmd(project, cmd)
```

- [ ] **Step 2: Add tests**

Add to `tests/test_agent_tasks.py`:

```python
import json

from examples.mcp_server.agent_tasks import (
    build_current_plan,
    build_initial_status,
    build_task_json,
)


class TestBuildTaskJson:
    def test_minimal(self):
        result = build_task_json(task_id="a12345678901", agent="opencode")
        data = json.loads(result)
        assert data["task_id"] == "a12345678901"
        assert data["agent"] == "opencode"
        assert data["allowed_files"] == []
        assert data["commit_allowed"] is False
        assert "created" in data

    def test_full(self):
        result = build_task_json(
            task_id="b23456789012",
            agent="mimo",
            allowed_files=["src/**", "tests/**"],
            forbidden_files=["migrations/**"],
            required_checks=["pytest -q", "ruff check"],
            worktree_path="../agent-worktrees/task-b",
            commit_allowed=False,
            push_allowed=False,
        )
        data = json.loads(result)
        assert data["agent"] == "mimo"
        assert "src/**" in data["allowed_files"]
        assert data["required_checks"] == ["pytest -q", "ruff check"]


class TestBuildInitialStatus:
    def test_created_status(self):
        result = build_initial_status(agent="opencode", task_id="a12345678901")
        assert "Status: created" in result
        assert "opencode" in result
        assert "a12345678901" in result

    def test_different_agent(self):
        result = build_initial_status(agent="mimo", task_id="b23456789012")
        assert "Status: created" in result
        assert "mimo" in result


class TestBuildCurrentPlan:
    def test_minimal(self):
        result = build_current_plan(task_id="c34567890123", task="Fix tests")
        assert "# Fix tests" in result
        assert "c34567890123" in result
        assert "implementation-diff.patch" in result
        assert "Do not commit or push" in result

    def test_full(self):
        result = build_current_plan(
            task_id="d45678901234",
            task="Add search chunks",
            scope="UI only",
            allowed_files=["father-ui/src/**"],
            forbidden_files=["app/**"],
            required_checks=["pytest -q"],
            acceptance_criteria=["Build passes", "Tests pass"],
            commit_message="polish: improve RAG search",
            constraints="No model changes",
        )
        assert "## Scope" in result
        assert "father-ui/src/**" in result
        assert "app/**" in result
        assert "polish: improve RAG search" in result
        assert "No model changes" in result
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_agent_tasks.py -v -x
```
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add examples/mcp_server/agent_tasks.py tests/test_agent_tasks.py
git commit -m "feat(agent-tasks): add write_agent_task and builders"
```

---

### Task 3: read\_agent\_task\_file function

**Files:**
- Modify: `examples/mcp_server/agent_tasks.py` (add `read_agent_task_file`)
- Modify: `tests/test_agent_tasks.py` (add tests)

**Interfaces:**
- Produces: `read_agent_task_file()`

- [ ] **Step 1: Add `read_agent_task_file` to `agent_tasks.py`**

```python
def read_agent_task_file(run_cmd, *, project: str, task_id: str, filename: str) -> dict[str, Any]:
    """Read a file from .ai-bridge/tasks/<task_id>/ via shell.

    run_cmd is a callable(project, command) that executes a shell command
    and returns dict with at least {'stdout': str, 'stderr': str, 'exit_code': int}.
    """
    validate_task_id(task_id)
    cmd = f"cat {_task_dir(task_id)}/{filename} 2>/dev/null || echo '(not found)'"
    return run_cmd(project, cmd)
```

- [ ] **Step 2: Add unit test**

```python
class TestReadAgentTaskFile:
    def test_returns_callable_result(self):
        """Verify read_agent_task_file passes args to run_cmd correctly."""
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "file content", "stderr": "", "exit_code": 0}

        result = read_agent_task_file(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            filename="agent-status.md",
        )
        assert result["stdout"] == "file content"
        assert len(calls) == 1
        assert calls[0][0] == "my-proj"
        assert "a12345678901/agent-status.md" in calls[0][1]
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_agent_tasks.py -v -x
```
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add examples/mcp_server/agent_tasks.py tests/test_agent_tasks.py
git commit -m "feat(agent-tasks): add read_agent_task_file"
```

---

### Task 4: list\_agent\_tasks function

**Files:**
- Modify: `examples/mcp_server/agent_tasks.py` (add `list_agent_tasks`)
- Modify: `tests/test_agent_tasks.py` (add tests)

**Interfaces:**
- Produces: `list_agent_tasks()`

- [ ] **Step 1: Add `list_agent_tasks` to `agent_tasks.py`**

```python
def list_agent_tasks(run_cmd, *, project: str) -> dict[str, Any]:
    """List task directories under .ai-bridge/tasks/."""
    cmd = (
        f"echo '## Tasks' && "
        f"ls -1 {TASKS_REL_DIR}/ 2>/dev/null | head -50 || echo '(no tasks)'"
    )
    return run_cmd(project, cmd)
```

- [ ] **Step 2: Add unit test**

```python
class TestListAgentTasks:
    def test_passes_project(self):
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "## Tasks\ntask-1\ntask-2", "stderr": "", "exit_code": 0}

        result = list_agent_tasks(fake_run_cmd, project="my-proj")
        assert calls[0][0] == "my-proj"
        assert ".ai-bridge/tasks/" in calls[0][1]
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_agent_tasks.py -v -x
```
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add examples/mcp_server/agent_tasks.py tests/test_agent_tasks.py
git commit -m "feat(agent-tasks): add list_agent_tasks"
```

---

### Task 5: archive\_agent\_task function

**Files:**
- Modify: `examples/mcp_server/agent_tasks.py` (add `archive_agent_task`)
- Modify: `tests/test_agent_tasks.py` (add tests)

**Interfaces:**
- Produces: `archive_agent_task()`

- [ ] **Step 1: Add `archive_agent_task` to `agent_tasks.py`**

```python
def archive_agent_task(run_cmd, *, project: str, task_id: str) -> dict[str, Any]:
    """Move .ai-bridge/tasks/<task_id>/ -> .ai-bridge/archive/<task_id>/.

    Move, not delete — physical deletion is never performed by this tool.
    """
    validate_task_id(task_id)
    cmd = (
        f"mkdir -p {ARCHIVE_REL_DIR} && "
        f"mv {_task_dir(task_id)} {_archive_dir(task_id)} 2>/dev/null "
        f"&& echo 'archived {task_id}' || echo 'task {task_id} not found'"
    )
    return run_cmd(project, cmd)
```

- [ ] **Step 2: Add unit test**

```python
class TestArchiveAgentTask:
    def test_passes_project_and_task_id(self):
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "archived a12345678901", "stderr": "", "exit_code": 0}

        result = archive_agent_task(
            fake_run_cmd, project="my-proj", task_id="a12345678901"
        )
        assert result["stdout"] == "archived a12345678901"
        assert ".ai-bridge/archive/" in calls[0][1]
        assert "mv" in calls[0][1]

    def test_invalid_task_id_raises(self):
        with pytest.raises(ValueError):
            archive_agent_task(lambda p, c: {}, project="p", task_id="bad")
```

Add the import at top of test file:

```python
from examples.mcp_server.agent_tasks import (
    archive_agent_task,
    build_current_plan,
    build_initial_status,
    build_task_json,
    list_agent_tasks,
    read_agent_task_file,
    validate_task_id,
)
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_agent_tasks.py -v -x
```
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add examples/mcp_server/agent_tasks.py tests/test_agent_tasks.py
git commit -m "feat(agent-tasks): add archive_agent_task"
```

---

### Task 6: MCP tool registration in server.py + tool_modes.py

**Files:**
- Modify: `examples/mcp_server/server.py` (add 6 new `@register_tool` entries + `_split_lines` helper)
- Modify: `examples/mcp_server/tool_modes.py` (add tool names to chatgpt set)

**Interfaces:**
- Consumes: all functions from `agent_tasks.py`
- Produces: 6 registered Gateway MCP tools in chatgpt mode

- [ ] **Step 1: Add import to `server.py`**

```python
from agent_tasks import (
    archive_agent_task as _archive_agent_task,
    list_agent_tasks as _list_agent_tasks,
    read_agent_task_file as _read_agent_task_file,
    validate_task_id,
    write_agent_task as _write_agent_task,
)
```

- [ ] **Step 2: Add `_split_lines` helper near other helpers**

```python
def _split_lines(value: str | None) -> list[str] | None:
    """Split newline-separated string into list, or return None."""
    if value is None:
        return None
    return [line.strip() for line in value.split("\n") if line.strip()]
```

- [ ] **Step 3: Register 6 tools at the end of `server.py` (before `if __name__`)**

```python
# ── Agent Handoff v2 tools ──────────────────────────────────────────


@register_tool("gateway_project_write_agent_task")
def gateway_project_write_agent_task(
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
) -> dict[str, Any]:
    """Write task.json + current-plan.md to .ai-bridge/tasks/<task_id>/."""
    def _fn() -> dict[str, Any]:
        return _write_agent_task(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            agent=agent,
            task=task,
            scope=scope,
            allowed_files=_split_lines(allowed_files),
            forbidden_files=_split_lines(forbidden_files),
            required_checks=_split_lines(required_checks),
            acceptance_criteria=_split_lines(acceptance_criteria),
            commit_message=commit_message,
            constraints=constraints,
            worktree_path=worktree_path,
        )
    return run_tool(
        tool="gateway_project_write_agent_task",
        title="Write agent task",
        fn=_fn,
        success_text="Wrote agent task.",
    )


@register_tool("gateway_project_read_agent_status")
def gateway_project_read_agent_status(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-status.md."""
    return run_tool(
        tool="gateway_project_read_agent_status",
        title="Read agent status",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project, task_id=task_id, filename="agent-status.md",
        ),
        success_text="Read agent status.",
    )


@register_tool("gateway_project_read_agent_report")
def gateway_project_read_agent_report(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-report.md."""
    return run_tool(
        tool="gateway_project_read_agent_report",
        title="Read agent report",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project, task_id=task_id, filename="agent-report.md",
        ),
        success_text="Read agent report.",
    )


@register_tool("gateway_project_read_agent_diff")
def gateway_project_read_agent_diff(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/implementation-diff.patch."""
    return run_tool(
        tool="gateway_project_read_agent_diff",
        title="Read agent diff",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project, task_id=task_id, filename="implementation-diff.patch",
        ),
        success_text="Read agent diff.",
    )


@register_tool("gateway_project_list_agent_tasks")
def gateway_project_list_agent_tasks(project: str) -> dict[str, Any]:
    """List task directories under .ai-bridge/tasks/."""
    return run_tool(
        tool="gateway_project_list_agent_tasks",
        title="List agent tasks",
        fn=lambda: _list_agent_tasks(
            lambda p, c: run_project_command(client, p, c),
            project=project,
        ),
        success_text="Listed agent tasks.",
    )


@register_tool("gateway_project_archive_agent_task")
def gateway_project_archive_agent_task(project: str, task_id: str) -> dict[str, Any]:
    """Move .ai-bridge/tasks/<task_id>/ -> .ai-bridge/archive/<task_id>/."""
    return run_tool(
        tool="gateway_project_archive_agent_task",
        title="Archive agent task",
        fn=lambda: _archive_agent_task(
            lambda p, c: run_project_command(client, p, c),
            project=project, task_id=task_id,
        ),
        success_text="Archived agent task.",
    )
```

- [ ] **Step 4: Register tool names in `tool_modes.py`**

In the `chatgpt` set, after the v1 handoff tools, add:

```python
        "gateway_project_write_agent_task",
        "gateway_project_read_agent_status",
        "gateway_project_read_agent_report",
        "gateway_project_read_agent_diff",
        "gateway_project_list_agent_tasks",
        "gateway_project_archive_agent_task",
```

- [ ] **Step 5: Verify compilation**

```bash
python -m compileall examples/mcp_server/ -q
echo "compileall exit: $?"
```
Expected: exit 0.

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_agent_tasks.py -v -x
pytest tests/test_mcp_handoff.py -v -x
```
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add examples/mcp_server/server.py examples/mcp_server/tool_modes.py
git commit -m "feat: add 6 Agent Handoff v2 MCP tools to gateway"
```

**Files:**
- Modify: `examples/mcp_server/server.py`
- Modify: `examples/mcp_server/tool_modes.py`

**Interfaces:**
- Consumes: `agent_tasks.py` functions — `write_agent_task`, `read_agent_task_file`, `list_agent_tasks`, `archive_agent_task`
- Produces: 6 registered MCP tools visible in `chatgpt` mode

- [ ] **Step 1: Add import in `server.py`**

Insert after existing handoff imports:

```python
from agent_tasks import (
    archive_agent_task as _archive_agent_task,
    list_agent_tasks as _list_agent_tasks,
    read_agent_task_file as _read_agent_task_file,
    validate_task_id,
    write_agent_task as _write_agent_task,
)
```

- [ ] **Step 2: Register tools at the end of `server.py` (before the `if __name__` block)**

```python
# ── Agent Handoff v2 tools ──────────────────────────────────────────


@register_tool("gateway_project_write_agent_task")
def gateway_project_write_agent_task(
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
) -> dict[str, Any]:
    """Write task.json + current-plan.md to .ai-bridge/tasks/<task_id>/."""
    def _fn() -> dict[str, Any]:
        return _write_agent_task(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            agent=agent,
            task=task,
            scope=scope,
            allowed_files=_split_lines(allowed_files),
            forbidden_files=_split_lines(forbidden_files),
            required_checks=_split_lines(required_checks),
            acceptance_criteria=_split_lines(acceptance_criteria),
            commit_message=commit_message,
            constraints=constraints,
            worktree_path=worktree_path,
        )
    return run_tool(
        tool="gateway_project_write_agent_task",
        title="Write agent task",
        fn=_fn,
        success_text="Wrote agent task.",
    )


@register_tool("gateway_project_read_agent_status")
def gateway_project_read_agent_status(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-status.md."""
    return run_tool(
        tool="gateway_project_read_agent_status",
        title="Read agent status",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project, task_id=task_id, filename="agent-status.md",
        ),
        success_text="Read agent status.",
    )


@register_tool("gateway_project_read_agent_report")
def gateway_project_read_agent_report(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/agent-report.md."""
    return run_tool(
        tool="gateway_project_read_agent_report",
        title="Read agent report",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project, task_id=task_id, filename="agent-report.md",
        ),
        success_text="Read agent report.",
    )


@register_tool("gateway_project_read_agent_diff")
def gateway_project_read_agent_diff(project: str, task_id: str) -> dict[str, Any]:
    """Read .ai-bridge/tasks/<task_id>/implementation-diff.patch."""
    return run_tool(
        tool="gateway_project_read_agent_diff",
        title="Read agent diff",
        fn=lambda: _read_agent_task_file(
            lambda p, c: run_project_command(client, p, c),
            project=project, task_id=task_id, filename="implementation-diff.patch",
        ),
        success_text="Read agent diff.",
    )


@register_tool("gateway_project_list_agent_tasks")
def gateway_project_list_agent_tasks(project: str) -> dict[str, Any]:
    """List task directories under .ai-bridge/tasks/."""
    return run_tool(
        tool="gateway_project_list_agent_tasks",
        title="List agent tasks",
        fn=lambda: _list_agent_tasks(
            lambda p, c: run_project_command(client, p, c),
            project=project,
        ),
        success_text="Listed agent tasks.",
    )


@register_tool("gateway_project_archive_agent_task")
def gateway_project_archive_agent_task(project: str, task_id: str) -> dict[str, Any]:
    """Move .ai-bridge/tasks/<task_id>/ -> .ai-bridge/archive/<task_id>/."""
    return run_tool(
        tool="gateway_project_archive_agent_task",
        title="Archive agent task",
        fn=lambda: _archive_agent_task(
            lambda p, c: run_project_command(client, p, c),
            project=project, task_id=task_id,
        ),
        success_text="Archived agent task.",
    )
```

- [ ] **Step 3: Add `_split_lines` helper in `server.py`**

Find a place near the other helpers (around line 567 area) and add:

```python
def _split_lines(value: str | None) -> list[str] | None:
    """Split newline-separated string into list, or return None."""
    if value is None:
        return None
    return [line.strip() for line in value.split("\n") if line.strip()]
```

- [ ] **Step 4: Register tool names in `tool_modes.py`**

In the `chatgpt` set, after the v1 handoff tools, add:

```python
        "gateway_project_write_agent_task",
        "gateway_project_read_agent_status",
        "gateway_project_read_agent_report",
        "gateway_project_read_agent_diff",
        "gateway_project_list_agent_tasks",
        "gateway_project_archive_agent_task",
```

- [ ] **Step 5: Verify compilation**

```bash
python -m compileall examples/mcp_server/ -q
echo "compileall exit: $?"
```

Expected: exit 0.

- [ ] **Step 6: Run existing tests**

```bash
pytest tests/test_agent_tasks.py -v -x
pytest tests/test_mcp_handoff.py -v -x
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add examples/mcp_server/server.py examples/mcp_server/tool_modes.py
git commit -m "feat: add 6 Agent Handoff v2 MCP tools"
```
