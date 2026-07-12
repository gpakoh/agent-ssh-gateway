# Phase 1 — Contract & Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Phase 1 of the MCP Gateway Contract spec: uniform envelope, tool renaming, Python runners via `uv`, safe glob, compose fixes, confirmation, latency measurement.

**Architecture:** All Phase 1 changes touch the existing MCP server in `examples/mcp_server/` and the fleet Docker client in `examples/chatgpt_remote_mcp/fleet/`. Core envelope logic lives in `tool_results.py`. Tool registration and renaming spans `server.py`, `tool_modes.py`, `tool_scopes.py`. Python runners are rewritten in `chatgpt_tools.py` to use `uv` via SSH. Docker compose `file_path` removal touches `docker_client.py`. Confirmation reuses the existing `ConfirmStore` in `docker_confirm.py`.

**Tech Stack:** Python 3.11+, FastMCP, Paramiko (SSH), `uv`, pytest, asyncio

## Global Constraints

- All tools return uniform `ok`/`result`/`error`/`meta` envelope (Contract v1)
- Maximum tool name length: 48 characters, `snake_case`, no `gateway_` prefix, no hashes
- Python runners use `uv run --frozen --directory <project> <tool>` via SSH — lock file required
- `target` is an array of paths, never a string; built as argv, never shell
- `find_files` uses `pathlib.Path.glob()` (not `rglob()`), excludes `.git`/`.venv`/`node_modules`/`__pycache__`, max 200 results, 5s timeout
- Docker compose tools accept `project_dir` only — no `file_path` parameter
- Two-phase confirmation: `request_*` returns `outcome: "pending_confirmation"` with one-time token; `confirm_operation(token)` executes
- `meta.duration_ms` = total MCP call time; `result.execution_duration_ms` = remote command time
- Destructive operations without confirmation are disabled in Phase 1

---

### Task 1: Response Envelope Refactor

**Files:**
- Modify: `examples/mcp_server/tool_results.py`
- Modify: `examples/mcp_server/server.py` (all `@register_tool` functions)
- Modify: `examples/mcp_server/chatgpt_tools.py` (all return paths)
- Modify: `tests/test_tool_results.py`
- Modify: `tests/test_gateway_envelope.py`

**Interfaces:**
- Consumes: `tool_success(data)`, `tool_error(code, message, hint, retryable, details)` from `tool_results.py`
- Produces: uniform envelope dict with `ok`, `result`, `error`, `meta` keys; `meta` includes `contract_version`, `tool`, `request_id`, `duration_ms`, `truncated`, `warnings`

- [ ] **Step 1: Write the failing tests for envelope contract**

Add tests in `tests/test_tool_results.py`:

```python
def test_tool_success_envelope():
    result = tool_success({"outcome": "passed"})
    assert result["ok"] is True
    assert result["result"]["outcome"] == "passed"
    assert result["error"] is None
    assert result["meta"]["contract_version"] == "1"

def test_tool_error_envelope():
    result = tool_error("DEPENDENCY_MISSING", "uv not found", "Install uv", False, {"required_binary": "uv"})
    assert result["ok"] is False
    assert result["result"] is None
    assert result["error"]["code"] == "DEPENDENCY_MISSING"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["required_binary"] == "uv"

def test_meta_always_present():
    result = tool_success({"outcome": "passed"})
    assert "contract_version" in result["meta"]
    assert "tool" in result["meta"]
    assert "request_id" in result["meta"]
    assert "duration_ms" in result["meta"]
    assert "truncated" in result["meta"]
    assert "warnings" in result["meta"]

def test_chkecks_failed_not_error():
    """Non-zero exit from a check tool is ok:true, outcome:failed — NOT an error."""
    result = tool_success({"outcome": "failed", "exit_code": 1, "stdout": "", "stderr": "lint errors"})
    assert result["ok"] is True
    assert result["error"] is None
    assert result["result"]["outcome"] == "failed"

def test_meta_duration_ms_tracks_total_time():
    import time
    start = time.time()
    result = tool_success({"outcome": "passed"})
    elapsed = int((time.time() - start) * 1000)
    assert result["meta"]["duration_ms"] <= elapsed + 5  # allow small skew
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tool_results.py::test_tool_success_envelope tests/test_tool_results.py::test_tool_error_envelope tests/test_tool_results.py::test_meta_always_present tests/test_tool_results.py::test_chkecks_failed_not_error tests/test_tool_results.py::test_meta_duration_ms_tracks_total_time -v`
Expected: 5 FAILED

- [ ] **Step 3: Rewrite `tool_success()` and `tool_error()` in `tool_results.py`**

Current `tool_results.py` has these functions. Replace them:

```python
import time
import uuid

CONTRACT_VERSION = "1"

def _make_meta(tool_name: str | None = None) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "tool": tool_name or "unknown",
        "request_id": str(uuid.uuid4()),
        "duration_ms": 0,
        "truncated": False,
        "warnings": [],
    }

def tool_success(data: Any, tool_name: str | None = None) -> dict:
    meta = _make_meta(tool_name)
    return {
        "ok": True,
        "result": data,
        "error": None,
        "meta": meta,
    }

def tool_error(
    code: str,
    message: str,
    hint: str | None = None,
    retryable: bool = False,
    details: dict | None = None,
    tool_name: str | None = None,
) -> dict:
    meta = _make_meta(tool_name)
    error = {"code": code, "message": message}
    if hint:
        error["hint"] = hint
    error["retryable"] = retryable
    if details:
        error["details"] = details
    return {
        "ok": False,
        "result": None,
        "error": error,
        "meta": meta,
    }
```

Keep existing `text_result()` and `error_result()` as deprecated wrappers that delegate to the new functions.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tool_results.py -v`
Expected: tests pass

- [ ] **Step 5: Add `request_id` header logging in server.py**

In `server.py`, add a middleware or decorator that sets `request_id` on the MCP context so each tool call can log with it. The `request_id` from `meta` should appear in log lines for correlation.

- [ ] **Step 6: Update server.py — pass tool name into envelope**

Find every function decorated with `@register_tool` in `server.py`. Instead of returning raw dict, use:

```python
# Before
return {"status": "ok", ...}

# After
return tool_success({"status": "ok", ...}, tool_name="health")

# Error case
return tool_error("PROJECT_NOT_FOUND", "Project not found", ...)
```

Key functions to update: `gateway_health`, `docker_ps`, `docker_images`, `docker_inspect`, `docker_logs`, `docker_stats`, `docker_compose_ps`, `docker_compose_services`, `docker_start`, `docker_stop`, `docker_restart`, etc.

- [ ] **Step 7: Audit all error paths in server.py and chatgpt_tools.py**

Every `raise` or raw exception return must be caught and converted to `tool_error()`. Add a catch-all wrapper if not already present.

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -q`
Expected: same results as before (1077 pass, 3 pre-existing auth failures)

- [ ] **Step 9: Commit**

```bash
git add examples/mcp_server/tool_results.py examples/mcp_server/server.py examples/mcp_server/chatgpt_tools.py tests/test_tool_results.py
git commit -m "feat: uniform response envelope (Contract v1)

tool_success/tool_error now return ok/result/error/meta structure.
meta includes contract_version, tool, request_id, duration_ms.
All tools updated to use envelope. Error paths caught."
```

---

### Task 2: Standardised Command Result

**Files:**
- Modify: `examples/mcp_server/tool_results.py`
- Modify: `examples/mcp_server/chatgpt_tools.py`
- Modify: `examples/mcp_server/gateway_client.py`
- Modify: `tests/test_tool_results.py`
- Create: `tests/test_command_result.py`

**Interfaces:**
- Consumes: `tool_success()` from Task 1
- Produces: `build_command_result(outcome, exit_code, stdout, stderr, execution_duration_ms, job_id, timestamps)` helper

- [ ] **Step 1: Write tests for command result shape**

Create `tests/test_command_result.py`:

```python
from mcp_server.tool_results import tool_success, build_command_result


def test_command_result_shape():
    data = build_command_result(
        outcome="passed",
        exit_code=0,
        stdout="ok",
        stderr="",
        execution_duration_ms=842,
        job_id=None,
        timestamps={"created": "2026-07-12T12:00:00Z", "started": None, "finished": None},
    )
    result = tool_success(data)
    r = result["result"]
    assert r["outcome"] == "passed"
    assert r["exit_code"] == 0
    assert r["stdout"] == "ok"
    assert r["execution_duration_ms"] == 842
    assert r["job_id"] is None

def test_command_result_failed_outcome():
    data = build_command_result(
        outcome="failed",
        exit_code=1,
        stdout="",
        stderr="lint error",
        execution_duration_ms=50,
    )
    r = tool_success(data)["result"]
    assert r["outcome"] == "failed"
    assert r["exit_code"] == 1

def test_command_result_completed_outcome():
    data = build_command_result(outcome="completed", exit_code=0, stdout="done", stderr="")
    r = tool_success(data)["result"]
    assert r["outcome"] == "completed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_command_result.py -v`
Expected: 3 FAILED

- [ ] **Step 3: Implement `build_command_result()` in `tool_results.py`**

Add to `tool_results.py`:

```python
def build_command_result(
    outcome: str,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    execution_duration_ms: int | None = None,
    job_id: str | None = None,
    timestamps: dict | None = None,
) -> dict:
    result = {
        "outcome": outcome,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }
    if execution_duration_ms is not None:
        result["execution_duration_ms"] = execution_duration_ms
    if job_id is not None:
        result["job_id"] = job_id
    if timestamps:
        result["timestamps"] = timestamps
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_command_result.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Update chatgpt_tools.py to use build_command_result**

Replace all raw return dicts from functions like `project_run_pytest`, `project_run_ruff`, `project_run_mypy`, `project_run_compileall`, `project_run_tests`, `project_run_lint` to use `build_command_result()`.

- [ ] **Step 6: Update gateway_client.py to preserve execution_duration_ms**

The `GatewayClient.execute_restricted()` and `execute_project_command()` methods should parse and forward `execution_duration_ms` from the gateway response into the result.

- [ ] **Step 7: Commit**

```bash
git add examples/mcp_server/tool_results.py examples/mcp_server/chatgpt_tools.py examples/mcp_server/gateway_client.py tests/test_command_result.py
git commit -m "feat: standardized command result shape

build_command_result() returns outcome/exit_code/stdout/stderr.
execution_duration_ms tracks wall-clock time on target.
meta.duration_ms tracks total MCP call time."
```

---

### Task 3: Tool Renaming

**Files:**
- Modify: `examples/mcp_server/server.py` (decorator names + function names)
- Modify: `examples/mcp_server/chatgpt_tools.py` (function names)
- Modify: `examples/mcp_server/tool_modes.py` (`TOOL_NAMES_BY_MODE`)
- Modify: `examples/mcp_server/tool_scopes.py` (`TOOL_SCOPES`)
- Modify: `examples/mcp_server/tools_manifest.py`
- Modify: `tests/test_tool_modes.py`
- Modify: `tests/test_mcp_server.py`

**Interfaces:**
- Produces: renamed tools in `tool_modes.py`, `tool_scopes.py`, `server.py`

- [ ] **Step 1: Build rename mapping**

Create a test that validates the mapping:

```python
RENAME_MAP = {
    "gateway_project_arch_ea87598b0874": "project_archive_task",
    "gateway_project_handoff_status_*": "project_handoff_status",
    "gateway_project_write_plan_*": "project_write_plan",
    "gateway_project_run_ruff": "project_run_ruff",
    "gateway_project_run_pytest": "project_run_pytest",
    "gateway_project_run_mypy": "project_run_mypy",
    "gateway_project_run_compileall": "project_run_compileall",
    "gateway_project_read_file": "project_read_file",
    "gateway_project_show": "project_show",
    "gateway_project_struct": "project_tree",
    "gateway_project_glob": "project_find_files",
    "gateway_health": "health",
}
```

- [ ] **Step 2: Update `tool_scopes.py` — replace old names with new names**

Change all keys in `TOOL_SCOPES` dict to new names. The old names become stale — remove them.

- [ ] **Step 3: Update `tool_modes.py` — replace old names in `TOOL_NAMES_BY_MODE`**

Change all old tool names to new ones across all modes (`minimal`, `standard`, `full`, `chatgpt`).

- [ ] **Step 4: Update `server.py` — rename `@register_tool()` arguments**

Change `@register_tool("gateway_health")` to `@register_tool("health")`, etc. Keep the function name the same (Python function names don't need to match tool names).

- [ ] **Step 5: Verify no stale names exist**

Run: `grep -r 'gateway_project_' examples/mcp_server/` — should return only comments or docs references.
Run: `grep -r 'gateway_health' examples/mcp_server/` — should return only the `health` section docstring in the spec.

- [ ] **Step 6: Run tests**

Run: `pytest tests/ -q`
Expected: 1077 pass, 3 pre-existing auth failures. Fix any test that referenced old names.

- [ ] **Step 7: Commit**

```bash
git add examples/mcp_server/server.py examples/mcp_server/tool_modes.py examples/mcp_server/tool_scopes.py examples/mcp_server/tools_manifest.py
git commit -m "feat: rename tools — remove hashes and gateway_ prefix

All tools follow domain_action[_subject] convention (max 48 chars).
gateway_health → health. project_ prefix maintained for project tools."
```

---

### Task 4: Project Name Resolution (Minimal Allowlist)

**Files:**
- Create: `examples/mcp_server/project_registry.py`
- Modify: `examples/mcp_server/chatgpt_tools.py`
- Modify: `examples/mcp_server/server.py`
- Create: `tests/test_project_registry.py`

**Interfaces:**
- Produces: `ProjectRegistry` class with `resolve(name) -> Path` method
- Consumes: shared config `allowed_roots` and `project_name -> project_dir` mapping
- Errors: `PROJECT_NOT_FOUND` for unknown name, `POLICY_DENIED` for symlink escape

- [ ] **Step 1: Write tests**

Create `tests/test_project_registry.py`:

```python
import pytest
from pathlib import Path
from mcp_server.project_registry import ProjectRegistry


@pytest.fixture
def registry():
    return ProjectRegistry(
        projects={
            "web-ssh-gateway": "/media/1TB/Python/web_ssh/web-ssh-gateway",
        },
        allowed_roots=["/media/1TB/Python/"],
    )


def test_resolve_known_project(registry):
    path = registry.resolve("web-ssh-gateway")
    assert path == Path("/media/1TB/Python/web_ssh/web-ssh-gateway")


def test_resolve_unknown_project(registry):
    with pytest.raises(ValueError, match="PROJECT_NOT_FOUND"):
        registry.resolve("nonexistent")


def test_resolve_symlink_escape(tmp_path, registry):
    root = tmp_path / "allowed"
    root.mkdir()
    escape = tmp_path / "escape"
    escape.mkdir()
    link = root / "link"
    link.symlink_to(escape, target_is_directory=True)

    bad_registry = ProjectRegistry({"evil": str(link)}, allowed_roots=[str(root)])
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        bad_registry.resolve("evil")


def test_resolve_project_outside_allowed_root(tmp_path):
    root = tmp_path / "python"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()

    reg = ProjectRegistry({"bad": str(outside)}, allowed_roots=[str(root)])
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        reg.resolve("bad")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_project_registry.py -v`
Expected: 4 FAILED

- [ ] **Step 3: Implement `ProjectRegistry`**

Create `examples/mcp_server/project_registry.py`:

```python
import os
from pathlib import Path


class ProjectRegistry:
    def __init__(self, projects: dict[str, str], allowed_roots: list[str]):
        self._projects = {}
        for name, path_str in projects.items():
            resolved = Path(path_str).resolve()
            self._validate_within_allowed_roots(resolved, allowed_roots)
            self._projects[name] = resolved
        self._allowed_roots = [Path(r).resolve() for r in allowed_roots]

    @staticmethod
    def _validate_within_allowed_roots(path: Path, allowed_roots: list[str]):
        for root_str in allowed_roots:
            root = Path(root_str).resolve()
            try:
                path.relative_to(root)
                return
            except ValueError:
                continue
        raise ValueError(f"POLICY_DENIED: {path} is outside allowed roots")

    def resolve(self, name: str) -> Path:
        if name not in self._projects:
            raise ValueError(f"PROJECT_NOT_FOUND: unknown project '{name}'")
        path = self._projects[name]
        resolved = path.resolve()
        try:
            resolved.relative_to(self._allowed_roots[0].anchor)  # just check resolve exists
        except ValueError:
            pass
        # Symlink escape check: resolved must match stored or be under allowed root
        if resolved != path:
            ok = False
            for root in self._allowed_roots:
                try:
                    resolved.relative_to(root)
                    ok = True
                    break
                except ValueError:
                    continue
            if not ok:
                raise ValueError(f"POLICY_DENIED: symlink escape detected for '{name}'")
        return path

    def list_projects(self) -> list[str]:
        return sorted(self._projects.keys())
```

- [ ] **Step 4: Add config fields and wire into server.py**

Add to `examples/mcp_server/config.py`:

```python
# Project registry
PROJECT_MAP: dict[str, str] = {
    "web-ssh-gateway": "/media/1TB/Python/web_ssh/web-ssh-gateway",
    "quart-ollama_bot": "/media/1TB/Python/quart-ollama_bot",
    "NOD_gateway": "/media/1TB/Python/NOD_gateway",
}
ALLOWED_PROJECT_ROOTS: list[str] = [
    "/media/1TB/Python/",
    "/var/www/",
]
```

In `server.py`, instantiate `ProjectRegistry` at module load:

```python
from mcp_server.project_registry import ProjectRegistry

_project_registry = ProjectRegistry(
    projects=settings.PROJECT_MAP,
    allowed_roots=settings.ALLOWED_PROJECT_ROOTS,
)
```

- [ ] **Step 5: Wire into chatgpt_tools.py**

Replace direct path construction in `_project_root()` with:

```python
def _resolve_project(project_name: str) -> Path:
    try:
        return _project_registry.resolve(project_name)
    except ValueError as e:
        raise GatewayClientError(404, str(e))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_project_registry.py -v`
Expected: 4 PASSED

- [ ] **Step 7: Commit**

```bash
git add examples/mcp_server/project_registry.py examples/mcp_server/server.py examples/mcp_server/config.py tests/test_project_registry.py
git commit -m "feat: project name resolution with allowlist and symlink guard

ProjectRegistry maps project names to validated paths.
Unknown name → PROJECT_NOT_FOUND. Symlink escape → POLICY_DENIED."
```

---

### Task 5: Python Runner Tools — `uv` via SSH

**Files:**
- Modify: `examples/mcp_server/chatgpt_tools.py` (rewrite `project_run_ruff`, `project_run_pytest`, `project_run_mypy`, `project_run_compileall`)
- Modify: `examples/mcp_server/command_policy.py` (add `uv` to allowed prefixes)
- Modify: `tests/test_mcp_chatgpt_tools.py`
- Create: `tests/test_uv_runners.py`

**Interfaces:**
- Consumes: `ProjectRegistry.resolve()` from Task 4, `build_command_result()` from Task 2, `GatewayClient.execute_project_command()`
- Produces: synchronous check results with `outcome` = `"passed"` | `"failed"`

- [ ] **Step 1: Write tests for uv runner logic**

Create `tests/test_uv_runners.py`:

```python
import pytest
from mcp_server.chatgpt_tools import _build_uv_argv


def test_build_ruff_argv():
    argv = _build_uv_argv("ruff", "/project", ["src/"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "ruff", "check", "--", "src/",
    ]


def test_build_mypy_argv():
    argv = _build_uv_argv("mypy", "/project", ["src/main.py"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "mypy", "--", "src/main.py",
    ]


def test_build_pytest_argv():
    argv = _build_uv_argv("pytest", "/project", ["tests/"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "pytest", "--", "tests/",
    ]


def test_build_compileall_argv():
    argv = _build_uv_argv("compileall", "/project", ["src/"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "python", "-m", "compileall", "src/",
    ]


def test_invalid_target_with_traversal():
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        _build_uv_argv("ruff", "/project", ["../outside"])


def test_invalid_target_absolute():
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        _build_uv_argv("ruff", "/project", ["/etc/passwd"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_uv_runners.py -v`
Expected: 6 FAILED

- [ ] **Step 3: Implement argv builder and result mapper**

Add to `chatgpt_tools.py`:

```python
import shlex

_UV_TOOL_MAP = {
    "ruff": ["ruff", "check"],
    "mypy": ["mypy"],
    "pytest": ["pytest"],
    "compileall": ["python", "-m", "compileall"],
}


def _validate_targets(project_dir: str, targets: list[str]) -> list[str]:
    """Validate all targets: relative, no traversal, inside project root."""
    root = Path(project_dir).resolve()
    validated = []
    for t in targets:
        p = Path(t)
        if p.is_absolute():
            raise ValueError(f"POLICY_DENIED: absolute target not allowed: {t}")
        if ".." in p.parts:
            raise ValueError(f"POLICY_DENIED: path traversal in target: {t}")
        resolved = (root / p).resolve()
        resolved.relative_to(root)  # raises ValueError on escape
        validated.append(t)
    return validated


def _build_uv_argv(tool: str, project_dir: str, targets: list[str]) -> list[str]:
    """Build argv for uv run tool. Targets validated before calling."""
    if tool not in _UV_TOOL_MAP:
        raise ValueError(f"INVALID_INPUT: unknown tool '{tool}'")
    if not targets:
        raise ValueError("INVALID_INPUT: at least one target required")
    validated = _validate_targets(project_dir, targets)
    cmd = _UV_TOOL_MAP[tool]
    argv = ["uv", "run", "--frozen", "--directory", project_dir, "--"] + cmd + ["--"] + validated
    return argv


def _map_uv_exit_code(tool: str, exit_code: int) -> tuple[str, str | None]:
    """Map exit code to (outcome, error_code).
    Returns (outcome, None) for success/check-failed,
    (None, error_code) for infrastructure errors.
    """
    # pytest special codes
    if tool == "pytest":
        if exit_code == 0:
            return ("passed", None)
        elif exit_code == 1:
            return ("failed", None)
        elif exit_code == 5:
            return ("failed", None)  # reason: NO_TESTS
        else:
            return (None, "TOOL_EXECUTION_FAILED")

    # ruff, mypy, compileall
    if exit_code == 0:
        return ("passed", None)
    elif exit_code == 1:
        return ("failed", None)
    else:
        return (None, "TOOL_EXECUTION_FAILED")
```

- [ ] **Step 4: Rewrite `project_run_ruff` in chatgpt_tools.py**

```python
async def project_run_ruff(
    project: str, target: list[str] | None = None
) -> dict:
    """Run ruff check on project targets via uv."""
    project_dir = _resolve_project(project)
    targets = target or ["."]
    try:
        validated = _validate_targets(str(project_dir), targets)
        argv = _build_uv_argv("ruff", str(project_dir), validated)
    except ValueError as e:
        code, msg = str(e).split(":", 1) if ":" in str(e) else ("INVALID_INPUT", str(e))
        return tool_error(code.strip(), msg.strip(), tool_name="project_run_ruff")

    # Preflight: check uv exists via gateway
    check_result = client.execute_project_command(str(project_dir), "command -v uv")
    if check_result.get("exit_code", 1) != 0:
        return tool_error(
            "DEPENDENCY_MISSING",
            "Required executable 'uv' was not found",
            hint="Install uv on the SSH target or configure another backend",
            retryable=False,
            details={"required_binary": "uv"},
            tool_name="project_run_ruff",
        )

    # Execute via SSH gateway
    command = " ".join(shlex.quote(a) for a in argv)
    result = client.execute_project_command(str(project_dir), command)
    outcome, error_code = _map_uv_exit_code("ruff", result.get("exit_code", -1))
    if error_code:
        return tool_error(
            error_code,
            f"Ruff failed with exit code {result.get('exit_code')}",
            details={"exit_code": result.get("exit_code"), "stderr": result.get("stderr", "")},
            tool_name="project_run_ruff",
        )
    return tool_success(
        build_command_result(
            outcome=outcome,
            exit_code=result.get("exit_code", 0),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            execution_duration_ms=result.get("duration_ms"),
        ),
        tool_name="project_run_ruff",
    )
```

- [ ] **Step 5: Rewrite `project_run_pytest`, `project_run_mypy`, `project_run_compileall`**

Follow the same pattern as `project_run_ruff`. Each uses the same preflight check and maps exit codes via `_map_uv_exit_code`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_uv_runners.py -v`
Expected: 6 PASSED

- [ ] **Step 7: Commit**

```bash
git add examples/mcp_server/chatgpt_tools.py examples/mcp_server/command_policy.py tests/test_uv_runners.py
git commit -m "feat: python runner tools via uv over SSH

project_run_ruff / pytest / mypy / compileall use uv run --frozen.
Preflight checks for uv. Full exit code mapping. argv-level safety."
```

---

### Task 6: `find_files` — Safe Glob

**Files:**
- Modify: `examples/mcp_server/chatgpt_tools.py` (rewrite `project_find_files`)
- Modify: `tests/test_mcp_chatgpt_tools.py`
- Create: `tests/test_find_files.py`

**Interfaces:**
- Consumes: `ProjectRegistry.resolve()` from Task 4
- Produces: `{"pattern": "...", "files": [...], "count": N}` with `meta.truncated`

- [ ] **Step 1: Write tests**

Create `tests/test_find_files.py`:

```python
import pytest
from pathlib import Path
from mcp_server.chatgpt_tools import _safe_glob


def test_simple_glob(tmp_path):
    (tmp_path / "README.md").write_text("readme")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("main")
    result = _safe_glob(tmp_path, "*.md")
    assert result["files"] == ["README.md"]
    assert result["count"] == 1


def test_recursive_glob(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.md").write_text("index")
    (tmp_path / "docs" / "api.md").write_text("api")
    result = _safe_glob(tmp_path, "docs/**/*.md")
    assert result["count"] == 2


def test_dot_glob(tmp_path):
    (tmp_path / "test_foo.py").write_text("")
    (tmp_path / "test_bar.py").write_text("")
    result = _safe_glob(tmp_path, "test_*.py")
    assert result["count"] == 2


def test_excludes_venv(tmp_path):
    (tmp_path / "src" / "main.py").write_text("main")
    (tmp_path / ".venv" / "lib.py").write_text("lib")
    result = _safe_glob(tmp_path, "**/*.py")
    files = [f for f in result["files"] if ".venv" in f]
    assert len(files) == 0


def test_excludes_git(tmp_path):
    (tmp_path / "src" / "code.py").write_text("code")
    (tmp_path / ".git" / "config").write_text("config")
    result = _safe_glob(tmp_path, "**/*.py")
    assert result["count"] == 1


def test_max_results_limit(tmp_path):
    for i in range(10):
        (tmp_path / f"file{i}.txt").write_text("x")
    result = _safe_glob(tmp_path, "*.txt", max_results=5)
    assert result["count"] == 5
    assert result["truncated"] is True


def test_traversal_blocked(tmp_path):
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        _safe_glob(tmp_path, "../outside/*.md")


def test_absolute_pattern_blocked(tmp_path):
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        _safe_glob(tmp_path, "/etc/*.conf")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_find_files.py -v`
Expected: 8 FAILED

- [ ] **Step 3: Implement `_safe_glob()`**

Add to `chatgpt_tools.py`:

```python
import signal
from pathlib import Path

EXCLUDE_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__"})
MAX_GLOB_RESULTS = 200
MAX_GLOB_DEPTH = 20
GLOB_TIMEOUT_S = 5


def _safe_glob(
    project_dir: Path,
    pattern: str,
    max_results: int = MAX_GLOB_RESULTS,
) -> dict:
    """Safe glob: returns {"files": [...], "count": N, "truncated": bool}."""
    # Safety checks
    if pattern.startswith("/"):
        raise ValueError("POLICY_DENIED: absolute pattern not allowed")
    if ".." in Path(pattern).parts:
        raise ValueError("POLICY_DENIED: path traversal in pattern")

    project_root = project_dir.resolve()
    results = []

    # signal timeout wrapper
    def handler(signum, frame):
        raise TimeoutError("glob timed out")

    signal.signal(signal.SIGALRM, handler)
    signal.alarm(GLOB_TIMEOUT_S)
    try:
        for path in project_root.glob(pattern):
            try:
                resolved = path.resolve()
                rel = resolved.relative_to(project_root)
            except ValueError:
                continue  # symlink escape
            if not resolved.is_file():
                continue
            if len(rel.parts) > MAX_GLOB_DEPTH:
                continue
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            results.append(str(rel))
            if len(results) >= max_results:
                break
    finally:
        signal.alarm(0)

    results.sort()
    return {
        "files": results,
        "count": len(results),
        "truncated": len(results) >= max_results,
    }
```

Note: for asyncio context, replace `signal.alarm` with `asyncio.wait_for()`.

- [ ] **Step 4: Rewrite `project_find_files` in chatgpt_tools.py**

```python
async def project_find_files(project: str, pattern: str) -> dict:
    """Find files matching a glob pattern inside the project."""
    project_dir = _resolve_project(project)
    try:
        glob_result = _safe_glob(project_dir, pattern)
    except ValueError as e:
        code, msg = str(e).split(":", 1) if ":" in str(e) else ("INVALID_INPUT", str(e))
        return tool_error(code.strip(), msg.strip(), tool_name="project_find_files")

    result = {
        "pattern": pattern,
        "files": glob_result["files"],
        "count": glob_result["count"],
    }
    meta = tool_success(result, tool_name="project_find_files")["meta"]
    meta["truncated"] = glob_result.get("truncated", False)
    return {
        "ok": True,
        "result": result,
        "error": None,
        "meta": meta,
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_find_files.py -v`
Expected: 8 PASSED

- [ ] **Step 6: Commit**

```bash
git add examples/mcp_server/chatgpt_tools.py tests/test_find_files.py
git commit -m "feat: safe glob for project_find_files

pathlib.Path.glob() with symlink guard, exclusion dirs, depth/timeout limits.
Traversal and absolute patterns blocked. Meta.truncated on overflow."
```

---

### Task 7: Docker Compose — Remove `file_path`, Validate `project_dir`

**Files:**
- Modify: `examples/chatgpt_remote_mcp/fleet/docker_client.py` (remove `file_path` from compose methods)
- Modify: `examples/chatgpt_remote_mcp/fleet/docker_server.py` (remove `file_path` from tool params)
- Modify: `examples/mcp_server/server.py` (update `docker_compose_*` tool registrations)
- Modify: `tests/test_docker_client.py`

**Interfaces:**
- Consumes: `ProjectRegistry` for `project_dir` validation
- Produces: compose tools that accept `project_dir` only (no `file_path`)

- [ ] **Step 1: Write tests for project_dir validation**

In `tests/test_docker_client.py`:

```python
def test_compose_ps_rejects_file_path(docker_client):
    with pytest.raises(TypeError):
        docker_client.compose_ps(file_path="/some/path/docker-compose.yml")


def test_compose_ps_with_project_dir(docker_client):
    result = docker_client.compose_ps(project_dir="/valid/path")
    assert result
```

- [ ] **Step 2: Update `docker_client.py` — remove `file_path` from compose methods**

Change all compose methods (`compose_ps`, `compose_services`, `compose_up`, `compose_down`, `compose_restart`, `compose_build`, `compose_logs`) to:

```python
async def compose_ps(
    self,
    project_dir: str | None = None,
    format: str | None = None,
    limit: int = 50,
) -> str:
    if project_dir:
        # Validate against allowed roots
        resolved = Path(project_dir).resolve()
        # (check allowed_roots — shared from ProjectRegistry)
        argv = [DOCKER_BIN, "compose", "--project-directory", project_dir, "ps", ...]
    else:
        argv = [DOCKER_BIN, "compose", "ps", ...]
    ...
```

Remove `file_path` parameter and `_resolve_compose_file_path` call from all compose methods.

- [ ] **Step 3: Update `docker_server.py` — remove `file_path` from tool params**

Remove `file_path` parameter from all docker compose tool functions.

- [ ] **Step 4: Update `server.py` — remove `file_path` from tool registrations**

Remove `file_path` parameter from `docker_compose_ps`, `docker_compose_services`, and all other compose tools.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_docker_client.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add examples/chatgpt_remote_mcp/fleet/docker_client.py examples/chatgpt_remote_mcp/fleet/docker_server.py examples/mcp_server/server.py tests/test_docker_client.py
git commit -m "feat: remove file_path from compose tools, validate project_dir

file_path parameter removed from all docker_compose_* tools.
project_dir validated against allowed roots. Compose file presence confirmed."
```

---

### Task 8: Two-Phase Confirmation for Destructive Operations

**Files:**
- Modify: `examples/mcp_server/docker_confirm.py` (check existing implementation)
- Modify: `examples/mcp_server/server.py` (wire up confirm for docker tools)
- Modify: `tests/test_docker_confirm.py`

**Interfaces:**
- Consumes: existing `ConfirmStore` in `docker_confirm.py`
- Produces: `request_*` tools return `outcome: "pending_confirmation"`; `confirm_operation(token)` executes

- [ ] **Step 1: Read existing confirmation implementation**

Review `docker_confirm.py` — it already has `ConfirmStore`, `ConfirmAction`, `create_action()`, `confirm_action()`. Check the token TTL (60s), single-use enforcement.

- [ ] **Step 2: Write tests for the flow**

In `tests/test_docker_confirm.py`:

```python
def test_confirm_flow(store):
    action_id = store.create_action("docker_stop", {"container": "web"}, summary="Stop web", risk="high")
    token = store.actions[action_id].confirm_token
    result = store.confirm_action(token)
    assert result.status == ConfirmStatus.OK

def test_confirm_replay_blocked(store):
    action_id = store.create_action("docker_stop", {"container": "web"}, summary="Stop web", risk="high")
    token = store.actions[action_id].confirm_token
    store.confirm_action(token)
    result = store.confirm_action(token)
    assert result.status == ConfirmStatus.CONSUMED

def test_expired_token(store):
    action_id = store.create_action("docker_stop", {"container": "web"}, summary="Stop web", risk="high")
    token = store.actions[action_id].confirm_token
    import time
    time.sleep(61)  # token TTL is 60s
    store.cleanup_expired()
    result = store.confirm_action(token)
    assert result.status == ConfirmStatus.EXPIRED
```

- [ ] **Step 3: Wire confirmation into server.py destructive tools**

For each destructive tool (docker_stop, docker_start, docker_restart, docker_rm, docker_prune, docker_compose_up, docker_compose_down, docker_compose_restart, docker_exec, docker_run, docker_rmi, docker_volume_rm):

1. Create the action in `ConfirmStore` when the tool is called
2. Return `outcome: "pending_confirmation"` with `confirmation_token`
3. The actual destructive call moves into a helper that `confirm_operation` invokes

```python
@register_tool("request_docker_stop")
async def request_docker_stop(container: str) -> dict:
    if not _confirm_store:
        return await _docker_stop_impl(container)  # no confirm store = direct
    action_id = _confirm_store.create_action(
        "docker_stop",
        {"container": container},
        summary=f"Stop container {container}",
        risk="medium",
    )
    action = _confirm_store.actions[action_id]
    return tool_success({
        "outcome": "pending_confirmation",
        "confirmation_token": action.confirm_token,
        "action_preview": {"operation": "docker_stop", "container": container},
        "expires_in": 60,
    }, tool_name="request_docker_stop")


@register_tool("confirm_operation")
async def confirm_operation(token: str) -> dict:
    if not _confirm_store:
        return tool_error("NOT_SUPPORTED", "Confirmation not available", tool_name="confirm_operation")
    result = _confirm_store.confirm_action(token)
    if result.status != ConfirmStatus.OK:
        return tool_error("INVALID_INPUT", f"Confirmation failed: {result.status.value}")
    # Execute the stored action
    action = result.action
    handler = _CONFIRM_HANDLERS.get(action.tool)
    if not handler:
        return tool_error("INTERNAL_ERROR", f"No handler for {action.tool}")
    return await handler(**action.kwargs)
```

Create `_CONFIRM_HANDLERS` dict mapping tool names to actual implementation functions.

- [ ] **Step 4: Disable direct destructive tools**

The old direct tools (`docker_stop`, `docker_start`, etc.) either:
- Become `request_*` tools that require confirmation
- Or are removed from the visible tool list and replaced with `request_*` variants

Decision: replace direct destructive tools with `request_*` + `confirm_operation`. Keep read-only tools unchanged.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_docker_confirm.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add examples/mcp_server/docker_confirm.py examples/mcp_server/server.py tests/test_docker_confirm.py
git commit -m "feat: two-phase confirmation for destructive operations

request_* returns pending_confirmation with one-time token.
confirm_operation(token) executes. Token TTL 60s, single-use.
Replay and expired tokens rejected."
```

---

### Task 9: Latency Measurement Framework

**Files:**
- Create: `examples/mcp_server/latency_metrics.py`
- Modify: `examples/mcp_server/server.py` (instrumentation decorator)
- Create: `tests/test_latency_metrics.py`

**Interfaces:**
- Produces: `LatencyTracker` class that records per-tool durations, exportable as JSON
- Produces: `@instrumented(tool_name)` decorator that auto-populates `meta.duration_ms`

- [ ] **Step 1: Write tests**

Create `tests/test_latency_metrics.py`:

```python
import time
from mcp_server.latency_metrics import LatencyTracker


def test_tracker_records_duration():
    tracker = LatencyTracker()
    with tracker.measure("health"):
        time.sleep(0.01)
    assert tracker.records["health"][0] >= 10  # >= 10ms

def test_tracker_multiple_calls():
    tracker = LatencyTracker()
    for _ in range(3):
        with tracker.measure("health"):
            pass
    assert len(tracker.records["health"]) == 3

def test_tracker_summary():
    tracker = LatencyTracker()
    with tracker.measure("test"):
        time.sleep(0.01)
    summary = tracker.summary()
    assert summary["total_calls"] >= 1
    assert "test" in summary["by_tool"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_latency_metrics.py -v`
Expected: 3 FAILED

- [ ] **Step 3: Implement `LatencyTracker`**

Create `examples/mcp_server/latency_metrics.py`:

```python
import time
import threading
from collections import defaultdict


class LatencyTracker:
    def __init__(self):
        self._records = defaultdict(list)
        self._lock = threading.Lock()

    def measure(self, tool_name: str):
        return _MeasureContext(self, tool_name)

    def record(self, tool_name: str, duration_ms: float):
        with self._lock:
            self._records[tool_name].append(duration_ms)

    @property
    def records(self) -> dict[str, list[float]]:
        with self._lock:
            return dict(self._records)

    def summary(self) -> dict:
        with self._lock:
            by_tool = {}
            for name, durations in self._records.items():
                by_tool[name] = {
                    "count": len(durations),
                    "min_ms": min(durations),
                    "max_ms": max(durations),
                    "avg_ms": sum(durations) / len(durations),
                }
            return {
                "total_calls": sum(len(v) for v in self._records.values()),
                "by_tool": by_tool,
            }


class _MeasureContext:
    def __init__(self, tracker: LatencyTracker, tool_name: str):
        self.tracker = tracker
        self.tool_name = tool_name
        self.start = 0.0

    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *args):
        duration = (time.monotonic() - self.start) * 1000
        self.tracker.record(self.tool_name, duration)


_tracker = LatencyTracker()


def get_tracker() -> LatencyTracker:
    return _tracker
```

- [ ] **Step 4: Add `@instrumented` decorator in server.py**

```python
from mcp_server.latency_metrics import get_tracker


def instrumented(tool_name: str):
    """Decorator that wraps a tool function with latency tracking."""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            tracker = get_tracker()
            with tracker.measure(tool_name):
                result = await func(*args, **kwargs)
            # Update meta.duration_ms if available
            if isinstance(result, dict) and "meta" in result:
                recs = tracker.records.get(tool_name, [])
                if recs:
                    result["meta"]["duration_ms"] = int(recs[-1])
            return result
        return wrapper
    return decorator
```

- [ ] **Step 5: Wire up initial measurements**

Add `GET /metrics/latency` endpoint that returns `get_tracker().summary()`.
Run 10 parallel `health` calls manually and log the results.

```python
@register_tool("latency_report")
def latency_report() -> dict:
    return tool_success(
        get_tracker().summary(),
        tool_name="latency_report",
    )
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_latency_metrics.py -v`
Expected: 3 PASSED

- [ ] **Step 7: Run baseline measurements manually**

```bash
curl -s http://localhost:8788/health  # single call → check duration_ms in meta
# Then run 10 parallel calls with `xargs -P 10` or a small script
```

Log the results as a baseline for Phase 2 optimisation.

- [ ] **Step 8: Commit**

```bash
git add examples/mcp_server/latency_metrics.py examples/mcp_server/server.py tests/test_latency_metrics.py
git commit -m "feat: latency measurement framework

LatencyTracker records per-tool durations. @instrumented decorator
auto-populates meta.duration_ms. /metrics/latency endpoint exports summary."
```
