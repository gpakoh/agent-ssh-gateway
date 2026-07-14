# Workspace Control Plane — Phase A Design

**Date:** 2026-07-13
**Status:** Approved
**Scope:** Phase A only — registry/info foundation

## Overview

Transform web-ssh-gateway from a single-project SSH gateway into a multi-project Workspace Control Plane. Phase A delivers the registry and info layer: project listing, metadata, and directory tree browsing with security boundaries.

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Project location | Module inside web-ssh-gateway (`app/workspace/`) | Leverage existing auth/Docker/MCP; clean boundaries for future extraction |
| Registry format | `projects.yaml` (existing file, extended) | Don't break working contract; mapping by project_id already tested |
| Project selection | Per-request `project_id` | Explicit boundary, safer, easier to audit |
| Architecture pattern | Thin Wrapper (library layer) | Minimal complexity for 3 tools; easy to extract later |
| Secrets filtering | Built-in patterns + YAML override in WorkspacePolicy | Security boundary separate from tree-builder |

## Invariants

1. `projects.yaml` — single source of truth for project registry
2. `projects` — mapping by `project_id` (dict, not list)
3. `root` in projects.yaml — relative to `registry_root`
4. `WorkspacePolicy` — security boundary. Tree-builder handles UI ignores, not safety.
5. `TreeNode` contract: name, path, type, size, children, truncated — no changes without separate approval
6. Phase A is read-only: list, info, tree. No file content, no search, no git, no docker, no write/edit
7. Tools are sync `def` — no async without real I/O
8. Router/MCP wiring is future work — Phase A deliverable is package + tools + tests + shims

## Data Model

### projects.yaml

```yaml
version: 1
registry_root: /media/1TB/Python

default_ignores:
  - __pycache__
  - .git
  - node_modules
  - .venv
  - "*.pyc"

projects:
  web-ssh-gateway:
    root: web_ssh/web-ssh-gateway
    type: fastapi
    description: "API-first SSH gateway for agents"
    tags: [gateway, ssh, mcp]
    compose: docker/docker-compose.yml

  nod-gateway:
    root: NOD_gateway
    type: python
    description: "NOD IoT gateway platform"
    tags: [iot, gateway]

  quart-platform:
    root: quart-platform
    type: python
    description: "Quart async web platform"
    tags: [web, quart]

  scraper:
    root: scraper
    type: python
    description: "PriceTuner scraper"
    tags: [scraper, pricetuner]
    compose: docker-compose.yml

  tg-audio-bot:
    root: tg_audio_bot
    type: python
    description: "Telegram audio bot"
    tags: [telegram, bot]
    compose: docker/docker-compose.yml

  flash-attention:
    root: flash-attention
    type: upstream
    description: "Dao-AILab flash-attention (upstream fork)"
    tags: [ml, cuda]
```

### Models (`app/workspace/models.py`)

```python
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Project:
    project_id: str
    root: Path            # absolute, resolved from registry_root + relative
    type: str             # fastapi | python | upstream
    description: str
    tags: list[str]
    compose: str | None = None

@dataclass(frozen=True)
class TreeNode:
    name: str
    path: str             # relative to project root
    type: str             # "directory" | "file" | "symlink"
    size: int | None = None
    children: list["TreeNode"] | None = None
    truncated: bool = False

@dataclass(frozen=True)
class WorkspaceConfig:
    registry_root: Path
    projects: dict[str, Project]  # keyed by project_id
    default_ignores: list[str]
```

## Policy Layer

### Typed Exceptions

```python
class WorkspacePolicyError(Exception): ...
class TraversalError(WorkspacePolicyError): ...
class SymlinkEscapeError(WorkspacePolicyError): ...
class HiddenPathError(WorkspacePolicyError): ...
class ScopeDeniedError(WorkspacePolicyError): ...
```

### Scopes

```python
SCOPES = {
    "project:read",     # file tree, project state
    "project:write",    # file write/edit (Phase B+)
    "project:execute",  # shell commands (Phase B+)
    "project:docker",   # docker compose ops (Phase B+)
    "workspace:read",   # list projects, project metadata
}
```

### WorkspacePolicy

```python
class WorkspacePolicy:
    def __init__(self, config: WorkspaceConfig): ...

    def resolve_project(self, project_id: str) -> Project: ...

    # Operation-specific validators
    def validate_read(self, project_id: str, path: str) -> Path: ...
    def validate_write(self, project_id: str, path: str) -> Path: ...
    def validate_execute(self, project_id: str, path: str) -> Path: ...
    def validate_docker(self, project_id: str) -> None: ...

    # Internal helpers
    def _validate_path(self, project: Project, path: str) -> Path: ...
    def _reject_input(self, path: str) -> None: ...
    def _check_symlink(self, resolved: Path, root: Path) -> None: ...

    # Secrets (by full relative path, not basename)
    def is_secret(self, project: Project, relative_path: str) -> bool: ...
```

### Rejection Rules (`_reject_input`)

- Absolute path → `TraversalError`
- `~` or `~user` prefix → `TraversalError`
- `..` in path parts → `TraversalError`
- Empty path → allowed only when caller maps `""` → `"."`

### Hidden Path Rules

- Path starting with `.` (e.g. `.env`, `.git/config`) → `HiddenPathError` on explicit access
- `is_secret()` filters known secret patterns from tree results (`.env`, `*.key`, `*.pem`, etc.)
- Tree-builder applies `default_ignores` (`__pycache__`, `.git`, `node_modules`) as UI convenience, not security
- Security boundary: `WorkspacePolicy` rejects explicit access to hidden paths; tree-builder silently omits ignored entries

### Path Validation (`_validate_path`)

1. Join `project.root + relative_path`
2. Resolve symlinks → must still be under `project.root`
3. No `..` components after resolution
4. `Path.resolve()` then check `is_relative_to(project.root)`

## Tools

### Signatures

```python
from typing import Any

def workspace_list_projects(
    registry: WorkspaceRegistry | None = None,
) -> list[dict[str, Any]]:
    """List all registered projects. No project_id needed."""

def project_info(
    project_id: str,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """Full info about one project."""

def project_tree(
    project_id: str,
    relative_path: str = "",
    depth: int = 3,
    max_nodes: int = 500,
    registry: WorkspaceRegistry | None = None,
) -> dict[str, Any]:
    """List directory contents at relative_path within project."""
```

### Invariants

- Sync `def` — no async without real I/O
- `registry` optional — singleton default when None
- Output is `dict[str, Any]` — JSON/MCP-ready
- `relative_path` is canonical param name
- Tools are library functions, not HTTP handlers

### Tree behavior

- `relative_path=""` → project root
- `depth=3` → default traversal depth
- `max_nodes=500` → default node limit
- `truncated=True` when max_nodes hit
- Secrets filtered by `policy.is_secret()`
- Ignore patterns applied: `__pycache__`, `.git`, `node_modules`, `.venv`, `*.pyc`

## Router + MCP Wiring (Future Design, No Code in Phase A)

### REST Endpoints

| Endpoint | Method | Scope | Tool call |
|----------|--------|-------|-----------|
| `/api/workspace/projects` | GET | `workspace:read` | `workspace_list_projects()` |
| `/api/workspace/projects/{project_id}` | GET | `workspace:read` | `project_info(project_id)` |
| `/api/workspace/projects/{project_id}/tree` | GET | `project:read` | `project_tree(project_id, relative_path, depth)` |

Query params for tree: `?path=&depth=3&max_nodes=500` (`path` alias → `relative_path` internally)

### MCP Tools

| Tool name | Scope | Tool call |
|-----------|-------|-----------|
| `workspace_list_projects` | `workspace:read` | `workspace_list_projects()` |
| `project_info` | `workspace:read` | `project_info(project_id)` |
| `project_tree` | `project:read` | `project_tree(project_id, relative_path, depth)` |

### Error Mapping

| Exception | HTTP | MCP | Message |
|-----------|------|-----|---------|
| Unknown project | 404 | `NOT_FOUND` | `Project not found: {id}` |
| TraversalError | 400 | `INVALID_ARGUMENT` | `Path traversal rejected` |
| SymlinkEscapeError | 400 | `INVALID_ARGUMENT` | `Symlink escape detected` |
| HiddenPathError | 403 | `PERMISSION_DENIED` | `Hidden or secret path denied` |
| ScopeDeniedError | 403 | `PERMISSION_DENIED` | `Scope required: {scope}` |
| WorkspacePolicyError | 400 | `INVALID_ARGUMENT` | Generic fallback |

## Package Layout

```
app/workspace/
├── __init__.py          # public API exports
├── models.py            # Project, TreeNode, WorkspaceConfig dataclasses
├── registry.py          # WorkspaceRegistry: load projects.yaml, lookup by project_id
├── policy.py            # WorkspacePolicy: path validation, secrets, scopes, typed exceptions
└── tools.py             # workspace_list_projects, project_info, project_tree

tests/
├── test_workspace_policy.py
├── test_workspace_registry.py
└── test_workspace_tools.py
```

### Shims (backward compatibility)

```python
# app/workspace_registry.py → shim
from app.workspace.registry import WorkspaceRegistry
from app.workspace.registry import load_registry, get_registry, reset_registry

# app/workspace_policy.py → shim
from app.workspace.policy import WorkspacePolicy
from app.workspace.policy import (
    WorkspacePolicyError, TraversalError, SymlinkEscapeError,
    HiddenPathError, ScopeDeniedError, SCOPES,
)

# Also re-export tools for backward compat
from app.workspace.tools import workspace_list_projects, project_info, project_tree
```

Old imports must continue working:
```python
import app.workspace_policy
import app.workspace_registry
```

## Tests

### test_workspace_policy.py

| Test | Coverage |
|------|----------|
| `test_path_traversal_rejected` | `../` in path → TraversalError |
| `test_symlink_escape_rejected` | symlink outside root → SymlinkEscapeError |
| `test_absolute_path_rejected` | absolute path input → TraversalError |
| `test_tilde_rejected` | `~` prefix → TraversalError |
| `test_hidden_path_rejected` | hidden/secret path → HiddenPathError |
| `test_valid_read_path` | validate_read returns resolved Path |
| `test_secrets_filtered` | is_secret matches .env, *.key, etc. |

### test_workspace_registry.py

| Test | Coverage |
|------|----------|
| `test_load_projects_yaml` | Loads 6 projects from projects.yaml |
| `test_project_lookup_valid` | resolve_project returns Project for known ID |
| `test_project_lookup_unknown` | resolve_project raises error for unknown ID |
| `test_project_root_resolved` | root is absolute Path from registry_root + relative |
| `test_unique_project_ids` | No duplicate project_ids in registry |

### test_workspace_tools.py

| Test | Coverage |
|------|----------|
| `test_list_projects_returns_all` | workspace_list_projects returns 6 projects |
| `test_project_info_valid` | project_info returns metadata for known project |
| `test_project_info_unknown` | project_info raises error for unknown ID |
| `test_tree_root` | project_tree with empty path returns root children |
| `test_tree_depth` | depth=1 returns only immediate children |
| `test_tree_max_nodes` | truncation at max_nodes limit |
| `test_tree_secrets_filtered` | .env, *.key files excluded |
| `test_tree_ignores` | __pycache__, .git, node_modules excluded |

## Acceptance Criteria

- [ ] `projects.yaml` loads 6 projects
- [ ] `project_tree` works for all 6 projects
- [ ] `ruff check .` clean
- [ ] `python3 -m mypy .` clean
- [ ] `pytest -q` green
- [ ] Old imports pass smoke:
  ```python
  import app.workspace_policy
  import app.workspace_registry
  ```
- [ ] Docker build/import checks:
  ```python
  import app.workspace.registry
  import app.workspace.tools
  ```
