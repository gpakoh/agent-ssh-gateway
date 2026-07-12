# Phase 2: MCP Latency, Jobs, Diagnostics & SDK

**Date:** 2026-07-12
**Status:** Draft → Accepted (pending review)
**Applies to:** web-ssh-gateway v0.1.30a0+

## Overview

Phase 2 extends the MCP server with latency instrumentation, long-poll job waiting, diagnostic
tools, a high-level SDK, argv-based script execution, and project-scoped patch apply.

Architecture principle: Gateway owns SSH sessions, job storage, and auth. MCP is a thin
translation layer that converts HTTP API responses into MCP Contract v1 envelopes.

---

## Section 1 — P0: Latency Instrumentation

### JobRecord timestamps (Gateway, `app/job_manager.py`)

Monotonic (`time.monotonic()`) for duration calculations:

```
queued_at_mono: float | None           # create_job()
acquired_at_mono: float | None          # _run_job() begins
command_started_at_mono: float | None   # execute_stream() SSH channel open
command_finished_at_mono: float | None  # execute_stream() yields exit
completed_at_mono: float | None         # job reaches terminal state
ssh_connect_started_at_mono: float | None
ssh_connected_at_mono: float | None     # null if session reused
```

Wall clock (`time.time()`) for display:

```
created_at: float           # exists (time.time)
started_at: float | None    # exists (time.time)
completed_at: float | None  # new (time.time)
```

### Note

Monotonic timestamps (`queued_at_mono`, etc.) are relative to process start. They do NOT survive
process restarts and must NOT be used as wall-clock values. Use `created_at`, `started_at`,
`completed_at` for display and cross-process ordering.

### Computed durations

```python
queue_wait_ms = (acquired_at_mono - queued_at_mono) * 1000
command_execution_ms = (command_finished_at_mono - command_started_at_mono) * 1000
gateway_total_ms = (completed_at_mono - queued_at_mono) * 1000
ssh_connect_ms = (ssh_connected_at_mono - ssh_connect_started_at_mono) * 1000
# ssh_connect_ms = null when session reused
```

### LatencyTracker breakdown (MCP, `examples/mcp_server/latency_metrics.py`)

```
ssh_job_ms       — POST execute → terminal result
gateway_http_ms  — individual HTTP call (GET/POST)
serialization_ms — Contract v1 envelope shaping
mcp_total_ms     — full MCP tool call (existing)
```

### Endpoint

`GET /api/diagnostics/latency` (Gateway, scope `diagnostics:read`). Not in `/health`.

MCP `LatencyTracker` is MCP-process-local. MCP exposes it via `diagnostics_latency` tool.

---

## Section 2 — P0: Long-poll `GET /api/jobs/{job_id}/wait`

### Endpoint

```http
GET /api/jobs/{job_id}/wait?timeout=30
Scope: jobs:read
```

`AuthIdentity.sub` compared against `JobRecord.owner_id`.

### JobManager.wait_for_completion()

```python
async def wait_for_completion(job_id: str, identity_sub: str, timeout_s: float) -> dict:
    job = await self.get_job(job_id)
    if not job:
        raise JobNotFoundError(job_id)

    if job.owner_id != identity_sub:
        raise PermissionDeniedError("Job belongs to a different owner")

    if job.status in TERMINAL_STATES:
        return job.to_dict()

    event = job.completed_event
    if job.status in TERMINAL_STATES:   # re-check after subscribe
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

### Error types

| Condition | Error | MCP mapping |
|-----------|-------|-------------|
| Job not found | `JobNotFoundError` | `JOB_NOT_FOUND` |
| Wrong owner | `PermissionDeniedError` | `PERMISSION_DENIED` |
| Timeout | JSON `wait_timed_out: true` | `WAIT_TIMEOUT` |
| Client disconnect | `asyncio.CancelledError` (re-raised) | — |

### Cancel behaviour

- `CancelledError` caught and re-raised only — job untouched.
- No HTTP 499 custom response.

### Single-worker enforcement

- Explicit env var `GATEWAY_WORKERS` (not `WEB_CONCURRENCY` — catches all uvicorn modes).
- Route is **always registered** regardless of worker count.
- At runtime, if `GATEWAY_WORKERS != "1"`, handler returns:
  ```json
  {"error": "NOT_SUPPORTED", "detail": "Long-poll requires GATEWAY_WORKERS=1"}
  ```
- `GatewayClient.wait_job()` falls back to polling-based wait only on `NOT_SUPPORTED`
  or HTTP 404 (old Gateway version without this endpoint). No fallback on
  `PERMISSION_DENIED`, `JOB_NOT_FOUND`, or other real errors.

### Timeout range

`timeout` query param: `0.1` to `300` seconds. Default `30`.

---

## Section 3 — P0: MCP Job Tools

### Naming

- `GatewayClient.wait_job(job_id, timeout)` — internal SDK method, calls long-poll endpoint.
  Does NOT take `session_id`: long-poll checks ownership via `AuthIdentity.sub`, not SSH session.
- MCP tool `job_wait(job_id, timeout)` — public, wraps `GatewayClient.wait_job`, handles timeout envelope.

### Existing tools unchanged

`job_status`, `job_result`, `job_cancel`, `job_list`.

### Behaviour

`GatewayClient.wait_job(job_id, timeout=30)`:

1. Calls `GET /api/jobs/{job_id}/wait?timeout={timeout}`.
2. If `wait_timed_out: true` → return with timeout indicator.
3. Otherwise return full job result.

Fallback: if Gateway returns `NOT_SUPPORTED` (multi-worker) or 404 (old Gateway version),
`GatewayClient.wait_job()` falls back to polling-based wait. No fallback on
`PERMISSION_DENIED`, `JOB_NOT_FOUND`, or other real errors.

MCP `job_wait`:

1. Calls `GatewayClient.wait_job()`.
2. Timeout → `ContractV1.error("WAIT_TIMEOUT", ...)`.
3. Completion → `ContractV1.set_output(result)`.

---

## Section 4 — P1: Diagnostics

### 4a. GET /api/auth/whoami

```http
GET /api/auth/whoami
Scope: auth:read
```

```json
{
  "identity": "user:admin",
  "scopes": ["jobs:read", "jobs:run", "files:read"],
  "auth_method": "api_key",
  "credential_id": "ak_abc123"
}
```

No `session_id`. `credential_id` = safe non-secret (first 8 chars of API key hash).

### 4b. Gateway health

`GET /health` returns Gateway metadata only:

```json
{
  "status": "ok",
  "build_sha": "f5403a9...",
  "build_time": "2026-07-12T12:30:00Z",
  "started_at": "2026-07-12T12:31:05Z",
  "version": "0.1.30a0"
}
```

### 4c. MCP health tool

MCP `health` tool aggregates:

```json
{
  "mcp": {
    "build_sha": "...", "build_time": "...", "started_at": "...",
    "toolset_hash": "sha256:abc123...",
    "tools_count": 105, "contract_version": "1"
  },
  "gateway": {
    "build_sha": "...", "build_time": "...", "started_at": "...",
    "version": "0.1.30a0"
  }
}
```

### 4d. Build metadata (`app/build_info.py`)

```
BUILD_SHA env → git rev-parse → "unknown"
BUILD_TIME env → ""
STARTED_AT set in app lifespan (not import time)
```

### 4e. Toolset hash (MCP init)

```python
items = [{"name": t.name, "inputSchema": t.inputSchema} for t in tools]
items.sort(key=lambda item: item["name"])
canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
```

(Not `builtins.hash()` — unstable across processes. Not `sorted(dicts)` — Python dicts aren't comparable.)

---

## Section 5 — P1: GatewaySession SDK

### File: `sdk/session.py`

```python
class GatewaySession:
    """Synchronous context manager for SSH Gateway.

    Usage:
        with GatewaySession(client) as gw:
            result = gw.run("ls -la")
    """
    def __init__(self, client: GatewayClient):
        self.client = client
        self.session_id: str | None = None

    def __enter__(self) -> GatewaySession:
        try:
            self.session_id = self.client.connect()
            return self
        except Exception:
            self._disconnect_best_effort()
            raise   # Python does NOT call __exit__ if __enter__ raises

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._disconnect_best_effort()

    def _disconnect_best_effort(self) -> None:
        if self.session_id:
            try:
                self.client.disconnect(self.session_id)
            except Exception:
                pass  # log only, never mask original exception

    def run(self, command: str, timeout: int | None = None) -> dict:
        job = self.client.execute_restricted(
            session_id=self.session_id, command=command
        )
        return self.client.wait_job(
            job_id=job["job_id"], timeout=timeout
        )  # wait_job uses auth, not session_id

    def read(self, path: str) -> str:
        result = self.client.read_file(session_id=self.session_id, path=path)
        return result.get("content", "")

    def write(self, path: str, content: str) -> dict:
        """Write file. Returns Gateway response — may contain pending_confirmation."""
        return self.client.write_file(
            session_id=self.session_id, path=path, content=content
        )

    def session_health(self) -> dict:
        return self.client.session_health(session_id=self.session_id)
```

All methods explicitly pass `session_id`. `write()` returns Gateway response as-is (may contain
`pending_confirmation` — SDK does NOT auto-confirm).

### AsyncGatewaySession

Same pattern with `async __aenter__` / `async __aexit__`.

### profile parameter

Removed from v1. Profile resolution is future scope.

### Requirements

- `__enter__` catches exceptions and calls `_disconnect_best_effort()` before re-raising.
- `__exit__`/`__aexit__` never masks original exception.
- All calls pass `session_id` explicitly.
- `write()` returns raw Gateway response (caller handles confirmation).
- `from sdk.session import GatewaySession, AsyncGatewaySession`.

---

## Section 6 — P2: argv-based Script Execution

### Priority

P2 (new DX feature, not stability).

### Gateway endpoint

```http
POST /api/ssh/execute-argv
Scope: ssh:execute:argv
```

```json
{
  "session_id": "uuid",
  "argv": ["python3", "-c", "print('hello')"],
  "stdin": "",
  "timeout_s": 30
}
```

### Validation

1. `argv` is `list[str]`, non-empty.
2. Each arg: `len <= 255`, NUL-free.
3. Total serialized UTF-8 length `<= 65536` bytes.
4. stdin `<= 1 MiB`, UTF-8 only (binary not supported in v1).
5. `timeout_s` in `[1, 3600]`.
6. Session ownership check.
7. Command policy applied to full `argv` (before serialization).

### Execution

1. Serialize: `shlex.join(argv)` (POSIX-target).
2. Do NOT wrap with `bash -c` / `sh -c`.
3. Execute via existing SSH channel (Paramiko exec).
4. stdin, stdout, stderr handled **concurrently** (separate asyncio tasks or
   `async for` reader) to prevent SSH channel deadlock.
5. Send stdin bytes → `channel.shutdown_write()`.
6. Stdout/stderr: `<= 10 MiB` each with redaction. Combined output `<= 10 MiB` for
   MCP response — if exceeded, set `meta.truncated`.
7. Return `stdout`, `stderr`, `exit_code`, `duration`.

### MCP tool

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
```

### Docstring

NOT: "no shell interpretation". Correct: "serialized as a safely quoted POSIX command."

---

## Section 7 — P2: Project-scoped Patch Apply

### Gateway endpoint

```http
POST /api/projects/{project}/apply-patch
Scope: project:patch
```

```json
{
  "session_id": "uuid",
  "patch": "--- a/file.py\n+++ b/file.py\n@@ -1,3 +1,4 @@\n...",
  "format": "unified",
  "expected_hashes": {"file.py": "sha256:abcdef..."},
  "strip": 1,
  "dry_run": false
}
```

### Unified diff parser

- Add `unidiff` to `pyproject.toml` dependencies.
- NOT "implement a lightweight parser" — `unidiff` for parsing, custom validator for policy.

Parses:
- File headers (`--- a/...`, `+++ b/...`) with `strip` applied.
- Hunk headers (`@@ -start,count +start,count @@`).
- Context/added/removed lines.

### Pre-flight (all in memory, no writes)

1. Parse entire patch into per-file hunks via `unidiff`.
2. Validate all paths against `ProjectRegistry`:
   - Under project root.
   - Project allowed for target/profile of this SSH session.
3. Read each existing file via `FileEditor.read_file()`.
4. Verify `expected_hashes` (required for existing files being modified).
5. Apply hunks in memory with position validation.
6. If `dry_run`: return diff preview and halt.

### Transactional write with rollback

Temp/backup files are created **alongside the original file** using hidden dot prefix on
**filename only** (not the full path):

```python
from pathlib import Path
path = Path("src/file.py")
rid = request_id  # unique per request
temp = path.parent / f".{path.name}.mcp-patch-{rid}.tmp"
backup = path.parent / f".{path.name}.mcp-patch-{rid}.bak"
```

State machine:

```
IDLE → BACKUP_CREATED → TEMP_WRITTEN → FSYNCED → RENAMED → CLEANUP_DONE
```

1. **For each file sequentially:**
   a. Copy with permissions preserved: `cp -p '{original}' '{backup}'`.
   b. Write new content to temp file via SSH base64 heredoc.
   c. Run `sync '{temp}'` (fsync equivalent via SSH).
   d. Atomic rename: `mv '{temp}' '{original}'` (same filesystem).
2. **After success:** delete backup and temp files (`rm -f`).
3. **On any write error during step 1:**
   a. Roll back completed files: `mv '{backup}' '{original}'`.
   b. If rollback itself fails: return `ROLLBACK_FAILED` with list of affected files.
   c. Delete remaining temp/backup files (best-effort).
   d. Report first write error with partial status.

All remote paths are validated and safely quoted (`shlex.quote`).

### v1 forbidden list

- Binary files (detect via `is_binary` heuristic).
- Rename/copy operations.
- File mode changes.
- Symlink modifications.
- `/dev/null` paths (file creation/deletion out of scope).

### Limits

- `<= 20` files per patch.
- `<= 100` hunks total.
- `<= 1 MiB` total patch size.
- `<= 10 MiB` per file (pre-patch content).

### MCP tool

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
    """
    Apply a unified diff patch to project files.

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
```

`session_id` is required (no managed session in MCP tools).

---

## Section 8 — Implementation Order

| Step | Priority | What | Depends On |
|------|----------|------|------------|
| 1 | P0 | Latency instrumentation (JobRecord mono timestamps, LatencyTracker breakdown) | — |
| 2 | P0 | Long-poll (wait_for_completion, endpoint, GATEWAY_WORKERS enforcement) | Step 1 |
| 3 | P0 | MCP job tools (job_wait via long-poll) | Step 2 |
| 4 | P1 | Whoami (GET /api/auth/whoami) | — |
| 5 | P1 | Build metadata (build_info.py, health expansion, toolset hash) | Step 1 |
| 6 | P1 | SDK session (sdk/session.py, GatewaySession/AsyncGatewaySession) | — |
| 7 | P2 | execute_argv (endpoint, command policy, auth scope, MCP tool) | auth scopes, SSH channel |
| 8 | P2 | Patch apply (unified diff parser, endpoint, rollback, MCP tool) | ProjectRegistry, FileEditor |

Steps 1, 4, 6 can be started in parallel. Steps 2→3 are sequential.
