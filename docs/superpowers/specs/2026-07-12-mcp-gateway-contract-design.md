# MCP SSH Gateway — Contract & Architecture Design

**Date:** 2026-07-12
**Status:** Draft
**Contract Version:** 1

---

## 1. Response Envelope

Every tool returns a uniform envelope placed in MCP `structuredContent` (not a JSON string inside text content).

```json
{
  "ok": true,
  "result": {},
  "error": null,
  "meta": {
    "contract_version": "1",
    "tool": "project_run_ruff",
    "request_id": "uuid",
    "duration_ms": 842,
    "truncated": false,
    "warnings": []
  }
}
```

```json
{
  "ok": false,
  "result": null,
  "error": {
    "code": "DEPENDENCY_MISSING",
    "message": "Required executable 'uv' was not found",
    "hint": "Install uv on the SSH target or configure another runner",
    "retryable": false,
    "details": {
      "required_binary": "uv"
    }
  },
  "meta": {
    "contract_version": "1",
    "tool": "project_run_ruff",
    "request_id": "uuid",
    "duration_ms": 104,
    "truncated": false,
    "warnings": []
  }
}
```

### Rules

| Field | Always | Description |
|-------|--------|-------------|
| `ok` | yes | `true` = operation executed as designed; `false` = infrastructure/input error |
| `result` | yes | `null` on error |
| `error` | yes | `null` on success |
| `meta` | yes | always present |
| `result.outcome` | conditional | `"passed"` / `"failed"` / `"completed"` for checks and commands — NOT a top-level error |

- `ok:true` → MCP `isError:false`
- `ok:false` → MCP `isError:true`
- All exceptions MUST be caught and return envelope; raw `RuntimeException` or upstream text NEVER leaks out
- Secrets are never logged. Internal stack traces may be logged and correlated by `request_id`.
- Transport-level errors (invalid MCP auth, malformed request) are returned at the transport layer, not in envelope.

---

## 2. Error Codes

### Infrastructure errors (top-level, `ok:false`)

| Code | When | retryable |
|------|------|-----------|
| `INVALID_INPUT` | Invalid parameter or format | false |
| `AUTHENTICATION_REQUIRED` | Missing or invalid token | false |
| `PERMISSION_DENIED` | Insufficient scope | false |
| `POLICY_DENIED` | Command/path blocked by policy | false |
| `DEPENDENCY_MISSING` | Required binary not found | false |
| `TOOL_EXECUTION_FAILED` | Tool-specific fatal exit code (config error, crash) | false |
| `PROJECT_NOT_FOUND` | Project directory does not exist | false |
| `PROJECT_INVALID` | Path exists but not a valid project | false |
| `FILE_NOT_FOUND` | File does not exist | false |
| `JOB_NOT_FOUND` | Job ID unknown | false |
| `JOB_NOT_READY` | Result requested before completion | true |
| `WAIT_TIMEOUT` | MCP stopped waiting, job may continue | true |
| `EXECUTION_TIMEOUT` | Execution stopped by timeout | depends¹ |
| `SSH_CONNECTION_FAILED` | Could not establish/reestablish SSH | true |
| `SSH_EXECUTION_FAILED` | Could not send/start command (not about exit code) | depends² |
| `UPSTREAM_UNAVAILABLE` | Docker/PostgreSQL/GitHub/Gitea unavailable | true |
| `RATE_LIMITED` | Rate limit hit upstream or gateway | true |
| `CONFLICT` | Operation incompatible with current state | false |
| `NOT_SUPPORTED` | Feature not supported by this backend | false |
| `INTERNAL_ERROR` | Unexpected internal failure | false |

### Result outcomes (inside `result.outcome`, NOT top-level errors)

| outcome | Meaning |
|---------|---------|
| `"completed"` | Operation finished (default for exec, query, etc.) |
| `"passed"` | Check passed (lint, type-check, test run green) |
| `"failed"` | Check found issues (lint violations, test failures) |

¹ `retryable` for `EXECUTION_TIMEOUT` depends on idempotency of the specific operation. Read-only tools (status, health, lint checks) are retryable; write operations should set `retryable: false`.

² `SSH_EXECUTION_FAILED` is retryable when the command was definitely not received by the target (connection dropped before send). When the state is uncertain (sent but no ack), automatic retry is dangerous — set `retryable: false` for write operations.

A non-zero exit code from a remote command is NOT an infrastructure error. If the command started and returned — transport worked.

---

## 3. Tool Naming Convention

### Format

```
<domain>_<action>[_<subject>]
```

- Maximum length: **48 characters**
- Only `snake_case`
- No `gateway_` prefix — server context makes it implicit
- No auto-generated hashes in public names
- Collisions detected at startup and in CI

### Mapped names

| Current (hashed) | New |
|---|---|
| `gateway_project_arch_ea87598b0874` | `project_archive_task` |
| `gateway_project_handoff_status_*` | `project_handoff_status` |
| `gateway_project_write_plan_*` | `project_write_plan` |
| `gateway_project_run_ruff` | `project_run_ruff` |
| `gateway_project_run_pytest` | `project_run_pytest` |
| `gateway_project_run_mypy` | `project_run_mypy` |
| `gateway_project_run_compileall` | `project_run_compileall` |
| `gateway_project_read_file` | `project_read_file` |
| `gateway_project_show` | `project_show` |
| `gateway_project_struct` | `project_tree` |
| `gateway_project_glob` | `project_find_files` |

Docker tools stay `docker_*`, Postgres stays `postgres_*`, etc.

Old hashed names are removed at next contract version. No aliases unless external clients exist.

---

## 4. Python Runner Tools — `uv` via SSH

### Architecture

```
MCP client → project_run_ruff(project="web-ssh-gateway", target=["src/"])
  → resolve project → project_dir (Phase 1: read-only allowlist)
  → SSH gateway → uv run --frozen ruff check src/
  → parse exit code → envelope
```

### Backends

| Backend | Default | Description |
|---------|---------|-------------|
| `ssh` | yes | Run `uv` on SSH target (project host) |
| `local` | opt-in | Run `uv` via subprocess on MCP host, for locally-mounted projects |

### Project resolution (Phase 1 — minimal)

A read-only mapping `project_name → project_dir` validated against configured
allowed roots. The mapping is defined in config and checked at startup — unknown
names return `PROJECT_NOT_FOUND` before any SSH call.

Phase 2 may add a dynamic registry; Phase 1 uses a static allowlist only:

```python
# config
projects = {
    "web-ssh-gateway": "/media/1TB/Python/web_ssh/web-ssh-gateway",
    "quart-ollama_bot": "/media/1TB/Python/quart-ollama_bot",
}
allowed_roots = ["/media/1TB/Python/", "/var/www/"]
```

### Preflight checks

1. `project` → resolves to `project_dir` from allowlist → unknown name = `PROJECT_NOT_FOUND`
2. `target` is an array of paths (not a string). Each must be relative, inside project root, no traversal (`..` as a segment is blocked), no symlink escape
3. `uv` binary exists on target (SSH `command -v uv`). Exit 127 or empty → `DEPENDENCY_MISSING`
4. Tool listed in dev-dependencies (optional — `uv run` will fail and return `DEPENDENCY_MISSING` otherwise)

### Execution safety

- Command is built as `argv`, never a shell string
- SSH command is assembled with safe quoting (`shlex.quote` for each argument)
- Arbitrary CLI flags are NOT accepted from caller; each tool defines its own allowlist (e.g. `--no-header` for mypy)
- Targets are passed as positional arguments separated from options by `--`

Each tool has its own argv template — `uv` requires `--` at the right level:

```python
# ruff
["uv", "run", "--frozen", "--directory", project_dir, "--",
 "ruff", "check", "--", *targets]

# mypy
["uv", "run", "--frozen", "--directory", project_dir, "--",
 "mypy", "--", *targets]

# pytest
["uv", "run", "--frozen", "--directory", project_dir, "--",
 "pytest", "--", *targets]

# compileall
["uv", "run", "--frozen", "--directory", project_dir, "--",
 "python", "-m", "compileall", *targets]
```

`--frozen` prevents lock file modification. `uv` may still create `.venv` and
download packages from the network. This is acceptable: the project's
`uv.lock` pins exact versions, and `--frozen` ensures the lock stays unchanged.

**Requirement:** `uv.lock` must exist in the project directory. If absent, the
tool returns `PROJECT_INVALID` — reproducibility cannot be guaranteed without
a pinned lock file.

### Tool → uv subcommand mapping

| Tool | uv command | Standard exit codes |
|------|-----------|-------------------|
| ruff | `uv run --frozen ruff check <targets>` | 0, 1 |
| mypy | `uv run --frozen mypy <targets>` | 0, 1, 2 |
| pytest | `uv run --frozen pytest <targets>` | 0, 1, 2, 3, 4, 5 |
| compileall | `uv run --frozen python -m compileall <targets>` | 0, 1 |

### Exit code → result mapping

| Exit code | Meaning | `ok` | `result.outcome` |
|-----------|---------|------|-------------------|
| 0 | Tool ran clean | true | `"passed"` |
| 1 | Tool found issues | true | `"failed"` |
| 2 (ruff) | Ruff internal error (config, crash) | false | null |
| 2 (mypy) | Mypy found issues (same as 1) | true | `"failed"` |
| 2 (pytest) | Pytest interrupted (SIGINT) | false | null |
| 3 | Pytest internal error | false | null |
| 4 | Pytest usage error | false | null |
| 5 | Pytest found no tests | true | `"failed"`, `reason: "NO_TESTS"` |
| 127 | uv/tool not found | false | null |

Known non-0/non-1 exit codes that indicate an infrastructure/configuration
problem → `TOOL_EXECUTION_FAILED`.

### Result mapping summary

| Scenario | `ok` | `result.outcome` | `error.code` |
|----------|------|-------------------|--------------|
| uv not found | false | null | `DEPENDENCY_MISSING` |
| tool not in deps | false | null | `DEPENDENCY_MISSING` |
| uv run → exit 0 | true | `"passed"` | null |
| uv run → exit 1 | true | `"failed"` | null |
| uv run → exit 5 (pytest) | true | `"failed"` | null |
| uv run → exit 2+ (tool crash) | false | null | `TOOL_EXECUTION_FAILED` |
| SSH connection lost | false | null | `SSH_CONNECTION_FAILED` |
| timeout | false | null | `EXECUTION_TIMEOUT` |

### Standardised command result

Every execution result contains:

```json
{
  "outcome": "passed",
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",
  "execution_duration_ms": 842,
  "job_id": null,
  "timestamps": {
    "created": "2026-07-12T12:00:00Z",
    "started": "2026-07-12T12:00:01Z",
    "finished": "2026-07-12T12:00:02Z"
  }
}
```

- `result.execution_duration_ms` — time the remote command actually took (wall clock on target)
- `meta.duration_ms` — total MCP tool call time (including auth, serialisation, polling overhead)
- `job_id` is null for synchronous tools, populated for deferred operations
- `outcome` is `"completed"` for non-check commands (sql query, exec)

### Configuration

```python
# Per-tool defaults with tool-specific overrides
tool_configs = {
    "ruff": {"timeout_s": 60, "max_output_bytes": 100_000},
    "pytest": {"timeout_s": 300, "max_output_bytes": 500_000},
    "mypy": {"timeout_s": 120, "max_output_bytes": 200_000},
    "compileall": {"timeout_s": 60, "max_output_bytes": 100_000},
}
```

### Output truncation

When output exceeds `max_output_bytes`:
- `meta.truncated = true`
- `meta.warnings` includes truncation notice
- `meta.truncation = {"limit_bytes": N, "returned_bytes": N}`
- Truncation can accompany both success and error responses

---

## 5. Latency — Measurement, Optimisation, Long-poll

### Current problem

10 health calls → 22.6s, 10 SQL queries → 22.6s, 5 pwd → 16.3s.
SSH command itself takes ~0.05s. Overhead is in MCP transport/serialisation/job polling.

### Phase A — Measure (before any optimisation)

1. MCP transport latency: single `health` → raw HTTP round-trip
2. Concurrency test: 10 parallel requests → total wall time (detect serialisation)
3. SSH job breakdown: single `project_run_ruff` → decompose into: auth + job-create + polling iterations + result-return
4. Non-SSH calls: `postgres_select` with dummy SQL → measure to distinguish MCP overhead from SSH polling

Run measurements, analyse where time is spent, THEN optimise the bottleneck.

### Phase B — Polling optimisation (after measurement confirms polling is a factor)

| Phase | Interval |
|-------|----------|
| First check | 0ms (immediate) |
| Retry 1-3 | 100ms |
| Retry 4-8 | 250ms |
| Retry 9+ | 500ms |
| Max total | configurable (default 30s) |

Stop immediately on terminal state.

### Phase C — Long-poll endpoint

```http
GET /jobs/{job_id}/wait?timeout=30
```

- Holds connection until job completes or timeout
- Returns job result on completion
- Closes with `WAIT_TIMEOUT`, `job_state: "running"` on timeout
- Authorised via same token as parent request
- Fallback to polling if `wait` unavailable

### Tool semantics

| Tool | Behaviour |
|------|-----------|
| `project_run_ruff` / `project_run_pytest` / `project_run_mypy` | synchronous — uses `wait` internally |
| `job_start` (generic) | returns `job_id` immediately |
| `job_wait(job_id)` | explicit `wait` call |
| `job_status(job_id)` | polling snapshot |
| `job_result(job_id)` | returns cached result |

`job_id` is preserved even for synchronously-waited jobs, so clients can
re-query the result later if needed.

---

## 6. find_files — Safe Glob

### Pattern rules

Allowed glob characters: `*`, `?`, `[abc]`, `**`
NOT allowed in v1: `{a,b}` (requires custom implementation; defer)

Blocked shell metacharacters: `;`, `|`, `&`, `$`, `` ` ``, `>`, `<`, `$(`
Blocked path segments: exact `..` as a path component (not substring match)

Pattern is processed by Python `pathlib`, never expanded by shell.

### Resolution

The caller passes a full glob pattern (e.g. `docs/**/*.md`, `tests/*.py`).
Use `Path.glob()` — NOT `rglob()`, since the pattern already includes `**`
for recursive search.

```python
EXCLUDE_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
MAX_DEPTH = 20
MAX_RESULTS = 200
TIMEOUT_S = 5

project_root = Path(project_dir).resolve()
results = []

def is_excluded(rel: Path) -> bool:
    return any(part in EXCLUDE_DIRS for part in rel.parts)

for path in project_root.glob(pattern):
    try:
        resolved = path.resolve()
        rel = resolved.relative_to(project_root)  # symlink escape guard
    except ValueError:
        continue  # outside project root → skip
    if not resolved.is_file():
        continue  # directories excluded by default
    if len(rel.parts) > MAX_DEPTH:
        continue
    if is_excluded(rel):
        continue
    results.append(str(rel))
    if len(results) >= MAX_RESULTS:
        break

results.sort()
```

- Directories are excluded by default. A future `include_dirs` param may be added.
- Depth limit applies to the resolved path segment count.
- Timeout of 5s per call prevents runaway searches; `signal.alarm` or `asyncio.wait_for`.

### Errors

| Pattern issue | Error code |
|---------------|------------|
| Syntax error in pattern | `INVALID_INPUT` |
| Traversal / escape attempt | `POLICY_DENIED` |
| Pattern too complex (>10 wildcards) | `INVALID_INPUT` |

---

## 7. Compose — project_dir only (Phase 1)

### Removed

- `file_path` parameter removed from all `docker_compose_*` tools

### project_dir (Phase 1)

All `docker_compose_*` tools accept `project_dir` only. Rules:

- Must be an absolute canonical path
- Validated against `allowed_roots` (same shared config as project resolution)
- Symlink escape checked: `Path(project_dir).resolve()` must stay within allowed root
- Compose file existence confirmed before execution (`PROJECT_INVALID` if absent)

### Phase 2 — registered project name

```json
{
  "project": "web-ssh-gateway",
  "compose_file": "docker/compose.production.yml"
}
```

`compose_file` is relative to project root, validated. Same `project` resolution
as other tools — single registry shared across all subsystems.

---

## 8. health — rename from gateway_health, rename postgres field

```
"postgres": false  →  "session_store_postgres": false
"persistent_sessions": false  →  leave as-is
```

The tool is renamed from `gateway_health` to `health` (naming convention:
no `gateway_` prefix). The field rename makes clear the field refers to
the session store, not the standalone PostgreSQL adapter.

---

## 9. Destructive Operations — Two-phase Confirmation

Confirmation MUST be required for:

- `docker_stop`, `docker_start`, `docker_restart`
- `docker_remove`, `docker_prune`
- `docker_compose_up`, `docker_compose_down`, `docker_compose_restart`
- `docker_exec`, `docker_run`
- All `project_stop`, `project_restart`
- All write/delete operations

### Protocol (NOT a `confirm: true` parameter — that is trivially bypassed)

The `request_*` tool returns a standard envelope with `outcome: "pending_confirmation"`:

```json
{
  "ok": true,
  "result": {
    "outcome": "pending_confirmation",
    "confirmation_token": "tok_abc123",
    "action_preview": {"operation": "docker_stop", "container": "web-ssh-gateway"},
    "expires_in": 60
  },
  "error": null,
  "meta": { ... }
}
```

Then a separate `confirm_operation(token)` tool executes the actual operation:

- `confirmation_token` is single-use, short-lived (60s default), bound to exact operation + arguments
- Token is generated server-side and stored in memory (not sent by client upfront)
- Client (model) CANNOT pre-send confirmation — token does not exist before request
- Expired or invalid token → `INVALID_INPUT`
- Token consumed on successful confirmation; replay returns `INVALID_INPUT`

---

## 10. GitHub/Gitea Response Size — Normalisation

### Goals

- No internal IPs/hostnames in responses
- No user email/private fields unless explicitly requested
- Decode text file contents (keep base64 only for binaries)
- Remove duplicated repository objects
- Pagination uses precise `limit`, `next_cursor`, `has_more` — not vague "1-2 pages"
- Maximum response: 50 KB hard limit (truncate with `meta.truncated = true`)

### Pagination contract

```json
{
  "result": {
    "items": [...],
    "pagination": {
      "limit": 50,
      "next_cursor": "cursor_string",
      "has_more": true
    }
  }
}
```

- `limit` matches requested or default page size
- `next_cursor` is opaque, returned only when `has_more` is true
- Lists are never silently truncated; if truncated by size limit, `meta.truncated = true`

### What to strip

| Field | Action |
|-------|--------|
| `node_id`, `graphql_id` | remove |
| `owner.email` | remove |
| `permissions` | keep only for current user |
| `self`, `html`, `git`, `ssh` URLs | keep only public SSH/HTTPS |
| `tarball_url`, `archive_url` | remove |
| Repeated `repository` in issue/PR objects | replace with `repo: "owner/name"` |
| `contents` in base64 | decode if text, remove if binary |
| `commit.committer` | keep name, remove email if internal |

---

## 11. Implementation Order

### Phase 1 (High priority)
1. Response envelope refactor (all tools → uniform `ok`/`result`/`error`/`meta`)
2. Standardised command result shape — `result` includes `outcome`, `exit_code`, `stdout`, `stderr`, `execution_duration_ms`, `job_id`, `timestamps`; `meta.duration_ms` for total MCP time
3. Tool renaming — remove hashes, `gateway_` prefix, `project_*` convention, `health` (was `gateway_health`), `postgres` → `session_store_postgres`
4. `project` name resolution — minimal read-only allowlist (shared across all tools)
5. `project_run_ruff` / `pytest` / `mypy` / `compileall` → `uv` via SSH with full exit-code mapping
6. `find_files` → safe glob (no brace expansion)
7. `docker_compose_*` — remove `file_path`, validate `project_dir`
8. Two-phase confirmation for destructive operations — protect all mutating docker, compose, project tools
9. Latency measurement framework + initial measurements

### Phase 2 (Medium priority)
10. Polling optimisation (immediate + exponential backoff)
11. Long-poll `wait` endpoint
12. `job_wait`, `job_status`, `job_result` tools

### Phase 3 (Lower priority)
13. GitHub/Gitea response normalisation
14. `local` backend for Python tools
15. Dynamic project registry (Phase 2 of project resolution)
