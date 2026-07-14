# Workspace Control Plane - Phase B Implementation Plan

**Date:** 2026-07-14
**Status:** Draft
**Spec:** `docs/superpowers/specs/2026-07-14-workspace-control-plane-phase-b.md`

## Objective

Implement safe read-only IDE/navigation tools on top of the Phase A workspace registry:

- read text files;
- find files;
- search text;
- inspect git status/diff/log/branch;
- expose them through the workspace tool layer and, after library validation, thin REST/MCP wrappers.

No write/edit, arbitrary execute, docker, restart, SSH, database mutation, or secret content access is allowed in Phase B.

## Frozen Decisions

- Local filesystem only under `projects.yaml` registry roots.
- Every operation accepts explicit `project_id`.
- Every path is project-relative and validated by `WorkspacePolicy`.
- Hidden/secret explicit reads fail.
- Search/find/tree silently skip hidden/secret/vendor/cache paths.
- Git operations are fixed read-only argv, `shell=False`, timeout-bound.
- Tool output is JSON/MCP-ready `dict[str, Any]`.

## Agent Tasks

### Agent 1 - File Read + Find

**Goal:** Implement safe read-only file inspection/discovery.

**Touch:**

- `app/workspace/files.py`
- `app/workspace/tools.py`
- `app/workspace/__init__.py`
- tests for file read/find

**Implement:**

- `project_file_read(project_id, relative_path, start_line=None, max_lines=None, max_bytes=200_000, registry=None)`
- `project_find_files(project_id, pattern="*", relative_path="", max_results=500, registry=None)`
- text/binary detection;
- max byte/result caps;
- skip vendor/cache/secret paths in find;
- reject explicit secret/hidden reads through `WorkspacePolicy`.

**Do not touch:**

- REST/MCP routers;
- git tools;
- search engine;
- write/edit/execute/docker code;
- auth/OAuth.

**Report:**

- changed files;
- exact tool signatures;
- negative security tests;
- targeted and full test exit codes.

### Agent 2 - Text Search

**Goal:** Implement literal text search with strict limits.

**Touch:**

- `app/workspace/search.py`
- `app/workspace/tools.py`
- `app/workspace/__init__.py`
- tests for search

**Implement:**

- `project_search_text(project_id, query, relative_path="", file_glob="**/*", case_sensitive=False, context_lines=2, max_matches=100, max_bytes_per_file=1_000_000, registry=None)`
- literal search only;
- context lines;
- binary skip;
- secret/vendor/cache skip;
- max match cap with `truncated=True`;
- empty query rejection.

**Do not touch:**

- REST/MCP routers;
- git tools;
- file read implementation except shared helpers agreed with Agent 1;
- write/edit/execute/docker code.

**Report:**

- changed files;
- result schema;
- limit behavior;
- targeted and full test exit codes.

### Agent 3 - Git Read-Only Tools

**Goal:** Implement safe git inspection commands.

**Touch:**

- `app/workspace/git.py`
- `app/workspace/tools.py`
- `app/workspace/__init__.py`
- tests for git tools

**Implement:**

- `project_git_status(project_id, registry=None)`
- `project_git_branch(project_id, registry=None)`
- `project_git_log(project_id, limit=20, relative_path=None, registry=None)`
- `project_git_diff(project_id, relative_path=None, staged=False, max_bytes=200_000, registry=None)`
- fixed argv only;
- `shell=False`;
- safe env with `GIT_TERMINAL_PROMPT=0`;
- timeout 10 seconds;
- non-git project returns `is_git_repo=False`;
- optional path validated and appended after `--`.

**Do not touch:**

- REST/MCP routers;
- file/search tools except shared result conventions;
- network-capable git commands;
- docker/execute code.

**Report:**

- exact argv per command;
- non-git behavior;
- timeout/limit behavior;
- targeted and full test exit codes.

### Agent 4 - Thin REST/MCP Wiring + Integration

**Goal:** Add callable read-only surface only after Agents 1-3 library tools are green.

**Touch:**

- REST router and/or MCP descriptor files only after identifying existing project conventions;
- integration tests/smoke docs;
- no business logic outside `app.workspace.tools`.

**Implement:**

- REST wrappers for Phase A+B read-only tools if existing router style supports it cleanly;
- MCP wrappers/tool descriptors if existing MCP registration supports it cleanly;
- shared error mapping from spec;
- parameter alias `path` -> `relative_path`.

**Do not touch:**

- auth/OAuth internals unless a compile/test failure requires import wiring;
- file/search/git business logic;
- write/edit/execute/docker;
- deployment secrets or env files.

**Report:**

- discovered existing router/MCP registration points;
- added endpoints/tools and scopes;
- discovery/smoke results;
- any blockers if wiring is larger than thin wrappers.

## Integration Order

1. Agent 1 file read/find.
2. Agent 2 search.
3. Agent 3 git.
4. Merge library exports in `app.workspace.tools`.
5. Run full checks.
6. Agent 4 thin REST/MCP wiring.
7. Real smoke across all 7 projects.

## Validation Commands

```bash
ruff check .
python3 -m mypy .
pytest -q
PYTHONPATH=. python3 - <<'PY'
from app.workspace.registry import WorkspaceRegistry
from app.workspace.tools import project_tree

r = WorkspaceRegistry.load("projects.yaml")
for p in r.list_projects():
    project_tree(p["project_id"], depth=1, registry=r)
print("workspace smoke ok")
PY
```

Docker smoke:

```bash
docker compose -f docker/docker-compose.yml build web-ssh-gateway
docker compose -f docker/docker-compose.yml run --rm web-ssh-gateway \
  python -c "import app.workspace.files, app.workspace.search, app.workspace.git, app.workspace.tools"
```

## Release Notes Draft

Phase B adds read-only workspace IDE tools scoped by `project_id`: file read, file discovery, text search, and git inspection. All paths are validated through `WorkspacePolicy`; secrets remain blocked; no write/execute/docker operations are introduced.
