# Dangerous Docker Operations with Confirmation Guard

## Status

Approved.

## Objective

Add two-phase confirmation for destructive Docker operations. Dangerous commands (`docker rm`, `docker compose down`, `docker prune`) cannot execute in one call — they require a confirm token, which is one-time and expires in 60 seconds.

## Scope (Session 164 MVP)

### Add

| Tool | What it does | Scope |
|------|-------------|-------|
| `docker_rm` | Remove a container by name | `mcp:docker` |
| `docker_compose_down` | Stop and remove compose stack | `mcp:docker` |
| `docker_prune` | Prune Docker resources (container, image, network only) | `mcp:docker` |
| `docker_confirm` | Confirm a pending dangerous action with a one-time token | `mcp:docker` |
| `docker_pending_actions` | List all pending dangerous actions | `mcp:docker` |

### Explicitly excluded (Session 164)

- `docker_exec` — postponed to Session 165 (needs `mcp:docker:admin` scope)
- `docker_run` — postponed to Session 165
- `docker_volume_rm` — postponed
- `docker_rmi` — postponed
- `prune volume` / `prune system` — too destructive for MVP
- `compose down -v` — volume removal blocked in MVP

## Confirmation Store

**File:** `examples/mcp_server/docker_confirm.py`

In-memory store, process-local, no persistence, no I/O.

```python
@dataclass
class ConfirmAction:
    action_id: str          # UUID v4
    tool: str               # e.g. "docker_rm"
    kwargs: dict[str, Any]  # original args
    confirm_token: str      # secrets.token_urlsafe(16)
    summary: str            # human-readable, e.g. "Remove container foo"
    risk: str               # "high"
    created_at: float       # time.monotonic()
    consumed: bool          # one-time flag
```

**API:**
- `create_action(tool, kwargs, summary) -> ConfirmAction`
- `confirm_action(token) -> tuple[ConfirmAction | None, ConfirmStatus]` — distinguishes invalid/expired/consumed
- `list_pending() -> list[dict]` — masks confirm_token (first 6 chars + "...")
- `cleanup_expired() -> int` — removes expired entries

**ConfirmStatus:**

```python
class ConfirmStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    EXPIRED = "expired"
    CONSUMED = "consumed"
```

**TTL:** 60 seconds via `time.monotonic()`.
**Token entropy:** `secrets.token_urlsafe(16)` = 128 bits.
**Token comparison:** `hmac.compare_digest()` to prevent timing attacks.
**Token logging:** never log full token, only `action_id` and token prefix.

## Dangerous Tool Behavior

First call does NOT execute Docker. Returns canonical envelope:

```json
{
  "ok": true,
  "tool": "docker_rm",
  "result": {
    "status": "confirmation_required",
    "action_id": "uuid",
    "confirm_token": "abc123...",
    "expires_in_sec": 60,
    "summary": "Remove container mcp-docker-confirm-test",
    "risk": "high"
  },
  "error": null,
  "meta": {
    "source": "docker",
    "dangerous": true
  }
}
```

Execution only via `docker_confirm(token)`:

Success (`exit_code == 0`):

```json
{
  "ok": true,
  "tool": "docker_confirm",
  "result": {
    "action": "docker_rm",
    "executed": true,
    "stdout": "mcp-docker-confirm-test",
    "stderr": "",
    "exit_code": 0
  },
  "error": null,
  "meta": {
    "source": "docker"
  }
}
```

Failure (`exit_code != 0`):

```json
{
  "ok": false,
  "tool": "docker_confirm",
  "result": {
    "action": "docker_rm",
    "stdout": "",
    "stderr": "Error response from daemon: No such container",
    "exit_code": 1
  },
  "error": {
    "code": "DOCKER_COMMAND_FAILED",
    "message": "Docker command failed",
    "retryable": false,
    "hint": "Check container name or Docker state."
  },
  "meta": {
    "source": "docker"
  }
}
```

## DockerClient Additions

**File:** `examples/chatgpt_remote_mcp/fleet/docker_client.py`

New methods follow existing pattern: argv-only, no shell=True, existing validation:

| Method | Signature | Docker command |
|--------|-----------|---------------|
| `rm` | `async rm(container, force=False) -> RunResult` | `docker rm [-f] <container>` |
| `compose_down` | `async compose_down(project_dir=None, file_path=None, remove_orphans=False, timeout=30) -> RunResult` | `docker compose down [--remove-orphans] [-t N]` |
| `prune` | `async prune(type="container") -> RunResult` | `docker <type> prune -f` |

**New return type `RunResult`:**

```python
@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int
```

Needed because dangerous tools need to report exit_code + stderr separately (existing `_run` only returns stdout and raises on non-zero).

Add `_run_with_result()` that returns `RunResult` instead of raising on non-zero exit.

### Prune type validation

```python
ALLOWED_PRUNE_TYPES = {"container", "image", "network"}

def _validate_prune_type(self, type: str) -> str:
    if type not in ALLOWED_PRUNE_TYPES:
        raise ValueError(f"Unsupported prune type '{type}'. Allowed: {sorted(ALLOWED_PRUNE_TYPES)}")
    return type
```

### compose_down — no volumes

```python
# No --volumes / -v flag in MVP
# Only --remove-orphans and --timeout
```

## Tool Registration

**File:** `examples/mcp_server/server.py`

New tools follow existing `@register_tool("docker_*")` pattern. All are async, all create `DockerClient()` per call, all return canonical envelope (dict with `ok`, `tool`, `result`, `error`, `meta`). First dangerous tool to break the str pattern, aligned with ADR-2026-07-08.

**Rule:** `RunResult.exit_code == 0` → `tool_success(result=RunResult)`. `RunResult.exit_code != 0` → `tool_error(code="DOCKER_COMMAND_FAILED", result=RunResult)`.

All scoped `["mcp:docker"]` in `tool_scopes.py`.

All registered in `chatgpt` mode in `tool_modes.py`.

## Safety Rules

- Confirm token TTL: 60 seconds
- Token one-time: consumed flag prevents replay
- **Token consumed BEFORE Docker execution** — prevents double-execute under parallel confirm. If Docker fails, token is still spent (safer for destructive ops).
- Token comparison via `hmac.compare_digest()`
- No shell=True anywhere — argv-only
- Container names validated via existing `_validate_container_name()` regex
- Compose file paths validated via existing `_validate_compose_file()` + `_resolve_compose_file_path()`
- Prune type validated against allowlist
- Never log full confirm_token, only prefix
- Pending actions masked in list output

## Tests

**File:** `tests/test_docker_confirm.py` (~30 tests)

- create_action returns ConfirmAction with expected fields
- confirm_action with valid token returns action
- confirm_action marks consumed
- second confirm returns None (one-time)
- expired token returns None
- invalid token returns None
- list_pending masks token (only prefix visible)
- list_pending excludes consumed
- cleanup_expired removes expired entries
- create_action generates unique tokens

**Integration tests** (`tests/test_docker_confirm_live.py`, optional, requires Docker):
- docker_rm returns confirmation_required (not executed)
- docker_confirm executes the pending action
- docker_compose_down returns confirmation_required
- docker_prune returns confirmation_required
- Full flow: create rm → confirm → verify container gone

## Live Smoke

Manual smoke via shell + curl (not through MCP tools, one-shot):

```bash
docker run -d --name mcp-docker-confirm-test alpine sleep 300
# Call docker_rm via tools/call → confirmation_required
# docker_confirm(token) → container removed
# docker_ps(all=true) → container gone
```

## Files Changed

| File | Change |
|------|--------|
| `examples/chatgpt_remote_mcp/fleet/docker_client.py` | Add `RunResult`, `_run_with_result()`, `rm()`, `compose_down()`, `prune()`, `_validate_prune_type()` |
| `examples/mcp_server/docker_confirm.py` | New — confirmation store |
| `examples/mcp_server/server.py` | Add 5 `@register_tool` functions |
| `examples/mcp_server/tool_scopes.py` | Add 5 tools → `["mcp:docker"]` |
| `examples/mcp_server/tool_modes.py` | Add 5 tools → `chatgpt` mode |
| `tests/test_docker_confirm.py` | New — unit tests for confirm store |
| `tests/test_docker_client.py` or new | Tests for new DockerClient methods |
| `scripts/mcp_fleet_healthcheck.py` | Update expected tool count: 97 → 102 |

## Migration Impact

- Healthcheck expected count: `97 → 102`
- No breaking changes to existing tools
- Dangerous tools return dict (not str) — first deviation from Docker str pattern, aligned with new response contract
- Existing read tools (ps, images, inspect, etc.) unchanged
