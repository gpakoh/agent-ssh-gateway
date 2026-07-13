# MCP Phase 2 P0 Core: Latency + Long-poll + Job Tools

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add monotonic latency instrumentation to JobRecord, a long-poll wait endpoint for jobs, and an MCP `job_wait` tool that uses long-poll with polling fallback.

**Architecture:** Gateway owns SSH sessions, job storage, and auth. MCP is a thin translation layer converting HTTP API responses into MCP Contract v1 envelopes. Latency instrumentation lives in the gateway (monotonic timestamps on JobRecord) and MCP process (LatencyTracker breakdown). Long-poll is a single-worker-only endpoint using `asyncio.Event.wait()` with timeout.

**Tech Stack:** Python 3.11+, FastAPI, asyncio, httpx (MCP client), pytest, pytest-asyncio

## Global Constraints

- Monotonic timestamps (`time.monotonic()`) do NOT survive process restart; never use as wall-clock
- `gateway_total_ms = (completed_at_mono - queued_at_mono) * 1000`
- `ssh_connect_ms = null` when session reused
- Latency NOT in `/health`
- `wait_job` does NOT take `session_id` — uses `AuthIdentity.sub` for ownership
- Fallback on `NOT_SUPPORTED` and `404` only, never on real errors (`PERMISSION_DENIED`, `JOB_NOT_FOUND`)
- Exceptions: `JobNotFoundError`, `PermissionDeniedError` (not `SessionNotFoundError`, `ExecutionError`)
- MCP tool returns `dict[str, Any]`, NOT `str`
- `GATEWAY_WORKERS` env var (not `WEB_CONCURRENCY`)
- Timeout range: 0.1–300 seconds, default 30
- `CancelledError` = client disconnect, job untouched

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `app/exceptions.py` | `JobNotFoundError`, `PermissionDeniedError` |
| Modify | `app/job_manager.py` | Add mono timestamps to `JobRecord`, add `wait_for_completion()` |
| Modify | `app/routers/jobs.py` | Add `GET /api/jobs/{job_id}/wait` endpoint |
| Modify | `app/auth_middleware.py` | Add `diagnostics:read` to `VALID_AGENT_SCOPES` |
| Create | `app/routers/diagnostics.py` | `GET /api/diagnostics/latency` endpoint |
| Modify | `app/main.py` | Include diagnostics router |
| Modify | `app/models.py` | Add `JobWaitResponse` model |
| Modify | `examples/mcp_server/latency_metrics.py` | Add breakdown categories (`ssh_job_ms`, `gateway_http_ms`, etc.) |
| Modify | `examples/mcp_server/gateway_client.py` | `wait_job()` uses long-poll, falls back to polling |
| Modify | `examples/mcp_server/tool_results.py` | Add `WAIT_TIMEOUT`, `JOB_NOT_FOUND`, `PERMISSION_DENIED` error codes |
| Modify | `examples/mcp_server/server.py` | Add `diagnostics_latency` and `job_wait` MCP tools |
| Create | `tests/test_exceptions.py` | Tests for new exception types |
| Create | `tests/test_job_wait.py` | Tests for `wait_for_completion` + wait endpoint |
| Create | `tests/test_diagnostics_latency.py` | Tests for latency endpoint |
| Create | `tests/test_mcp_job_wait.py` | Tests for MCP `job_wait` tool |

---

### Task 1: Custom Exceptions (JobNotFoundError, PermissionDeniedError)

**Files:**
- Create: `app/exceptions.py`
- Create: `tests/test_exceptions.py`

**Interfaces:**
- Produces: `JobNotFoundError(job_id: str)`, `PermissionDeniedError(message: str)` — both subclass `Exception`

- [ ] **Step 1: Write the failing test**

Create `tests/test_exceptions.py`:

```python
"""Tests for custom exception types used in P0 Core."""

from app.exceptions import JobNotFoundError, PermissionDeniedError


class TestJobNotFoundError:
    def test_message_contains_job_id(self):
        exc = JobNotFoundError("job-abc-123")
        assert "job-abc-123" in str(exc)

    def test_is_exception(self):
        assert issubclass(JobNotFoundError, Exception)


class TestPermissionDeniedError:
    def test_message(self):
        exc = PermissionDeniedError("Job belongs to a different owner")
        assert "different owner" in str(exc)

    def test_is_exception(self):
        assert issubclass(PermissionDeniedError, Exception)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_exceptions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.exceptions'`

- [ ] **Step 3: Write the implementation**

Create `app/exceptions.py`:

```python
"""Application-level exceptions for job operations."""


class JobNotFoundError(Exception):
    """Raised when a job ID is not found in the job store."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Job {job_id} not found")


class PermissionDeniedError(Exception):
    """Raised when the caller does not own the requested resource."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_exceptions.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add app/exceptions.py tests/test_exceptions.py
git commit -m "feat: add JobNotFoundError and PermissionDeniedError exceptions"
```

---

### Task 2: JobRecord Monotonic Timestamps + Wall-Clock completed_at

**Files:**
- Modify: `app/job_manager.py:36-85` (JobRecord dataclass + `to_dict`)
- Modify: `app/job_manager.py:217-306` (_run_job instrumentation)
- Create: `tests/test_job_timestamps.py`

**Interfaces:**
- Produces: `JobRecord` with fields `queued_at_mono`, `acquired_at_mono`, `command_started_at_mono`, `command_finished_at_mono`, `completed_at_mono`, `ssh_connect_started_at_mono`, `ssh_connected_at_mono` (all `float | None`)
- Produces: `JobRecord.completed_at` wall-clock field (already exists, now set in finally block)

- [ ] **Step 1: Write the failing test**

Create `tests/test_job_timestamps.py`:

```python
"""Tests for monotonic timestamp fields on JobRecord."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

from app.job_manager import JobManager, JobRecord


class TestJobRecordMonoTimestamps:
    """Verify mono timestamp fields exist and to_dict includes them."""

    def test_new_fields_exist_with_none_defaults(self):
        job = JobRecord(job_id="j1", session_id="s1", command="echo hi")
        assert job.queued_at_mono is None
        assert job.acquired_at_mono is None
        assert job.command_started_at_mono is None
        assert job.command_finished_at_mono is None
        assert job.completed_at_mono is None
        assert job.ssh_connect_started_at_mono is None
        assert job.ssh_connected_at_mono is None

    def test_to_dict_includes_mono_timestamps(self):
        job = JobRecord(job_id="j1", session_id="s1", command="echo hi")
        d = job.to_dict()
        assert "queued_at_mono" in d
        assert "completed_at_mono" in d
        assert d["queued_at_mono"] is None

    def test_to_dict_includes_completed_at_wall_clock(self):
        job = JobRecord(job_id="j1", session_id="s1", command="echo hi")
        d = job.to_dict()
        assert "completed_at" in d
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_job_timestamps.py -v`
Expected: FAIL — `AttributeError` (fields don't exist yet)

- [ ] **Step 3: Add mono timestamp fields to JobRecord**

In `app/job_manager.py`, modify the `JobRecord` dataclass — add after `completed_at: float | None = None`:

```python
    # Monotonic timestamps (relative to process start; do NOT survive restart)
    queued_at_mono: float | None = None
    acquired_at_mono: float | None = None
    command_started_at_mono: float | None = None
    command_finished_at_mono: float | None = None
    completed_at_mono: float | None = None
    ssh_connect_started_at_mono: float | None = None
    ssh_connected_at_mono: float | None = None
```

- [ ] **Step 4: Update `to_dict` to include mono timestamps**

In `app/job_manager.py`, update `JobRecord.to_dict()` to add these keys after `"duration": self.duration`:

```python
            "queued_at_mono": self.queued_at_mono,
            "completed_at_mono": self.completed_at_mono,
```

- [ ] **Step 5: Instrument `create_job` with `queued_at_mono`**

In `app/job_manager.py`, in `create_job()`, after `job = JobRecord(...)`, before `self._jobs[job_id] = job`:

```python
            job.queued_at_mono = time.monotonic()
```

- [ ] **Step 6: Instrument `_run_job` with all mono timestamps**

Replace `_run_job` method body with:

```python
    async def _run_job(self, job_id: str) -> None:
        """Execute a command in the background."""
        async with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return

        job.status = "running"
        job.started_at = time.time()
        job.acquired_at_mono = time.monotonic()
        await job.notify_listeners(
            {
                "type": "status",
                "status": "running",
                "message": f"Started: {job.command}",
            }
        )

        try:
            job.command_started_at_mono = time.monotonic()
            async for msg_type, msg_data in self._ssh_manager.execute_stream(
                job.session_id, job.command, cancel_event=job.cancel_event
            ):
                job.touch()

                if msg_type == "stdout":
                    remaining = MAX_STDOUT_SIZE - len(job.stdout)
                    if remaining > 0:
                        job.stdout += msg_data[:remaining]
                        if remaining < len(msg_data) and "[truncated]" not in job.stdout:
                            job.stdout += "\n... [output truncated, exceeded 10MB]"
                    await job.notify_listeners(
                        {
                            "type": "stdout",
                            "data": msg_data,
                        }
                    )
                elif msg_type == "stderr":
                    remaining = MAX_STDOUT_SIZE - len(job.stderr)
                    if remaining > 0:
                        job.stderr += msg_data[:remaining]
                        if remaining < len(msg_data) and "[truncated]" not in job.stderr:
                            job.stderr += "\n... [output truncated, exceeded 10MB]"
                    await job.notify_listeners(
                        {
                            "type": "stderr",
                            "data": msg_data,
                        }
                    )
                elif msg_type == "exit":
                    job.exit_code = int(msg_data)
                    job.command_finished_at_mono = time.monotonic()
                    await job.notify_listeners(
                        {
                            "type": "exit",
                            "exit_code": job.exit_code,
                        }
                    )

            job.status = "completed" if (job.exit_code == 0) else "failed"
            if job.exit_code != 0:
                job.error_message = f"Exit code: {job.exit_code}"

        except SessionNotFoundError as exc:
            job.status = "failed"
            job.error_message = str(exc)
            await job.notify_listeners(
                {
                    "type": "error",
                    "error": str(exc),
                }
            )
        except Exception as exc:
            job.status = "failed"
            job.error_message = str(exc)
            await job.notify_listeners(
                {
                    "type": "error",
                    "error": str(exc),
                }
            )
        finally:
            job.completed_at = time.time()
            job.completed_at_mono = time.monotonic()
            job.completed_event.set()
            await job.notify_listeners(
                {
                    "type": "status",
                    "status": job.status,
                    "duration": job.duration,
                    "exit_code": job.exit_code,
                }
            )
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/test_job_timestamps.py -v`
Expected: 3 passed

- [ ] **Step 8: Commit**

```bash
git add app/job_manager.py tests/test_job_timestamps.py
git commit -m "feat: add monotonic timestamps to JobRecord for latency breakdown"
```

---

### Task 3: Custom Exceptions in JobManager + wait_for_completion

**Files:**
- Modify: `app/job_manager.py` — use `JobNotFoundError`, `PermissionDeniedError`, add `wait_for_completion()`
- Modify: `tests/test_job_timestamps.py` — add wait_for_completion tests

**Interfaces:**
- Consumes: `JobNotFoundError`, `PermissionDeniedError` from `app/exceptions.py`
- Produces: `JobManager.wait_for_completion(job_id, identity_sub, timeout_s) -> dict`
  - Returns `job.to_dict()` on terminal state
  - Returns `{"job_id": ..., "status": "running", "wait_timed_out": True}` on timeout
  - Raises `JobNotFoundError` when job not found
  - Raises `PermissionDeniedError` when owner mismatch
  - Re-raises `asyncio.CancelledError` on client disconnect

- [ ] **Step 1: Write the failing test**

Append to `tests/test_job_timestamps.py`:

```python
import asyncio
import pytest
from app.exceptions import JobNotFoundError, PermissionDeniedError


class TestWaitForCompletion:
    """Tests for JobManager.wait_for_completion()."""

    def _make_job_manager(self):
        mock_ssh = AsyncMock()
        return JobManager(ssh_manager=mock_ssh, max_jobs=10)

    @pytest.mark.asyncio
    async def test_raises_job_not_found(self):
        jm = self._make_job_manager()
        with pytest.raises(JobNotFoundError):
            await jm.wait_for_completion("nonexistent", "user:admin", 1.0)

    @pytest.mark.asyncio
    async def test_raises_permission_denied(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "echo hi")
        with pytest.raises(PermissionDeniedError):
            await jm.wait_for_completion(job_id, "user:other", 1.0)

    @pytest.mark.asyncio
    async def test_returns_dict_on_already_completed(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "echo hi")
        job = await jm.get_job(job_id)
        job.status = "completed"
        job.completed_at = time.time()
        job.completed_event.set()
        result = await jm.wait_for_completion(job_id, "user:admin", 1.0)
        assert result["job_id"] == job_id
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_timeout_returns_running_with_timed_out(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "sleep 999")
        result = await jm.wait_for_completion(job_id, "user:admin", 0.1)
        assert result["wait_timed_out"] is True
        assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_completed_during_wait(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "echo hi")
        job = await jm.get_job(job_id)

        async def _complete_later():
            await asyncio.sleep(0.05)
            job.status = "completed"
            job.completed_at = time.time()
            job.completed_event.set()

        asyncio.create_task(_complete_later())
        result = await jm.wait_for_completion(job_id, "user:admin", 5.0)
        assert result["status"] == "completed"
        assert result.get("wait_timed_out") is not True

    @pytest.mark.asyncio
    async def test_cancelled_error_re_raised(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "sleep 999")

        async def _cancel():
            await asyncio.sleep(0.05)
            task.cancel()

        task = asyncio.create_task(jm.wait_for_completion(job_id, "user:admin", 5.0))
        asyncio.create_task(_cancel())
        with pytest.raises(asyncio.CancelledError):
            await task
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_job_timestamps.py::TestWaitForCompletion -v`
Expected: FAIL — `AttributeError` (wait_for_completion doesn't exist)

- [ ] **Step 3: Implement `wait_for_completion`**

In `app/job_manager.py`, add import at top:

```python
from app.exceptions import JobNotFoundError, PermissionDeniedError
```

Add `TERMINAL_STATES` constant after `MAX_STDOUT_SIZE`:

```python
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
```

Add the method to `JobManager` class (after `cancel_job`):

```python
    async def wait_for_completion(
        self, job_id: str, identity_sub: str, timeout_s: float
    ) -> dict:
        """Long-poll: wait for job completion or timeout.

        Returns job.to_dict() on completion, or
        {"job_id": ..., "status": "running", "wait_timed_out": True} on timeout.
        Raises JobNotFoundError, PermissionDeniedError, re-raises CancelledError.
        """
        job = await self.get_job(job_id)
        if not job:
            raise JobNotFoundError(job_id)

        if job.owner_id != identity_sub:
            raise PermissionDeniedError("Job belongs to a different owner")

        if job.status in TERMINAL_STATES:
            return job.to_dict()

        event = job.completed_event
        # Re-check after subscribe (race with fast jobs)
        if job.status in TERMINAL_STATES:
            return job.to_dict()

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return {"job_id": job_id, "status": "running", "wait_timed_out": True}
        except asyncio.CancelledError:
            # Client disconnected — job UNCHANGED
            raise

        return job.to_dict()
```

- [ ] **Step 4: Add `owner_id` to JobRecord**

In `app/job_manager.py`, add to `JobRecord` dataclass after `completed_at: float | None = None`:

```python
    owner_id: str = ""
```

Update `to_dict` to include it:

```python
            "owner_id": self.owner_id,
```

In `create_job`, set `owner_id` from the parameter. Update `create_job` signature:

```python
    async def create_job(self, session_id: str, command: str, owner_id: str = "") -> str:
```

And in the job creation block:

```python
            job = JobRecord(
                job_id=job_id,
                session_id=session_id,
                command=command,
                owner_id=owner_id,
            )
```

- [ ] **Step 5: Update tests to use owner_id="user:admin"**

Update the test helper and test calls:

```python
    @pytest.mark.asyncio
    async def test_raises_job_not_found(self):
        jm = self._make_job_manager()
        with pytest.raises(JobNotFoundError):
            await jm.wait_for_completion("nonexistent", "user:admin", 1.0)

    @pytest.mark.asyncio
    async def test_raises_permission_denied(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "echo hi", owner_id="user:admin")
        with pytest.raises(PermissionDeniedError):
            await jm.wait_for_completion(job_id, "user:other", 1.0)

    @pytest.mark.asyncio
    async def test_returns_dict_on_already_completed(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "echo hi", owner_id="user:admin")
        ...

    @pytest.mark.asyncio
    async def test_timeout_returns_running_with_timed_out(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "sleep 999", owner_id="user:admin")
        ...

    @pytest.mark.asyncio
    async def test_completed_during_wait(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "echo hi", owner_id="user:admin")
        ...

    @pytest.mark.asyncio
    async def test_cancelled_error_re_raised(self):
        jm = self._make_job_manager()
        job_id = await jm.create_job("s1", "sleep 999", owner_id="user:admin")
        ...
```

- [ ] **Step 6: Run all tests**

Run: `pytest tests/test_job_timestamps.py -v`
Expected: 8 passed

- [ ] **Step 7: Commit**

```bash
git add app/job_manager.py app/exceptions.py tests/test_job_timestamps.py
git commit -m "feat: add wait_for_completion with owner check and timeout support"
```

---

### Task 4: JobWaitResponse Model + Long-Poll Endpoint

**Files:**
- Modify: `app/models.py` — add `JobWaitResponse`
- Modify: `app/routers/jobs.py` — add `GET /api/jobs/{job_id}/wait`
- Create: `tests/test_job_wait.py`

**Interfaces:**
- Consumes: `JobManager.wait_for_completion(job_id, identity_sub, timeout_s) -> dict`
- Consumes: `AuthIdentity` from `require_scope("jobs:read")`
- Produces: `GET /api/jobs/{job_id}/wait?timeout=30` returning JSON with job dict or timeout indicator

- [ ] **Step 1: Write the failing test**

Create `tests/test_job_wait.py`:

```python
"""Tests for GET /api/jobs/{job_id}/wait long-poll endpoint."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

from app.config import settings
from app.main import app


MOCK_JOB = {
    "job_id": "job-1",
    "session_id": "s-1",
    "command": "echo hi",
    "status": "completed",
    "stdout": "hi\n",
    "stderr": "",
    "exit_code": 0,
    "created_at": 1000.0,
    "started_at": 1000.1,
    "completed_at": 1000.5,
    "duration": 0.4,
    "error_message": None,
    "progress": {},
    "owner_id": "user:admin",
    "queued_at_mono": None,
    "completed_at_mono": None,
}


class TestJobWaitEndpoint:
    def _setup_mocks(self):
        from app import state as _app_state

        _app_state.job_manager = AsyncMock()
        _app_state.job_manager.wait_for_completion = AsyncMock(return_value=dict(MOCK_JOB))
        _app_state.job_manager.get_job = AsyncMock(return_value=MagicMock(status="completed"))
        _app_state.job_manager.get_job_status = AsyncMock(return_value={})
        _app_state.job_manager.list_jobs = AsyncMock(return_value=[])
        _app_state.job_manager._jobs = {}
        _app_state.job_manager.stop_cleanup_task = AsyncMock()
        _app_state.job_manager.wait_for_all_jobs = AsyncMock()
        _app_state.audit_logger = MagicMock()
        _app_state.manager = AsyncMock()
        _app_state.manager.stop_cleanup_task = AsyncMock()
        _app_state.manager.start_cleanup_task = AsyncMock()
        _app_state.manager.list_sessions = AsyncMock(return_value=[])
        _app_state.event_hook_store = None
        _app_state.delivery_service = None

    def _client(self, monkeypatch):
        self._setup_mocks()
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        return TestClient(app, raise_server_exceptions=False)

    def test_wait_returns_job_result(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get(
            "/api/jobs/job-1/wait?timeout=30",
            headers={"X-API-Key": "secret-42"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "job-1"
        assert data["status"] == "completed"

    def test_wait_timeout_param_validated(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get(
            "/api/jobs/job-1/wait?timeout=0.01",
            headers={"X-API-Key": "secret-42"},
        )
        assert resp.status_code == 422

    def test_wait_timeout_too_large(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get(
            "/api/jobs/job-1/wait?timeout=999",
            headers={"X-API-Key": "secret-42"},
        )
        assert resp.status_code == 422

    def test_wait_no_auth_returns_401(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/api/jobs/job-1/wait?timeout=30")
        assert resp.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_job_wait.py -v`
Expected: FAIL — `AssertionError: 404 != 200` (route doesn't exist yet)

- [ ] **Step 3: Add `JobWaitResponse` model**

In `app/models.py`, add after `JobListResponse`:

```python
class JobWaitResponse(BaseModel):
    """Long-poll wait response."""

    job_id: str
    status: str
    wait_timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    created_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    duration: float | None = None
    error_message: str | None = None
    owner_id: str = ""
```

- [ ] **Step 4: Add `GATEWAY_WORKERS` check helper**

In `app/routers/jobs.py`, add import at top:

```python
import os
```

Add constant after imports:

```python
_GATEWAY_WORKERS = os.environ.get("GATEWAY_WORKERS", "1")
```

- [ ] **Step 5: Add the wait endpoint**

In `app/routers/jobs.py`, add the endpoint after `jobs_cancel`:

```python
@router.get("/api/jobs/{job_id}/wait")
async def jobs_wait(
    job_id: str,
    request: Request,
    timeout: float = Query(default=30.0, ge=0.1, le=300.0),
    _identity: AuthIdentity = Depends(require_scope("jobs:read")),
):
    """Long-poll: wait for job completion or timeout."""
    if _GATEWAY_WORKERS != "1":
        return JSONResponse(
            status_code=200,
            content={
                "error": "NOT_SUPPORTED",
                "detail": "Long-poll requires GATEWAY_WORKERS=1",
            },
        )

    try:
        result = await _state.job_manager.wait_for_completion(
            job_id=job_id,
            identity_sub=_identity.fingerprint,
            timeout_s=timeout,
        )
    except Exception:
        from app.exceptions import JobNotFoundError, PermissionDeniedError

        import sys
        exc_type = sys.exc_info()[0]
        if exc_type is JobNotFoundError:
            raise HTTPException(
                status_code=404,
                detail=_err(404, f"Job {job_id} not found"),
            )
        if exc_type is PermissionDeniedError:
            raise HTTPException(
                status_code=403,
                detail=_err(403, "Job belongs to a different owner"),
            )
        raise

    return result
```

- [ ] **Step 6: Add JSONResponse import**

In `app/routers/jobs.py`, add to imports:

```python
from fastapi.responses import JSONResponse, StreamingResponse
```

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_job_wait.py -v`
Expected: 4 passed

- [ ] **Step 8: Run all existing job tests**

Run: `pytest tests/test_jobs_output_redaction.py tests/test_jobs_stream_redaction.py tests/test_job_wait.py -v`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add app/models.py app/routers/jobs.py tests/test_job_wait.py
git commit -m "feat: add GET /api/jobs/{job_id}/wait long-poll endpoint with GATEWAY_WORKERS check"
```

---

### Task 5: Diagnostics Router + Latency Endpoint + Scope

**Files:**
- Modify: `app/auth_middleware.py:56-64` — add `diagnostics:read` to `VALID_AGENT_SCOPES`
- Create: `app/routers/diagnostics.py` — `GET /api/diagnostics/latency`
- Modify: `app/main.py:928-944` — include diagnostics router
- Create: `tests/test_diagnostics_latency.py`

**Interfaces:**
- Consumes: `state.job_manager` for job-based latency breakdown
- Produces: `GET /api/diagnostics/latency` returning per-job latency breakdown
- Scope: `diagnostics:read`

- [ ] **Step 1: Add `diagnostics:read` scope**

In `app/auth_middleware.py`, add to `VALID_AGENT_SCOPES` set:

```python
VALID_AGENT_SCOPES: set[str] = {
    "ssh:connect",
    "ssh:execute",
    "ssh:disconnect",
    "ssh:files",
    "ssh:port-check",
    "jobs:read",
    "jobs:run",
    "diagnostics:read",
}
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_diagnostics_latency.py`:

```python
"""Tests for GET /api/diagnostics/latency endpoint."""

import time
from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from app.config import settings
from app.main import app


class TestDiagnosticsLatency:
    def _setup_mocks(self):
        from app import state as _app_state

        _app_state.job_manager = AsyncMock()
        _app_state.job_manager.get_job_status = AsyncMock(return_value={})
        _app_state.job_manager.list_jobs = AsyncMock(return_value=[])
        _app_state.job_manager._jobs = {}
        _app_state.job_manager.stop_cleanup_task = AsyncMock()
        _app_state.job_manager.wait_for_all_jobs = AsyncMock()
        _app_state.audit_logger = MagicMock()
        _app_state.manager = AsyncMock()
        _app_state.manager.stop_cleanup_task = AsyncMock()
        _app_state.manager.start_cleanup_task = AsyncMock()
        _app_state.manager.list_sessions = AsyncMock(return_value=[])
        _app_state.event_hook_store = None
        _app_state.delivery_service = None

    def _client(self, monkeypatch):
        self._setup_mocks()
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        return TestClient(app, raise_server_exceptions=False)

    def test_latency_endpoint_returns_json(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get(
            "/api/diagnostics/latency",
            headers={"X-API-Key": "secret-42"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "jobs" in data
        assert "mcp" in data

    def test_latency_not_in_health(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "latency" not in data

    def test_latency_requires_auth(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/api/diagnostics/latency")
        assert resp.status_code == 401
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_diagnostics_latency.py -v`
Expected: FAIL — 404 (route not registered)

- [ ] **Step 4: Create diagnostics router**

Create `app/routers/diagnostics.py`:

```python
"""Diagnostics routes — latency breakdown, system info."""

import time

from fastapi import APIRouter, Depends, Request

from app import state as _state
from app.auth_middleware import AuthIdentity, require_scope

router = APIRouter(tags=["diagnostics"])


def _compute_job_latency_breakdown() -> dict:
    """Compute latency breakdown from completed jobs with mono timestamps."""
    jobs_summary = []
    total_jobs = 0

    if _state.job_manager is None:
        return {"jobs": [], "total": 0}

    for job in _state.job_manager._jobs.values():
        total_jobs += 1
        if job.status not in ("completed", "failed", "cancelled"):
            continue
        if job.queued_at_mono is None or job.completed_at_mono is None:
            continue

        entry: dict = {
            "job_id": job.job_id,
            "status": job.status,
            "gateway_total_ms": round(
                (job.completed_at_mono - job.queued_at_mono) * 1000, 1
            ),
        }
        if job.acquired_at_mono is not None and job.queued_at_mono is not None:
            entry["queue_wait_ms"] = round(
                (job.acquired_at_mono - job.queued_at_mono) * 1000, 1
            )
        if (
            job.command_finished_at_mono is not None
            and job.command_started_at_mono is not None
        ):
            entry["command_execution_ms"] = round(
                (job.command_finished_at_mono - job.command_started_at_mono) * 1000, 1
            )
        if (
            job.ssh_connected_at_mono is not None
            and job.ssh_connect_started_at_mono is not None
        ):
            entry["ssh_connect_ms"] = round(
                (job.ssh_connected_at_mono - job.ssh_connect_started_at_mono) * 1000, 1
            )
        elif (
            job.ssh_connect_started_at_mono is not None
            and job.ssh_connected_at_mono is None
        ):
            entry["ssh_connect_ms"] = None  # session reused or still connecting

        jobs_summary.append(entry)

    return {"jobs": jobs_summary, "total": total_jobs}


@router.get("/api/diagnostics/latency")
async def diagnostics_latency(
    _identity: AuthIdentity = Depends(require_scope("diagnostics:read")),
):
    """Latency breakdown for completed jobs and MCP process metrics."""
    job_breakdown = _compute_job_latency_breakdown()

    return {
        "gateway": {
            "timestamp": time.time(),
            **job_breakdown,
        },
        "mcp": {
            "note": "MCP latency is process-local. Use the diagnostics_latency MCP tool.",
        },
    }
```

- [ ] **Step 5: Register the diagnostics router in main.py**

In `app/main.py`, add after the existing router imports:

```python
from app.routers.diagnostics import router as diagnostics_router  # noqa: E402
```

And add in the router registration block:

```python
app.include_router(diagnostics_router)
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_diagnostics_latency.py -v`
Expected: 3 passed

- [ ] **Step 7: Commit**

```bash
git add app/auth_middleware.py app/routers/diagnostics.py app/main.py tests/test_diagnostics_latency.py
git commit -m "feat: add GET /api/diagnostics/latency endpoint with job breakdown"
```

---

### Task 6: MCP LatencyTracker Breakdown + diagnostics_latency Tool

**Files:**
- Modify: `examples/mcp_server/latency_metrics.py` — add breakdown categories
- Modify: `examples/mcp_server/server.py` — add `diagnostics_latency` tool

**Interfaces:**
- Consumes: `get_tracker()` from `latency_metrics.py`
- Produces: `LatencyTracker` with `summary()` including `by_category` breakdown
- Produces: MCP `diagnostics_latency` tool

- [ ] **Step 1: Add breakdown categories to LatencyTracker**

In `examples/mcp_server/latency_metrics.py`, update the `LatencyTracker.summary()` method:

```python
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
            by_category: dict[str, dict] = {}
            for name, durations in self._records.items():
                cat = name.split("_", 1)[0] if "_" in name else name
                if cat not in by_category:
                    by_category[cat] = {"count": 0, "total_ms": 0.0}
                by_category[cat]["count"] += len(durations)
                by_category[cat]["total_ms"] += sum(durations)
            for cat_data in by_category.values():
                if cat_data["count"] > 0:
                    cat_data["avg_ms"] = round(
                        cat_data["total_ms"] / cat_data["count"], 1
                    )
                else:
                    cat_data["avg_ms"] = 0.0
                cat_data["total_ms"] = round(cat_data["total_ms"], 1)

            return {
                "total_calls": sum(len(v) for v in self._records.values()),
                "by_tool": by_tool,
                "by_category": by_category,
            }
```

- [ ] **Step 2: Add diagnostics_latency MCP tool**

In `examples/mcp_server/server.py`, add after `gateway_latency_report`:

```python
@register_tool("diagnostics_latency")
def gateway_diagnostics_latency() -> dict[str, Any]:
    """Return MCP-side latency breakdown and gateway latency summary."""
    tracker = get_tracker()
    mcp_summary = tracker.summary()

    try:
        gw_data = client._get("/api/diagnostics/latency")
    except Exception:
        gw_data = {"error": "gateway diagnostics unavailable"}

    return tool_success(
        {
            "mcp": mcp_summary,
            "gateway": gw_data,
        },
        tool_name="diagnostics_latency",
    )
```

- [ ] **Step 3: Run existing MCP tests**

Run: `cd examples/mcp_server && python -c "from latency_metrics import get_tracker; t = get_tracker(); t.record('ssh_job_ms', 100.0); t.record('gateway_http_ms', 50.0); print(t.summary())"`
Expected: Output contains `by_category` with `ssh` and `gateway` keys

- [ ] **Step 4: Commit**

```bash
git add examples/mcp_server/latency_metrics.py examples/mcp_server/server.py
git commit -m "feat: add LatencyTracker breakdown categories and diagnostics_latency MCP tool"
```

---

### Task 7: GatewayClient.wait_job Long-Poll + Fallback

**Files:**
- Modify: `examples/mcp_server/gateway_client.py:269-279` — replace polling with long-poll + fallback
- Create: `tests/test_mcp_gateway_client_wait.py`

**Interfaces:**
- Consumes: `GatewayClient._get(path, params, timeout)` for long-poll call
- Produces: `GatewayClient.wait_job(job_id, timeout)` using long-poll with fallback to polling on `NOT_SUPPORTED` / 404

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_gateway_client_wait.py`:

```python
"""Tests for GatewayClient.wait_job long-poll with fallback."""

import time
from unittest.mock import MagicMock, patch

from examples.mcp_server.gateway_client import GatewayClient, GatewayClientError


class TestGatewayClientWaitJob:
    def _client(self):
        c = GatewayClient.__new__(GatewayClient)
        c.base_url = "http://localhost:8085"
        c.api_key = "test-key"
        c.session_id = "s1"
        c.command_timeout = 30
        c.job_timeout = 5
        c._reconnect_lock = __import__("threading").Lock()
        c._ssh_host = ""
        c._ssh_port = 22
        c._ssh_user = ""
        c._ssh_password = ""
        c._ssh_private_key = ""
        return c

    def test_long_poll_success(self):
        client = self._client()
        completed_response = {
            "job_id": "j1",
            "status": "completed",
            "stdout": "hi\n",
            "exit_code": 0,
        }
        with patch.object(client, "_get", return_value=completed_response) as mock_get:
            result = client.wait_job("j1", timeout=10)
            assert result["status"] == "completed"
            mock_get.assert_called_once_with(
                "/api/jobs/j1/wait",
                params={"timeout": 10},
                timeout=15,
            )

    def test_long_poll_timeout_returns_dict(self):
        client = self._client()
        timeout_response = {
            "job_id": "j1",
            "status": "running",
            "wait_timed_out": True,
        }
        with patch.object(client, "_get", return_value=timeout_response):
            result = client.wait_job("j1", timeout=0.5)
            assert result.get("wait_timed_out") is True

    def test_fallback_on_not_supported(self):
        client = self._client()

        call_count = 0

        def _mock_get(path, params=None, timeout=30):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GatewayClientError(
                    "NOT_SUPPORTED",
                    status_code=200,
                    body={"error": "NOT_SUPPORTED"},
                )
            return {
                "job_id": "j1",
                "status": "completed",
                "exit_code": 0,
            }

        with patch.object(client, "_get", side_effect=_mock_get):
            result = client.wait_job("j1", timeout=10)
            assert result["status"] == "completed"
            assert call_count >= 2

    def test_fallback_on_404(self):
        client = self._client()

        call_count = 0

        def _mock_get(path, params=None, timeout=30):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GatewayClientError("Not Found", status_code=404)
            return {
                "job_id": "j1",
                "status": "completed",
                "exit_code": 0,
            }

        with patch.object(client, "_get", side_effect=_mock_get):
            result = client.wait_job("j1", timeout=10)
            assert result["status"] == "completed"
            assert call_count >= 2

    def test_no_fallback_on_permission_denied(self):
        client = self._client()

        with patch.object(
            client,
            "_get",
            side_effect=GatewayClientError(
                "Permission denied",
                status_code=403,
                body={"error": "PERMISSION_DENIED"},
            ),
        ):
            try:
                client.wait_job("j1", timeout=10)
                assert False, "Should have raised"
            except GatewayClientError as e:
                assert e.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_gateway_client_wait.py -v`
Expected: FAIL — the old `wait_job` doesn't use `_get` with timeout param

- [ ] **Step 3: Replace `wait_job` in GatewayClient**

In `examples/mcp_server/gateway_client.py`, replace the `wait_job` method:

```python
    def wait_job(self, job_id: str, timeout_sec: int | None = None) -> dict[str, Any]:
        """Wait for job completion using long-poll, falling back to polling.

        Falls back to polling on NOT_SUPPORTED (multi-worker) or 404 (old gateway).
        No fallback on PERMISSION_DENIED, JOB_NOT_FOUND, or other real errors.
        """
        effective_timeout = timeout_sec or self.job_timeout
        http_timeout = effective_timeout + 5

        try:
            result = self._get(
                f"/api/jobs/{job_id}/wait",
                params={"timeout": effective_timeout},
                timeout=http_timeout,
            )
            return result
        except GatewayClientError as exc:
            should_fallback = False
            if exc.status_code == 404:
                should_fallback = True
            elif exc.body and exc.body.get("error") == "NOT_SUPPORTED":
                should_fallback = True
            elif exc.status_code == 200 and exc.body and exc.body.get("error") == "NOT_SUPPORTED":
                should_fallback = True

            if not should_fallback:
                raise

        # Polling fallback
        deadline = time.time() + effective_timeout
        while time.time() < deadline:
            status = self.job_status(job_id)
            if status.get("status") in {"completed", "failed", "cancelled"}:
                result = self.job_result(job_id)
                if "execution_duration_ms" not in result and result.get("duration") is not None:
                    result["execution_duration_ms"] = int(result["duration"] * 1000)
                return result
            time.sleep(1)
        raise GatewayClientError(f"Job {job_id} did not finish before timeout")
```

- [ ] **Step 4: Add `_get` timeout parameter support**

In `examples/mcp_server/gateway_client.py`, update `_get` to accept optional timeout:

```python
    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        response = httpx.get(
            f"{self.base_url}{path}",
            params=params,
            headers=self._headers(),
            timeout=timeout,
        )
        if response.status_code >= 400:
            body: dict[str, Any] | None = None
            try:
                body = response.json()
            except Exception:
                pass
            raise GatewayClientError(
                f"GET {path} failed: {response.status_code} {response.text}",
                status_code=response.status_code,
                body=body,
            )
        return response.json()
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_mcp_gateway_client_wait.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add examples/mcp_server/gateway_client.py tests/test_mcp_gateway_client_wait.py
git commit -m "feat: GatewayClient.wait_job uses long-poll with polling fallback"
```

---

### Task 8: MCP job_wait Tool

**Files:**
- Modify: `examples/mcp_server/server.py` — add `job_wait` tool
- Modify: `examples/mcp_server/tool_results.py` — add new error codes
- Create: `tests/test_mcp_job_wait.py`

**Interfaces:**
- Consumes: `GatewayClient.wait_job(job_id, timeout)` from Task 7
- Produces: MCP `job_wait(job_id, timeout)` tool returning `dict[str, Any]` (Contract v1 envelope)

- [ ] **Step 1: Add error codes to tool_results.py**

In `examples/mcp_server/tool_results.py`, add to `ERROR_CODES` set:

```python
ERROR_CODES = {
    "TOOL_NOT_FOUND",
    "CONTAINER_NOT_FOUND",
    "SESSION_NOT_FOUND",
    "AUTH_ERROR",
    "POLICY_VIOLATION",
    "RATE_LIMITED",
    "TIMEOUT",
    "DEPENDENCY_MISSING",
    "INVALID_INPUT",
    "INTERNAL_ERROR",
    "FILE_NOT_FOUND",
    "CONFIRM_TOKEN_INVALID",
    "CONFIRM_TOKEN_EXPIRED",
    "CONFIRM_TOKEN_CONSUMED",
    "DOCKER_COMMAND_FAILED",
    "DOCKER_ADMIN_SCOPE_REQUIRED",
    "DOCKER_EXEC_COMMAND_BLOCKED",
    "DOCKER_EXEC_CONTAINER_NOT_FOUND",
    "DOCKER_EXEC_TIMEOUT",
    "DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED",
    "DOCKER_RUN_IMAGE_NOT_ALLOWED",
    "DOCKER_RUN_IMAGE_INVALID",
    "DOCKER_RUN_CONTAINER_CREATE_FAILED",
    "DOCKER_RUN_TIMEOUT",
    "DOCKER_RMI_INVALID_REFERENCE",
    "DOCKER_RMI_FAILED",
    "DOCKER_VOLUME_RM_INVALID_NAME",
    "DOCKER_VOLUME_RM_FAILED",
    "TOOL_EXECUTION_FAILED",
    "POLICY_DENIED",
    "WAIT_TIMEOUT",
    "JOB_NOT_FOUND",
    "PERMISSION_DENIED",
}
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_mcp_job_wait.py`:

```python
"""Tests for MCP job_wait tool."""

from unittest.mock import MagicMock, patch

from examples.mcp_server.gateway_client import GatewayClient, GatewayClientError


class TestMCPJobWait:
    def _make_client(self):
        c = GatewayClient.__new__(GatewayClient)
        c.base_url = "http://localhost:8085"
        c.api_key = "test-key"
        c.session_id = "s1"
        c.command_timeout = 30
        c.job_timeout = 5
        c._reconnect_lock = __import__("threading").Lock()
        c._ssh_host = ""
        c._ssh_port = 22
        c._ssh_user = ""
        c._ssh_password = ""
        c._ssh_private_key = ""
        return c

    def test_job_wait_returns_completed_result(self):
        from examples.mcp_server.server import gateway_wait_job

        client = self._make_client()
        completed = {
            "job_id": "j1",
            "status": "completed",
            "exit_code": 0,
            "stdout": "hello\n",
        }
        with patch("examples.mcp_server.server.client", client):
            with patch.object(client, "wait_job", return_value=completed):
                result = gateway_wait_job(job_id="j1", timeout_sec=30)
                assert result["ok"] is True
                assert result["result"]["status"] == "completed"

    def test_job_wait_timeout_returns_error(self):
        from examples.mcp_server.server import gateway_wait_job

        client = self._make_client()
        with patch("examples.mcp_server.server.client", client):
            with patch.object(
                client,
                "wait_job",
                return_value={"job_id": "j1", "status": "running", "wait_timed_out": True},
            ):
                result = gateway_wait_job(job_id="j1", timeout_sec=30)
                assert result["ok"] is False
                assert result["error"]["code"] == "WAIT_TIMEOUT"

    def test_job_wait_handles_gateway_error(self):
        from examples.mcp_server.server import gateway_wait_job

        client = self._make_client()
        with patch("examples.mcp_server.server.client", client):
            with patch.object(
                client,
                "wait_job",
                side_effect=GatewayClientError("Job j1 not found", status_code=404),
            ):
                result = gateway_wait_job(job_id="j1", timeout_sec=30)
                assert result["ok"] is False
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_mcp_job_wait.py -v`
Expected: FAIL — `ImportError` or `AttributeError` (tool doesn't exist)

- [ ] **Step 4: Add `job_wait` MCP tool**

In `examples/mcp_server/server.py`, add after the existing `gateway_wait_job` tool:

```python
@register_tool("job_wait")
@instrumented("job_wait")
def gateway_job_wait(job_id: str, timeout_sec: int | None = None) -> dict[str, Any]:
    """Wait for a background job to complete using long-poll.

    Uses the Gateway long-poll endpoint. Falls back to polling if the
    Gateway does not support long-poll (multi-worker or old version).

    Args:
        job_id: Background job identifier.
        timeout_sec: Maximum seconds to wait (default: 180).

    Returns:
        Contract v1 dict with job result or WAIT_TIMEOUT error.
    """
    try:
        result = client.wait_job(job_id, timeout_sec=timeout_sec)
    except GatewayClientError as exc:
        code, retryable = _classify_gateway_error(exc)
        return tool_error(
            tool="job_wait",
            code=code,
            message=str(exc),
            retryable=retryable,
            source="gateway",
        )

    if result.get("wait_timed_out"):
        return tool_error(
            tool="job_wait",
            code="WAIT_TIMEOUT",
            message=f"Job {job_id} did not complete within timeout",
            retryable=True,
            details={"job_id": job_id, "status": result.get("status", "running")},
            source="gateway",
        )

    return tool_success(
        tool="job_wait",
        result=result,
        source="gateway",
    )
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_mcp_job_wait.py -v`
Expected: 3 passed

- [ ] **Step 6: Run full test suite to verify no regressions**

Run: `pytest -q`
Expected: All existing tests pass plus new tests

- [ ] **Step 7: Commit**

```bash
git add examples/mcp_server/server.py examples/mcp_server/tool_results.py tests/test_mcp_job_wait.py
git commit -m "feat: add MCP job_wait tool with long-poll and timeout envelope"
```

---

### Task 9: Integration Verification

- [ ] **Step 1: Run all tests**

Run: `pytest -q`
Expected: All tests pass (existing + new)

- [ ] **Step 2: Run mypy if configured**

Run: `mypy app/exceptions.py app/job_manager.py app/routers/jobs.py app/routers/diagnostics.py`
Expected: No errors (or pre-existing only)

- [ ] **Step 3: Run ruff if configured**

Run: `ruff check app/exceptions.py app/job_manager.py app/routers/jobs.py app/routers/diagnostics.py`
Expected: No errors

- [ ] **Step 4: Final commit with any lint fixes**

```bash
git add -A
git commit -m "fix: lint and type check fixes for P0 Core"
```
