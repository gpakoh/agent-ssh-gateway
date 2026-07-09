# Docker Admin Scope Design — `mcp:docker:admin`

**Date:** 2026-07-09
**Status:** Draft
**Session:** 165-A

## 1. Motivation

Session 164 added dangerous Docker operations (`docker_rm`, `docker_compose_down`, `docker_prune`) behind a one-time confirmation guard with validated container names. The current `mcp:docker` scope grants access to all Docker tooling, including these guarded destructive tools.

Some Docker operations are qualitatively more dangerous — they can grant shell/root-like access to the host through containers (`docker_exec`, `docker_run`) or destroy persistent infrastructure (`docker_rmi`, `docker_volume_rm`, volume/system prune). These require an **additional escalation** beyond the confirmation guard.

## 2. Scope Model

Introduce a new scope:

```
mcp:docker:admin
```

This scope is **never granted by default**. It must be explicitly added to a token's capabilities or profile definition.

### Flat scope model (no inheritance)

`mcp:docker:admin` is a **separate, independent scope**. It does NOT imply `mcp:docker`. No prefix matching. No inheritance.

```
mcp:docker          — read-only + safe lifecycle (ps, images, inspect, logs, stats,
                      start, stop, restart, compose up/restart/build/logs)
                      + guarded dangerous ops (rm, compose_down, prune)
mcp:docker:admin    — exec, run, rmi, volume_rm,
                      volume/system prune, compose down --volumes
```

A token with `mcp:docker:admin` CANNOT call `mcp:docker` tools unless it also has `mcp:docker` explicitly. Profiles that need both must explicitly include both scopes.

### Profile mapping

| Profile | Current docker scopes | Proposed change |
|---------|----------------------|-----------------|
| `viewer` | none | none |
| `operator` | none | none |
| `agent-runner` | none | none (unless explicitly justified by infra team) |
| `infra` | `mcp:docker` | **add `mcp:docker:admin`** |
| `full` | `mcp:docker` + `mcp:admin` | **add `mcp:docker:admin`** |

The `infra` and `full` profiles are the natural owners of admin-level Docker operations.

## 3. Double Barrier Pattern

All admin tools require **both** checks at different layers:

```
Layer 1 — Scope enforcement (fail-closed at proxy/gateway):
  Tool registered with scope = ["mcp:docker:admin"]
  Token without this scope → 403 before request reaches server

Layer 2 — Confirmation guard (at server, before execution):
  docker_confirm one-time token (60s TTL, consumed before exec)
  Token is created by the tool call, confirmed via docker_confirm
```

The confirmation guard **does not replace** scope enforcement. Scope is checked first; confirmation is checked second.

## 4. Tools

### 4.1 `docker_exec`

Execute a command inside an existing container.

**Scope:** `["mcp:docker:admin"]`
**Confirmation:** required
**Safety barriers:** 3 (scope + confirmation + argv denylist)

**Parameters:**
```json
{
  "container": "string (required)",
  "command": "string[] (required, argv only)",
  "timeout": "integer (optional, seconds, default 30, max 300)"
}
```

**Validation rules:**
- `command` must be a non-empty array of strings (argv style)
- Each element must be non-empty, printable ASCII
- `container` must match `^[a-zA-Z0-9_.-]+$`
- `timeout` clamped to [1, 300]

**Pre-confirm argv denylist (blocked before confirmation token is created):**

Reject if any argv element matches (case-sensitive exact or substring match):

| Pattern | Reason |
|---------|--------|
| `env` | environment dump |
| `printenv` | environment dump |
| `/proc/self/environ` | environment dump via procfs |
| `/proc/1/environ` | environment dump via procfs |
| `/etc/shadow` | credential file |
| `/etc/gshadow` | credential file |
| `/root/.ssh` | SSH key access |
| `/.ssh/id_` | SSH key access |
| `sh` + `-c` as adjacent argv elements | shell launcher (bypasses blocklist) |
| `bash` + `-c` as adjacent argv elements | shell launcher |
| `ash` + `-c` as adjacent argv elements | shell launcher |
| `zsh` + `-c` as adjacent argv elements | shell launcher |

Shell launcher check: if `argv[0]` is `sh`, `bash`, `ash`, or `zsh` AND `argv[1]` is `-c`, reject.

**Blocked error:**
```json
{
  "ok": false,
  "tool": "docker_exec",
  "result": null,
  "error": {
    "code": "DOCKER_EXEC_COMMAND_BLOCKED",
    "message": "docker_exec command is blocked by safety policy.",
    "retryable": false,
    "hint": "Use a narrower diagnostic command that does not dump environment variables, SSH keys, or shadow files."
  },
  "meta": {
    "source": "docker",
    "dangerous": true
  }
}
```

**Denylist caveat (MUST be in tool description):**
> This denylist is a safety guardrail, not a security boundary. `docker_exec` remains an admin-only dangerous operation and requires both `mcp:docker:admin` and confirmation guard. The system does not guarantee prevention of all data exfiltration through `docker_exec`.

**Output:**
```json
{
  "ok": true,
  "tool": "docker_exec",
  "result": {
    "stdout": "string (truncated to 64KB)",
    "stderr": "string (truncated to 64KB)",
    "exit_code": 0
  },
  "meta": { "source": "docker", "dangerous": true }
}
```

### 4.2 `docker_run`

Create and start a container from an image, run a command.

**Scope:** `["mcp:docker:admin"]`
**Confirmation:** required
**Safety barriers:** 3 (scope + confirmation + image allowlist)

**Parameters:**
```json
{
  "image": "string (required)",
  "command": "string[] (required, argv only)",
  "container_name": "string (optional, auto-generated if omitted)",
  "timeout": "integer (optional, seconds, default 60, max 600)"
}
```

**Image allowlist (MANDATORY, fail-closed):**
- Configured via env var `MCP_DOCKER_RUN_ALLOWED_IMAGES`
- Format: comma-separated exact image references with tag: `alpine:3.20,busybox:1.36,python:3.11-slim`
- If env var is empty or missing → `docker_run` is disabled with error code `DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED`
- MVP supports exact references only — no wildcards, no globs, no implicit `latest`
- Image tag must be specified explicitly; `alpine` (without tag) is rejected
- Allowlist is validated before confirmation token is created

**Image validation:**
- `image` must match `^[a-zA-Z0-9._/-]+:[a-zA-Z0-9._-]+$` (name + tag)
- Must be present in `MCP_DOCKER_RUN_ALLOWED_IMAGES` (exact string match)
- `image` with registry prefix (e.g. `docker.io/library/`) is matched literally against the configured list

**Disabled error (allowlist not configured):**
```json
{
  "ok": false,
  "tool": "docker_run",
  "result": null,
  "error": {
    "code": "DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED",
    "message": "docker_run requires MCP_DOCKER_RUN_ALLOWED_IMAGES environment variable.",
    "retryable": false,
    "hint": "Set MCP_DOCKER_RUN_ALLOWED_IMAGES with comma-separated image:tag entries."
  },
  "meta": { "source": "docker", "dangerous": true }
}
```

**Image not in allowlist error:**
```json
{
  "ok": false,
  "tool": "docker_run",
  "result": null,
  "error": {
    "code": "DOCKER_RUN_IMAGE_NOT_ALLOWED",
    "message": "Image 'nginx:latest' is not in the configured allowlist.",
    "retryable": false,
    "hint": "Only images listed in MCP_DOCKER_RUN_ALLOWED_IMAGES are permitted."
  },
  "meta": { "source": "docker", "dangerous": true }
}
```

**Explicitly excluded from MVP:**
| Feature | Reason | Future? |
|---------|--------|---------|
| `privileged` mode | full host access | Session 166+ |
| `host` network | network namespace escape | Session 166+ |
| `pid: host` | process visibility | Session 166+ |
| Arbitrary volume mounts | filesystem access | Session 166+ |
| Docker socket mount | Docker-in-Docker escape | Session 166+ |
| Port publishing | network exposure | Session 166+ |
| Environment variables | credential injection | Session 166+ |
| Interactive / TTY mode | console access | Session 166+ |
| `detach` / background | lifecycle management | Session 166+ |
| Auto-removal (`--rm` toggle) | cleanup policy | TBD |

MVP `docker_run` is strictly **run-a-process-in-a-container-and-wait**. No infrastructure setup.

**Output:** same envelope as `docker_exec` (stdout, stderr, exit_code).

### 4.3 `docker_rmi`

Remove one or more Docker images.

**Scope:** `["mcp:docker:admin"]`
**Confirmation:** required

**Parameters:**
```json
{
  "images": "string[] (required, 1-5 items)"
}
```

**Validation rules:**
- `images` must be a non-empty array of image references
- Each reference validated against `^[a-zA-Z0-9._/-]+(:[a-zA-Z0-9._-]+)?$`
- Maximum 5 images per call (no bulk/remove-all)
- No wildcard patterns accepted
- Validation is symmetric — confirmation is created per call, not per image
- If any image reference fails validation, the entire call is rejected

### 4.4 `docker_volume_rm`

Remove one or more Docker volumes.

**Scope:** `["mcp:docker:admin"]`
**Confirmation:** required

**Parameters:**
```json
{
  "volumes": "string[] (required, 1-5 items)"
}
```

**Validation rules:**
- `volumes` must be a non-empty array of volume names
- Each name validated against `^[a-zA-Z0-9_.-]+$`
- Maximum 5 volumes per call (no bulk/remove-all)
- No wildcard patterns accepted
- No `docker volume prune` mode for volumes in `docker_volume_rm` — use `docker_prune` with admin scope instead

### 4.5 `docker_prune` — expanded for admin scope

The existing `docker_prune` tool currently restricts type to `{"container", "image", "network"}`.

With `mcp:docker:admin` scope, the caller MAY specify additional types: `"volume"` and `"system"`.

**Implementation approach:**
- `_validate_prune_type` retains the existing restriction for `mcp:docker` scope
- Add an overloaded check: if the caller has `mcp:docker:admin`, accept `volume` and `system`
- Caller without `mcp:docker:admin` requesting `volume` or `system` → `ok=false` with `DOCKER_ADMIN_SCOPE_REQUIRED`. No silent fallback to default types.
- Confirmation guard is already required for `docker_prune` (from Session 164)

**Admin-only prune types:**
| Type | Behavior |
|------|----------|
| `volume` | Remove all unused volumes |
| `system` | Remove all unused containers, networks, images, and volumes |

### 4.6 `docker_compose_down` — expanded for admin scope

The existing `docker_compose_down` tool runs without `--volumes`.

With `mcp:docker:admin` scope, accept an additional boolean parameter:

```json
{
  "project_dir": "string",
  "file_path": "string",
  "remove_orphans": "boolean",
  "timeout": "integer",
  "volumes": "boolean (admin only, default false)"
}
```

- `volumes=true` without `mcp:docker:admin` → `ok=false` with error `DOCKER_ADMIN_SCOPE_REQUIRED`. No silent fallback.
- Confirmation guard is already required for `docker_compose_down` (from Session 164)

## 5. Error Codes (new)

| Code | Tool | Meaning |
|------|------|---------|
| `DOCKER_ADMIN_SCOPE_REQUIRED` | any expanded tool | caller lacks `mcp:docker:admin` for admin-only parameter |
| `DOCKER_EXEC_COMMAND_BLOCKED` | docker_exec | argv matched denylist |
| `DOCKER_EXEC_CONTAINER_NOT_FOUND` | docker_exec | container does not exist or is not running |
| `DOCKER_EXEC_TIMEOUT` | docker_exec | command exceeded timeout |
| `DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED` | docker_run | `MCP_DOCKER_RUN_ALLOWED_IMAGES` not set |
| `DOCKER_RUN_IMAGE_NOT_ALLOWED` | docker_run | image not in allowlist |
| `DOCKER_RUN_IMAGE_INVALID` | docker_run | image ref format rejected |
| `DOCKER_RUN_CONTAINER_CREATE_FAILED` | docker_run | container creation error |
| `DOCKER_RUN_TIMEOUT` | docker_run | command exceeded timeout |
| `DOCKER_RMI_INVALID_REFERENCE` | docker_rmi | image ref validation failed |
| `DOCKER_RMI_FAILED` | docker_rmi | docker rmi returned error |
| `DOCKER_VOLUME_RM_INVALID_NAME` | docker_volume_rm | volume name validation failed |
| `DOCKER_VOLUME_RM_FAILED` | docker_volume_rm | docker volume rm returned error |

## 6. Tool Registration

All 4 new tools + 2 expanded tools must be registered in:

| File | Change |
|------|--------|
| `tool_scopes.py` | Add tools with `["mcp:docker:admin"]` scope |
| `tool_modes.py` | Register in `chatgpt` mode |
| `tools_manifest.py` | Add to manifest |
| `server.py` | Add `@register_tool` functions |

## 7. Scope Enforcement in `tool_scopes.py`

Current: `TOOL_SCOPES` maps tool name → list of required scopes.
`has_required_scope` uses `any(s in token_scopes for s in required)`.

This means if a token has `["mcp:docker:admin"]`, it already satisfies `["mcp:docker"]` because `"mcp:docker:admin" in ["mcp:docker"]` is `False` with the current implementation! Wait — let me verify.

Current `has_required_scope`:
```python
def has_required_scope(token_scopes: list[str], tool_name: str) -> bool:
    required = get_required_scopes(tool_name)
    return any(s in token_scopes for s in required)
```

This checks: does any required scope appear in token_scopes? If tool requires `["mcp:docker"]` and token has `["mcp:docker:admin"]`, then `"mcp:docker" in ["mcp:docker:admin"]` is `False`. So `mcp:docker:admin` does NOT satisfy `mcp:docker` with the current code.

**Decision:** This is intentional and correct. `mcp:docker:admin` is a separate scope, not a superset of `mcp:docker`. Tokens must have BOTH `mcp:docker` AND `mcp:docker:admin` to call admin tools, OR we can change the profile to include both scopes.

**Recommended approach:** Update the `infra` and `full` profiles to include BOTH `mcp:docker` AND `mcp:docker:admin`. This keeps the scope enforcement simple and explicit.

```python
ACCESS_PROFILES = {
    "infra": [
        "mcp:read",
        "mcp:docker",
        "mcp:docker:admin",  # NEW
        "mcp:postgres",
        "mcp:repo",
    ],
    "full": [
        ...
        "mcp:docker",
        "mcp:docker:admin",  # NEW
        ...
    ],
}
```

Admin tools are registered with:
```python
"docker_exec": ["mcp:docker:admin"],
```

This means:
- Token with only `mcp:docker` → can call all existing docker tools but NOT admin tools
- Token with both `mcp:docker` + `mcp:docker:admin` → can call ALL docker tools
- Token with only `mcp:docker:admin` (no `mcp:docker`) → can ONLY call admin tools (not regular docker ones)

This flat approach is simpler and safer than implementing scope inheritance.

## 8. Modifications to Existing Tool Scopes

No changes to existing `mcp:docker` tool scopes. All current docker tools keep `["mcp:docker"]`.

The two tools with admin scope expansion (`docker_prune`, `docker_compose_down`) retain their existing tool scope. The admin-specific behavior (pruning volumes, `--volumes` flag) is gated at runtime by checking the token scopes for `mcp:docker:admin`.

## 9. Test Plan

| Area | Tests |
|------|-------|
| Scope enforcement | Token with only `mcp:docker` cannot call `docker_exec`, `docker_run`, `docker_rmi`, `docker_volume_rm` |
| Scope enforcement | Profile `infra` with `mcp:docker:admin` can call all admin tools |
| docker_exec | Valid argv accepted |
| docker_exec | `sh -c` rejected before confirmation |
| docker_exec | `env` and `printenv` rejected |
| docker_exec | `/etc/shadow` in any argv element rejected |
| docker_exec | SSH key paths rejected |
| docker_exec | Non-existent container rejected |
| docker_exec | Timeout produces DOCKER_EXEC_TIMEOUT |
| docker_run | Empty allowlist → DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED |
| docker_run | Image not in list → DOCKER_RUN_IMAGE_NOT_ALLOWED |
| docker_run | Missing tag rejected at format level |
| docker_run | Valid image + argv runs and returns output |
| docker_run | Container name validation |
| docker_rmi | Single image removed with confirmation |
| docker_rmi | Multiple images (up to 5) |
| docker_rmi | More than 5 rejected |
| docker_rmi | Invalid reference rejected |
| docker_volume_rm | Single volume removed with confirmation |
| docker_volume_rm | Invalid name rejected |
| docker_prune (admin) | `volume` type accepted with admin scope |
| docker_prune (admin) | `system` type accepted with admin scope |
| docker_prune (no admin) | `volume` type → DOCKER_ADMIN_SCOPE_REQUIRED |
| docker_compose_down | `volumes=True` with admin scope passes `--volumes` |
| docker_compose_down | `volumes=True` without admin scope → DOCKER_ADMIN_SCOPE_REQUIRED |
| Confirmation guard | All admin tools require confirmation |
| Confirmation guard | Consumed token rejected on admin tools |

## 10. Open Questions

| Question | Status |
|----------|--------|
| Should `docker:admin` imply `docker`? | **NO** — flat scopes, profiles contain both explicitly |
| `docker_exec` — block `cat` with `/etc/` prefix? | **YES** — specific paths listed, not general `cat` blocking |
| `docker_run` — should we support registry prefixes in allowlist? | **YES** — matched literally, user configures exact ref |
| `docker_run` — pull policy? | **Auto-pull is allowed.** Image is validated against allowlist before the Docker API call. Since the allowlist is the trust gate, pulling from an allowlisted registry is safe. Future: force-digest pinning. |
| `docker_rmi` — force flag? | **NO** in MVP. All removals are non-force. |
| `docker_volume_rm` — force flag? | **NO** in MVP. All removals are non-force. |
| Healthcheck tool count impact | +4 new tools (docker_exec, docker_run, docker_rmi, docker_volume_rm). Existing docker_prune and docker_compose_down are expanded in-place, not new tools. Expected count: 102 → 106. |

## 11. Security Model Summary

| Tool | Scope | Confirmation | Additional Guard |
|------|-------|-------------|-----------------|
| docker_ps | mcp:docker | no | — |
| docker_inspect | mcp:docker | no | — |
| docker_logs | mcp:docker | no | — |
| docker_start/stop/restart | mcp:docker | no | — |
| docker_compose_up | mcp:docker | no | — |
| docker_rm | mcp:docker | yes | confirmation guard, validated container name |
| docker_compose_down | mcp:docker | yes | no --volumes (by default) |
| docker_prune (c/i/n) | mcp:docker | yes | limited types |
| **docker_exec** | **mcp:docker:admin** | **yes** | **argv denylist** |
| **docker_run** | **mcp:docker:admin** | **yes** | **image allowlist** |
| **docker_rmi** | **mcp:docker:admin** | **yes** | **no bulk** |
| **docker_volume_rm** | **mcp:docker:admin** | **yes** | **no bulk** |
| **docker_prune (v/s)** | **mcp:docker:admin** | **yes** | — |
| **docker_compose_down -v** | **mcp:docker:admin** | **yes** | — |
