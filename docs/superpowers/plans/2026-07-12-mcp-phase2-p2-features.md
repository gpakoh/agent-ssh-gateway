# P2 Features: execute_argv + Project Patch Apply

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two P2 features — argv-based SSH execution with stdin support, and project-scoped unified diff patch apply with transactional writes and rollback.

**Architecture:** Each feature is an independent addition: a Gateway endpoint + request/response models + an MCP tool + a GatewayClient method. execute_argv extends the existing SSH session execute path with stdin piping and `shlex.join` serialization. Patch apply adds a new `app/patch_apply.py` module that uses `unidiff` for parsing, validates against `ProjectRegistry`, and writes files atomically via SSH with backup/rollback. Both features reuse existing auth (`require_scope`), command policy, and session ownership patterns.

**Tech Stack:** Python 3.11+, FastAPI, Paramiko, unidiff, shlex, pydantic

## Global Constraints

- Python >=3.11 (pyproject.toml)
- Existing patterns: Pydantic BaseModel for request/response, `require_scope()` for auth, `ensure_session_owner()` for ownership, `_state.manager.execute()` for SSH commands, `tool_success`/`tool_error` for MCP contract v1
- Scope strings: `ssh:execute:argv` and `project:patch` (new, added to `VALID_AGENT_SCOPES`)
- `shlex.join(argv)` — no `bash -c` wrapping (spec §6)
- stdin <= 1 MiB UTF-8 only, stdout/stderr <= 10 MiB each, combined <= 10 MiB for MCP (spec §6)
- Patch limits: <= 20 files, <= 100 hunks, <= 1 MiB patch, <= 10 MiB per file (spec §7)
- Temp/backup naming: `path.parent / f".{path.name}.mcp-patch-{rid}.{tmp,bak}"` (spec §7)
- v1 forbidden: binary, rename/copy, mode, symlink, /dev/null (spec §7)
- Command policy applied to full argv before serialization (spec §6)

---

## Part A: execute_argv

### Task A1: Models and auth scope for execute_argv

**Files:**
- Modify: `app/auth_middleware.py:56-64`
- Modify: `app/models.py` (after `ExecuteResponse` class, ~line 78)
- Test: `tests/test_execute_argv_models.py`

**Interfaces:**
- Produces: `ExecuteArgvRequest`, `ExecuteArgvResponse` pydantic models
- Produces: `"ssh:execute:argv"` added to `VALID_AGENT_SCOPES`

- [ ] **Step 1: Write failing test for models and scope**

```python
# tests/test_execute_argv_models.py
"""Tests for execute_argv models and auth scope."""

from app.auth_middleware import VALID_AGENT_SCOPES
from app.models import ExecuteArgvRequest, ExecuteArgvResponse


def test_argv_scope_exists():
    assert "ssh:execute:argv" in VALID_AGENT_SCOPES


def test_execute_argv_request_valid():
    req = ExecuteArgvRequest(
        session_id="abc-123",
        argv=["python3", "-c", "print('hello')"],
    )
    assert req.session_id == "abc-123"
    assert req.argv == ["python3", "-c", "print('hello')"]
    assert req.stdin == ""
    assert req.timeout_s == 30


def test_execute_argv_request_minimal():
    req = ExecuteArgvRequest(session_id="x", argv=["ls"])
    assert req.stdin == ""
    assert req.timeout_s == 30


def test_execute_argv_request_empty_argv_rejected():
    from pydantic import ValidationError

    try:
        ExecuteArgvRequest(session_id="x", argv=[])
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_execute_argv_request_arg_too_long_rejected():
    from pydantic import ValidationError

    try:
        ExecuteArgvRequest(session_id="x", argv=["x" * 256])
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_execute_argv_request_nul_in_arg_rejected():
    from pydantic import ValidationError

    try:
        ExecuteArgvRequest(session_id="x", argv=["hello\x00world"])
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_execute_argv_request_timeout_bounds():
    from pydantic import ValidationError

    try:
        ExecuteArgvRequest(session_id="x", argv=["ls"], timeout_s=0)
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass

    try:
        ExecuteArgvRequest(session_id="x", argv=["ls"], timeout_s=3601)
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_execute_argv_request_total_argv_length():
    from pydantic import ValidationError

    # Total UTF-8 length of all args <= 65536
    try:
        ExecuteArgvRequest(
            session_id="x",
            argv=["a" * 30000, "b" * 30000, "c" * 6000],
        )
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_execute_argv_request_stdin_limit():
    from pydantic import ValidationError

    try:
        ExecuteArgvRequest(
            session_id="x", argv=["ls"], stdin="x" * (1024 * 1024 + 1)
        )
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_execute_argv_request_stdin_non_utf8_rejected():
    from pydantic import ValidationError

    try:
        ExecuteArgvRequest(
            session_id="x", argv=["ls"], stdin="hello\x80\xff"
        )
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_execute_argv_response():
    resp = ExecuteArgvResponse(
        stdout="hello\n",
        stderr="",
        exit_code=0,
        duration=0.123,
    )
    assert resp.stdout == "hello\n"
    assert resp.exit_code == 0
    assert resp.duration == 0.123
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_execute_argv_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'ExecuteArgvRequest'`

- [ ] **Step 3: Add scope to VALID_AGENT_SCOPES**

In `app/auth_middleware.py`, add `"ssh:execute:argv"` to the `VALID_AGENT_SCOPES` set:

```python
VALID_AGENT_SCOPES: set[str] = {
    "ssh:connect",
    "ssh:execute",
    "ssh:execute:argv",
    "ssh:disconnect",
    "ssh:files",
    "ssh:port-check",
    "jobs:read",
    "jobs:run",
}
```

- [ ] **Step 4: Add ExecuteArgvRequest and ExecuteArgvResponse models**

In `app/models.py`, after the `ExecuteResponse` class (line ~78), add:

```python
class ExecuteArgvRequest(BaseModel):
    """Request body for executing an argv command with stdin."""

    session_id: str = Field(..., min_length=1)
    argv: list[str] = Field(..., min_length=1)
    stdin: str = Field(default="", max_length=1_048_576)
    timeout_s: int = Field(default=30, ge=1, le=3600)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("argv must not be empty")
        total = 0
        for arg in v:
            if len(arg) > 255:
                raise ValueError(f"Individual arg too long ({len(arg)} chars, max 255)")
            if "\x00" in arg:
                raise ValueError("NUL byte not allowed in argv")
            total += len(arg.encode("utf-8"))
        if total > 65536:
            raise ValueError(f"Total argv UTF-8 length {total} exceeds 65536 bytes")
        return v

    @field_validator("stdin")
    @classmethod
    def validate_stdin_utf8(cls, v: str) -> str:
        try:
            v.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("stdin must be valid UTF-8") from exc
        return v


class ExecuteArgvResponse(BaseModel):
    """Response after argv command execution."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration: float = 0.0
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_execute_argv_models.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add app/auth_middleware.py app/models.py tests/test_execute_argv_models.py
git commit -m "feat: add ExecuteArgvRequest/Response models and ssh:execute:argv scope"
```

---

### Task A2: execute_argv method on SSHSessionManager

**Files:**
- Modify: `app/ssh_manager.py` (add method after `execute()`)
- Test: `tests/test_ssh_manager_argv.py`

**Interfaces:**
- Consumes: `SSHSessionManager._sessions`, `SessionRecord`, `execute()` pattern
- Produces: `SSHSessionManager.execute_argv(session_id, command_str, stdin_bytes, timeout) -> CommandResult`

- [ ] **Step 1: Write failing test**

```python
# tests/test_ssh_manager_argv.py
"""Tests for execute_argv on SSHSessionManager."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_record():
    """Create a mock SessionRecord with a working SSH client."""
    record = MagicMock()
    record.is_connected.return_value = True
    record.touch = MagicMock()

    # Mock SSH client and exec_command
    stdin_channel = MagicMock()
    stdout_channel = MagicMock()
    stderr_channel = MagicMock()

    stdin_file = MagicMock()
    stdout_file = MagicMock()
    stderr_file = MagicMock()

    stdout_file.read.return_value = b"output"
    stderr_file.read.return_value = b""
    stdout_channel.recv_exit_status.return_value = 0

    record.client.exec_command.return_value = (stdin_file, stdout_file, stderr_file)
    record.client.get_transport.return_value = MagicMock()
    record.client.get_transport.return_value.is_active.return_value = True

    return record, stdin_file, stdout_file, stderr_file


@pytest.mark.asyncio
async def test_execute_argv_sends_stdin():
    from app.ssh_manager import SSHSessionManager

    manager = SSHSessionManager()
    record, stdin_file, stdout_file, stderr_file = _make_mock_record()
    manager._sessions["test-id"] = record

    result = await manager.execute_argv(
        session_id="test-id",
        command_str="python3 -c print('hi')",
        stdin_data=b"input data",
        timeout=10,
    )

    assert result["exit_code"] == 0
    assert result["stdout"] == "output"
    # stdin should have been written to and shutdown_write called
    stdin_file.write.assert_called_once_with(b"input data")


@pytest.mark.asyncio
async def test_execute_argv_empty_stdin():
    from app.ssh_manager import SSHSessionManager

    manager = SSHSessionManager()
    record, stdin_file, stdout_file, stderr_file = _make_mock_record()
    manager._sessions["test-id"] = record

    result = await manager.execute_argv(
        session_id="test-id",
        command_str="ls",
        stdin_data=b"",
        timeout=10,
    )

    assert result["exit_code"] == 0
    # stdin.write should NOT be called for empty data
    stdin_file.write.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ssh_manager_argv.py -v`
Expected: FAIL with `AttributeError: 'SSHSessionManager' object has no attribute 'execute_argv'`

- [ ] **Step 3: Implement execute_argv on SSHSessionManager**

In `app/ssh_manager.py`, add the method after `execute()` (after the `execute` method ending around line 449). Read the file first to find the exact insertion point, then add:

```python
    async def execute_argv(
        self,
        session_id: str,
        command_str: str,
        stdin_data: bytes,
        timeout: int = 30,
    ) -> CommandResult:
        """Execute a pre-serialized command with optional stdin data.

        Unlike execute(), this method writes stdin_data to the channel before
        shutting down write, then reads stdout/stderr concurrently.
        """
        async with self._lock:
            record = self._sessions.get(session_id)
        if not record:
            raise SessionNotFoundError(f"Session {session_id} not found")

        if not record.is_connected():
            logger.warning("Session %s disconnected, attempting auto-reconnect", session_id)
            reconnected = await self.reconnect(session_id)
            if not reconnected:
                raise ConnectionError(
                    f"Session {session_id} is disconnected and reconnection failed"
                )

        record.touch()
        host, port, username = record.host, record.port, record.username
        client = record.client
        loop = asyncio.get_event_loop()
        start = time.time()

        _emit(
            "command.started",
            session_id=session_id,
            host=host,
            port=port,
            username=username,
            command=command_str,
        )

        try:
            stdin, stdout, stderr = await loop.run_in_executor(
                None,
                lambda: client.exec_command(command_str, timeout=timeout),
            )

            # Write stdin data if any, then shutdown write
            if stdin_data:
                stdin.write(stdin_data)
            stdin.channel.shutdown_write()

            # Read stdout and stderr concurrently with timeout
            out_data = await asyncio.wait_for(
                loop.run_in_executor(None, stdout.read),
                timeout=timeout,
            )
            err_data = await asyncio.wait_for(
                loop.run_in_executor(None, stderr.read),
                timeout=timeout,
            )
            exit_code = stdout.channel.recv_exit_status()
        except builtins.TimeoutError:
            raise TimeoutError(
                f"Command timed out after {timeout}s: {command_str}"
            ) from None
        except SSHException as exc:
            logger.warning(
                "SSH error during execution for session %s: %s", session_id, exc
            )
            raise ExecutionError(f"SSH error during execution: {exc}") from exc

        duration = time.time() - start

        _emit(
            "command.completed",
            session_id=session_id,
            command=command_str,
            exit_code=exit_code,
            duration=duration,
        )

        return CommandResult(
            stdout=out_data.decode("utf-8", errors="replace"),
            stderr=err_data.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            duration=duration,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ssh_manager_argv.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/ssh_manager.py tests/test_ssh_manager_argv.py
git commit -m "feat: add execute_argv method to SSHSessionManager with stdin support"
```

---

### Task A3: Gateway endpoint POST /api/ssh/execute-argv

**Files:**
- Modify: `app/routers/ssh.py` (add route after `ssh_execute`)
- Test: `tests/test_routers_execute_argv.py`

**Interfaces:**
- Consumes: `ExecuteArgvRequest`, `ExecuteArgvResponse` (Task A1), `SSHSessionManager.execute_argv()` (Task A2), `evaluate_command_policy()`, `shlex.join()`
- Produces: `POST /api/ssh/execute-argv` endpoint

- [ ] **Step 1: Write failing test**

```python
# tests/test_routers_execute_argv.py
"""Tests for POST /api/ssh/execute-argv endpoint."""

import shlex
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def _auth_headers():
    from app.config import settings

    return {"X-API-Key": settings.api_key}


def test_execute_argv_requires_auth(client):
    resp = client.post(
        "/api/ssh/execute-argv",
        json={"session_id": "x", "argv": ["ls"]},
    )
    assert resp.status_code == 401


def test_execute_argv_empty_argv_rejected(client):
    resp = client.post(
        "/api/ssh/execute-argv",
        json={"session_id": "x", "argv": []},
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


def test_execute_argv_arg_too_long_rejected(client):
    resp = client.post(
        "/api/ssh/execute-argv",
        json={"session_id": "x", "argv": ["x" * 256]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


def test_execute_argv_timeout_bounds_rejected(client):
    resp = client.post(
        "/api/ssh/execute-argv",
        json={"session_id": "x", "argv": ["ls"], "timeout_s": 0},
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_execute_argv_session_not_found(client):
    with patch("app.routers.ssh._state") as mock_state:
        mock_state.manager.get_session = AsyncMock(return_value=None)
        resp = client.post(
            "/api/ssh/execute-argv",
            json={"session_id": "nonexistent", "argv": ["ls"]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_execute_argv_command_policy_denied(client):
    from app.auth_middleware import AuthIdentity

    mock_session = MagicMock()
    mock_session.owner_type = "master"
    mock_session.owner_token_fingerprint = None

    with (
        patch("app.routers.ssh._state") as mock_state,
        patch("app.routers.ssh.require_scope") as mock_require,
    ):
        mock_state.manager.get_session = AsyncMock(return_value=mock_session)
        mock_require.return_value = lambda req: AuthIdentity(
            token_type="master", token="test", name="master", scopes=("*",)
        )
        with patch("app.routers.ssh.evaluate_command_policy") as mock_policy:
            mock_policy.return_value = MagicMock(
                allowed=False, reason="denied", profile="default", mode="enforce", command_root="rm"
            )
            resp = client.post(
                "/api/ssh/execute-argv",
                json={"session_id": "sid", "argv": ["rm", "-rf", "/"]},
                headers=_auth_headers(),
            )
            assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_routers_execute_argv.py -v`
Expected: FAIL — endpoint does not exist yet (404 or similar)

- [ ] **Step 3: Implement the endpoint**

In `app/routers/ssh.py`, add imports at top (after existing imports):

```python
import shlex
```

Add the model imports to the existing import block:

```python
from app.models import (
    # ... existing imports ...
    ExecuteArgvRequest,
    ExecuteArgvResponse,
)
```

Add the endpoint after `ssh_execute` (after line ~343):

```python
@router.post("/api/ssh/execute-argv", response_model=ExecuteArgvResponse)
@rate_limit_mutation(60, "minute")
async def ssh_execute_argv(
    req: ExecuteArgvRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:execute:argv")),
):
    """Execute an argv command with stdin support on an existing SSH session.

    argv is serialized via shlex.join — no bash -c wrapping.
    """
    # Serialize argv to command string
    command_str = shlex.join(req.argv)

    # Command policy evaluation on full argv
    decision = evaluate_command_policy(
        command_str,
        mode=settings.command_policy_mode,
        profile=settings.command_policy_profile,
    )

    _state.audit_logger.log_security_event(
        "COMMAND_POLICY_DECISION",
        (
            f"session_id={req.session_id}; "
            f"command={command_str}; "
            f"allowed={decision.allowed}; "
            f"reason={decision.reason}; "
            f"profile={decision.profile}; "
            f"mode={decision.mode}; "
            f"command_root={decision.command_root}"
        ),
        request.client.host if request.client else "unknown",
    )

    if not decision.allowed:
        raise HTTPException(
            status_code=403,
            detail=_err(403, f"Command denied by policy: {decision.reason}"),
        )

    # Audit Log
    _state.audit_logger.log_command(req.session_id, command_str, request.client.host)

    # Session Ownership Check
    session = await _state.manager.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=_err(404, "Session not found"))
    ensure_session_owner(session, _identity)

    # Encode stdin to bytes
    stdin_bytes = req.stdin.encode("utf-8") if req.stdin else b""

    result = await _state.manager.execute_argv(
        session_id=req.session_id,
        command_str=command_str,
        stdin_data=stdin_bytes,
        timeout=req.timeout_s,
    )

    # Truncate stdout/stderr to 10 MiB each
    max_output = 10 * 1024 * 1024
    stdout = result["stdout"][:max_output]
    stderr = result["stderr"][:max_output]

    return ExecuteArgvResponse(
        stdout=stdout,
        stderr=stderr,
        exit_code=result["exit_code"],
        duration=result.get("duration", 0.0),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_routers_execute_argv.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/routers/ssh.py tests/test_routers_execute_argv.py
git commit -m "feat: add POST /api/ssh/execute-argv endpoint"
```

---

### Task A4: MCP tool execute_argv + GatewayClient method

**Files:**
- Modify: `examples/mcp_server/gateway_client.py` (add method)
- Modify: `examples/mcp_server/server.py` (add tool registration)
- Test: `tests/test_mcp_execute_argv.py`

**Interfaces:**
- Consumes: `GatewayClient._post()`, `tool_success`, `tool_error`, `build_command_result`
- Produces: `GatewayClient.execute_argv()`, MCP tool `execute_argv`

- [ ] **Step 1: Write failing test**

```python
# tests/test_mcp_execute_argv.py
"""Tests for MCP execute_argv tool and GatewayClient method."""

from unittest.mock import MagicMock, patch

import pytest


def test_gateway_client_execute_argv_calls_correct_endpoint():
    from examples.mcp_server.gateway_client import GatewayClient

    client = GatewayClient.__new__(GatewayClient)
    client.base_url = "http://test:8085"
    client.api_key = "test-key"
    client.session_id = "test-session"
    client.command_timeout = 30
    client.job_timeout = 180
    client._reconnect_lock = MagicMock()
    client._ssh_host = ""
    client._ssh_port = 22
    client._ssh_user = ""
    client._ssh_password = ""
    client._ssh_private_key = ""

    with patch.object(client, "_post", return_value={"exit_code": 0, "stdout": "hi", "stderr": "", "duration": 0.1}) as mock_post:
        result = client.execute_argv(
            argv=["python3", "-c", "print('hi')"],
            stdin="",
            timeout_s=30,
        )

    mock_post.assert_called_once_with(
        "/api/ssh/execute-argv",
        {
            "session_id": "test-session",
            "argv": ["python3", "-c", "print('hi')"],
            "stdin": "",
            "timeout_s": 30,
        },
    )
    assert result["exit_code"] == 0


def test_mcp_execute_argv_tool_exists():
    from examples.mcp_server.server import mcp

    # Check that the tool is registered
    tool_names = [t.name for t in mcp._tool_manager._tools.values()]
    assert "execute_argv" in tool_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_execute_argv.py -v`
Expected: FAIL — `execute_argv` method not found on `GatewayClient` or tool not registered

- [ ] **Step 3: Add execute_argv to GatewayClient**

In `examples/mcp_server/gateway_client.py`, add after `execute_project_command` method (around line 258):

```python
    @_retry_on_session_not_found
    def execute_argv(
        self,
        argv: list[str],
        stdin: str = "",
        timeout_s: int = 30,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute explicit argv via /api/ssh/execute-argv.

        Uses shlex.join on the Gateway side — no bash -c wrapping.
        """
        sid = session_id or self._require_session_id()
        return self._post(
            "/api/ssh/execute-argv",
            {
                "session_id": sid,
                "argv": argv,
                "stdin": stdin,
                "timeout_s": timeout_s,
            },
        )
```

- [ ] **Step 4: Add MCP tool execute_argv to server.py**

In `examples/mcp_server/server.py`, find the section where tools are registered (after existing `@mcp.tool()` functions). Add:

```python
@mcp.tool()
def execute_argv(
    session_id: str,
    argv: list[str],
    stdin: str = "",
    timeout_s: int = 30,
) -> dict[str, Any]:
    """Execute explicit argv serialized as a safely quoted POSIX command.

    Args:
        session_id: Active SSH session ID.
        argv: Command and arguments as a list.
        stdin: Optional stdin content (UTF-8 only).
        timeout_s: Execution timeout (1-3600).

    Returns:
        Contract v1 dict with stdout/stderr/exit_code (not a JSON string).
    """
    client = GatewayClient(session_id=session_id)
    try:
        raw = client.execute_argv(
            argv=argv,
            stdin=stdin,
            timeout_s=timeout_s,
        )
    except GatewayClientError as e:
        return tool_error(
            "execute_argv",
            code="TOOL_EXECUTION_FAILED",
            message=str(e),
            tool_name="execute_argv",
        )
    return tool_success(
        build_command_result(
            outcome="passed" if raw.get("exit_code", 1) == 0 else "failed",
            exit_code=raw.get("exit_code", -1),
            stdout=raw.get("stdout", ""),
            stderr=raw.get("stderr", ""),
            execution_duration_ms=int(raw.get("duration", 0) * 1000),
        ),
        tool_name="execute_argv",
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_mcp_execute_argv.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add examples/mcp_server/gateway_client.py examples/mcp_server/server.py tests/test_mcp_execute_argv.py
git commit -m "feat: add MCP execute_argv tool and GatewayClient.execute_argv method"
```

---

## Part B: Project Patch Apply

### Task B1: Add unidiff dependency

**Files:**
- Modify: `pyproject.toml`
- Test: `tests/test_patch_apply_models.py`

**Interfaces:**
- Produces: `unidiff` in project dependencies

- [ ] **Step 1: Write failing test for import**

```python
# tests/test_patch_apply_models.py
"""Tests for patch apply models and unidiff import."""

from app.models import ProjectPatchApplyRequest, ProjectPatchApplyResponse


def test_unidiff_importable():
    import unidiff
    assert hasattr(unidiff, "PatchSet")


def test_patch_apply_request_valid():
    req = ProjectPatchApplyRequest(
        session_id="abc",
        project="myproject",
        patch="--- a/file.py\n+++ b/file.py\n@@ -1,3 +1,4 @@\n line1\n+new\n line2\n line3\n",
        expected_hashes={"file.py": "sha256:abcdef"},
    )
    assert req.session_id == "abc"
    assert req.project == "myproject"
    assert req.strip == 1
    assert req.dry_run is False


def test_patch_apply_request_empty_patch_rejected():
    from pydantic import ValidationError

    try:
        ProjectPatchApplyRequest(
            session_id="x", project="p", patch="", expected_hashes={}
        )
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_patch_apply_request_empty_project_rejected():
    from pydantic import ValidationError

    try:
        ProjectPatchApplyRequest(
            session_id="x",
            project="",
            patch="--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
            expected_hashes={},
        )
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_patch_apply_request_strip_bounds():
    from pydantic import ValidationError

    try:
        ProjectPatchApplyRequest(
            session_id="x",
            project="p",
            patch="--- a/f\n+++ b/f\n",
            expected_hashes={},
            strip=-1,
        )
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_patch_apply_response():
    resp = ProjectPatchApplyResponse(
        success=True,
        files_applied=1,
        files_failed=0,
        hunks_applied=3,
        preview=None,
        errors=[],
    )
    assert resp.success is True
    assert resp.files_applied == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_patch_apply_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'ProjectPatchApplyRequest'`

- [ ] **Step 3: Add unidiff to pyproject.toml**

In `pyproject.toml`, add `"unidiff"` to the `dependencies` list:

```toml
dependencies = [
  "fastapi",
  "uvicorn[standard]",
  "mcp>=1.28.0",
  "paramiko",
  "pydantic",
  "pydantic-settings",
  "python-multipart",
  "cryptography",
  "redis",
  "prometheus-client",
  "sqlalchemy",
  "alembic",
  "asyncpg",
  "aiosqlite",
  "aiohttp",
  "slowapi",
  "bcrypt>=4.0",
  "PyJWT>=2.8",
  "websockets",
  "unidiff",
]
```

Then install: `pip install unidiff`

- [ ] **Step 4: Add ProjectPatchApplyRequest and ProjectPatchApplyResponse models**

In `app/models.py`, after `PatchApplyResponse` (around line 456), add:

```python
class ProjectPatchApplyRequest(BaseModel):
    """Request to apply a unified diff patch to project files."""

    session_id: str = Field(..., min_length=1)
    project: str = Field(..., min_length=1)
    patch: str = Field(..., min_length=1, max_length=1_048_576)
    expected_hashes: dict[str, str] = Field(default_factory=dict)
    strip: int = Field(default=1, ge=0)
    dry_run: bool = Field(default=False)


class ProjectPatchFileResult(BaseModel):
    """Result of applying patch to a single file."""

    path: str
    status: str  # "applied", "skipped", "failed"
    hunks_applied: int = 0
    error: str | None = None


class ProjectPatchApplyResponse(BaseModel):
    """Response after applying a unified diff patch."""

    success: bool = True
    files_applied: int = 0
    files_failed: int = 0
    hunks_applied: int = 0
    preview: str | None = None
    errors: list[ProjectPatchFileResult] = Field(default_factory=list)
    files: list[ProjectPatchFileResult] = Field(default_factory=list)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_patch_apply_models.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml app/models.py tests/test_patch_apply_models.py
git commit -m "feat: add unidiff dep and ProjectPatchApply request/response models"
```

---

### Task B2: PatchApplier class (core logic)

**Files:**
- Create: `app/patch_apply.py`
- Test: `tests/test_patch_apply.py`

**Interfaces:**
- Consumes: `unidiff.PatchSet`, `app.ssh_manager.SSHSessionManager.execute()` and `execute_argv()`, `Path` operations
- Produces: `PatchApplier.apply_patch(session_id, patch_str, expected_hashes, project_root, strip, dry_run) -> PatchResult`

- [ ] **Step 1: Write failing test for validation and parsing**

```python
# tests/test_patch_apply.py
"""Tests for PatchApplier: validation, parsing, hash check, dry_run."""

import hashlib
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_parse_patch_single_file():
    from app.patch_apply import PatchApplier

    patch_text = textwrap.dedent("""\
        --- a/src/foo.py
        +++ b/src/foo.py
        @@ -1,3 +1,4 @@
         line1
        +added line
         line2
         line3
    """)
    applier = PatchApplier.__new__(PatchApplier)
    files = applier._parse_patch(patch_text, strip=1)
    assert len(files) == 1
    assert files[0]["path"] == "src/foo.py"
    assert len(files[0]["hunks"]) == 1


def test_parse_patch_multiple_files():
    from app.patch_apply import PatchApplier

    patch_text = textwrap.dedent("""\
        --- a/a.py
        +++ b/a.py
        @@ -1 +1 @@
        -old
        +new
        --- a/b.py
        +++ b/b.py
        @@ -1 +1 @@
        -old
        +new
    """)
    applier = PatchApplier.__new__(PatchApplier)
    files = applier._parse_patch(patch_text, strip=1)
    assert len(files) == 2


def test_validate_limits_too_many_files():
    from app.patch_apply import PatchApplier, PatchValidationError

    applier = PatchApplier.__new__(PatchApplier)
    with pytest.raises(PatchValidationError, match="20 files"):
        applier._validate_file_count(21)


def test_validate_limits_too_many_hunks():
    from app.patch_apply import PatchApplier, PatchValidationError

    applier = PatchApplier.__new__(PatchApplier)
    with pytest.raises(PatchValidationError, match="100 hunks"):
        applier._validate_hunk_count(101)


def test_validate_limits_patch_too_large():
    from app.patch_apply import PatchApplier, PatchValidationError

    applier = PatchApplier.__new__(PatchApplier)
    with pytest.raises(PatchValidationError, match="1 MiB"):
        applier._validate_patch_size(1_048_577)


def test_compute_file_hash():
    from app.patch_apply import PatchApplier

    applier = PatchApplier.__new__(PatchApplier)
    content = "hello world\n"
    h = applier._compute_sha256(content)
    expected = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert h == expected


def test_check_hash_match():
    from app.patch_apply import PatchApplier, HashMismatchError

    applier = PatchApplier.__new__(PatchApplier)
    content = "hello\n"
    correct_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Should not raise
    applier._check_hash("file.py", content, correct_hash)


def test_check_hash_mismatch():
    from app.patch_apply import PatchApplier, HashMismatchError

    applier = PatchApplier.__new__(PatchApplier)
    content = "hello\n"
    wrong_hash = "sha256:0000000000000000000000000000000000000000000000000000000000000000"

    with pytest.raises(HashMismatchError, match="file.py"):
        applier._check_hash("file.py", content, wrong_hash)


def test_forbid_binary_operations():
    from app.patch_apply import PatchApplier, PatchValidationError

    applier = PatchApplier.__new__(PatchApplier)
    patch_text = textwrap.dedent("""\
        --- a/old.txt
        +++ b/new.txt
        @@ -1 +1 @@
        -old
        +new
    """)
    files = applier._parse_patch(patch_text, strip=1)
    with pytest.raises(PatchValidationError, match="rename/copy"):
        applier._validate_no_forbidden_ops(files)


def test_forbid_dev_null():
    from app.patch_apply import PatchApplier, PatchValidationError

    applier = PatchApplier.__new__(PatchApplier)
    patch_text = textwrap.dedent("""\
        --- /dev/null
        +++ b/new.py
        @@ -0,0 +1 @@
        +content
    """)
    files = applier._parse_patch(patch_text, strip=0)
    with pytest.raises(PatchValidationError, match="/dev/null"):
        applier._validate_no_forbidden_ops(files)


def test_apply_in_memory():
    from app.patch_apply import PatchApplier

    applier = PatchApplier.__new__(PatchApplier)
    original = "line1\nline2\nline3\n"
    patch_text = textwrap.dedent("""\
        --- a/file.py
        +++ b/file.py
        @@ -1,3 +1,4 @@
         line1
        +added
         line2
         line3
    """)
    files = applier._parse_patch(patch_text, strip=1)
    result = applier._apply_in_memory(original, files[0])
    assert "added" in result
    assert "line1" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_patch_apply.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.patch_apply'`

- [ ] **Step 3: Implement PatchApplier**

Create `app/patch_apply.py`:

```python
"""Unified diff patch apply with validation, hash checks, and transactional writes."""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

import unidiff

logger = logging.getLogger(__name__)


class PatchValidationError(ValueError):
    """Raised when patch validation fails."""


class HashMismatchError(ValueError):
    """Raised when file hash doesn't match expected."""


class RollbackFailedError(RuntimeError):
    """Raised when rollback after failed write also fails."""


@dataclass
class FileApplyResult:
    """Result of applying patch to a single file."""

    path: str
    status: str  # "applied", "skipped", "dry_run", "failed"
    hunks_applied: int = 0
    error: str | None = None


@dataclass
class PatchResult:
    """Result of applying a patch."""

    success: bool
    files_applied: int
    files_failed: int
    hunks_applied: int
    preview: str | None = None
    errors: list[FileApplyResult] = field(default_factory=list)
    files: list[FileApplyResult] = field(default_factory=list)


class PatchApplier:
    """Apply unified diff patches with validation and transactional writes."""

    MAX_FILES = 20
    MAX_HUNKS = 100
    MAX_PATCH_SIZE = 1_048_576  # 1 MiB
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MiB

    def _parse_patch(self, patch_text: str, strip: int = 1) -> list[dict]:
        """Parse unified diff into per-file dicts with path and hunks."""
        patch_set = unidiff.PatchSet(patch_text)
        result = []
        for patched_file in patch_set:
            source_file = patched_file.source_file
            # Apply strip to path
            parts = source_file.split("/")
            if strip > 0 and len(parts) > strip:
                path = "/".join(parts[strip:])
            elif source_file.startswith("a/"):
                path = source_file[2:]
            else:
                path = source_file

            hunks = []
            for hunk in patched_file:
                hunks.append({
                    "source_start": hunk.source_start,
                    "source_length": hunk.source_length,
                    "target_start": hunk.target_start,
                    "target_length": hunk.target_length,
                    "lines": [str(line) for line in hunk],
                })

            result.append({
                "path": path,
                "hunks": hunks,
                "hunk_count": len(hunks),
                "is_rename": patched_file.is_rename,
                "is_copy": patched_file.is_copy,
                "is_device_file": patched_file.is_device_file,
                "added": patched_file.added,
                "removed": patched_file.removed,
            })
        return result

    def _validate_file_count(self, count: int) -> None:
        if count > self.MAX_FILES:
            raise PatchValidationError(
                f"Patch contains {count} files, exceeds limit of {self.MAX_FILES}"
            )

    def _validate_hunk_count(self, count: int) -> None:
        if count > self.MAX_HUNKS:
            raise PatchValidationError(
                f"Patch contains {count} hunks, exceeds limit of {self.MAX_HUNKS}"
            )

    def _validate_patch_size(self, size: int) -> None:
        if size > self.MAX_PATCH_SIZE:
            raise PatchValidationError(
                f"Patch size {size} bytes exceeds limit of {self.MAX_PATCH_SIZE}"
            )

    def _validate_no_forbidden_ops(self, files: list[dict]) -> None:
        for f in files:
            if f.get("is_rename"):
                raise PatchValidationError(
                    "v1: rename/copy operations are not supported"
                )
            if f.get("is_copy"):
                raise PatchValidationError(
                    "v1: rename/copy operations are not supported"
                )
            if f.get("is_device_file"):
                raise PatchValidationError(
                    "v1: /dev/null paths are not supported"
                )
            if f["path"] == "/dev/null" or f["path"].endswith("/dev/null"):
                raise PatchValidationError(
                    "v1: /dev/null paths are not supported"
                )

    def _compute_sha256(self, content: str) -> str:
        """Compute sha256 hash of content with 'sha256:' prefix."""
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _check_hash(self, path: str, content: str, expected: str) -> None:
        """Verify file content matches expected hash."""
        actual = self._compute_sha256(content)
        if actual != expected:
            raise HashMismatchError(
                f"Hash mismatch for '{path}': expected {expected}, got {actual}"
            )

    def _apply_in_memory(self, original: str, file_info: dict) -> str:
        """Apply hunks to original content in memory, return new content."""
        lines = original.splitlines(keepends=True)
        # Rebuild PatchSet for this specific file
        patch_text = self._rebuild_patch_for_file(file_info)
        patch_set = unidiff.PatchSet(patch_text)

        if not patch_set:
            return original

        patched_file = patch_set[0]
        result_lines = []
        source_idx = 0

        for hunk in patched_file:
            # Add context before hunk
            while source_idx < hunk.source_start - 1 and source_idx < len(lines):
                result_lines.append(lines[source_idx])
                source_idx += 1

            # Process hunk lines
            for line in hunk:
                if line.is_added:
                    result_lines.append(line.value)
                elif line.is_removed:
                    source_idx += 1
                elif line.is_context:
                    result_lines.append(line.value)
                    source_idx += 1

        # Add remaining lines
        while source_idx < len(lines):
            result_lines.append(lines[source_idx])
            source_idx += 1

        return "".join(result_lines)

    def _rebuild_patch_for_file(self, file_info: dict) -> str:
        """Rebuild a minimal unified diff string for a single file."""
        lines = [f"--- a/{file_info['path']}", f"+++ b/{file_info['path']}"]
        for hunk in file_info["hunks"]:
            lines.append(
                f"@@ -{hunk['source_start']},{hunk['source_length']} "
                f"+{hunk['target_start']},{hunk['target_length']} @@"
            )
            lines.extend(hunk["lines"])
        return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_patch_apply.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/patch_apply.py tests/test_patch_apply.py
git commit -m "feat: add PatchApplier with validation, parsing, hash checks, and in-memory apply"
```

---

### Task B3: Add scope, project:patch route, and transactional write

**Files:**
- Modify: `app/auth_middleware.py:56-64` (add scope)
- Modify: `app/routers/files.py` (add route)
- Test: `tests/test_patch_apply_route.py`

**Interfaces:**
- Consumes: `PatchApplier` (Task B2), `ProjectPatchApplyRequest`/`Response` (Task B1), `ProjectRegistry`, `require_scope("project:patch")`, `ensure_session_owner()`
- Produces: `POST /api/projects/{project}/apply-patch` endpoint

- [ ] **Step 1: Write failing test**

```python
# tests/test_patch_apply_route.py
"""Tests for POST /api/projects/{project}/apply-patch endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def _auth_headers():
    from app.config import settings
    return {"X-API-Key": settings.api_key}


def test_apply_patch_requires_auth(client):
    resp = client.post(
        "/api/projects/myproject/apply-patch",
        json={
            "session_id": "x",
            "patch": "--- a/f\n+++ b/f\n",
            "expected_hashes": {},
        },
    )
    assert resp.status_code == 401


def test_apply_patch_empty_patch_rejected(client):
    resp = client.post(
        "/api/projects/myproject/apply-patch",
        json={
            "session_id": "x",
            "patch": "",
            "expected_hashes": {},
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_apply_patch_session_not_found(client):
    from app.auth_middleware import AuthIdentity

    with patch("app.routers.files._state") as mock_state:
        mock_state.manager.get_session = AsyncMock(return_value=None)
        resp = client.post(
            "/api/projects/myproject/apply-patch",
            json={
                "session_id": "nonexistent",
                "patch": "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n",
                "expected_hashes": {},
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_patch_apply_route.py -v`
Expected: FAIL — endpoint does not exist (404)

- [ ] **Step 3: Add project:patch scope**

In `app/auth_middleware.py`, add `"project:patch"` to `VALID_AGENT_SCOPES`:

```python
VALID_AGENT_SCOPES: set[str] = {
    "ssh:connect",
    "ssh:execute",
    "ssh:execute:argv",
    "ssh:disconnect",
    "ssh:files",
    "ssh:port-check",
    "jobs:read",
    "jobs:run",
    "project:patch",
}
```

- [ ] **Step 4: Add the endpoint to files.py**

In `app/routers/files.py`, add the necessary imports at the top:

```python
import hashlib
import os
import time
import uuid
from pathlib import Path

from app.auth_middleware import (
    AuthIdentity,
    ensure_session_owner,
    require_master_key,
    require_scope,
)
from app.models import (
    ProjectPatchApplyRequest,
    ProjectPatchApplyResponse,
    ProjectPatchFileResult,
)
from app.patch_apply import (
    HashMismatchError,
    PatchApplier,
    PatchResult,
    PatchValidationError,
    RollbackFailedError,
)
```

Add the endpoint (after the existing `file_patch` endpoint, around line 151):

```python
@router.post("/api/projects/{project}/apply-patch", response_model=ProjectPatchApplyResponse)
async def project_apply_patch(
    project: str,
    req: ProjectPatchApplyRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("project:patch")),
):
    """Apply a unified diff patch to project files with hash verification and rollback."""
    # Session ownership
    session = await _state.manager.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=_err(404, "Session not found"))
    ensure_session_owner(session, _identity)

    applier = PatchApplier()
    rid = uuid.uuid4().hex[:12]

    try:
        # Validate patch size
        applier._validate_patch_size(len(req.patch.encode("utf-8")))

        # Parse patch
        files = applier._parse_patch(req.patch, strip=req.strip)

        # Validate limits
        applier._validate_file_count(len(files))
        total_hunks = sum(f["hunk_count"] for f in files)
        applier._validate_hunk_count(total_hunks)

        # Validate forbidden ops
        applier._validate_no_forbidden_ops(files)

        # Resolve project path via registry (imported lazily)
        from examples.mcp_server.project_registry import get_project_registry

        registry = get_project_registry()
        try:
            project_root = registry.resolve(project)
        except ValueError as exc:
            raise HTTPException(
                status_code=404, detail=_err(404, str(exc))
            ) from exc

        file_results: list[ProjectPatchFileResult] = []

        # Dry run: apply in memory only
        if req.dry_run:
            preview_parts = []
            for f in files:
                full_path = project_root / f["path"]
                if full_path.exists():
                    original = full_path.read_text(encoding="utf-8", errors="replace")
                else:
                    original = ""
                new_content = applier._apply_in_memory(original, f)
                if original != new_content:
                    preview_parts.append(f"--- {f['path']}\n+++ {f['path']}\n{new_content}")
                file_results.append(
                    ProjectPatchFileResult(
                        path=f["path"],
                        status="dry_run",
                        hunks_applied=f["hunk_count"],
                    )
                )
            return ProjectPatchApplyResponse(
                success=True,
                files_applied=len(files),
                files_failed=0,
                hunks_applied=total_hunks,
                preview="\n".join(preview_parts),
                files=file_results,
            )

        # Apply: read, verify hashes, apply in memory
        prepared: list[tuple[Path, str, str, int]] = []  # (path, original, new_content, hunk_count)

        for f in files:
            full_path = project_root / f["path"]

            # Check file size
            if full_path.exists():
                file_size = full_path.stat().st_size
                if file_size > applier.MAX_FILE_SIZE:
                    raise PatchValidationError(
                        f"File '{f['path']}' is {file_size} bytes, exceeds {applier.MAX_FILE_SIZE} limit"
                    )
                original = full_path.read_text(encoding="utf-8", errors="replace")
            else:
                original = ""

            # Verify hash for existing files
            if f["path"] in req.expected_hashes and full_path.exists():
                applier._check_hash(f["path"], original, req.expected_hashes[f["path"]])

            new_content = applier._apply_in_memory(original, f)
            prepared.append((full_path, original, new_content, f["hunk_count"]))

        # Transactional write with rollback
        completed: list[tuple[Path, str]] = []  # (path, backup_path) for rollback

        for full_path, original, new_content, hunk_count in prepared:
            backup = full_path.parent / f".{full_path.name}.mcp-patch-{rid}.bak"
            tmp = full_path.parent / f".{full_path.name}.mcp-patch-{rid}.tmp"

            try:
                # Backup
                if full_path.exists():
                    import shutil
                    shutil.copy2(str(full_path), str(backup))
                else:
                    # New file — create empty backup marker
                    backup.write_text("", encoding="utf-8")

                # Write temp file
                tmp.write_text(new_content, encoding="utf-8")

                # fsync
                fd = os.open(str(tmp), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

                # Atomic rename
                os.rename(str(tmp), str(full_path))

                completed.append((full_path, backup))

            except Exception as exc:
                logger.error("Patch write failed for %s: %s", full_path, exc)
                # Rollback completed files
                rollback_errors = []
                for rb_path, rb_backup in completed:
                    try:
                        os.rename(str(rb_backup), str(rb_path))
                    except Exception as rb_exc:
                        rollback_errors.append(f"{rb_path}: {rb_exc}")
                        logger.error("Rollback failed for %s: %s", rb_path, rb_exc)

                # Cleanup temp files
                for rb_path, _ in completed:
                    tmp_rb = rb_path.parent / f".{rb_path.name}.mcp-patch-{rid}.tmp"
                    try:
                        tmp_rb.unlink(missing_ok=True)
                    except Exception:
                        pass

                if rollback_errors:
                    raise RollbackFailedError(
                        f"Write failed for {full_path} and rollback also failed: "
                        + "; ".join(rollback_errors)
                    )

                file_results.append(
                    ProjectPatchFileResult(
                        path=str(full_path.relative_to(project_root)),
                        status="failed",
                        error=str(exc),
                    )
                )
                return ProjectPatchApplyResponse(
                    success=False,
                    files_applied=0,
                    files_failed=1,
                    hunks_applied=0,
                    errors=file_results,
                    files=file_results,
                )

        # Cleanup backups on success
        for rb_path, rb_backup in completed:
            try:
                rb_backup.unlink(missing_ok=True)
            except Exception:
                pass
            # Cleanup any leftover temp files
            tmp = rb_path.parent / f".{rb_path.name}.mcp-patch-{rid}.tmp"
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

        file_results = [
            ProjectPatchFileResult(
                path=str(p.relative_to(project_root)),
                status="applied",
                hunks_applied=h,
            )
            for p, _, _, h in prepared
        ]

        return ProjectPatchApplyResponse(
            success=True,
            files_applied=len(prepared),
            files_failed=0,
            hunks_applied=sum(h for _, _, _, h in prepared),
            files=file_results,
        )

    except (PatchValidationError, HashMismatchError) as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc
    except RollbackFailedError as exc:
        raise HTTPException(status_code=500, detail=_err(500, str(exc))) from exc
    except Exception as exc:
        logger.error("Patch apply failed: %s", exc)
        raise HTTPException(
            status_code=500, detail=_err(500, f"Patch apply failed: {exc}")
        ) from exc
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_patch_apply_route.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add app/auth_middleware.py app/routers/files.py tests/test_patch_apply_route.py
git commit -m "feat: add POST /api/projects/{project}/apply-patch endpoint with rollback"
```

---

### Task B4: MCP tool project_apply_patch + GatewayClient method

**Files:**
- Modify: `examples/mcp_server/gateway_client.py` (add method)
- Modify: `examples/mcp_server/server.py` (add tool)
- Test: `tests/test_mcp_project_apply_patch.py`

**Interfaces:**
- Consumes: `GatewayClient._post()`, `tool_success`, `tool_error`
- Produces: `GatewayClient.apply_patch()`, MCP tool `project_apply_patch`

- [ ] **Step 1: Write failing test**

```python
# tests/test_mcp_project_apply_patch.py
"""Tests for MCP project_apply_patch tool and GatewayClient method."""

from unittest.mock import MagicMock, patch

import pytest


def test_gateway_client_apply_patch_calls_correct_endpoint():
    from examples.mcp_server.gateway_client import GatewayClient

    client = GatewayClient.__new__(GatewayClient)
    client.base_url = "http://test:8085"
    client.api_key = "test-key"
    client.session_id = "test-session"
    client.command_timeout = 30
    client.job_timeout = 180
    client._reconnect_lock = MagicMock()
    client._ssh_host = ""
    client._ssh_port = 22
    client._ssh_user = ""
    client._ssh_password = ""
    client._ssh_private_key = ""

    patch_text = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n"
    hashes = {"f": "sha256:abc"}

    with patch.object(client, "_post", return_value={"success": True, "files_applied": 1}) as mock_post:
        result = client.apply_patch(
            project="myproject",
            patch=patch_text,
            expected_hashes=hashes,
            strip=1,
            dry_run=False,
        )

    mock_post.assert_called_once_with(
        "/api/projects/myproject/apply-patch",
        {
            "session_id": "test-session",
            "patch": patch_text,
            "expected_hashes": hashes,
            "strip": 1,
            "dry_run": False,
        },
    )
    assert result["success"] is True


def test_mcp_project_apply_patch_tool_exists():
    from examples.mcp_server.server import mcp

    tool_names = [t.name for t in mcp._tool_manager._tools.values()]
    assert "project_apply_patch" in tool_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_project_apply_patch.py -v`
Expected: FAIL — `apply_patch` method not found on `GatewayClient` or tool not registered

- [ ] **Step 3: Add apply_patch to GatewayClient**

In `examples/mcp_server/gateway_client.py`, add after `execute_argv` (Task A4 method):

```python
    @_retry_on_session_not_found
    def apply_patch(
        self,
        project: str,
        patch: str,
        expected_hashes: dict[str, str],
        strip: int = 1,
        dry_run: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply a unified diff patch to project files via Gateway."""
        sid = session_id or self._require_session_id()
        proj = _safe_project(project)
        return self._post(
            f"/api/projects/{proj}/apply-patch",
            {
                "session_id": sid,
                "patch": patch,
                "expected_hashes": expected_hashes,
                "strip": strip,
                "dry_run": dry_run,
            },
        )
```

- [ ] **Step 4: Add MCP tool project_apply_patch to server.py**

In `examples/mcp_server/server.py`, add after the `execute_argv` tool:

```python
@mcp.tool()
def project_apply_patch(
    session_id: str,
    project: str,
    patch: str,
    expected_hashes: dict[str, str],
    strip: int = 1,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a unified diff patch to project files.

    Args:
        session_id: Active SSH session ID.
        project: Project name (registered in MCP_GATEWAY_PROJECT_ROOT).
        patch: Unified diff content.
        expected_hashes: Per-file sha256 hashes for safety check.
        strip: Strip leading path components (default 1 for a/b prefix).
        dry_run: Preview changes without applying.

    Returns:
        Contract v1 dict with per-file status (not a JSON string).
    """
    _validate_project(project)
    client = GatewayClient(session_id=session_id)
    try:
        raw = client.apply_patch(
            project=project,
            patch=patch,
            expected_hashes=expected_hashes,
            strip=strip,
            dry_run=dry_run,
        )
    except GatewayClientError as e:
        return tool_error(
            "project_apply_patch",
            code="TOOL_EXECUTION_FAILED",
            message=str(e),
            tool_name="project_apply_patch",
        )
    return tool_success(
        {
            "success": raw.get("success", False),
            "files_applied": raw.get("files_applied", 0),
            "files_failed": raw.get("files_failed", 0),
            "hunks_applied": raw.get("hunks_applied", 0),
            "preview": raw.get("preview"),
            "errors": raw.get("errors", []),
            "files": raw.get("files", []),
        },
        tool_name="project_apply_patch",
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_mcp_project_apply_patch.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add examples/mcp_server/gateway_client.py examples/mcp_server/server.py tests/test_mcp_project_apply_patch.py
git commit -m "feat: add MCP project_apply_patch tool and GatewayClient.apply_patch method"
```

---

### Task B5: Integration test for full patch flow

**Files:**
- Create: `tests/test_patch_apply_integration.py`

**Interfaces:**
- Consumes: PatchApplier, models, route (Tasks B1-B4)

- [ ] **Step 1: Write integration test**

```python
# tests/test_patch_apply_integration.py
"""Integration tests for full patch apply flow: parse → validate → apply → rollback."""

import hashlib
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_full_dry_run_flow():
    from app.patch_apply import PatchApplier

    applier = PatchApplier()
    patch_text = textwrap.dedent("""\
        --- a/src/app.py
        +++ b/src/app.py
        @@ -1,3 +1,4 @@
         def hello():
        +    print("hi")
             pass
         """)
    files = applier._parse_patch(patch_text, strip=1)
    applier._validate_file_count(len(files))
    total_hunks = sum(f["hunk_count"] for f in files)
    applier._validate_hunk_count(total_hunks)
    applier._validate_no_forbidden_ops(files)

    original = textwrap.dedent("""\
        def hello():
            pass
        """)
    new_content = applier._apply_in_memory(original, files[0])

    assert 'print("hi")' in new_content
    assert "def hello():" in new_content


def test_hash_check_prevents_stale_apply():
    from app.patch_apply import HashMismatchError, PatchApplier

    applier = PatchApplier()
    content = "original content\n"
    expected = "sha256:" + hashlib.sha256(b"wrong content\n").hexdigest()

    with pytest.raises(HashMismatchError):
        applier._check_hash("file.py", content, expected)


def test_multiple_hunks_apply():
    from app.patch_apply import PatchApplier

    applier = PatchApplier()
    patch_text = textwrap.dedent("""\
        --- a/file.py
        +++ b/file.py
        @@ -1,3 +1,3 @@
         line1
        -old middle
        +new middle
         line3
        @@ -10,3 +10,3 @@
         line10
        -old2
        +new2
         line12
    """)
    files = applier._parse_patch(patch_text, strip=1)
    assert len(files) == 1
    assert files[0]["hunk_count"] == 2

    original = textwrap.dedent("""\
        line1
        old middle
        line3
        line4
        line5
        line6
        line7
        line8
        line9
        line10
        old2
        line12
    """)
    new_content = applier._apply_in_memory(original, files[0])
    assert "new middle" in new_content
    assert "new2" in new_content
    assert "old middle" not in new_content
    assert "old2" not in new_content
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_patch_apply_integration.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run full test suite**

Run: `pytest -q`
Expected: All tests pass (including all new tests)

- [ ] **Step 4: Commit**

```bash
git add tests/test_patch_apply_integration.py
git commit -m "test: add integration tests for full patch apply flow"
```

---

### Task B6: Lint and typecheck

**Files:**
- All files from Tasks A1-A4, B1-B5

**Interfaces:**
- Consumes: ruff, mypy

- [ ] **Step 1: Run ruff format and check**

Run: `ruff format app/ tests/ examples/mcp_server/ && ruff check app/ tests/ examples/mcp_server/`
Expected: No errors (or auto-fixable only)

- [ ] **Step 2: Run mypy**

Run: `mypy app/patch_apply.py app/models.py app/routers/ssh.py app/routers/files.py`
Expected: No errors

- [ ] **Step 3: Fix any lint/type issues**

Fix all reported issues inline.

- [ ] **Step 4: Run full test suite again**

Run: `pytest -q`
Expected: All tests pass

- [ ] **Step 5: Commit if needed**

```bash
git add -A
git commit -m "style: lint and typecheck fixes for P2 features"
```

---

## Self-Review

1. **Spec coverage:**
   - §6 execute_argv: ✅ Gateway endpoint, validation (argv list, NUL-free, length, stdin limit, timeout bounds, session ownership, command policy), shlex.join serialization, no bash -c, stdin concurrent with stdout/stderr, stdout/stderr truncation, MCP tool with docstring
   - §7 Patch apply: ✅ Gateway endpoint, unidiff parsing, ProjectRegistry validation, expected_hashes, dry_run, transactional write with rollback, temp/backup naming, v1 forbidden list (binary/rename/copy/mode/symlink/devnull), limits (20 files, 100 hunks, 1 MiB patch, 10 MiB file), MCP tool

2. **Placeholder scan:** No TBD, TODO, "implement later", "add validation", "handle edge cases", "write tests", or "similar to Task N" found.

3. **Type consistency:**
   - `ExecuteArgvRequest.argv: list[str]` matches `execute_argv(session_id, argv, ...)` signatures
   - `ProjectPatchApplyRequest.patch: str` matches `apply_patch(project, patch, ...)` signatures
   - `PatchApplier._parse_patch()` → list[dict] with "path", "hunks", "hunk_count" used consistently
   - All MCP tools return `tool_success`/`tool_error` with `build_command_result` for execute_argv
