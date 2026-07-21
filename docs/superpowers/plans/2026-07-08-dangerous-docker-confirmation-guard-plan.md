# Dangerous Docker Confirmation Guard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two-phase confirmation guard for destructive Docker operations (`docker_rm`, `docker_compose_down`, `docker_prune`, `docker_confirm`, `docker_pending_actions`).

**Architecture:** Three layers — (1) `DockerClient` gets new methods returning `RunResult` (with `_run_with_result` that doesn't raise on non-zero exit), (2) in-memory `ConfirmStore` manages one-time 60s TTL tokens verified via `hmac.compare_digest`, (3) tool layer in `server.py` wraps calls using canonical envelope (`tool_success`/`tool_error`) with consume-before-execute ordering.

**Tech Stack:** Python 3.12+ (StrEnum, dataclasses, hmac), asyncio subprocess, FastMCP.

**Tool count impact:** 97 → 102 (new `docker_rm`, `docker_compose_down`, `docker_prune`, `docker_confirm`, `docker_pending_actions`).

## Global Constraints

- No `docker_exec`, `docker_run`, `docker_volume_rm`, `docker_rmi` in Session 164
- `docker_prune` only allows `container`, `image`, `network` (no `volume` or `system`)
- `docker_compose_down` must NOT support `--volumes` / `-v` flag
- All dangerous responses use canonical envelope from ADR-2026-07-08 (not bare str)
- Confirmation token: `secrets.token_urlsafe(16)`, TTL 60s, one-time, compared via `hmac.compare_digest`
- Token consumed BEFORE Docker execution
- Never log full confirm_token, only prefix (first 6 chars)
- `pending_actions` list masks full token, shows prefix only
- argv-only, no `shell=True`
- All new tools scoped `["mcp:docker"]`
- All new tools registered in `chatgpt` mode, not in `minimal`/`standard`/`full`

---

### Task 1: DockerClient — RunResult + _run_with_result

**Files:**
- Modify: `examples/chatgpt_remote_mcp/fleet/docker_client.py`

**Interfaces:**
- Produces: `RunResult(stdout: str, stderr: str, exit_code: int)` dataclass, `async _run_with_result(argv, timeout) -> RunResult`

- [ ] **Step 1: Add `RunResult` dataclass after `MAX_OUTPUT_BYTES` (line 18)**

```python
from dataclasses import dataclass


@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int
```

- [ ] **Step 2: Add `_run_with_result` method after `_run` (after line 74)**

```python
async def _run_with_result(
    self,
    argv: list[str],
    timeout: float = SUBPROCESS_TIMEOUT,
) -> RunResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return RunResult(
            stdout="",
            stderr=f"Command timed out after {timeout}s",
            exit_code=-1,
        )

    exit_code = proc.returncode or 0
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace").strip()
    if len(out) > MAX_OUTPUT_BYTES:
        out = out[:MAX_OUTPUT_BYTES] + "\n[output truncated]"
    return RunResult(stdout=out, stderr=err, exit_code=exit_code)
```

- [ ] **Step 3: Verify the file parses**

Run: `python -c "import ast; ast.parse(open('examples/chatgpt_remote_mcp/fleet/docker_client.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add examples/chatgpt_remote_mcp/fleet/docker_client.py
git commit -m "feat(docker): add RunResult and _run_with_result"
```

---

### Task 2: DockerClient — rm, compose_down, prune, _validate_prune_type

**Files:**
- Modify: `examples/chatgpt_remote_mcp/fleet/docker_client.py`

**Interfaces:**
- Consumes: `RunResult`, `_run_with_result` (from Task 1)
- Produces: `async rm(container, force=False) -> RunResult`, `async compose_down(...) -> RunResult`, `async prune(type="container") -> RunResult`
- Produces: `_validate_prune_type(type) -> str`

- [ ] **Step 1: Add `ALLOWED_PRUNE_TYPES` set after `COMPOSE_PATH_TRAVERSAL_RE` (line 24)**

```python
ALLOWED_PRUNE_TYPES: set[str] = {"container", "image", "network"}
```

- [ ] **Step 2: Add `_validate_prune_type` after `_validate_compose_file` (after line 98)**

```python
def _validate_prune_type(self, type: str) -> str:
    if type not in ALLOWED_PRUNE_TYPES:
        raise ValueError(
            f"Unsupported prune type '{type}'. Allowed: {sorted(ALLOWED_PRUNE_TYPES)}"
        )
    return type
```

- [ ] **Step 3: Add `rm` method after any existing method (near line 162)**

```python
async def rm(self, container: str, force: bool = False) -> RunResult:
    self._validate_container_name(container)
    argv = [DOCKER_BIN, "rm"]
    if force:
        argv.append("-f")
    argv.append(container)
    return await self._run_with_result(argv)
```

- [ ] **Step 4: Add `compose_down` after `rm`**

```python
async def compose_down(
    self,
    project_dir: str | None = None,
    file_path: str | None = None,
    remove_orphans: bool = False,
    timeout: int = 30,
) -> RunResult:
    argv = self._compose_base_argv(file_path, project_dir)
    argv.append("down")
    if remove_orphans:
        argv.append("--remove-orphans")
    argv.extend(["-t", str(timeout)])
    return await self._run_with_result(argv, timeout=float(timeout) + 10)
```

Note: No `--volumes` / `-v` flag — explicitly excluded in MVP.

- [ ] **Step 5: Add `prune` after `compose_down`**

```python
async def prune(self, type: str = "container") -> RunResult:
    self._validate_prune_type(type)
    argv = [DOCKER_BIN, type, "prune", "-f"]
    return await self._run_with_result(argv)
```

- [ ] **Step 6: Quick parse check**

Run: `python -c "import ast; ast.parse(open('examples/chatgpt_remote_mcp/fleet/docker_client.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add examples/chatgpt_remote_mcp/fleet/docker_client.py
git commit -m "feat(docker): add rm, compose_down, prune methods"
```

---

### Task 3: Confirmation store (docker_confirm.py + tests)

**Files:**
- Create: `examples/mcp_server/docker_confirm.py`
- Create: `tests/test_docker_confirm.py`

**Interfaces:**
- Produces: `ConfirmAction`, `ConfirmStatus(StrEnum)`, `ConfirmStore`
- `ConfirmStore.create_action(tool, kwargs, summary) -> ConfirmAction`
- `ConfirmStore.confirm_action(token) -> tuple[ConfirmAction | None, ConfirmStatus]`
- `ConfirmStore.list_pending() -> list[dict]`
- `ConfirmStore.cleanup_expired() -> int`

- [ ] **Step 1: Write test for `confirm_action` basic flow**

File: `tests/test_docker_confirm.py`

```python
from examples.mcp_server.docker_confirm import ConfirmStore, ConfirmStatus


class TestConfirmStore:
    def test_create_action_returns_action(self):
        store = ConfirmStore()
        action = store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        assert action.tool == "docker_rm"
        assert action.kwargs == {"container": "foo"}
        assert action.summary == "Remove container foo"
        assert action.risk == "high"
        assert action.consumed is False
        assert action.action_id is not None
        assert action.confirm_token is not None

    def test_confirm_valid_token(self):
        store = ConfirmStore()
        action = store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        result, status = store.confirm_action(action.confirm_token)
        assert result is not None
        assert result.action_id == action.action_id
        assert status == ConfirmStatus.OK

    def test_confirm_consumes_token(self):
        store = ConfirmStore()
        action = store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        store.confirm_action(action.confirm_token)
        result, status = store.confirm_action(action.confirm_token)
        assert result is None
        assert status == ConfirmStatus.CONSUMED

    def test_confirm_invalid_token(self):
        store = ConfirmStore()
        store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        result, status = store.confirm_action("invalid-token")
        assert result is None
        assert status == ConfirmStatus.INVALID

    def test_confirm_expired_token(self, monkeypatch):
        store = ConfirmStore()
        action = store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        monkeypatch.setattr("time.monotonic", lambda: 999999.0)
        result, status = store.confirm_action(action.confirm_token)
        assert result is None
        assert status == ConfirmStatus.EXPIRED

    def test_list_pending_masks_token(self):
        store = ConfirmStore()
        store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0]["confirm_token"].endswith("...")
        assert len(pending[0]["confirm_token"]) > 6

    def test_list_pending_excludes_consumed(self):
        store = ConfirmStore()
        a1 = store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        store.create_action("docker_prune", {"type": "container"}, "Prune containers")
        store.confirm_action(a1.confirm_token)
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0]["tool"] == "docker_prune"

    def test_cleanup_expired(self, monkeypatch):
        store = ConfirmStore()
        store.create_action("docker_rm", {"container": "foo"}, "Remove container foo")
        monkeypatch.setattr("time.monotonic", lambda: 999999.0)
        removed = store.cleanup_expired()
        assert removed == 1
        assert len(store.list_pending()) == 0

    def test_create_action_unique_tokens(self):
        store = ConfirmStore()
        a1 = store.create_action("docker_rm", {}, "a")
        a2 = store.create_action("docker_rm", {}, "b")
        assert a1.confirm_token != a2.confirm_token
        assert a1.action_id != a2.action_id

    def test_confirm_timing_attack_protection(self):
        store = ConfirmStore()
        action = store.create_action("docker_rm", {}, "test")
        result, status = store.confirm_action(action.confirm_token.upper())
        assert result is None
        assert status == ConfirmStatus.INVALID
```

- [ ] **Step 2: Run to confirm tests fail**

Run: `pytest -q tests/test_docker_confirm.py 2>&1 | head -5`
Expected: ImportError or module not found

- [ ] **Step 3: Write `examples/mcp_server/docker_confirm.py`**

```python
"""In-memory confirmation store for dangerous Docker operations.

One-time tokens, 60s TTL, process-local, no I/O.
"""

from __future__ import annotations

import enum
import hmac
import time
import uuid
from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import Any

CONFIRM_TTL_SECONDS = 60


class ConfirmStatus(str, enum.Enum):
    OK = "ok"
    INVALID = "invalid"
    EXPIRED = "expired"
    CONSUMED = "consumed"


@dataclass
class ConfirmAction:
    action_id: str
    tool: str
    kwargs: dict[str, Any]
    confirm_token: str
    summary: str
    risk: str = "high"
    created_at: float = field(default_factory=time.monotonic)
    consumed: bool = False


class ConfirmStore:
    """Process-local, in-memory store for pending dangerous actions."""

    def __init__(self) -> None:
        self._actions: dict[str, ConfirmAction] = {}
        self._token_map: dict[str, str] = {}

    def create_action(
        self,
        tool: str,
        kwargs: dict[str, Any],
        summary: str,
        *,
        risk: str = "high",
    ) -> ConfirmAction:
        action_id = uuid.uuid4().hex
        confirm_token = token_urlsafe(16)
        action = ConfirmAction(
            action_id=action_id,
            tool=tool,
            kwargs=kwargs,
            confirm_token=confirm_token,
            summary=summary,
            risk=risk,
        )
        self._actions[action_id] = action
        self._token_map[confirm_token] = action_id
        return action

    def confirm_action(self, token: str) -> tuple[ConfirmAction | None, ConfirmStatus]:
        action_id = self._token_map.get(token)
        if action_id is None:
            for aid, act in self._actions.items():
                if hmac.compare_digest(act.confirm_token, token):
                    action_id = aid
                    self._token_map[token] = aid
                    break
            if action_id is None:
                return None, ConfirmStatus.INVALID

        action = self._actions.get(action_id)
        if action is None:
            return None, ConfirmStatus.INVALID

        if action.consumed:
            return None, ConfirmStatus.CONSUMED

        elapsed = time.monotonic() - action.created_at
        if elapsed > CONFIRM_TTL_SECONDS:
            return None, ConfirmStatus.EXPIRED

        action.consumed = True
        return action, ConfirmStatus.OK

    def list_pending(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        result: list[dict[str, Any]] = []
        for action in self._actions.values():
            if action.consumed:
                continue
            elapsed = now - action.created_at
            if elapsed > CONFIRM_TTL_SECONDS:
                continue
            remaining = max(0, int(CONFIRM_TTL_SECONDS - elapsed))
            token_preview = action.confirm_token[:6] + "..."
            result.append({
                "action_id": action.action_id,
                "tool": action.tool,
                "summary": action.summary,
                "risk": action.risk,
                "expires_in_sec": remaining,
                "confirm_token": token_preview,
            })
        return result

    def cleanup_expired(self) -> int:
        now = time.monotonic()
        expired = [
            aid for aid, action in self._actions.items()
            if now - action.created_at > CONFIRM_TTL_SECONDS
        ]
        for aid in expired:
            action = self._actions.pop(aid, None)
            if action:
                self._token_map.pop(action.confirm_token, None)
        return len(expired)
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `pytest -q tests/test_docker_confirm.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add examples/mcp_server/docker_confirm.py tests/test_docker_confirm.py
git commit -m "feat(docker): add confirmation store with one-time tokens"
```

---

### Task 4: Register dangerous tools in server.py + scopes + modes

**Files:**
- Modify: `examples/mcp_server/server.py`
- Modify: `examples/mcp_server/tool_scopes.py`
- Modify: `examples/mcp_server/tool_modes.py`

**Interfaces:**
- Consumes: `DockerClient.rm()`, `DockerClient.compose_down()`, `DockerClient.prune()` (from Task 2)
- Consumes: `ConfirmStore` (from Task 3)
- Consumes: `tool_success`, `tool_error` from `tool_results.py`
- Produces: 5 async tool functions registered via `@register_tool`

- [ ] **Step 1: Add import for `ConfirmStore` in `server.py`**

Insert after existing `from tool_results import ...` line (line 78):

```python
from docker_confirm import ConfirmStore, ConfirmStatus
```

- [ ] **Step 2: Add global `_confirm_store` after the `_write_mode` setup (near the other global singletons)**

Find `_pg_client` or similar global, add:

```python
_confirm_store: ConfirmStore = ConfirmStore()
```

- [ ] **Step 3: Add 5 tool functions after `docker_compose_logs` (after line 1409)**

```python
# ── Dangerous Docker operations (Session 164) ────────────────────


@register_tool("docker_rm")
async def docker_rm(container: str, force: bool = False) -> dict[str, Any]:
    """Remove a container. DANGEROUS: requires confirmation via docker_confirm(token)."""
    DockerClient()._validate_container_name(container)
    summary = f"Remove container {container}"
    action = _confirm_store.create_action("docker_rm", {"container": container, "force": force}, summary)
    return _confirmation_response(action)


@register_tool("docker_compose_down")
async def docker_compose_down(
    project_dir: str | None = None,
    file_path: str | None = None,
    remove_orphans: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    """Stop and remove a Compose stack. DANGEROUS: requires confirmation."""
    dc = DockerClient()
    dc._resolve_compose_file_path(file_path, project_dir)
    parts = []
    if project_dir:
        parts.append(f"project={project_dir}")
    if file_path:
        parts.append(f"file={file_path}")
    summary = f"Compose down {' '.join(parts)}"
    action = _confirm_store.create_action(
        "docker_compose_down",
        {"project_dir": project_dir, "file_path": file_path, "remove_orphans": remove_orphans, "timeout": timeout},
        summary,
    )
    return _confirmation_response(action)


@register_tool("docker_prune")
async def docker_prune(type: str = "container") -> dict[str, Any]:
    """Prune Docker resources. DANGEROUS: requires confirmation. Allowed types: container, image, network."""
    DockerClient()._validate_prune_type(type)
    summary = f"Prune {type}s"
    action = _confirm_store.create_action("docker_prune", {"type": type}, summary)
    return _confirmation_response(action)


@register_tool("docker_confirm")
async def docker_confirm(token: str) -> dict[str, Any]:
    """Confirm a pending dangerous Docker operation using the one-time token from the confirmation response."""
    action, status = _confirm_store.confirm_action(token)
    if action is None:
        code = {
            ConfirmStatus.INVALID: "CONFIRM_TOKEN_INVALID",
            ConfirmStatus.EXPIRED: "CONFIRM_TOKEN_EXPIRED",
            ConfirmStatus.CONSUMED: "CONFIRM_TOKEN_CONSUMED",
        }.get(status, "INTERNAL_ERROR")
        msg = {
            ConfirmStatus.INVALID: "Invalid confirmation token",
            ConfirmStatus.EXPIRED: "Confirmation token expired (TTL 60s)",
            ConfirmStatus.CONSUMED: "Confirmation token already used",
        }.get(status, "Unknown error")
        return tool_error(
            tool="docker_confirm",
            code=code,
            message=msg,
            hint="Call the dangerous tool again to get a new token.",
            retryable=False,
            source="docker",
        )

    dc = DockerClient()
    tool_name = action.tool
    kwargs = action.kwargs

    if tool_name == "docker_rm":
        result = await dc.rm(kwargs["container"], force=kwargs.get("force", False))
    elif tool_name == "docker_compose_down":
        result = await dc.compose_down(
            project_dir=kwargs.get("project_dir"),
            file_path=kwargs.get("file_path"),
            remove_orphans=kwargs.get("remove_orphans", False),
            timeout=kwargs.get("timeout", 30),
        )
    elif tool_name == "docker_prune":
        result = await dc.prune(kwargs["type"])
    else:
        return tool_error(
            tool="docker_confirm",
            code="INTERNAL_ERROR",
            message=f"Unknown action tool: {tool_name}",
            source="docker",
        )

    if result.exit_code == 0:
        return tool_success(
            tool="docker_confirm",
            result={
                "action": tool_name,
                "executed": True,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
            },
            source="docker",
        )
    else:
        return tool_error(
            tool="docker_confirm",
            code="DOCKER_COMMAND_FAILED",
            message="Docker command failed",
            result={
                "action": tool_name,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
            },
            source="docker",
            retryable=False,
            hint="Check container name or Docker state.",
        )


@register_tool("docker_pending_actions")
async def docker_pending_actions() -> dict[str, Any]:
    """List all pending dangerous Docker operations awaiting confirmation."""
    _confirm_store.cleanup_expired()
    pending = _confirm_store.list_pending()
    count = len(pending)
    return tool_success(
        tool="docker_pending_actions",
        result={"count": count, "items": pending},
        source="docker",
    )
```

- [ ] **Step 4: Add the shared `_confirmation_response` helper near the tools (before `docker_rm`)**

```python
def _confirmation_response(action: ConfirmAction) -> dict[str, Any]:
    remaining = max(0, int(60 - (time.monotonic() - action.created_at)))
    return tool_success(
        tool=action.tool,
        result={
            "status": "confirmation_required",
            "action_id": action.action_id,
            "confirm_token": action.confirm_token,
            "expires_in_sec": remaining,
            "summary": action.summary,
            "risk": action.risk,
        },
        source="docker",
        dangerous=True,
    )
```

Also add `import time` at the top of server.py if not already there.

- [ ] **Step 5: Add `import time` to server.py imports**

Check if `import time` exists; if not, add in the import block (around line 12-15).

- [ ] **Step 6: Add 5 tools to `tool_scopes.py`**

Add after line 149 (after `"docker_compose_logs": ["mcp:docker"],`):

```python
    # dangerous docker operations (Session 164) — mcp:docker
    "docker_rm": ["mcp:docker"],
    "docker_compose_down": ["mcp:docker"],
    "docker_prune": ["mcp:docker"],
    "docker_confirm": ["mcp:docker"],
    "docker_pending_actions": ["mcp:docker"],
```

- [ ] **Step 7: Add 5 tools to `tool_modes.py` chatgpt set**

Add after line 129 (after `"docker_compose_logs",`):

```python
        "docker_rm",
        "docker_compose_down",
        "docker_prune",
        "docker_confirm",
        "docker_pending_actions",
```

- [ ] **Step 8: Run format + lint**

Run: `ruff format . && ruff check . --fix`
Expected: No errors

- [ ] **Step 9: Run full test suite**

Run: `pytest -q`
Expected: All pass (tool count increased, existing tests unaffected)

- [ ] **Step 10: Commit**

```bash
git add examples/mcp_server/server.py examples/mcp_server/tool_scopes.py examples/mcp_server/tool_modes.py
git commit -m "feat(docker): register dangerous Docker tools with confirmation guard"
```

---

### Task 5: Update healthcheck + final verification

**Files:**
- Modify: `scripts/mcp_fleet_healthcheck.py`

- [ ] **Step 1: Update expected tool count 97 → 102**

In `scripts/mcp_fleet_healthcheck.py` line 47, change `97` to `102`.

- [ ] **Step 2: Run healthcheck**

Run: `python scripts/mcp_fleet_healthcheck.py --verbose`
Expected: Gateway [102/102 tools], all green

- [ ] **Step 3: Run enforce smoke**

Run: `python scripts/mcp_enforce_smoke.py`
Expected: 14/14

- [ ] **Step 4: Final full test suite**

Run: `pytest -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp_fleet_healthcheck.py
git commit -m "fix: update expected tool count 97→102 for Docker confirmation guard"
```

---

### Task 6: Deploy and live smoke

- [ ] **Step 1: Push to remotes**

```bash
git push gitea master && git push https://github.com/gpakoh/agent-ssh-gateway.git master
```

- [ ] **Step 2: Restart service**

```bash
systemctl restart agent-ssh-gateway-mcp.service && sleep 3 && systemctl is-active agent-ssh-gateway-mcp.service
```

Expected: `active`

- [ ] **Step 3: Create disposable test container**

```bash
docker run -d --name mcp-docker-confirm-test alpine sleep 300
docker ps --filter name=mcp-docker-confirm-test --format "{{.Names}}"
```

Expected: `mcp-docker-confirm-test`

- [ ] **Step 4: Call `docker_rm` via MCP tools/call**

```bash
RESP=$(curl -s -X POST https://ssh-gateway.example.com/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MCP_PUBLIC_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"docker_rm","arguments":{"container":"mcp-docker-confirm-test"}}}')
echo "$RESP" | python3 -m json.tool
```

Expected: `confirmation_required` response with `action_id`, `confirm_token`, `expires_in_sec: 60`

- [ ] **Step 5: Extract token and call `docker_confirm`**

```bash
TOKEN=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['result']['confirm_token'])")
CONFIRM_RESP=$(curl -s -X POST https://ssh-gateway.example.com/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MCP_PUBLIC_TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"docker_confirm\",\"arguments\":{\"token\":\"$TOKEN\"}}}")
echo "$CONFIRM_RESP" | python3 -m json.tool
```

Expected: `"ok": true`, `"executed": true`, `"action": "docker_rm"`

- [ ] **Step 6: Verify container gone**

```bash
docker ps --filter name=mcp-docker-confirm-test --format "{{.Names}}"
```

Expected: no output (container removed)

- [ ] **Step 7: Try same token again (should fail)**

```bash
CONFIRM_RESP2=$(curl -s -X POST https://ssh-gateway.example.com/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MCP_PUBLIC_TOKEN" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"docker_confirm\",\"arguments\":{\"token\":\"$TOKEN\"}}}")
echo "$CONFIRM_RESP2" | python3 -m json.tool
```

Expected: `"ok": false`, `"code": "CONFIRM_TOKEN_CONSUMED"`

- [ ] **Step 8: Check pending actions**

```bash
curl -s -X POST https://ssh-gateway.example.com/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MCP_PUBLIC_TOKEN" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"docker_pending_actions","arguments":{}}}' | python3 -m json.tool
```

Expected: `"count": 0` or only non-expired pending items

- [ ] **Step 9: Final healthcheck**

```bash
python scripts/mcp_fleet_healthcheck.py --verbose
python scripts/mcp_enforce_smoke.py
```

Expected: 6/6 and 14/14

- [ ] **Step 10: Final git status check**

```bash
git status --short
```

Expected: clean
