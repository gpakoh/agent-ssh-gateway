# Session 160: Docker Operations Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development or executing-plans to implement this plan task-by-task.

**Goal:** Add Docker operational tools (start/stop/restart/compose up/build) with safety validation, fix compose path resolving, add unit tests, and update docs.

**Architecture:** Extend existing `DockerClient` in `fleet/docker_client.py` with write operations (argv-based, no `shell=True`), add scope `mcp:docker` write-capable profile, register new tools in server.py.

**Tech Stack:** Python/asyncio, FastMCP, Docker CLI subprocess, pytest.

## Global Constraints

- Docker commands must use argv arrays, never `shell=True`
- Container names: validate with `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$`. Forbid `; & | \` $() ../` — absolute service names — empty strings
- Compose path resolving: relative `file_path` joined under `project_dir`; absolute only if inside allowed roots (`MCP_GATEWAY_PROJECT_ROOT` or `MCP_DOCKER_ALLOWED_ROOTS`)
- Operational write tools need scope `mcp:docker` with write-capable mode/profile
- Dangerous tools NOT to add: `docker_compose_down`, `docker_rm/rmi/volume_rm/prune/exec/run`
- Limits: `tail` 1–1000, `timeout` 1–120, `detach=True` default, `build=False` default

---

## File Structure

| File | Action | Purpose |
|------|--------|---------|
| `examples/chatgpt_remote_mcp/fleet/docker_client.py` | Modify | Add write methods + compose path resolver |
| `examples/mcp_server/server.py` | Modify | Register new Docker tools |
| `examples/mcp_server/tool_scopes.py` | Modify | Add scopes for new tools |
| `examples/mcp_server/tool_modes.py` | Modify | Add new tools to chatgpt mode |
| `tests/test_docker_client.py` | Create | Unit tests for new Docker write tools |
| `docs/operations/MCP_OPERATOR_RUNBOOK.md` | Modify | Document Docker operations |

### Task 1: Fix compose path resolving + add path validators

**Files:**
- Modify: `examples/chatgpt_remote_mcp/fleet/docker_client.py`

**Interfaces:**
- Consumes: existing `DockerClient`, `CONTAINER_NAME_RE`, `COMPOSE_FILE_RE`, `COMPOSE_PATH_TRAVERSAL_RE`
- Produces: `_resolve_compose_file_path(file_path, project_dir) -> str`, `COMPOSE_PATH_RE` new regex

- [ ] **Step 1: Write the failing tests first (in tests/test_docker_client.py)**

```python
"""Tests for DockerClient compose path resolving and write operations."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "chatgpt_remote_mcp"))

import pytest
from fleet.docker_client import DockerClient, CONTAINER_NAME_RE, SERVICE_NAME_RE


def _client() -> DockerClient:
    return DockerClient()


# ── Container name validation ──

def test_valid_container_names():
    for name in ["web", "my-app_1", "redis.cache", "a", "a" * 128]:
        assert CONTAINER_NAME_RE.match(name), f"should accept: {name}"


def test_invalid_container_names():
    for name in ["", "-leading-hyphen", ".leading-dot", "name;evil", "name&more", "name|pipe", "name$(id)", "name`id`", "../name", "name with space"]:
        assert not CONTAINER_NAME_RE.match(name), f"should reject: {name}"


# ── Service name validation ──

def test_valid_service_names():
    for name in ["web", "my-service", "api_gateway", "a", "a" * 64]:
        assert SERVICE_NAME_RE.match(name), f"should accept: {name}"


def test_invalid_service_names():
    for name in ["", "-leading-hyphen", ".leading-dot", "name;evil", "name/../", "capitals", "a" * 65]:
        assert not SERVICE_NAME_RE.match(name), f"should reject: {name}"


# ── Compose path resolving ──

def test_compose_relative_path_resolved_under_project_dir():
    """Relative file_path is joined under project_dir."""
    c = _client()
    result = c._resolve_compose_file_path("docker-compose.yml", project_dir="/opt/proj")
    assert result == "/opt/proj/docker-compose.yml"


def test_compose_relative_path_defaults_to_cwd():
    """Relative file_path without project_dir uses current dir."""
    c = _client()
    result = c._resolve_compose_file_path("docker-compose.yml", project_dir=None)
    assert result == "docker-compose.yml"


def test_compose_absolute_path_inside_allowed_root():
    """Absolute file_path within MCP_GATEWAY_PROJECT_ROOT is allowed."""
    c = _client()
    result = c._resolve_compose_file_path(
        "<repo-root>/docker/docker-compose.yml",
        project_dir=None,
        allowed_roots={"<workspace-root>/web_ssh"},
    )
    assert result == "<repo-root>/docker/docker-compose.yml"


def test_compose_absolute_path_outside_allowed_root():
    """Absolute file_path outside allowed roots raises."""
    c = _client()
    with pytest.raises(ValueError, match="outside allowed root"):
        c._resolve_compose_file_path(
            "/etc/passwd",
            project_dir=None,
            allowed_roots={"<workspace-root>/web_ssh"},
        )


def test_compose_absolute_path_blocked():
    """Absolute paths are blocked when no allowed roots configured."""
    c = _client()
    with pytest.raises(ValueError, match="outside allowed root"):
        c._resolve_compose_file_path("/etc/passwd", project_dir=None, allowed_roots=set())


def test_compose_path_traversal_blocked():
    """Path traversal in compose file path is always blocked."""
    c = _client()
    for path in ["../etc/passwd", "foo/../../etc/passwd"]:
        with pytest.raises(ValueError, match="traversal"):
            c._resolve_compose_file_path(path, project_dir="/opt/proj")


def test_compose_project_dir_does_not_exist():
    """Raises if project_dir does not exist."""
    c = _client()
    with pytest.raises(ValueError, match="does not exist"):
        c._resolve_compose_file_path("docker-compose.yml", project_dir="/nonexistent/path/xyz123")


def test_compose_path_validation_contract():
    """file_path must match COMPOSE_PATH_RE when given."""
    c = _client()
    with pytest.raises(ValueError, match="Invalid compose file path"):
        c._resolve_compose_file_path("", project_dir=None)
    with pytest.raises(ValueError, match="Invalid compose file path"):
        c._resolve_compose_file_path("a" * 257, project_dir=None)


# ── Container write operations (validation only, no real docker) ──

def test_start_invalid_container_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid container name"):
        c.start("bad;name")


def test_stop_invalid_container_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid container name"):
        c.stop("bad;name")


def test_restart_invalid_container_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid container name"):
        c.restart("bad;name")


def test_restart_timeout_out_of_range():
    c = _client()
    with pytest.raises(ValueError, match="timeout"):
        c.restart("web", timeout=0)
    with pytest.raises(ValueError, match="timeout"):
        c.restart("web", timeout=121)


# ── Compose write operations (validation only) ──

def test_compose_up_invalid_file_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid compose file path"):
        c.compose_up(project_dir="/tmp", file_path="../bad.yml")


def test_compose_up_invalid_service_raises():
    c = _client()
    with pytest.raises(ValueError, match="Invalid service name"):
        c.compose_up(project_dir="/tmp", services=["ok", "bad;name"])


def test_compose_up_detach_default_true():
    c = _client()
    argv = c._compose_up_argv(project_dir="/tmp", detach=True)
    assert "--detach" in argv


def test_compose_up_no_detach():
    c = _client()
    argv = c._compose_up_argv(project_dir="/tmp", detach=False)
    assert "--detach" not in argv
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd <repo-root> && python -m pytest tests/test_docker_client.py -v 2>&1 | head -40`
Expected: ModuleNotFoundError or AttributeError — methods not yet implemented

- [ ] **Step 3: Add `COMPOSE_PATH_RE` and `_resolve_compose_file_path` to `docker_client.py`**

Add after `COMPOSE_PATH_TRAVERSAL_RE`:

```python
COMPOSE_FILE_RE = re.compile(r"^[a-zA-Z0-9_/.-]{1,256}$")
```

Replace the existing `_validate_compose_file` method and add:

```python
def _resolve_compose_file_path(
    self,
    file_path: str | None,
    project_dir: str | None = None,
    allowed_roots: set[str] | None = None,
) -> str | None:
```

Implementation logic:
- If `file_path` is None, return None
- Validate format with `COMPOSE_FILE_RE`
- Check for `..` path traversal
- If `file_path` is absolute:
  - Check it's within allowed roots (project root or MCP_DOCKER_ALLOWED_ROOTS)
  - Raise ValueError if outside
- If `file_path` is relative:
  - If `project_dir` is set and exists, join under project_dir
  - Otherwise use file_path as-is (docker compose resolves relative to CWD)

- [ ] **Step 4: Run tests to verify pass**

Run: `cd <repo-root> && python -m pytest tests/test_docker_client.py::test_compose_relative_path_resolved_under_project_dir tests/test_docker_client.py::test_compose_absolute_path_inside_allowed_root tests/test_docker_client.py::test_compose_path_traversal_blocked -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add examples/chatgpt_remote_mcp/fleet/docker_client.py tests/test_docker_client.py
git commit -m "fix: add compose path resolver with safety validation"
```

---

### Task 2: Implement Docker write operations (start/stop/restart)

**Files:**
- Modify: `examples/chatgpt_remote_mcp/fleet/docker_client.py`

- [ ] **Step 1: Add `start`, `stop`, `restart` methods to `DockerClient`**

```python
async def start(self, container: str, timeout: int | None = None) -> str:
    self._validate_container_name(container)
    argv = [DOCKER_BIN, "start", container]
    return await self._run(argv, timeout=float(timeout or SUBPROCESS_TIMEOUT))

async def stop(self, container: str, timeout: int = 10) -> str:
    self._validate_container_name(container)
    timeout = max(1, min(timeout, 120))
    argv = [DOCKER_BIN, "stop", "--time", str(timeout), container]
    return await self._run(argv)

async def restart(self, container: str, timeout: int = 10) -> str:
    self._validate_container_name(container)
    timeout = max(1, min(timeout, 120))
    argv = [DOCKER_BIN, "restart", "--time", str(timeout), container]
    return await self._run(argv)
```

- [ ] **Step 2: Run unit tests for validation**

Run: `cd <repo-root> && python -m pytest tests/test_docker_client.py::test_start_invalid_container_raises tests/test_docker_client.py::test_stop_invalid_container_raises tests/test_docker_client.py::test_restart_invalid_container_raises tests/test_docker_client.py::test_restart_timeout_out_of_range -v`
Expected: PASS

- [ ] **Step 3: Run live smoke test on sshd container**

```bash
cd <repo-root>
# Get test-sshd container name
docker ps --filter name=ssh --format '{{.Names}}'
# Test restart (stop then start via the agent)
python -c "
import asyncio, sys
sys.path.insert(0, 'examples/chatgpt_remote_mcp')
from fleet.docker_client import DockerClient
async def test():
    c = DockerClient()
    # find a test container
    ps = await c.ps(all=True)
    print('=== PS ===')
    print(ps)
asyncio.run(test())
"
```

- [ ] **Step 4: Commit**

```bash
git add examples/chatgpt_remote_mcp/fleet/docker_client.py
git commit -m "feat: add Docker start/stop/restart operations"
```

---

### Task 3: Implement Docker Compose write operations (up/restart/build)

**Files:**
- Modify: `examples/chatgpt_remote_mcp/fleet/docker_client.py`

- [ ] **Step 1: Add `compose_up`, `compose_restart`, `compose_build` methods**

```python
def _compose_base_argv(self, file_path: str | None = None, project_dir: str | None = None) -> list[str]:
    argv = [DOCKER_BIN, "compose"]
    if file_path:
        argv.extend(["-f", file_path])
    if project_dir:
        argv.extend(["--project-directory", project_dir])
    return argv

async def compose_up(
    self,
    project_dir: str | None = None,
    file_path: str | None = None,
    services: list[str] | None = None,
    detach: bool = True,
    build: bool = False,
    timeout: int = 120,
) -> str:
    resolved = _resolve_compose_file_path(file_path, project_dir)
    argv = self._compose_base_argv(resolved, project_dir)
    argv.append("up")
    if detach:
        argv.append("--detach")
    if build:
        argv.append("--build")
    if services:
        for s in services:
            self._validate_service_name(s)
        argv.extend(services)
    return await self._run(argv, timeout=float(timeout))

async def compose_restart(
    self,
    project_dir: str | None = None,
    file_path: str | None = None,
    services: list[str] | None = None,
    timeout: int = 30,
) -> str:
    resolved = _resolve_compose_file_path(file_path, project_dir)
    argv = self._compose_base_argv(resolved, project_dir)
    argv.append("restart")
    if services:
        for s in services:
            self._validate_service_name(s)
        argv.extend(services)
    return await self._run(argv, timeout=float(timeout))

async def compose_build(
    self,
    project_dir: str | None = None,
    file_path: str | None = None,
    services: list[str] | None = None,
    no_cache: bool = False,
    timeout: int = 300,
) -> str:
    resolved = _resolve_compose_file_path(file_path, project_dir)
    argv = self._compose_base_argv(resolved, project_dir)
    argv.append("build")
    if no_cache:
        argv.append("--no-cache")
    if services:
        for s in services:
            self._validate_service_name(s)
        argv.extend(services)
    return await self._run(argv, timeout=float(timeout))

async def compose_logs(
    self,
    project_dir: str | None = None,
    file_path: str | None = None,
    services: list[str] | None = None,
    tail: int = 100,
    follow: bool = False,
    timestamps: bool = False,
    timeout: int = 30,
) -> str:
    resolved = _resolve_compose_file_path(file_path, project_dir)
    argv = self._compose_base_argv(resolved, project_dir)
    argv.append("logs")
    tail = max(1, min(tail, 1000))
    argv.extend(["--tail", str(tail)])
    if follow:
        argv.append("--follow")
    if timestamps:
        argv.append("--timestamps")
    if services:
        for s in services:
            self._validate_service_name(s)
        argv.extend(services)
    return await self._run(argv, timeout=float(timeout))
```

- [ ] **Step 2: Run unit tests for compose validation**

Run: `cd <repo-root> && python -m pytest tests/test_docker_client.py::test_compose_up_invalid_file_raises tests/test_docker_client.py::test_compose_up_invalid_service_raises tests/test_docker_client.py::test_compose_up_detach_default_true tests/test_docker_client.py::test_compose_up_no_detach -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add examples/chatgpt_remote_mcp/fleet/docker_client.py
git commit -m "feat: add Docker compose up/restart/build/logs operations"
```

---

### Task 4: Register new tools in server.py, tool_scopes.py, tool_modes.py

**Files:**
- Modify: `examples/mcp_server/server.py`
- Modify: `examples/mcp_server/tool_scopes.py`
- Modify: `examples/mcp_server/tool_modes.py`

- [ ] **Step 1: Add tool registrations in server.py** (after existing Docker tools block, line ~1313)

```python
@register_tool("docker_start")
async def docker_start(container: str, timeout: int | None = None) -> str:
    """Start a stopped container."""
    return await DockerClient().start(container, timeout=timeout)


@register_tool("docker_stop")
async def docker_stop(container: str, timeout: int = 10) -> str:
    """Stop a running container. timeout: seconds before force kill (1-120, default 10)."""
    return await DockerClient().stop(container, timeout=timeout)


@register_tool("docker_restart")
async def docker_restart(container: str, timeout: int = 10) -> str:
    """Restart a container. timeout: seconds before force kill (1-120, default 10)."""
    return await DockerClient().restart(container, timeout=timeout)


@register_tool("docker_compose_up")
async def docker_compose_up(
    project_dir: str | None = None,
    file_path: str | None = None,
    services: list[str] | None = None,
    detach: bool = True,
    build: bool = False,
    timeout: int = 120,
) -> str:
    """Start services in a Docker Compose project."""
    return await DockerClient().compose_up(
        project_dir=project_dir, file_path=file_path,
        services=services, detach=detach, build=build, timeout=timeout,
    )


@register_tool("docker_compose_restart")
async def docker_compose_restart(
    project_dir: str | None = None,
    file_path: str | None = None,
    services: list[str] | None = None,
    timeout: int = 30,
) -> str:
    """Restart services in a Docker Compose project."""
    return await DockerClient().compose_restart(
        project_dir=project_dir, file_path=file_path,
        services=services, timeout=timeout,
    )


@register_tool("docker_compose_build")
async def docker_compose_build(
    project_dir: str | None = None,
    file_path: str | None = None,
    services: list[str] | None = None,
    no_cache: bool = False,
    timeout: int = 300,
) -> str:
    """Build (or rebuild) services in a Docker Compose project."""
    return await DockerClient().compose_build(
        project_dir=project_dir, file_path=file_path,
        services=services, no_cache=no_cache, timeout=timeout,
    )


@register_tool("docker_compose_logs")
async def docker_compose_logs(
    project_dir: str | None = None,
    file_path: str | None = None,
    services: list[str] | None = None,
    tail: int = 100,
    follow: bool = False,
    timestamps: bool = False,
) -> str:
    """Fetch logs from services in a Docker Compose project."""
    return await DockerClient().compose_logs(
        project_dir=project_dir, file_path=file_path,
        services=services, tail=tail, follow=follow, timestamps=timestamps,
    )
```

- [ ] **Step 2: Add scopes in tool_scopes.py** (after `"docker_compose_services":` line)

```python
    # docker operations — mcp:docker (write-capable)
    "docker_start": ["mcp:docker"],
    "docker_stop": ["mcp:docker"],
    "docker_restart": ["mcp:docker"],
    "docker_compose_up": ["mcp:docker"],
    "docker_compose_restart": ["mcp:docker"],
    "docker_compose_build": ["mcp:docker"],
    "docker_compose_logs": ["mcp:docker"],
```

- [ ] **Step 3: Add tools to tool_modes.py `chatgpt` set** (around line 118)

```python
        "docker_start",
        "docker_stop",
        "docker_restart",
        "docker_compose_up",
        "docker_compose_restart",
        "docker_compose_build",
        "docker_compose_logs",
```

- [ ] **Step 4: Run syntax check**

Run: `cd <repo-root> && python -m py_compile examples/mcp_server/server.py examples/mcp_server/tool_scopes.py examples/mcp_server/tool_modes.py`
Expected: no output (clean compile)

- [ ] **Step 5: Run existing tests to ensure no regressions**

Run: `cd <repo-root> && python -m pytest tests/test_docker_output_redaction.py tests/test_docker_client.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add examples/mcp_server/server.py examples/mcp_server/tool_scopes.py examples/mcp_server/tool_modes.py
git commit -m "feat: register Docker operations tools in server, scopes, and modes"
```

---

### Task 5: Update MCP_OPERATOR_RUNBOOK.md

**Files:**
- Modify: `docs/operations/MCP_OPERATOR_RUNBOOK.md`

- [ ] **Step 1: Update baseline table** — change Docker tool count from 7 to 14

Update line 29: `| Docker | 14 | 8793 | \`/mcp/docker\` |`

- [ ] **Step 2: Add Docker operations section**

```markdown
---
## 8. Docker Operations Tools

### Added in Session 160 (2026-07-08)

New Docker tools for container lifecycle management:

| Tool | Purpose | Safety |
|------|---------|--------|
| `docker_start` | Start a stopped container | Container name validation |
| `docker_stop` | Stop a running container | Container name validation, timeout 1-120s |
| `docker_restart` | Restart a container | Container name validation, timeout 1-120s |
| `docker_compose_up` | Start Compose services | Path validation, service name validation |
| `docker_compose_restart` | Restart Compose services | Path validation, service name validation |
| `docker_compose_build` | Build Compose services | Path validation, service name validation |
| `docker_compose_logs` | Fetch Compose service logs | Path validation, service name validation, tail 1-1000 |

### Safety

- All Docker commands use argv arrays (`["docker", "restart", "--time", "10", "web"]`) — never `shell=True`
- Container names validated against `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$`
- Compose paths validated against path traversal and restricted to allowed roots
- Service names validated against `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`
- Timeouts bounded: 1-120s for stop/restart, 1-300s for compose build

### Not included (reserved for future)

- `docker_compose_down` — too destructive for current scope
- `docker_rm/rmi/volume_rm/prune/exec/run` — dangerous operations
```

- [ ] **Step 3: Commit**

```bash
git add docs/operations/MCP_OPERATOR_RUNBOOK.md
git commit -m "docs: add Docker operations section to runbook"
```

---

### Task 6: Final verification and push

- [ ] **Step 1: Run full test suite**

```bash
cd <repo-root>
python -m pytest tests/test_docker_output_redaction.py tests/test_docker_client.py -v
```

Expected: all tests pass

- [ ] **Step 2: Run compile check on all modified files**

```bash
python -m py_compile \
  examples/chatgpt_remote_mcp/fleet/docker_client.py \
  examples/mcp_server/server.py \
  examples/mcp_server/tool_scopes.py \
  examples/mcp_server/tool_modes.py
```

Expected: no output (clean compile)

- [ ] **Step 3: Run live smoke test if test-sshd container available**

```bash
docker ps --filter name=ssh --format '{{.Names}}' | head -1
```

If a container is found:

```bash
python -c "
import asyncio, sys
sys.path.insert(0, 'examples/chatgpt_remote_mcp')
from fleet.docker_client import DockerClient
async def test():
    c = DockerClient()
    ps = await c.ps(all=True)
    print('=== DOCKER PS ===')
    print(ps)
asyncio.run(test())
"
```

- [ ] **Step 4: Commit and push to Gitea**

```bash
git add -A && git status
git commit -m "feat(session-160): add Docker operations tools with safety validation

- Add Docker start/stop/restart operations
- Add Docker compose up/restart/build/logs operations
- Fix compose path resolving (relative under project_dir, absolute under allowed roots)
- Add unit tests for validation and path resolving
- Register tools in server.py, tool_scopes.py, tool_modes.py
- Update MCP operator runbook

Breaking: compose_ps and compose_services now use _resolve_compose_file_path for path safety"
git push gitea master
```
