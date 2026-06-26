# MCP Tool-Level Scope Enforcement

## Purpose

Phase 3 introduces capability-based access profiles for MCP tools.
It supports both safe read-only clients and trusted full-control ChatGPT clients.
The purpose is not to reduce ChatGPT capability, but to make access explicit,
auditable, and configurable.

OAuth scopes are not only restrictions — they are capability bundles for
different operating modes.

## Access Profiles

```python
ACCESS_PROFILES = {
    "viewer": [
        "mcp:read",
        "mcp:repo",
        "mcp:docs",
    ],
    "operator": [
        "mcp:read",
        "mcp:project",
        "mcp:handoff",
        "mcp:repo",
        "mcp:docs",
    ],
    "agent-runner": [
        "mcp:read",
        "mcp:project",
        "mcp:handoff",
        "mcp:agent-run",
        "mcp:repo",
        "mcp:docs",
    ],
    "infra": [
        "mcp:read",
        "mcp:docker",
        "mcp:postgres",
        "mcp:repo",
    ],
    "full": [
        "mcp:read",
        "mcp:project",
        "mcp:handoff",
        "mcp:agent-run",
        "mcp:execute",
        "mcp:repo",
        "mcp:docker",
        "mcp:postgres",
        "mcp:docs",
        "mcp:admin",
    ],
}
```

| Profile | Назначение | ChatGPT |
|---------|-----------|---------|
| `viewer` | Внешний/безопасный read-only доступ | для публичного App |
| `operator` | Обычная работа с проектом | default |
| `agent-runner` | GPT-координатор агентов (OpenCode/Mimo) | по необходимости |
| `infra` | Диагностика инфраструктуры | по необходимости |
| `full` | Доверенный GPT с полным доступом | для приватного App |

## Config

```env
MCP_DEFAULT_ACCESS_PROFILE=operator
MCP_SCOPE_ENFORCEMENT=off       # off | audit | enforce
```

- `off` — текущий режим, scopes не проверяются
- `audit` — логирует denied, но не блокирует
- `enforce` — реально блокирует

## Scope Definitions

| Scope | Назначение |
|-------|-----------|
| `mcp:read` | Health, инфо, basic read-only |
| `mcp:project` | Project tools: read/search/tree/test/lint/compile — safe verification commands |
| `mcp:handoff` | Agent task lifecycle (read/write/archive) |
| `mcp:agent-run` | `project_run_opencode` / `gateway_project_run_mimo` — запуск runner-ов |
| `mcp:execute` | `gateway_execute_restricted` и будущие controlled command tools |
| `mcp:repo` | GitHub/Gitea read-only |
| `mcp:docker` | Docker read-only (ps, images, logs, inspect, stats) |
| `mcp:postgres` | Postgres read-only (select/schemas/tables) |
| `mcp:docs` | Context7 |
| `mcp:admin` | Unknown tools / privileged fallback — только full profile |

## TOOL_SCOPES Map

Fail-closed: если tool нет в карте → scope `mcp:admin` (только full profile).

### ops — mcp:read

| Tool | Scopes |
|------|--------|
| `gateway_health` | `mcp:read` |
| `gateway_list_sessions` | `mcp:read` |
| `gateway_session_health` | `mcp:read` |
| `gateway_execute_restricted` | `mcp:execute` |
| `gateway_job_status` | `mcp:read` |
| `gateway_job_result` | `mcp:read` |
| `gateway_wait_job` | `mcp:read` |
| `gateway_read_file` | `mcp:read`, `mcp:project` |
| `gateway_repo_status` | `mcp:read` |
| `gateway_working_directory` | `mcp:read`, `mcp:project` |
| `gateway_git_status` | `mcp:read`, `mcp:project` |
| `gateway_recent_commits` | `mcp:read`, `mcp:project` |
| `gateway_git_diff_stat` | `mcp:read`, `mcp:project` |
| `gateway_show_changes` | `mcp:read`, `mcp:project` |
| `gateway_run_tests` | `mcp:project` |
| `gateway_run_lint` | `mcp:project` |
| `gateway_run_compileall` | `mcp:project` |
| `gateway_self_test` | `mcp:read` |

### project — mcp:project

| Tool | Scopes |
|------|--------|
| `gateway_project_working_directory` | `mcp:project` |
| `gateway_project_git_status` | `mcp:project` |
| `gateway_project_recent_commits` | `mcp:project` |
| `gateway_project_git_diff_stat` | `mcp:project` |
| `gateway_project_show_changes` | `mcp:project` |
| `gateway_project_run_tests` | `mcp:project` |
| `gateway_project_run_lint` | `mcp:project` |
| `gateway_project_run_compileall` | `mcp:project` |
| `gateway_project_read_file` | `mcp:project` |
| `gateway_project_search_text` | `mcp:project` |
| `gateway_project_find_files` | `mcp:project` |
| `gateway_project_tree` | `mcp:project` |
| `gateway_project_git_diff` | `mcp:project` |
| `gateway_project_git_diff_cached` | `mcp:project` |
| `gateway_project_show_file_diff` | `mcp:project` |
| `gateway_project_run_pytest` | `mcp:project` |
| `gateway_project_run_ruff` | `mcp:project` |
| `gateway_project_run_mypy` | `mcp:project` |
| `gateway_project_remotes` | `mcp:project` |
| `gateway_project_current_branch` | `mcp:project` |
| `gateway_project_commit_head` | `mcp:project` |

### handoff — mcp:handoff

| Tool | Scopes |
|------|--------|
| `gateway_read_handoff` | `mcp:handoff` |
| `gateway_show_handoff_status` | `mcp:handoff` |
| `gateway_write_handoff_plan` | `mcp:handoff` |
| `gateway_project_read_handoff` | `mcp:handoff` |
| `gateway_project_write_handoff_plan` | `mcp:handoff` |
| `gateway_project_show_handoff_status` | `mcp:handoff` |
| `gateway_project_write_agent_task` | `mcp:handoff` |
| `gateway_project_read_agent_status` | `mcp:handoff` |
| `gateway_project_read_agent_report` | `mcp:handoff` |
| `gateway_project_read_agent_diff` | `mcp:handoff` |
| `gateway_project_list_agent_tasks` | `mcp:handoff` |
| `gateway_project_archive_agent_task` | `mcp:handoff` |

### agent-run — mcp:agent-run

| Tool | Scopes |
|------|--------|
| `project_run_opencode` | `mcp:agent-run` |
| `gateway_project_run_mimo` | `mcp:agent-run` |

### repo (fleet) — mcp:repo

| Tool | Scopes |
|------|--------|
| `gitea_get_repo` | `mcp:repo` |
| `gitea_list_branches` | `mcp:repo` |
| `gitea_list_commits` | `mcp:repo` |
| `gitea_get_file` | `mcp:repo` |
| `gitea_list_issues` | `mcp:repo` |
| `gitea_get_issue` | `mcp:repo` |
| `gitea_list_pull_requests` | `mcp:repo` |
| `gitea_get_pull_request` | `mcp:repo` |
| `gitea_list_action_runs` | `mcp:repo` |
| `gitea_get_action_run` | `mcp:repo` |
| `gitea_list_action_run_jobs` | `mcp:repo` |
| `gitea_list_workflows` | `mcp:repo` |
| `github_get_repo` | `mcp:repo` |
| `github_list_branches` | `mcp:repo` |
| `github_list_commits` | `mcp:repo` |
| `github_get_file` | `mcp:repo` |
| `github_list_issues` | `mcp:repo` |
| `github_get_issue` | `mcp:repo` |
| `github_list_pull_requests` | `mcp:repo` |
| `github_get_pull_request` | `mcp:repo` |

### docker — mcp:docker

| Tool | Scopes |
|------|--------|
| `docker_ps` | `mcp:docker` |
| `docker_images` | `mcp:docker` |
| `docker_inspect` | `mcp:docker` |
| `docker_logs` | `mcp:docker` |
| `docker_stats` | `mcp:docker` |
| `docker_compose_ps` | `mcp:docker` |
| `docker_compose_services` | `mcp:docker` |

### postgres — mcp:postgres

| Tool | Scopes |
|------|--------|
| `postgres_health` | `mcp:postgres` |
| `postgres_list_schemas` | `mcp:postgres` |
| `postgres_list_tables` | `mcp:postgres` |
| `postgres_describe_table` | `mcp:postgres` |
| `postgres_select` | `mcp:postgres` |
| `postgres_vector_status` | `mcp:postgres` |

### docs (Context7) — mcp:docs

| Tool | Scopes |
|------|--------|
| `resolve_library_id` | `mcp:docs` |
| `query_docs` | `mcp:docs` |

## Healthcheck Token

`MCP_HEALTHCHECK_BEARER_TOKEN` получает `full` profile:

```
mcp:read mcp:project mcp:handoff mcp:agent-run mcp:execute mcp:repo mcp:docker mcp:postgres mcp:docs mcp:admin
```

## Implementation Sketch

1. `examples/mcp_server/tool_scopes.py` — `TOOL_SCOPES` словарь + `ACCESS_PROFILES`
2. `ScopeGuard` — middleware/decorator, читает `MCP_SCOPE_ENFORCEMENT` и `MCP_DEFAULT_ACCESS_PROFILE`
3. Интеграция с `OAuthProxyMiddleware` — scopes проверяются на уровне прокси
4. Audit mode: логирует denied, не блокирует
5. Enforce mode: реально блокирует, возвращает 403
6. Для fleet-адаптеров — route-level scope check (`/mcp/gitea` → `mcp:repo`)
