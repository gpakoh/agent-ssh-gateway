# Workspace Control Plane — Phase C1 Write Tools

**Date:** 2026-07-14
**Status:** Draft
**Scope:** Phase C1 only — project_file_write, project_file_edit, project_apply_patch

## Overview

Phase C1 adds write capability to the workspace control plane: create and modify files inside registered projects, always scoped by `project_id` and guarded by `WorkspacePolicy.validate_write`. Writes are local-filesystem operations (no SSH session required).

Phase C1 does not add: command execution, test runners, docker operations, service restarts, database operations, or arbitrary shell access.

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Write atomicity | Write to temp path then `os.replace()` | Prevents partial/corrupt writes on crash or interrupt |
| Edit model | Search/replace (literal old_string) | Safer than line-number-based edits that drift on concurrent writes; matches LLM agent workflow |
| Patch model | Unified diff (`git diff`-style) | Industry standard; LLM agents diff output is directly applicable |
| Diff reporting | Returned in mutation response | Lets the caller audit what changed after a successful write/edit/patch |
| Binary rejection | Write/edit/patch all require UTF-8 content | Binary mutation is outside scope; existing `project_file_read` already rejects binary |
| Max size | Per-tool constant at module level | Consistent with Phase B pattern (`files.py`, `search.py`, `git.py`) |
| Rollback | Atomic replace only; `backup_hash` is returned for audit | No backup files in Phase C1; original remains intact if validation/temp-write/replace fails before replacement |
| Secret guard | `validate_write` raises `HiddenPathError` | No separate write-time filter needed; `validate_write` calls `_is_hidden_or_secret` |

## Invariants

1. All write operations require `project:write` scope.
2. All paths are project-relative.
3. `validate_write` is called before any filesystem mutation.
4. Writes to hidden/secret paths are rejected via `validate_write`.
5. Binary content (by null-byte detection) is rejected with a typed error.
6. Atomic writes use `<target>.tmp` then `os.replace()` (`os.rename()` on Linux).
7. Existing files are not copied into backup files in Phase C1; `backup_hash` is audit metadata, not a rollback artifact.
8. `max_bytes` is a soft cap — content exceeding it is rejected before any write.
9. Parent directories must exist; write tools do not auto-create directories.
10. Symlinks are never followed or created. If any existing component in the target path is a symlink, the write is rejected.
11. Write/edit/patch never mutate truncated reads. Existing content must be read completely within `max_bytes` or rejected before mutation.

## Phase C1 Tools

### 1. project_file_write

Create a new file or overwrite an existing file with full content.

```python
def project_file_write(
    project_id: str,
    relative_path: str,
    content: str,
    max_bytes: int = 1_000_000,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Write (create or overwrite) a UTF-8 text file inside a project."""
```

Return contract:

```python
{
    "project_id": "web-ssh-gateway",
    "path": "app/example.py",
    "size": 1234,
    "encoding": "utf-8",
}
```

Rules:

- Calls `WorkspacePolicy.validate_write(project_id, relative_path)`.
- Rejects if target path or any existing parent component is a symlink.
- Raises `WorkspacePolicyError` if `relative_path` resolves to an existing directory.
- Encodes `content` to UTF-8 bytes; rejects content that exceeds `max_bytes` after encoding.
- Detects binary content (null byte) and raises `WorkspacePolicyError` without writing.
- Performs atomic write: writes to `{path}.tmp`, then `os.replace()` (atomic on Linux).
- If `{path}.tmp` already exists, it is overwritten.
- If the target file already exists, it is overwritten without backup.
- If the parent directory does not exist, raises `WorkspacePolicyError`.
- If the write itself fails (disk full, permission), implementation must attempt best-effort `.tmp` cleanup and raise a typed error.

### 2. project_file_edit

Search and replace within an existing text file.

```python
def project_file_edit(
    project_id: str,
    relative_path: str,
    old_string: str,
    new_string: str,
    max_bytes: int = 1_000_000,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Edit a file by replacing the first occurrence of old_string with new_string."""
```

Return contract:

```python
{
    "project_id": "web-ssh-gateway",
    "path": "app/example.py",
    "size": 1234,
    "encoding": "utf-8",
    "old_string": "def foo():",
    "new_string": "def bar():",
    "diff": "@@ -1,3 +1,3 @@\n-def foo():\n+def bar():",
    "replaced": True,
}
```

Rules:

- Calls `WorkspacePolicy.validate_write(project_id, relative_path)`.
- Reads the existing file through an internal exact-read helper that shares `project_file_read` safety checks but rejects files larger than `max_bytes` instead of editing a truncated prefix.
- Rejects if target path or any existing parent component is a symlink.
- Raises `WorkspacePolicyError` if `old_string` is empty.
- Raises `WorkspacePolicyError` if `old_string` is not found in the file content.
- Raises `WorkspacePolicyError` if the replaced content exceeds `max_bytes` after encoding.
- Replaces only the **first** occurrence of `old_string` in the file content.
- Computes a unified diff of the change (via `difflib.unified_diff`) for the `diff` field.
- Writes the modified content atomically (same temp + rename pattern as `project_file_write`).
- Returns `{"replaced": True}` on success.
- If `old_string` is found but the file would not change (old_string == new_string), returns `{"replaced": False}` with the same diff preview. This is not an error.

### 3. project_apply_patch

Apply a unified diff (patch) to a file inside a project.

```python
def project_apply_patch(
    project_id: str,
    relative_path: str,
    patch: str,
    max_bytes: int = 1_000_000,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Apply a unified diff patch to a file."""
```

Return contract:

```python
{
    "project_id": "web-ssh-gateway",
    "path": "app/example.py",
    "size": 1234,
    "encoding": "utf-8",
    "patch": "@@ -1,3 +1,3 @@\n ...",
    "applied": True,
    "backup_hash": "sha256:abc123...",
}
```

Rules:

- Calls `WorkspacePolicy.validate_write(project_id, relative_path)`.
- Reads current file content through the same exact-read helper used by `project_file_edit`.
- Rejects if target path or any existing parent component is a symlink.
- Parses the patch as a unified diff. Use `unidiff` if helpful; otherwise use a small local parser for one-file unified diffs.
- Validates that the patch applies cleanly (no fuzz, no hunk failures).
- If the patch parser reports conflicts, raises `WorkspacePolicyError` with a description of the hunk(s) that failed.
- Before writing, computes a SHA-256 hash of the current file content (`backup_hash`) for audit and caller-side verification.
- Writes the patched content atomically.
- Returns the patch text in the response (echoed from input).
- If the file size after patching exceeds `max_bytes`, raises `WorkspacePolicyError` without writing.
- Non-file patches (new file creation, deletion, binary diffs) are out of scope for Phase C1.
- Patch must target exactly one file (the `relative_path` target).
- Hunk-level validation: check context lines match; if any context line differs, raise with the hunk index.

## Error Mapping (Phase C1 additions)

| Exception | HTTP | MCP | Message |
|-----------|------|-----|---------|
| Write to existing directory | 400 | `INVALID_ARGUMENT` | `Path is a directory, not a file: {path}` |
| Content exceeds max_bytes | 413 | `OUT_OF_RANGE` | `Content exceeds maximum size of {bytes} bytes` |
| Binary content rejected | 415 | `FAILED_PRECONDITION` | `Binary content is not allowed` |
| Empty old_string (edit) | 400 | `INVALID_ARGUMENT` | `old_string must not be empty` |
| old_string not found (edit) | 404 | `NOT_FOUND` | `old_string not found in file content` |
| Patch rejected (conflicts) | 409 | `FAILED_PRECONDITION` | `Patch does not apply cleanly at hunk {n}` |
| Parent directory missing | 400 | `FAILED_PRECONDITION` | `Parent directory does not exist: {path}` |
| Write fails (disk/permission) | 500 | `INTERNAL` | `Write failed: {os error}` |

Existing `WorkspacePolicy` errors propagate unchanged:
| Exception | HTTP | MCP | Message |
|-----------|------|-----|---------|
| Unknown project | 404 | `NOT_FOUND` | `Project not found: {id}` |
| TraversalError | 400 | `INVALID_ARGUMENT` | `Path traversal rejected` |
| SymlinkEscapeError | 400 | `INVALID_ARGUMENT` | `Symlink escape detected` |
| HiddenPathError | 403 | `PERMISSION_DENIED` | `Write to hidden/secret path denied` |
| ScopeDeniedError | 403 | `PERMISSION_DENIED` | `Scope required: {scope}` |

## Implementation Notes

### Symlink-safe write preflight

`validate_write()` remains mandatory, but C1 must add stricter write preflight:

1. Resolve the project root.
2. Walk every component from `project.root` to the target parent.
3. If any existing component is a symlink, reject before creating temp files.
4. If the target exists and is a symlink, reject.
5. Re-run this preflight immediately before opening the temp file.

This closes the write-specific TOCTOU/symlink class that is stricter than Phase B read behavior.

### Atomic write pattern

```python
tmp_path = resolved_path.with_suffix(resolved_path.suffix + ".tmp")
try:
    if not resolved_path.parent.exists():
        raise WorkspacePolicyError("Parent directory does not exist")
    if resolved_path.parent.is_symlink() or resolved_path.is_symlink():
        raise SymlinkEscapeError("Symlink writes are not allowed")
    tmp_path.write_bytes(utf8_bytes)
    os.replace(tmp_path, resolved_path)  # atomic on same filesystem
except Exception:
    if tmp_path.exists():
        tmp_path.unlink(missing_ok=True)
    raise
```

### Diff computation (edit tool)

Use `difflib.unified_diff` on `old_content.splitlines()`, `new_content.splitlines()` with `n=3` context lines. Include a file header line even for single-file edits (use the relative path as the label).

### Patch validation

For `project_apply_patch`:
1. Parse the patch string — reject if it is not valid unified diff format.
2. Check that the patch references exactly one file (the `relative_path`).
3. Attempt to apply each hunk in order; verify context lines match exactly.
4. If any hunk fails, report the hunk index and the expected vs actual context.

### Module layout (addition to Phase B)

```text
app/workspace/
├── edit.py          # project_file_write, project_file_edit, project_apply_patch
├── files.py         # existing read/find
├── git.py           # existing git
├── search.py        # existing search
├── tools.py         # existing + re-export new write tools
├── ...
```

The edit module (`app/workspace/edit.py`) contains all three write tools. `tools.py` re-exports them alongside existing read tools.

## Tests

### unit tests (temp fixtures)

- `test_file_write_creates_new_file`
- `test_file_write_overwrites_existing`
- `test_file_write_atomicity` — failed temp write/replace does not corrupt target
- `test_file_write_content_exceeds_max_bytes`
- `test_file_write_binary_rejected`
- `test_file_write_to_directory_rejected`
- `test_file_write_parent_dir_missing`
- `test_file_write_hidden_path_rejected`
- `test_file_write_traversal_rejected`
- `test_file_write_scope_denied`
- `test_file_write_symlink_target_rejected`
- `test_file_write_symlink_parent_rejected`

- `test_file_edit_replaces_first_occurrence`
- `test_file_edit_empty_old_string`
- `test_file_edit_old_string_not_found`
- `test_file_edit_no_change` — old_string == new_string
- `test_file_edit_content_exceeds_max_bytes`
- `test_file_edit_large_source_rejected_before_partial_edit`
- `test_file_edit_hidden_path_rejected`
- `test_file_edit_scope_denied`
- `test_file_edit_diff_contains_change`

- `test_apply_patch_clean_apply`
- `test_apply_patch_invalid_patch_format`
- `test_apply_patch_hunk_conflict`
- `test_apply_patch_no_file_target`
- `test_apply_patch_content_exceeds_max_bytes`
- `test_apply_patch_hidden_path_rejected`
- `test_apply_patch_scope_denied`
- `test_apply_patch_atomic_failure_preserves_original` — simulated write failure leaves original content intact
- `test_apply_patch_backup_hash_matches` — returned hash equals pre-patch content hash
- `test_apply_patch_symlink_target_rejected`

### real smoke

- `project_file_write("web-ssh-gateway", "test_write_cleanup.txt", "hello")` succeeds
- `project_file_read("web-ssh-gateway", "test_write_cleanup.txt")` returns content
- `project_file_edit("web-ssh-gateway", "test_write_cleanup.txt", "hello", "world")` succeeds
- `project_apply_patch("web-ssh-gateway", ...)` succeeds with valid diff
- Cleanup: delete temporary test files after smoke

## Acceptance Criteria

- [ ] All three tools are exported from `app.workspace.tools`.
- [ ] All Phase A + Phase B tools still work unchanged.
- [ ] `validate_write` is called before every write operation.
- [ ] Atomic write pattern prevents partial file corruption.
- [ ] Binary content raises a typed error (never written to disk).
- [ ] Hidden/secret paths raise `HiddenPathError` from `validate_write`.
- [ ] Apply_patch validates patch format and hunk context before writing.
- [ ] Apply_patch returns `backup_hash` for audit/caller-side verification.
- [ ] Edit tool returns a `diff` preview alongside the result.
- [ ] `ruff check .` clean.
- [ ] `python3 -m mypy .` clean.
- [ ] `pytest -q` green.
- [ ] Docker build/import smoke passes for `app.workspace.edit`.
