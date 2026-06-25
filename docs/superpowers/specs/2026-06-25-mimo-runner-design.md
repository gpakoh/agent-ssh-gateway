# Mimo Runner MCP Tool — Design Spec

- **Session**: 104
- **Date**: 2026-06-25
- **Status**: Draft
- **Author**: agent-ssh-gateway

## Overview

Add a new MCP tool `gateway_project_run_mimo` that executes an existing handoff task via Mimo CLI **inside a disposable git worktree**. Unlike the OpenCode runner, Mimo runs with `--dangerously-skip-permissions` and is restricted to linked git worktrees only.

## Architecture

```
examples/mcp_server/mimo_tools.py
→ project_run_mimo(run_cmd, *, project, task_id, model=None)

examples/mcp_server/server.py
→ @register_tool("gateway_project_run_mimo")
→ MCP tool: gateway_project_run_mimo(project, task_id, model=None)

examples/mcp_server/tool_modes.py
→ chatgpt set only

tests/test_mcp_mimo.py
→ unit tests, command construction, registration, integration
```

### Visibility

| Mode | Visible |
|------|---------|
| chatgpt | yes |
| full | no |
| standard | no |
| minimal | no |

### Python function signature

```python
def project_run_mimo(
    run_cmd: Callable[[str, str], dict[str, Any]],
    *,
    project: str,
    task_id: str,
    model: str | None = None,
) -> dict[str, Any]:
```

Returns: `task_id, status, exit_code, stdout, stderr, started_at, finished_at`

## Guards — Pre-flight checks (11 checks)

All guards execute inside the shell script on the SSH target, before `mimo run`.

### 1. task.json exists

```bash
if [ ! -f "$td/task.json" ]; then
  echo "Error: task.json not found in $td" >&2; exit 1
fi
```

### 2. agent == "mimo"

```bash
AGENT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("agent",""))' "$td/task.json" 2>/dev/null)
if [ "$AGENT" != "mimo" ]; then
  echo "Error: task.json agent is not mimo" >&2; exit 1
fi
```

### 3. worktree_path exists in task.json

```bash
WORKTREE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("worktree_path",""))' "$td/task.json" 2>/dev/null)
if [ -z "$WORKTREE" ]; then
  echo "Error: worktree_path not set in task.json" >&2; exit 1
fi
```

### 4. MCP_GATEWAY_WORKTREE_ROOT is set

```bash
if [ -z "$MCP_GATEWAY_WORKTREE_ROOT" ]; then
  echo "Error: MCP_GATEWAY_WORKTREE_ROOT not set" >&2; exit 1
fi
```

### 5. worktree_path exists as directory

```bash
if [ ! -d "$WORKTREE" ]; then
  echo "Error: worktree_path does not exist or is not a directory" >&2; exit 1
fi
```

### 6. Canonical realpath variables

```bash
PROJECT_REAL=$(realpath .)
WORKTREE_REAL=$(realpath "$WORKTREE")
WORKTREE_ROOT_REAL=$(realpath "$MCP_GATEWAY_WORKTREE_ROOT")
```

### 7. worktree_path != project root

```bash
if [ "$WORKTREE_REAL" = "$PROJECT_REAL" ]; then
  echo "Error: worktree_path equals project root" >&2; exit 1
fi
```

### 8. worktree_path under MCP_GATEWAY_WORKTREE_ROOT

```bash
case "$WORKTREE_REAL" in
  "$WORKTREE_ROOT_REAL"/*) ;;
  *)
    echo "Error: worktree_path outside MCP_GATEWAY_WORKTREE_ROOT" >&2; exit 1
    ;;
esac
```

### 9. Valid git worktree

```bash
if ! git -C "$WORKTREE_REAL" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: worktree_path is not a git worktree" >&2; exit 1
fi
```

### 10. worktree top-level matches

```bash
GIT_TOP=$(git -C "$WORKTREE_REAL" rev-parse --show-toplevel 2>/dev/null)
if [ "$(realpath "$GIT_TOP")" != "$WORKTREE_REAL" ]; then
  echo "Error: worktree_path is not the top-level of its worktree" >&2; exit 1
fi
```

### 11. Linked worktree (not main checkout)

```bash
GIT_DIR=$(git -C "$WORKTREE_REAL" rev-parse --git-dir 2>/dev/null)
GIT_COMMON_DIR=$(git -C "$WORKTREE_REAL" rev-parse --git-common-dir 2>/dev/null)
if [ "$GIT_DIR" = "$GIT_COMMON_DIR" ]; then
  echo "Error: worktree_path is a main git checkout, not a linked disposable worktree" >&2; exit 1
fi
```

## Binary discovery

Priority:

1. `MIMO_BIN` env var
2. `command -v mimo`
3. `/root/.mimocode/bin/mimo`

```bash
MIMO_BIN="${MIMO_BIN:-$(command -v mimo 2>/dev/null || true)}"
if [ -z "$MIMO_BIN" ] && [ -x "/root/.mimocode/bin/mimo" ]; then
  MIMO_BIN="/root/.mimocode/bin/mimo"
fi
if [ -z "$MIMO_BIN" ] || [ ! -x "$MIMO_BIN" ]; then
  echo "Error: Mimo binary not found" >&2; exit 127
fi
```

Python-side fallback (for unit tests / dry-run):

```python
import shutil, os

def find_mimo_bin(mimo_bin: str | None = None) -> str:
    if mimo_bin and os.path.isfile(mimo_bin):
        return mimo_bin
    env_val = os.environ.get("MIMO_BIN")
    if env_val and os.path.isfile(env_val):
        return env_val
    if os.path.isfile("/root/.mimocode/bin/mimo"):
        return "/root/.mimocode/bin/mimo"
    which = shutil.which("mimo")
    if which:
        return which
    raise FileNotFoundError("Mimo binary not found")
```

## Model validation

```python
import re

MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,80}$")

def validate_model(model: str | None) -> str | None:
    if model is None:
        return None
    if not MODEL_RE.fullmatch(model):
        raise ValueError(f"Invalid model name: {model!r}")
    return model
```

Allowed examples: `big-pickle`, `zen/big-pickle`, `claude-sonnet-4`, `provider:model`.

## Execution command

After guards pass:

```bash
cd "$WORKTREE_REAL"

MIMO_FLAGS="--dangerously-skip-permissions"
MODEL_FLAG=""
if [ -n "$MODEL" ]; then
  MODEL_FLAG=" --model $MODEL"
fi

"$MIMO_BIN" run $MIMO_FLAGS$MODEL_FLAG \
  "Read $PROJECT_REAL/.ai-bridge/tasks/$TASK_ID/current-plan.md in the parent repo at $PROJECT_REAL. Execute the plan fully inside worktree $WORKTREE_REAL. Do not commit, do not push, do not create branches. Save the implementation diff to $PROJECT_REAL/.ai-bridge/tasks/$TASK_ID/implementation-diff.patch. Update $PROJECT_REAL/.ai-bridge/tasks/$TASK_ID/agent-status.md as you go. Work only inside $WORKTREE_REAL."
RC=$?
```

## Result reporting

After execution:

```bash
# status
if [ $RC -eq 0 ]; then
  echo 'Status: needs-review' > "$PROJECT_REAL/$td/agent-status.md"
else
  echo 'Status: failed' > "$PROJECT_REAL/$td/agent-status.md"
fi

# diff from worktree
git -C "$WORKTREE_REAL" diff --no-color > "$PROJECT_REAL/$td/implementation-diff.patch" 2>/dev/null

# agent-report
cat > "$PROJECT_REAL/$td/agent-report.md" << REOF
# Mimo Runner Result — $TASK_ID

- Agent: mimo
- Status: $(head -1 "$PROJECT_REAL/$td/agent-status.md" | cut -d' ' -f2)
- Exit code: $RC
- Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)
- Worktree: $WORKTREE_REAL
REOF
```

## Tool Registration

In `server.py`:

```python
from mimo_tools import project_run_mimo as _project_run_mimo

@register_tool("gateway_project_run_mimo")
def gateway_project_run_mimo(
    project: str,
    task_id: str,
    model: str | None = None,
) -> dict[str, Any]:
    from write_modes import assert_handoff_write_allowed
    assert_handoff_write_allowed()
    return run_tool(
        tool="gateway_project_run_mimo",
        title="Run mimo task",
        fn=lambda: _project_run_mimo(
            lambda p, c: run_project_command(client, p, c),
            project=project,
            task_id=task_id,
            model=model,
        ),
        success_text="Submitted mimo task.",
    )
```

## Testing

File: `tests/test_mcp_mimo.py`

### Unit tests (no binary required)

- `test_invalid_task_id_raises` — same as opencode
- `test_valid_model_accepted` — `big-pickle`, `zen/big-pickle`, `claude-sonnet-4`
- `test_invalid_model_rejected` — `Big Pickle`, `x; rm -rf /`, `$(whoami)` → ValueError before run_cmd
- `test_accepted_task_id_returns_structured_result` — smoke result
- `test_failed_run_returns_failed_status` — fake run_cmd with exit_code=1

### Command construction tests (check shell script text)

- `test_command_contains_worktree_root_guard`
- `test_command_contains_agent_mimo_guard`
- `test_command_contains_dangerously_skip_permissions`
- `test_command_contains_do_not_commit`
- `test_command_contains_diff_from_worktree`
- `test_command_contains_linked_worktree_check`
- `test_command_contains_mimo_binary_discovery`

### Registration tests

- `test_registered_in_chatgpt_mode` — `tool_modes.should_register_tool("gateway_project_run_mimo") is True`
- `test_tool_function_can_be_imported` — from server module (skipif no mcp)

### Integration tests (skipif no MIMO_BIN)

- `test_mimo_bin_not_found_returns_error` — with fake worktree
- `test_dry_run_does_not_require_bin` — if dry-run mode exists

## Files to create/modify

| File | Action |
|------|--------|
| `examples/mcp_server/mimo_tools.py` | Create — `project_run_mimo()` function |
| `examples/mcp_server/server.py` | Edit — add `gateway_project_run_mimo` tool |
| `examples/mcp_server/tool_modes.py` | Edit — add `"gateway_project_run_mimo"` to chatgpt set |
| `tests/test_mcp_mimo.py` | Create — all test groups |
| `scripts/mimo_runner_wrapper.py` | Create (optional) — if local CLI wrapper needed |
