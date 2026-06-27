# Agent Backend Router Design

**Status**: Draft  
**Session**: 129  
**ADR**: [ADR-2026-06-26-agent-backend-routing.md](../../architecture/ADR-2026-06-26-agent-backend-routing.md)

---

## 1. Problem

Two agent backends exist (OpenCode, Mimo) but there is no automatic selection or
fallback between them. If OpenCode is rate-limited (`Free usage exceeded`), the
task fails with no retry. The operator must manually detect the state and call
the alternative tool.

Existing ADR explicitly calls for a **quota-aware backend router** that:
- Detects provider cooldown
- Records structured cooldown entries
- Falls back to an alternative backend
- Sets task status to `blocked/provider-cooldown` when all backends unavailable

## 2. Scope

**In scope:**
- Backend registry (providers + status)
- Provider cooldown tracking (time-based)
- Selection policy with fallback order
- Integration with existing `project_run_opencode` / `project_run_mimo` call sites
- Task status updates when fallback chain exhausted
- Env var configuration for fallback order, cooldown durations
- Unit-testable in isolation (no SSH, no real runners)

**Out of scope:**
- Proxy rotation (per ADR)
- UI / API for managing backends
- Live health-check pings — status is updated on execution result only
- Multi-machine scheduling — all execution goes through existing SSH session

## 3. Architecture

### 3.1 Backend Registry

An in-memory dict keyed by backend name, holding runtime status.
Loaded once at startup, mutated during execution.

```python
@dataclass
class BackendEntry:
    name: str                    # "opencode" | "mimo"
    priority: int                # 0 = primary, 1 = first fallback, …
    status: BackendStatus        # current runtime status
    cooldown_until: float | None # epoch timestamp; None if not in cooldown
    last_error: str | None       # last error message for diagnostics
    last_tried_at: float | None  # timestamp of last execution attempt
```

```python
class BackendStatus(enum.Enum):
    AVAILABLE = "available"       # ready to accept tasks
    COOLDOWN = "cooldown"         # rate-limited; retry after cooldown_until
    FAILED = "failed"             # non-recoverable error (binary not found, config)
    DISABLED = "disabled"         # manually disabled by operator
```

### 3.2 Provider Cooldown Registry

Separate concern from the backend registry — specifically tracks rate-limit
cooldowns so they survive a single task attempt and prevent busy-looping.

```python
@dataclass
class CooldownEntry:
    provider: str
    detected_at: float
    cooldown_seconds: int
    reason: str                  # "rate_limit" | "timeout" | "error"

    @property
    def until(self) -> float:
        return self.detected_at + self.cooldown_seconds

    @property
    def active(self) -> bool:
        return time.time() < self.until
```

Cooldown detection happens on **structured error output** from the runner:
- OpenCode: stdout/stderr contains `Free usage exceeded, retrying in 7h`
- Mimo: stdout/stderr returns non-zero exit with timeout/model error
- Detection is a configurable regex list in the router

### 3.3 Selection Policy

Policy interface with one method:

```python
class SelectionPolicy(ABC):
    @abstractmethod
    def select(
        self,
        backends: dict[str, BackendEntry],
        cooldowns: list[CooldownEntry],
        preferred: str | None,     # from task.agent field, if set
    ) -> str | None:
        ...
```

Built-in policies:

| Policy | Behavior |
|--------|----------|
| `TryPrimaryFallback` (default) | Try preferred → ordered fallbacks → skip cooldown/failed/disabled |
| `RoundRobin` | Cycle through available backends evenly |

Policy selection is determined by `MCP_BACKEND_SELECTION_POLICY` env var
(default: `try-primary-fallback`).

#### TryPrimaryFallback details

1. If `preferred` is set and status is `AVAILABLE` — return preferred
2. If `preferred` is in `COOLDOWN` — skip (don't wait; don't error)
3. Iterate remaining backends sorted by `priority` ascending
4. Return first where status is `AVAILABLE`
5. If none found — return `None` (all backends exhausted)

### 3.4 Router Class

```python
class AgentBackendRouter:
    def __init__(
        self,
        backends: dict[str, BackendEntry] | None = None,
        policy: SelectionPolicy | None = None,
        cooldown_default: int = 25200,       # 7h default for rate limits
        cooldown_error: int = 300,            # 5m default for transient errors
    ):
        ...

    def select_backend(
        self, task_agent: str | None = None
    ) -> str | None:
        """Return best backend name, or None if all unavailable."""

    def record_result(
        self, backend: str, exit_code: int, stdout: str, stderr: str
    ) -> CooldownEntry | None:
        """Update backend status based on execution result.
        Returns a CooldownEntry if a cooldown was triggered."""

    def get_status(self) -> dict[str, BackendEntry]:
        """Snapshot of all backends for diagnostics."""

    def get_cooldowns(self) -> list[CooldownEntry]:
        """Active cooldowns."""
```

`record_result` logic:

1. exit_code == 0 → set status `AVAILABLE`, clear `cooldown_until`
2. exit_code != 0 and stderr matches rate-limit pattern → status `COOLDOWN`,
   `cooldown_until = now + cooldown_default`, record `CooldownEntry`
3. exit_code != 0 and no rate-limit match → status `FAILED`,
   `cooldown_until = now + cooldown_error`, record `CooldownEntry` with
   `reason = "error"`
4. If a cooldown is already active for this backend, extend it if new
   cooldown_until is later

### 3.5 Integration Model

The router is **not** a new MCP tool. It is a library class used by existing
runner code. Integration is a mechanical change to `opencode_tools.py` and
`mimo_tools.py`.

**Current flow (both runners):**
```
MCP tool handler (server.py)
  → opencode_tools.project_run_opencode(run_cmd, ...)
    → run_cmd(project, shell_script)         # via GatewayClient
    → return structured result
```

**Proposed flow:**
```
MCP tool handler (server.py)
  → router.select_backend(task_agent)
  → if None: return blocked/provider-cooldown

  → opencode_tools.project_run_opencode(run_cmd, ...)
    → run_cmd(project, shell_script)
    → router.record_result(result)

  → if result.status == "failed" and backends remain:
      → router.select_backend()  # next in fallback chain
      → repeat with next backend
  → return result
```

### 3.6 Cooldown Detection Patterns

```python
COOLDOWN_PATTERNS: dict[str, list[re.Pattern]] = {
    "opencode": [
        re.compile(r"Free usage exceeded", re.IGNORECASE),
        re.compile(r"rate.limit", re.IGNORECASE),
        re.compile(r"retry in", re.IGNORECASE),
    ],
    "mimo": [
        re.compile(r"model.*not.*found", re.IGNORECASE),
        re.compile(r"ollama.*timeout", re.IGNORECASE),
        re.compile(r"OLLAMA_RETRY_EXCEEDED", re.IGNORECASE),
    ],
}
```

These patterns are matched against combined `stdout + "\n" + stderr` from the
runner's result.

## 4. Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `MCP_BACKEND_SELECTION_POLICY` | `try-primary-fallback` | Selection policy name |
| `MCP_BACKEND_FALLBACK_ORDER` | `opencode,mimo` | Comma-separated priority list |
| `MCP_BACKEND_COOLDOWN_DEFAULT` | `25200` | Default cooldown seconds (7h) |
| `MCP_BACKEND_COOLDOWN_ERROR` | `300` | Cooldown for transient errors (5m) |
| `MCP_BACKEND_COOLDOWN_PATTERNS` | (built-in) | JSON override for cooldown detection patterns |

None of these are required; defaults work out of the box.

## 5. Status: all backends unavailable

When `select_backend()` returns `None`, the calling tool returns a structured
error response:

```json
{
    "isError": true,
    "content": [{
        "type": "text",
        "text": "Task blocked: all agent backends unavailable.\n"
                "  opencode: COOLDOWN until 2026-06-28T03:00:00Z (rate_limit)\n"
                "  mimo: FAILED (binary not found)\n"
                "Resolve the issue or disable the failing backend and retry."
    }]
}
```

The task's `.ai-bridge/tasks/<id>/agent-status.md` is NOT written — execution
never started. The `write_agent_task` already created task.json, so the
operator knows the task exists but has not been dispatched.

## 6. File Layout

```
examples/mcp_server/
├── agent_backend_router.py    # NEW — AgentBackendRouter, BackendEntry,
│                              #         BackendStatus, CooldownEntry,
│                              #         SelectionPolicy, COOLDOWN_PATTERNS
├── opencode_tools.py          # MODIFY — wrap run_cmd with router
├── mimo_tools.py              # MODIFY — wrap run_cmd with router
├── server.py                  # MODIFY — instantiate router, pass to tools
└── config.py                  # (reuse existing settings or add new function)

tests/
├── test_agent_backend_router.py  # NEW — unit tests in isolation
└── test_mcp_opencode.py          # EXISTING — verify integration
```

## 7. Testing Strategy

| Test | Scope | What |
|------|-------|------|
| `test_select_returns_available` | Unit | Preferred available backend returned |
| `test_select_skips_cooldown` | Unit | Cooldown backend skipped, falls to next |
| `test_select_none_available` | Unit | All backends exhausted → None |
| `test_select_respects_priority` | Unit | Lower priority chosen only when higher unavailable |
| `test_record_cooldown_rate_limit` | Unit | Rate-limit pattern triggers COOLDOWN status |
| `test_record_cooldown_error` | Unit | Non-rate-limit error triggers short cooldown |
| `test_record_success_clears_cooldown` | Unit | Successful run clears prior cooldown |
| `test_record_extends_existing_cooldown` | Unit | Longer cooldown extends, shorter does not |
| `test_cooldown_entry_active_expired` | Unit | Time-based active/expired boundaries |
| `test_integration_with_opencode` | Integration | Router wraps opencode_tools, fallback works |

## 8. Migration Path

**Phase 1 (this session):** Design spec only — no code.

**Phase 2 (Session 130+):** Implementation.
1. Create `agent_backend_router.py` with router + cooldown + selection
2. Add unit tests (8-10 tests)
3. Wire into `server.py` at startup
4. Modify `opencode_tools.py` / `mimo_tools.py` to use router
5. Add integration test for fallback flow
6. Deploy, verify with existing healthcheck + enforce smoke
7. Document in `MCP_OPERATOR_RUNBOOK.md` under "Common failures"

## 9. Non-Goals

- **Live health pings** — router only learns status from execution results.
  An operator can manually reset via `MCP_BACKEND_RESET=opencode` env var or
  future admin command.
- **Multi-instance coordination** — each service instance has its own
  in-memory state. No Redis/shared state for cooldown awareness.
  Acceptable for single-replica deployment.
- **Proxy rotation** — handled externally via `OPENCODE_BIN` override per ADR.
- **Operator UI** — router exposes `get_status()` for `mcp-token status`
  or future admin tool, not a web UI.

## 10. Related

- [ADR-2026-06-26-agent-backend-routing.md](../../architecture/ADR-2026-06-26-agent-backend-routing.md)
- [MCP_OPERATOR_RUNBOOK.md](../../operations/MCP_OPERATOR_RUNBOOK.md) (post-implementation)
- [MCP_FLEET_RUNBOOK.md](../../operations/MCP_FLEET_RUNBOOK.md)
- `examples/mcp_server/opencode_tools.py` — `project_run_opencode()`
- `examples/mcp_server/mimo_tools.py` — `project_run_mimo()`
- `scripts/opencode_runner_wrapper.py` — local runner, `find_opencode_bin()`
