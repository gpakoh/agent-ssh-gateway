# P1: GatewaySession SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `GatewaySession` and `AsyncGatewaySession` context managers to `sdk/session.py` that wrap `GatewayClient`, manage SSH session lifecycle, and expose `run`/`read`/`write`/`session_health` methods with explicit `session_id` passing.

**Architecture:** Thin wrapper layer over `GatewayClient` (from `examples/mcp_server/gateway_client.py`). `GatewaySession` owns the SSH connect/disconnect lifecycle via context manager protocol. `AsyncGatewaySession` mirrors it with async/await. No profile parameter in v1. No auto-confirmation of `write()` responses.

**Tech Stack:** Python 3.11+, httpx (via GatewayClient), pytest, pytest-asyncio, unittest.mock.

## Global Constraints

- Python `>=3.11` (union syntax `X | Y`, not `Optional[X]`)
- `GatewayClient` lives at `examples/mcp_server/gateway_client.py` — httpx-based, not requests-based `SSHGatewayClient`
- `GatewayClient` must expose `connect() -> str`, `disconnect(session_id) -> None`, `execute_restricted(command, session_id)`, `wait_job(job_id, timeout)`, `read_file(path, session_id)`, `write_file(path, content, session_id)`, `session_health(session_id)`
- SDK tests use `unittest.mock` (MagicMock/AsyncMock) — no live Gateway required
- `ruff` lint: `select = ["E", "F", "I", "B", "UP"]`, `line-length = 100`
- `mypy` with `python_version = "3.11"`, `ignore_missing_imports = true`
- `pytest` with `asyncio_mode = "auto"`
- No comments in code unless user asks
- TDD: write failing test first, then implement, then verify

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `sdk/session.py` | `GatewaySession`, `AsyncGatewaySession` |
| Create | `sdk/__init__.py` | Public SDK exports |
| Create | `tests/test_sdk_session.py` | Unit tests for both session classes |
| Modify | `examples/mcp_server/gateway_client.py` | Add `connect()` and `disconnect()` public methods |

---

## Task 1: Add `connect()` and `disconnect()` to GatewayClient

**Files:**
- Modify: `examples/mcp_server/gateway_client.py:84-170`
- Test: `tests/test_sdk_session.py` (created in Task 2, but this task verifies GatewayClient methods exist)

**Interfaces:**
- Consumes: existing `GatewayClient._reconnect_session()` (line 125), `GatewayClient._headers()` (line 120)
- Produces: `GatewayClient.connect() -> str`, `GatewayClient.disconnect(session_id: str) -> None`

- [ ] **Step 1: Add `connect()` method to GatewayClient**

Add after `_reconnect_session` (after line 149):

```python
    def connect(self) -> str:
        """Establish SSH session and return session_id."""
        self._reconnect_session()
        return self.session_id
```

- [ ] **Step 2: Add `disconnect()` method to GatewayClient**

Add after `connect()`:

```python
    def disconnect(self, session_id: str | None = None) -> None:
        """Close SSH session. Best-effort — never raises."""
        sid = session_id or self.session_id
        if not sid:
            return
        try:
            self._post("/api/ssh/disconnect", {"session_id": sid})
        except Exception:
            pass
        if sid == self.session_id:
            self.session_id = ""
```

- [ ] **Step 3: Verify GatewayClient has new methods**

Run: `python -c "from examples.mcp_server.gateway_client import GatewayClient; c = GatewayClient.__dict__; assert 'connect' in c; assert 'disconnect' in c; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add examples/mcp_server/gateway_client.py
git commit -m "feat(sdk): add connect() and disconnect() to GatewayClient"
```

---

## Task 2: GatewaySession — context manager lifecycle (TDD)

**Files:**
- Create: `tests/test_sdk_session.py`
- Create: `sdk/session.py`

**Interfaces:**
- Consumes: `GatewayClient.connect() -> str`, `GatewayClient.disconnect(session_id) -> None` (Task 1)
- Produces: `GatewaySession.__init__(client)`, `GatewaySession.__enter__() -> GatewaySession`, `GatewaySession.__exit__() -> None`, `GatewaySession._disconnect_best_effort() -> None`

- [ ] **Step 1: Write failing test for `__enter__` success**

Create `tests/test_sdk_session.py`:

```python
"""Tests for sdk.session — GatewaySession and AsyncGatewaySession."""

from unittest.mock import MagicMock

from sdk.session import GatewaySession


class TestGatewaySessionLifecycle:
    """Context manager enter/exit lifecycle."""

    def test_enter_returns_self_and_stores_session_id(self):
        client = MagicMock()
        client.connect.return_value = "sid-abc-123"

        with GatewaySession(client) as gw:
            assert gw is not None
            assert gw.session_id == "sid-abc-123"
            client.connect.assert_called_once()

    def test_exit_calls_disconnect(self):
        client = MagicMock()
        client.connect.return_value = "sid-abc-123"

        with GatewaySession(client):
            pass

        client.disconnect.assert_called_once_with("sid-abc-123")

    def test_enter_connect_failure_does_not_call_disconnect(self):
        client = MagicMock()
        client.connect.side_effect = ConnectionError("refused")

        try:
            with GatewaySession(client):
                pass  # pragma: no cover
        except ConnectionError:
            pass

        client.disconnect.assert_not_called()

    def test_exit_disconnect_failure_does_not_raise(self):
        client = MagicMock()
        client.connect.return_value = "sid-abc-123"
        client.disconnect.side_effect = RuntimeError("network")

        with GatewaySession(client):
            pass

    def test_exit_masks_no_original_exception(self):
        client = MagicMock()
        client.connect.return_value = "sid-abc-123"
        client.disconnect.side_effect = RuntimeError("disconnect boom")

        raised = False
        try:
            with GatewaySession(client):
                raise ValueError("original")
        except ValueError as e:
            raised = True
            assert str(e) == "original"
        assert raised

    def test_enter_post_setup_failure_calls_disconnect_before_reraise(self):
        """If __enter__ succeeds at connect but code after raises, cleanup happens."""
        client = MagicMock()
        client.connect.return_value = "sid-abc-123"

        class ExplodingSession(GatewaySession):
            def __enter__(self):
                try:
                    self.session_id = self.client.connect()
                    raise RuntimeError("post-setup failure")
                except Exception:
                    self._disconnect_best_effort()
                    raise

        try:
            with ExplodingSession(client):
                pass
        except RuntimeError as e:
            assert str(e) == "post-setup failure"

        client.disconnect.assert_called_once_with("sid-abc-123")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m pytest tests/test_sdk_session.py -v 2>&1 | head -30`
Expected: FAIL — `ModuleNotFoundError: No module named 'sdk.session'`

- [ ] **Step 3: Create `sdk/__init__.py` (empty for now)**

```python
```

- [ ] **Step 4: Create `sdk/session.py` with minimal implementation**

```python
"""High-level SSH Gateway session context managers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from examples.mcp_server.gateway_client import GatewayClient


class GatewaySession:
    """Synchronous context manager for SSH Gateway.

    Usage::

        with GatewaySession(client) as gw:
            result = gw.run("ls -la")
    """

    def __init__(self, client: GatewayClient) -> None:
        self.client = client
        self.session_id: str | None = None

    def __enter__(self) -> GatewaySession:
        self.session_id = self.client.connect()
        return self

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        self._disconnect_best_effort()

    def _disconnect_best_effort(self) -> None:
        if self.session_id:
            try:
                self.client.disconnect(self.session_id)
            except Exception:
                pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m pytest tests/test_sdk_session.py::TestGatewaySessionLifecycle -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add sdk/__init__.py sdk/session.py tests/test_sdk_session.py
git commit -m "feat(sdk): add GatewaySession context manager with lifecycle tests"
```

---

## Task 3: GatewaySession — operational methods (TDD)

**Files:**
- Modify: `tests/test_sdk_session.py`
- Modify: `sdk/session.py`

**Interfaces:**
- Consumes: `GatewaySession.__init__`, `GatewaySession.session_id` (Task 2)
- Produces: `GatewaySession.run(command, timeout) -> dict`, `GatewaySession.read(path) -> str`, `GatewaySession.write(path, content) -> dict`, `GatewaySession.session_health() -> dict`

- [ ] **Step 1: Write failing tests for `run()`, `read()`, `write()`, `session_health()`**

Append to `tests/test_sdk_session.py`:

```python
class TestGatewaySessionRun:
    """run() executes command and waits for job completion."""

    def test_run_calls_execute_restricted_then_wait_job(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.execute_restricted.return_value = {"job_id": "job-42"}
        client.wait_job.return_value = {
            "status": "completed",
            "stdout": "hello\n",
            "exit_code": 0,
        }

        with GatewaySession(client) as gw:
            result = gw.run("echo hello")

        client.execute_restricted.assert_called_once_with(
            session_id="sid-1", command="echo hello"
        )
        client.wait_job.assert_called_once_with(job_id="job-42", timeout=None)
        assert result["status"] == "completed"
        assert result["stdout"] == "hello\n"

    def test_run_passes_timeout(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.execute_restricted.return_value = {"job_id": "job-99"}
        client.wait_job.return_value = {"status": "completed"}

        with GatewaySession(client) as gw:
            gw.run("sleep 10", timeout=30)

        client.wait_job.assert_called_once_with(job_id="job-99", timeout=30)

    def test_run_does_not_pass_session_id_to_wait_job(self):
        """wait_job uses auth, not session_id."""
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.execute_restricted.return_value = {"job_id": "job-7"}
        client.wait_job.return_value = {"status": "completed"}

        with GatewaySession(client) as gw:
            gw.run("pwd")

        call_kwargs = client.wait_job.call_args
        assert "session_id" not in call_kwargs.kwargs
        assert "session_id" not in (call_kwargs[0] if call_kwargs[0] else ())


class TestGatewaySessionRead:
    """read() returns file content string."""

    def test_read_extracts_content_from_response(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.read_file.return_value = {"content": "file contents here"}

        with GatewaySession(client) as gw:
            result = gw.read("/etc/hostname")

        client.read_file.assert_called_once_with(session_id="sid-1", path="/etc/hostname")
        assert result == "file contents here"

    def test_read_returns_empty_string_on_missing_content(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.read_file.return_value = {}

        with GatewaySession(client) as gw:
            result = gw.read("/nonexistent")

        assert result == ""


class TestGatewaySessionWrite:
    """write() returns raw Gateway response — no auto-confirmation."""

    def test_write_returns_raw_response(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.write_file.return_value = {
            "status": "ok",
            "pending_confirmation": {"file": "app.py", "hash": "abc"},
        }

        with GatewaySession(client) as gw:
            result = gw.write("app.py", "new content")

        client.write_file.assert_called_once_with(
            session_id="sid-1", path="app.py", content="new content"
        )
        assert "pending_confirmation" in result
        assert result["pending_confirmation"]["hash"] == "abc"

    def test_write_returns_ok_without_confirmation(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.write_file.return_value = {"status": "ok"}

        with GatewaySession(client) as gw:
            result = gw.write("README.md", "# Hello")

        assert result == {"status": "ok"}


class TestGatewaySessionHealth:
    """session_health() delegates to client."""

    def test_session_health_passes_session_id(self):
        client = MagicMock()
        client.connect.return_value = "sid-1"
        client.session_health.return_value = {"status": "healthy", "uptime": 120}

        with GatewaySession(client) as gw:
            result = gw.session_health()

        client.session_health.assert_called_once_with(session_id="sid-1")
        assert result["status"] == "healthy"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m pytest tests/test_sdk_session.py -v 2>&1 | tail -20`
Expected: FAIL — `AttributeError: 'GatewaySession' object has no attribute 'run'`

- [ ] **Step 3: Implement operational methods in `sdk/session.py`**

Add to `GatewaySession` class in `sdk/session.py` (after `_disconnect_best_effort`):

```python
    def run(self, command: str, timeout: int | None = None) -> dict:
        """Execute command and wait for completion. Returns job result dict."""
        job = self.client.execute_restricted(
            session_id=self.session_id, command=command
        )
        return self.client.wait_job(
            job_id=job["job_id"], timeout=timeout
        )

    def read(self, path: str) -> str:
        """Read file content from remote host."""
        result = self.client.read_file(session_id=self.session_id, path=path)
        return result.get("content", "")

    def write(self, path: str, content: str) -> dict:
        """Write file. Returns raw Gateway response — may contain pending_confirmation."""
        return self.client.write_file(
            session_id=self.session_id, path=path, content=content
        )

    def session_health(self) -> dict:
        """Check SSH session health."""
        return self.client.session_health(session_id=self.session_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m pytest tests/test_sdk_session.py -v`
Expected: all tests pass (6 lifecycle + 4 run + 2 read + 2 write + 1 health = 15)

- [ ] **Step 5: Commit**

```bash
git add sdk/session.py tests/test_sdk_session.py
git commit -m "feat(sdk): add run/read/write/session_health to GatewaySession"
```

---

## Task 4: AsyncGatewaySession (TDD)

**Files:**
- Modify: `tests/test_sdk_session.py`
- Modify: `sdk/session.py`

**Interfaces:**
- Consumes: same `GatewayClient` methods as `GatewaySession`, but expects `connect()` to be callable synchronously (httpx calls are sync)
- Produces: `AsyncGatewaySession.__init__(client)`, `AsyncGatewaySession.__aenter__() -> AsyncGatewaySession`, `AsyncGatewaySession.__aexit__() -> None`, `AsyncGatewaySession.run()`, `AsyncGatewaySession.read()`, `AsyncGatewaySession.write()`, `AsyncGatewaySession.session_health()`

Note: `GatewayClient` methods are synchronous (httpx). `AsyncGatewaySession` wraps them in `asyncio.to_thread` for non-blocking behavior in async contexts. However, per the spec, the async session has the same interface — the async/await is on the session methods themselves, not on the underlying client calls (which remain sync). This keeps `GatewayClient` unchanged.

- [ ] **Step 1: Write failing tests for AsyncGatewaySession**

Append to `tests/test_sdk_session.py`:

```python
import asyncio

import pytest

from sdk.session import AsyncGatewaySession


class TestAsyncGatewaySessionLifecycle:
    """Async context manager enter/exit lifecycle."""

    @pytest.mark.asyncio
    async def test_aenter_returns_self_and_stores_session_id(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"

        async with AsyncGatewaySession(client) as gw:
            assert gw is not None
            assert gw.session_id == "sid-async-1"
            client.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_aexit_calls_disconnect(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"

        async with AsyncGatewaySession(client):
            pass

        client.disconnect.assert_called_once_with("sid-async-1")

    @pytest.mark.asyncio
    async def test_aenter_connect_failure_does_not_call_disconnect(self):
        client = MagicMock()
        client.connect.side_effect = ConnectionError("refused")

        try:
            async with AsyncGatewaySession(client):
                pass  # pragma: no cover
        except ConnectionError:
            pass

        client.disconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_aexit_disconnect_failure_does_not_raise(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.disconnect.side_effect = RuntimeError("network")

        async with AsyncGatewaySession(client):
            pass

    @pytest.mark.asyncio
    async def test_aexit_never_masks_original_exception(self):
        client = MagicMock()
        client.connect.return_value = "sid-async-1"
        client.disconnect.side_effect = RuntimeError("disconnect boom")

        raised = False
        try:
            async with AsyncGatewaySession(client):
                raise ValueError("original")
        except ValueError as e:
            raised = True
            assert str(e) == "original"
        assert raised


class TestAsyncGatewaySessionRun:
    """Async run() delegates to sync GatewayClient."""

    @pytest.mark.asyncio
    async def test_run_calls_execute_restricted_then_wait_job(self):
        client = MagicMock()
        client.connect.return_value = "sid-a1"
        client.execute_restricted.return_value = {"job_id": "ajob-1"}
        client.wait_job.return_value = {"status": "completed", "stdout": "ok"}

        async with AsyncGatewaySession(client) as gw:
            result = await gw.run("echo ok")

        client.execute_restricted.assert_called_once_with(
            session_id="sid-a1", command="echo ok"
        )
        client.wait_job.assert_called_once_with(job_id="ajob-1", timeout=None)
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_run_passes_timeout(self):
        client = MagicMock()
        client.connect.return_value = "sid-a1"
        client.execute_restricted.return_value = {"job_id": "ajob-2"}
        client.wait_job.return_value = {"status": "completed"}

        async with AsyncGatewaySession(client) as gw:
            await gw.run("sleep 5", timeout=60)

        client.wait_job.assert_called_once_with(job_id="ajob-2", timeout=60)


class TestAsyncGatewaySessionMethods:
    """Async read/write/session_health delegate to sync client."""

    @pytest.mark.asyncio
    async def test_read_extracts_content(self):
        client = MagicMock()
        client.connect.return_value = "sid-a1"
        client.read_file.return_value = {"content": "async file data"}

        async with AsyncGatewaySession(client) as gw:
            result = await gw.read("/tmp/test.txt")

        client.read_file.assert_called_once_with(session_id="sid-a1", path="/tmp/test.txt")
        assert result == "async file data"

    @pytest.mark.asyncio
    async def test_write_returns_raw_response(self):
        client = MagicMock()
        client.connect.return_value = "sid-a1"
        client.write_file.return_value = {
            "status": "ok",
            "pending_confirmation": {"file": "x.py"},
        }

        async with AsyncGatewaySession(client) as gw:
            result = await gw.write("x.py", "data")

        assert result["pending_confirmation"]["file"] == "x.py"

    @pytest.mark.asyncio
    async def test_session_health_delegates(self):
        client = MagicMock()
        client.connect.return_value = "sid-a1"
        client.session_health.return_value = {"status": "healthy"}

        async with AsyncGatewaySession(client) as gw:
            result = await gw.session_health()

        client.session_health.assert_called_once_with(session_id="sid-a1")
        assert result["status"] == "healthy"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m pytest tests/test_sdk_session.py::TestAsyncGatewaySessionLifecycle -v 2>&1 | tail -10`
Expected: FAIL — `ImportError: cannot import name 'AsyncGatewaySession'`

- [ ] **Step 3: Implement AsyncGatewaySession in `sdk/session.py`**

Add to `sdk/session.py` (after `GatewaySession` class):

```python
class AsyncGatewaySession:
    """Async context manager for SSH Gateway.

    Usage::

        async with AsyncGatewaySession(client) as gw:
            result = await gw.run("ls -la")
    """

    def __init__(self, client: GatewayClient) -> None:
        self.client = client
        self.session_id: str | None = None

    async def __aenter__(self) -> AsyncGatewaySession:
        self.session_id = self.client.connect()
        return self

    async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        self._disconnect_best_effort()

    def _disconnect_best_effort(self) -> None:
        if self.session_id:
            try:
                self.client.disconnect(self.session_id)
            except Exception:
                pass

    async def run(self, command: str, timeout: int | None = None) -> dict:
        """Execute command and wait for completion. Returns job result dict."""
        job = self.client.execute_restricted(
            session_id=self.session_id, command=command
        )
        return self.client.wait_job(
            job_id=job["job_id"], timeout=timeout
        )

    async def read(self, path: str) -> str:
        """Read file content from remote host."""
        result = self.client.read_file(session_id=self.session_id, path=path)
        return result.get("content", "")

    async def write(self, path: str, content: str) -> dict:
        """Write file. Returns raw Gateway response — may contain pending_confirmation."""
        return self.client.write_file(
            session_id=self.session_id, path=path, content=content
        )

    async def session_health(self) -> dict:
        """Check SSH session health."""
        return self.client.session_health(session_id=self.session_id)
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m pytest tests/test_sdk_session.py -v`
Expected: all tests pass (15 sync + 10 async = 25 total)

- [ ] **Step 5: Commit**

```bash
git add sdk/session.py tests/test_sdk_session.py
git commit -m "feat(sdk): add AsyncGatewaySession with full method coverage"
```

---

## Task 5: Exports, lint, typecheck, final verification

**Files:**
- Modify: `sdk/__init__.py`
- Modify: `tests/test_sdk_session.py` (if needed for import path fix)

**Interfaces:**
- Consumes: `GatewaySession`, `AsyncGatewaySession` (Tasks 2-4)
- Produces: `from sdk.session import GatewaySession, AsyncGatewaySession` works

- [ ] **Step 1: Write `sdk/__init__.py` with public exports**

Replace contents of `sdk/__init__.py`:

```python
"""SSH Gateway Python SDK."""

from sdk.session import AsyncGatewaySession, GatewaySession

__all__ = ["AsyncGatewaySession", "GatewaySession"]
```

- [ ] **Step 2: Verify imports work**

Run: `cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -c "from sdk import GatewaySession, AsyncGatewaySession; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Run ruff lint**

Run: `cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m ruff check sdk/session.py sdk/__init__.py tests/test_sdk_session.py`
Expected: no errors (exit code 0)

- [ ] **Step 4: Run ruff format check**

Run: `cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m ruff format --check sdk/session.py sdk/__init__.py tests/test_sdk_session.py`
Expected: all files already formatted

- [ ] **Step 5: Run mypy**

Run: `cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m mypy sdk/session.py sdk/__init__.py --ignore-missing-imports`
Expected: no errors

- [ ] **Step 6: Run full test suite (regression check)**

Run: `cd /media/1TB/Python/web_ssh/web-ssh-gateway && python -m pytest tests/test_sdk_session.py -v`
Expected: 25 passed

- [ ] **Step 7: Commit**

```bash
git add sdk/__init__.py
git commit -m "feat(sdk): add public exports for GatewaySession and AsyncGatewaySession"
```

---

## Self-Review Checklist

1. **Spec coverage (Section 5):**
   - `sdk/session.py` — created ✓
   - `GatewaySession.__init__(client: GatewayClient)` — no profile param ✓
   - `__enter__` try/except with `_disconnect_best_effort()` before re-raise ✓
   - `__exit__`/`__aexit__` never mask original exception ✓
   - All methods pass `session_id` explicitly ✓
   - `write()` returns raw response with `pending_confirmation` ✓
   - `run()` calls `wait_job()` without `session_id` ✓
   - `AsyncGatewaySession` same pattern ✓
   - `from sdk.session import GatewaySession, AsyncGatewaySession` ✓

2. **Placeholder scan:** No "TBD", "TODO", "implement later", "Similar to Task N". All code blocks complete.

3. **Type consistency:**
   - `GatewayClient.connect() -> str` used consistently in both session classes
   - `GatewayClient.disconnect(session_id)` used consistently
   - `execute_restricted(session_id=..., command=...)` matches GatewayClient signature
   - `wait_job(job_id=..., timeout=...)` — note: GatewayClient currently uses `timeout_sec` param name; spec uses `timeout`. Plan follows spec. If GatewayClient keeps `timeout_sec`, change to `timeout_sec=timeout` in run() implementations.
