# C2.1 Implementation Plan — Preview + Verify (read-only, safe)

> **Base:** `master @ c776198`
> **Status:** Planning — Agents 2-4 execute in parallel
> **Out of scope:** Rollback REST/MCP, release/tag, deploy

---

## 1. Contract Overview

Add **read-only** preview and verify operations to the workspace control plane. No disk mutation, no rollback wiring, no release.

```
preview (write | edit | patch) → diff + hashes + metadata (no write)
verify  (path + expected_hash) → matches: bool + current_hash (no write)
```

### 1.1 Preview — `project_file_preview_*`

Three functions in `app/workspace/preview.py` (already implemented by Agent 2):

| Function | Signature | Returns |
|----------|-----------|---------|
| `project_file_preview_write` | `(project_id, path, content, ...)` | `{before_hash, after_hash, diff, changed, file_exists_before, size_before, size_after, encoding}` |
| `project_file_preview_edit` | `(project_id, path, old_string, new_string, ...)` | `{before_hash, after_hash, diff, changed, replaced, size_before, size_after, encoding}` |
| `project_file_preview_patch` | `(project_id, path, patch, ...)` | `{before_hash, after_hash, diff, changed, applied, size_before, size_after, encoding}` |

**Rules (all three):**
- Validate path through `WorkspacePolicy.validate_read` (project:read scope).
- Run `_symlink_safe_preflight` — reject intermediate symlinks.
- Read current file if exists; compute `before_hash`, `size_before`.
- Apply operation **in memory only** — no disk write, no file creation.
- Compute `after_hash` from predicted content.
- Return unified diff via `_make_diff`.
- Never create directories, never snapshot, never write.

**Error mapping:**

| Condition | Exception | HTTP |
|-----------|-----------|------|
| Traversal | `TraversalError` | 400 |
| Symlink in path | `SymlinkEscapeError` | 400 |
| Hidden/secret path | `HiddenPathError` | 403 |
| File not found (edit/patch) | `WorkspacePolicyError` | 404 |
| old_string not found | `WorkspacePolicyError` | 400 |
| Binary content (write) | `WorkspacePolicyError` | 400 |
| Content too large | `WorkspacePolicyError` | 413 |
| No hunks in patch | `PatchError` | 400 |
| Scope denied | `ScopeDeniedError` | 403 |

### 1.2 Verify — `project_file_verify`

Already implemented in `app/workspace/preview.py`:

```python
def project_file_verify(
    project_id, relative_path, expected_hash, registry=None
) -> dict:
```

Returns `{project_id, path, matches: bool, current_hash: str | None, file_exists: bool}`.

- Reads raw bytes from disk, computes `sha256:<hex>`.
- Compares against `expected_hash`.
- If file does not exist: `matches=False, current_hash=None, file_exists=False`.
- If read error (permission, I/O): `matches=False, current_hash=None, file_exists=True`.
- Uses `validate_read` scope — does NOT require `project:write`.

### 1.3 No Content Leak by Default

Preview returns **diff** (unified diff lines) plus metadata. The full file content is NOT returned unless the caller already supplied it (write content, edit old/new strings). This matches C1 behavior: the caller already knows what they wrote.

Verify returns only `matches: bool` and `current_hash` — no file content.

---

## 2. REST Surface

### 2.1 Endpoints

All under `/api/workspace/projects/{project_id}/files/`:

| Method | Path | Scope | Core function |
|--------|------|-------|---------------|
| POST | `/preview/write` | `project:read` | `project_file_preview_write` |
| POST | `/preview/edit` | `project:read` | `project_file_preview_edit` |
| POST | `/preview/patch` | `project:read` | `project_file_preview_patch` |
| POST | `/verify` | `project:read` | `project_file_verify` |

**Scope rationale:** Preview/verify are read-only operations — they inspect file content without mutation. `project:read` is the correct scope. The scope hierarchy (`project:write` implies `project:read`) ensures write-capable callers can also preview.

### 2.2 Request/Response Schemas

#### POST `/preview/write`

```json
// Request
{
  "path": "src/main.py",
  "content": "print('hello')"
}

// Response 200
{
  "project_id": "my-project",
  "path": "src/main.py",
  "file_exists_before": true,
  "before_hash": "sha256:abc...",
  "after_hash": "sha256:def...",
  "size_before": 1024,
  "size_after": 2048,
  "diff": "@@ -1 +1 @@\n-old content\n+new content",
  "changed": true,
  "encoding": "utf-8"
}
```

#### POST `/preview/edit`

```json
// Request
{
  "path": "src/main.py",
  "old_string": "print('hello')",
  "new_string": "print('world')"
}

// Response 200
{
  "project_id": "my-project",
  "path": "src/main.py",
  "file_exists_before": true,
  "before_hash": "sha256:abc...",
  "after_hash": "sha256:def...",
  "size_before": 1024,
  "size_after": 1024,
  "diff": "@@ -1 +1 @@\n-print('hello')\n+print('world')",
  "changed": true,
  "replaced": true,
  "encoding": "utf-8"
}
```

#### POST `/preview/patch`

```json
// Request
{
  "path": "src/main.py",
  "patch": "@@ -1 +1 @@\n-print('hello')\n+print('world')"
}

// Response 200
{
  "project_id": "my-project",
  "path": "src/main.py",
  "file_exists_before": true,
  "before_hash": "sha256:abc...",
  "after_hash": "sha256:def...",
  "size_before": 1024,
  "size_after": 1024,
  "diff": "@@ -1 +1 @@\n-print('hello')\n+print('world')",
  "changed": true,
  "applied": true,
  "encoding": "utf-8"
}
```

#### POST `/verify`

```json
// Request
{
  "path": "src/main.py",
  "expected_hash": "sha256:def456..."
}

// Response 200
{
  "project_id": "my-project",
  "path": "src/main.py",
  "matches": true,
  "current_hash": "sha256:def456...",
  "file_exists": true
}
```

### 2.3 Auth Wiring

All four endpoints use `require_scope("project:read")`.

If `require_scope("project:read")` does not yet exist in `app/auth_middleware.py` as a standalone dependency, use `require_scope("project:read")` — the existing `require_scope` function already accepts any scope string. Only add to `ALL_SCOPES` if missing.

### 2.4 Error Mapping

Reuse `_map_workspace_error` from `app/routers/workspace.py` — it already handles all error types that preview/verify can raise (`WorkspacePolicyError`, `SymlinkEscapeError`, `TraversalError`, `HiddenPathError`, `ScopeDeniedError`, `PatchError`, `ValueError`).

Add `"not found"` detection for preview edit/patch when file is missing (currently returns 400 via `WorkspacePolicyError`; should return 404 if file not found). This requires either a new error class or explicit `if "not found" in lower` check — the existing mapper already has this at line 101-105.

---

## 3. MCP Surface

**Out of scope for C2.1.** MCP tool registration (`workspace_file_preview`, `workspace_file_verify`) is deferred to a follow-up session after REST wiring is stable and tested.

---

## 4. SDK Surface

**Out of scope for C2.1.** SDK methods (`gateway.workspace_file_preview`, `gateway.workspace_file_verify`) are deferred until MCP tools are wired.

---

## 5. CI / Discoverability

### 5.1 `/api/help` documentation

Add help entries for the four new endpoints in `app/api_help.py`:

```
preview/write — Preview a file write without writing to disk (project:read)
preview/edit — Preview a file edit without writing to disk (project:read)
preview/patch — Preview a patch application without writing to disk (project:read)
verify       — Verify a file's SHA-256 hash matches an expected value (project:read)
```

Explicitly state: **"Rollback is not exposed via REST or MCP in C2.1."**

### 5.2 CI upload-artifact fix

The Gitea CI pipeline fails on the "Upload test results" step due to missing JUnit XML. Fix options (choose one):

1. **Conditional upload:** only upload when `junit.xml` exists:
   ```yaml
   - name: Upload test results
     if: always() && hashFiles('junit.xml') != ''
     uses: actions/upload-artifact@v4
   ```
2. **Disable upload:** remove the step entirely — tests already pass/fail visibly in the step log.
3. **Generate JUnit XML:** add `--junit-xml=junit.xml` to pytest invocation.

**Recommendation:** Option 1 (conditional upload) — preserves artifact capability without breaking the run on missing file. Tests already pass/fail in step 5; the upload step is cosmetic.

---

## 6. Tasks by Agent

### Agent 2 (core helpers) — ✅ DONE
- `app/workspace/preview.py` — all three preview functions + verify
- `app/workspace/__init__.py` — exports updated
- `tests/test_workspace_preview.py` — unit tests
- **Status:** Already implemented and merged in `c776198`

### Agent 3 (REST wiring) — TODO
- `app/routers/workspace.py` — add 4 endpoints, reuse `_map_workspace_error`
- `tests/test_workspace_rest.py` — integration tests with test registry
- Verify scope: `require_scope("project:read")`

### Agent 4 (CI + discoverability) — TODO
- `app/api_help.py` — add preview/verify help entries, note rollback not exposed
- `.github/workflows/ci.yml` — fix upload-artifact conditional
- `Makefile` — optional: add `test-preview` target
- Verify: `ruff check app tests`, `mypy app`

---

## 7. Open Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| `project:read` scope not yet wired for agent tokens | 401 for non-master callers | Add to `ALL_SCOPES` + `SCOPE_IMPLIES` in policy if missing. Document in agent 3 report. |
| Preview edit/patch returns 400 instead of 404 for missing file | Confusing error for callers | Add explicit "file not found" → 404 in error mapper or add `FileNotFoundError` class. |
| No MCP wiring means agents can't use preview/verify from chat | Reduced utility in current UX | Accept as C2.1 scope limit; MCP is follow-up. |
| Upload-artifact fix not tested in Gitea CI before merge | CI may still fail | Test in branch first; if Gitea Actions runner doesn't have `hashFiles`, use `if: always()` with file check. |

---

## 8. Merge Criteria

1. ✅ Core helpers implemented + tested + merged (Agent 2 — DONE)
2. REST endpoints wired + passing tests with `project:read` scope
3. `ruff check .` — all passed
4. `python3 -m mypy .` — no new errors
5. `pytest -m "not host_smoke"` — all preview/verify tests pass
6. `pytest tests/test_workspace_rest.py` — preview/verify REST tests pass
7. CI `upload-artifact` no longer breaks on missing JUnit XML
8. Help docs updated with rollback-not-exposed note
9. **No release/tag, no deploy, no rollback wiring.**
