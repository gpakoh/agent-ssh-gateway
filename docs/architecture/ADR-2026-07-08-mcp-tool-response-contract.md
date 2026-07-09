# ADR-2026-07-08: MCP Tool Response Contract

## Status

Accepted.

## Context

MCP tools across the gateway return responses in at least five distinct formats:

1. **Structured content via `text_result()`/`error_result()`** — all `gateway_*`, `gitea_*`, and `github_*` tools return a `dict[str, Any]` containing `text`, `structuredContent`, and `_meta` keys. This is the most evolved pattern and includes metadata.
2. **Raw string** — all `docker_*` tools (14 tools) and `postgres_*` tools (6 tools) return `str` directly. The MCP layer wraps it as `content[0].text`, but no `structuredContent` or `_meta` is provided.
3. **Arbitrary `dict`** — `Context7` tools return `str` extracted from upstream MCP content blocks. The upstream response is opaque.
4. **Job result dict** — `chatgpt_tools.py` utility functions return raw GatewayClient job result dicts with varying schemas (e.g. `{"stdout", "stdout_full", "stderr", "exit_code", "timed_out", "cancelled"}`).
5. **Error text with `isError`** — `error_result()` sets `isError: True` in the content block but returns a plain error string. There is no structured error envelope.

This inconsistency creates problems:

- **Client friction**: clients must handle at least 3 different response shapes. A ChatGPT/OpenCode agent must check `isError`, then look at `structuredContent` or fall back to `content[0].text`, or parse arbitrary string formats.
- **Observability gap**: tools that return bare `str` bypass any standard `_meta` like `duration_ms`, `redacted`, or `source`.
- **Error handling ambiguity**: some tools return `"error: ..."` as a plain string without `isError`, forcing clients to pattern-match on text prefixes.
- **No migration path**: without a canonical envelope, each new tool guesses which format to use.

## Decision

1. **Adopt a canonical response envelope** for all MCP tools returning to the MCP client.
2. **Provide a reusable helper** (`tool_results.py`) that all tools CAN use.
3. **Do NOT immediately migrate existing tools.** The helper is available for new tools and gradual opt-in migration.
4. **The canonical envelope replaces `text_result()`/`error_result()` for new tools.** Existing helpers remain for backward compatibility during migration.
5. **Docker and Postgres tools are the highest-priority migration targets** because they return bare `str`.

### Canonical Success Envelope

```json
{
  "ok": true,
  "tool": "gateway_project_read_file",
  "result": { ... },
  "error": null,
  "meta": {
    "duration_ms": 53,
    "redacted": false,
    "truncated": false,
    "source": "gateway"
  }
}
```

### Canonical Error Envelope

```json
{
  "ok": false,
  "tool": "docker_restart",
  "result": null,
  "error": {
    "code": "CONTAINER_NOT_FOUND",
    "message": "Container not found: example",
    "retryable": false,
    "hint": "Run docker_ps to list containers."
  },
  "meta": {
    "duration_ms": 12,
    "source": "docker"
  }
}
```

### Rules

| Rule | Detail |
|------|--------|
| `ok` | Required. `true` for success, `false` for error. |
| `tool` | Required. Tool name matching the MCP tool name. |
| `result` | Optional on error (`null`). Can be `str`, `dict`, `list`, or `None`. |
| `error` | `null` on success. On error: `{"code", "message", "retryable", "hint"}`. |
| `meta.duration_ms` | Elapsed wall-clock time for the operation. |
| `meta.redacted` | `true` if any field was redacted (secrets, keys). |
| `meta.truncated` | `true` if result was truncated (large response). |
| `meta.source` | Origin: `gateway`, `docker`, `postgres`, `gitea`, `github`, `context7`, `agent`. |
| No secrets | Never include tokens, passwords, keys in `meta` or `result`. |
| `error.retryable` | `true` if the operation is safe to retry. |
| `error.hint` | Optional user-facing guidance string. |

### Error Codes

Canonical error codes (extensible):

| Code | Meaning |
|------|---------|
| `TOOL_NOT_FOUND` | Tool or resource not found |
| `CONTAINER_NOT_FOUND` | Docker container not found |
| `SESSION_NOT_FOUND` | SSH session not found |
| `AUTH_ERROR` | Authentication or permission failure |
| `POLICY_VIOLATION` | Command or path blocked by policy |
| `RATE_LIMITED` | Rate limit exceeded |
| `TIMEOUT` | Operation timed out |
| `DEPENDENCY_MISSING` | Required service/dependency unavailable |
| `INVALID_INPUT` | Invalid parameters |
| `INTERNAL_ERROR` | Unexpected internal failure |

## Migration Strategy

### Phase 1 (current session)
- Define ADR and `tool_results.py` helper.
- Add `normalize_tool_result()` for wrapping existing arbitrary returns.
- Zero migration of existing tools.

### Phase 2 (in progress, tool-by-tool)
- Migrate `docker_*` tools to use `tool_results.py` envelope.
  - **Batch 1 complete (Session 167)**: `docker_confirm` — `tool` now reports underlying tool, flat `result`, `exit_code != 0` uses `tool_error`.
  - **Batch 2 complete (Session 167)**: Envelope test coverage for all 7 remaining dangerous/admin tools: `docker_rm`, `docker_compose_down`, `docker_prune`, `docker_exec`, `docker_run`, `docker_rmi`, `docker_volume_rm`. All confirmed canonical — no raw `str`, no flat dict without `ok/tool/result/error/meta`. 25 new tests covering confirmation_required + all validation/scope/denylist/allowlist error paths.
  - `_confirmation_response()` was already canonical (`tool_success` with `confirmation_required` status).
  - `docker_pending_actions()` was already canonical.
- Migrate `postgres_*` tools to use `tool_results.py` envelope.
- Migrate `Context7` tools to use `tool_results.py` envelope.
- Keep `text_result()`/`error_result()` working for backward compatibility.

### Phase 3 (future)
- Deprecate `text_result()`/`error_result()` in favour of the canonical envelope.
- Remove or redirect old helpers after all tools are migrated.

## Non-goals

- This ADR does NOT mandate immediate migration of all 97 tools.
- This ADR does NOT define a wire-level schema for the MCP transport (that is the MCP protocol's job).
- This ADR does NOT define a client SDK — clients should consume `ok`, `result`, `error` from the envelope.
- This ADR does NOT change the error reporting from `GatewayClient`, `PostgresClient`, or `DockerClient` — those remain as-is. The envelope wraps their output.

## Consequences

- New tools should use `tool_success()` / `tool_error()` from `tool_results.py`.
- Existing `text_result()` / `error_result()` helpers remain usable and unchanged.
- MCP clients that rely on `isError` from `error_result()` will continue to work — the envelope is a superset.
- The `result` field can hold any valid JSON type, preserving full flexibility for tool-specific schemas.
- Error handling becomes predictable: check `ok`, then read `result` or `error.message`.
- Observability improves: `meta.duration_ms` and `meta.source` are available for all migrated tools.
