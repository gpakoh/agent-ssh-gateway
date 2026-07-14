# Workspace Control Plane - Phase B Design

**Date:** 2026-07-14
**Status:** Draft
**Scope:** Phase B only - read-only IDE/navigation tools

## Overview

Phase B turns the Phase A workspace registry into a safe read-only IDE surface for agents. It adds file inspection, file discovery, text search, and git inspection for projects under `/media/1TB/Python`, always scoped by `project_id` and guarded by `WorkspacePolicy`.

Phase B is still read-only. It does not add file write/edit, command execution, test runners, docker operations, service restarts, database operations, or arbitrary shell access.

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Filesystem model | Local only | Workspace root is the gateway host filesystem, not Paramiko/SSH |
| Project selection | Per-request `project_id` | Explicit boundary, audit-friendly |
| Security boundary | `WorkspacePolicy` | All path inputs must pass operation-specific validators |
| Tool style | Sync library functions | Local filesystem/git reads do not need async |
| Callable surface | Thin REST/MCP wrappers after library tests | Wrappers map params/errors only; no business logic |
| Search model | Literal text search by default | Safer and easier to bound than arbitrary regex |
| Git model | Fixed read-only argv, no shell | Allows `status/diff/log/branch` without arbitrary execution |

## Invariants

1. `projects.yaml` remains the single source of truth.
2. All project operations require explicit `project_id`.
3. All user-provided paths are project-relative.
4. Absolute paths, `~`, `~user`, and `..` are rejected before filesystem access.
5. Symlinks are resolved and must remain inside the selected project root.
6. Explicit reads of hidden/secret paths raise `HiddenPathError`.
7. Search/find/tree silently omit hidden/secret/vendor/cache paths.
8. Binary files are never returned as content.
9. All limits must fail closed with `truncated=True` or a typed policy error.
10. No shell command accepts user-controlled argv fragments except validated relative paths after `--`.

## Phase B Tools

### File inspection

```python
def project_file_read(
    project_id: str,
    relative_path: str,
    start_line: int | None = None,
    max_lines: int | None = None,
    max_bytes: int = 200_000,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Read a UTF-8/text file inside a project."""
```

Return contract:

```python
{
    "project_id": "web-ssh-gateway",
    "path": "app/main.py",
    "type": "file",
    "size": 12345,
    "encoding": "utf-8",
    "content": "...",
    "start_line": 1,
    "end_line": 120,
    "truncated": False,
}
```

Rules:

- Calls `WorkspacePolicy.validate_read(project_id, relative_path)`.
- Rejects directories.
- Rejects hidden/secret paths via policy.
- Detects binary files and raises `WorkspacePolicyError` without returning bytes.
- Applies `max_bytes` before decoding.
- Supports optional line slicing after byte limit is applied.

### File discovery

```python
def project_find_files(
    project_id: str,
    pattern: str = "*",
    relative_path: str = "",
    max_results: int = 500,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Find files/directories by glob-like pattern inside a project."""
```

Return contract:

```python
{
    "project_id": "web-ssh-gateway",
    "root": "",
    "pattern": "*.py",
    "results": [
        {"path": "app/main.py", "type": "file", "size": 12345}
    ],
    "truncated": False,
}
```

Rules:

- `relative_path=""` maps to `"."`.
- Search root is validated with `validate_read`.
- Pattern is a filename/glob filter, not a shell command.
- Hidden/secret/vendor/cache paths are skipped.
- Symlink escapes are skipped or raise a policy error; they are never followed outside root.
- `max_results` hard cap is enforced.

### Text search

```python
def project_search_text(
    project_id: str,
    query: str,
    relative_path: str = "",
    file_glob: str = "**/*",
    case_sensitive: bool = False,
    context_lines: int = 2,
    max_matches: int = 100,
    max_bytes_per_file: int = 1_000_000,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Search text content inside project files."""
```

Return contract:

```python
{
    "project_id": "web-ssh-gateway",
    "query": "WorkspacePolicy",
    "matches": [
        {
            "path": "app/workspace/policy.py",
            "line": 100,
            "column": 7,
            "preview": "class WorkspacePolicy:",
            "before": ["..."],
            "after": ["..."]
        }
    ],
    "truncated": False,
}
```

Rules:

- Query is literal text in Phase B; regex is out of scope.
- Empty query is rejected.
- Search root is validated with `validate_read` or `validate_search`.
- Hidden/secret/vendor/cache paths are skipped.
- Binary files are skipped.
- Files larger than `max_bytes_per_file` are skipped.
- `max_matches` hard cap is enforced.

### Git read-only inspection

```python
def project_git_status(project_id: str, registry: WorkspaceRegistry | None = None) -> dict[str, Any]: ...
def project_git_branch(project_id: str, registry: WorkspaceRegistry | None = None) -> dict[str, Any]: ...
def project_git_log(project_id: str, limit: int = 20, relative_path: str | None = None, registry: WorkspaceRegistry | None = None) -> dict[str, Any]: ...
def project_git_diff(project_id: str, relative_path: str | None = None, staged: bool = False, max_bytes: int = 200_000, registry: WorkspaceRegistry | None = None) -> dict[str, Any]: ...
```

Git rules:

- Only fixed commands are allowed: `status --porcelain=v1 --branch`, `branch --show-current`, `log`, `diff`.
- No shell. Use `subprocess.run(argv, cwd=project.root, shell=False, timeout=10, env=safe_env)`.
- `GIT_TERMINAL_PROMPT=0` must be set.
- Network-capable git commands (`fetch`, `pull`, `push`, `remote`, `submodule update`) are out of scope.
- Optional `relative_path` is validated, then appended after `--`.
- Non-git projects return `{"is_git_repo": False}` instead of failing.
- Diff/log output is capped and returns `truncated=True` when clipped.

## Public API Layout

Phase B may add modules inside `app/workspace/`:

```text
app/workspace/files.py      # file read/find helpers
app/workspace/search.py     # text search helpers
app/workspace/git.py        # read-only git helpers
app/workspace/tools.py      # public tool functions re-exporting Phase A+B tools
```

Backward-compatible shims remain in place:

```python
import app.workspace_policy
import app.workspace_registry
```

Phase B should not add a second registry, second policy layer, or Paramiko-based local project operations.

## REST/MCP Wiring

REST/MCP wrappers are in scope only as thin wrappers over `app.workspace.tools`.

### REST endpoints

| Endpoint | Method | Scope | Tool call |
|----------|--------|-------|-----------|
| `/api/workspace/projects` | GET | `workspace:read` | `workspace_list_projects()` |
| `/api/workspace/projects/{project_id}` | GET | `workspace:read` | `project_info(project_id)` |
| `/api/workspace/projects/{project_id}/tree` | GET | `project:read` | `project_tree(project_id, relative_path, depth, max_nodes)` |
| `/api/workspace/projects/{project_id}/files/read` | GET | `project:read` | `project_file_read(project_id, relative_path, ...)` |
| `/api/workspace/projects/{project_id}/files/find` | GET | `project:read` | `project_find_files(project_id, pattern, relative_path, max_results)` |
| `/api/workspace/projects/{project_id}/search` | GET | `project:read` | `project_search_text(project_id, query, ...)` |
| `/api/workspace/projects/{project_id}/git/status` | GET | `project:read` | `project_git_status(project_id)` |
| `/api/workspace/projects/{project_id}/git/diff` | GET | `project:read` | `project_git_diff(project_id, relative_path, staged, max_bytes)` |
| `/api/workspace/projects/{project_id}/git/log` | GET | `project:read` | `project_git_log(project_id, limit, relative_path)` |

REST may accept `path` as a query alias, but internal code uses `relative_path`.

### MCP tools

MCP tool names use a workspace prefix:

- `workspace_list_projects`
- `workspace_project_info`
- `workspace_project_tree`
- `workspace_file_read`
- `workspace_find_files`
- `workspace_search_text`
- `workspace_git_status`
- `workspace_git_diff`
- `workspace_git_log`
- `workspace_git_branch`

Wrappers do parameter mapping and error mapping only. They must not reimplement validation.

## Error Mapping

| Exception | HTTP | MCP | Message |
|-----------|------|-----|---------|
| Unknown project | 404 | `NOT_FOUND` | `Project not found: {id}` |
| TraversalError | 400 | `INVALID_ARGUMENT` | `Path traversal rejected` |
| SymlinkEscapeError | 400 | `INVALID_ARGUMENT` | `Symlink escape detected` |
| HiddenPathError | 403 | `PERMISSION_DENIED` | `Hidden or secret path denied` |
| ScopeDeniedError | 403 | `PERMISSION_DENIED` | `Scope required: {scope}` |
| Binary file read | 415 | `FAILED_PRECONDITION` | `Binary content is not returned` |
| Limit exceeded | 200 | OK | Return partial result with `truncated=True` |
| WorkspacePolicyError | 400 | `INVALID_ARGUMENT` | Generic fallback |

## Tests

### Unit tests

- `test_file_read_valid_text`
- `test_file_read_line_slice`
- `test_file_read_hidden_secret_rejected`
- `test_file_read_absolute_tilde_traversal_rejected`
- `test_file_read_binary_rejected`
- `test_find_files_skips_secrets_and_ignores`
- `test_find_files_truncated`
- `test_search_text_literal_match`
- `test_search_text_context_lines`
- `test_search_text_skips_binary_and_secrets`
- `test_search_text_truncated`
- `test_git_status_clean_dirty_repo`
- `test_git_non_repo_returns_false`
- `test_git_diff_path_validated`
- `test_git_log_limit_capped`
- `test_shims_still_import`

Unit tests use temp projects and temp git repos. They must not depend on `/media/1TB/Python`.

### Real smoke

- `WorkspaceRegistry.load("projects.yaml")` loads all 7 projects.
- For each project:
  - `project_tree(project_id, depth=1)` succeeds.
  - `project_find_files(project_id, pattern="*", max_results=10)` succeeds.
  - `project_git_status(project_id)` succeeds or returns `is_git_repo=False`.
- For `web-ssh-gateway`:
  - `project_file_read("web-ssh-gateway", "pyproject.toml")` succeeds.
  - `project_search_text("web-ssh-gateway", "WorkspacePolicy")` returns at least one match.

## Acceptance Criteria

- [ ] Phase B tools are exported from `app.workspace.tools`.
- [ ] All Phase A tools still work unchanged.
- [ ] `projects.yaml` real smoke passes for all 7 projects.
- [ ] Explicit hidden/secret file reads are rejected.
- [ ] Search/find do not return hidden/secret files.
- [ ] Git tools use fixed argv, no shell, and safe env.
- [ ] REST/MCP wrappers, if implemented in this phase, are thin wrappers only.
- [ ] `ruff check .` clean.
- [ ] `python3 -m mypy .` clean.
- [ ] `pytest -q` green.
- [ ] Docker build/import smoke passes for `app.workspace.files`, `app.workspace.search`, `app.workspace.git`, and `app.workspace.tools`.
