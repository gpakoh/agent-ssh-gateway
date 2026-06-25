# web-ssh-gateway — Agent Reference

FastAPI SSH gateway: REST commands + WebSocket PTY + persistent sessions.
Stack: FastAPI + Paramiko + PostgreSQL 16 + Redis 7, Docker Compose stack (3 containers).

## Gitea (CI/CD, repos, issues, PRs)

**openocode.json** MCP (`gitea`) — доступен как инструменты агенту.
12 tools: repo, branches, commits, file, issues, PRs + Gitea Actions (CI/CD).

**ChatGPT remote endpoint:**
```
URL:  https://ssh.xloud.ru/mcp/gitea?mcp_token=06KkheuSqP7A6dKeCCZdKtcDMe5UhzouvcwI5NyR_Xk
Type: streamable-http (initialize → Mcp-Session-Id → tools/list)
```

**Gitea API токен:** `opencode` (`read:repository`) — записан в `/etc/agent-mcp-gitea.env` (600).

**Основные команды:**
```bash
# Push to Gitea
git push gitea master

# Push to GitHub
GIT_TOKEN=$(python3 -c "import yaml; print(yaml.safe_load(open('/root/.config/gh/hosts.yml'))['github.com']['oauth_token'])")
git push "https://gpakoh:${GIT_TOKEN}@github.com/gpakoh/web-ssh-gateway.git" master

# Gitea API напрямую (CI/CD runs, jobs, workflows)
curl -s --noproxy '*' -H "Authorization: token $(grep GITEA_TOKEN /etc/agent-mcp-gitea.env | cut -d= -f2)" \
  "http://192.168.1.103:3005/api/v1/repos/gpakoh/<repo>/actions/runs?limit=5"
```

## CI Status

```bash
curl --noproxy '*' -s http://192.168.1.11:5123/status
```

## Fleet Healthcheck

Перед любой диагностикой — запустить:

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway
python scripts/mcp_fleet_healthcheck.py
```

Проверяет: systemd, env file security, MCP tools/list, nginx route.
Все 6 адаптеров должны быть `OK` с корректным количеством инструментов.
После рестарта любого сервиса/nginx — прогнать healthcheck в первую очередь.

## Fleet MCP Endpoints (ChatGPT)

| Adapter | Internal | Public | Token |
|---------|----------|--------|-------|
| Main gateway | `10.10.10.3:8788` | `/mcp` | в `/etc/agent-ssh-gateway-mcp.env` |
| Context7 | `10.10.10.3:8790` | `/mcp/context7` | в `/etc/agent-mcp-context7.env` |
| GitHub | `10.10.10.3:8791` | `/mcp/github` | в `/etc/agent-mcp-github.env` |
| Gitea | `10.10.10.3:8792` | `/mcp/gitea` | `06KkheuSqP7A6dKeCCZdKtcDMe5UhzouvcwI5NyR_Xk` |
| Docker | `10.10.10.3:8793` | `/mcp/docker` | в `/etc/agent-mcp-docker.env` |
| Postgres (rag_vectordb) | `10.10.10.3:8794` | `/mcp/postgres` | в `/etc/agent-mcp-postgres.env` |

Все nginx-прокси на VPS `192.168.1.100` → `/etc/nginx/sites-available/ssh.xloud.ru`.

## GitHub (opencode.json MCP)

Classic PAT с полным доступом — инструменты агента (26 tools, 14 read + 12 write).
Для ChatGPT: `https://ssh.xloud.ru/mcp/github?mcp_token=<token>` (8 read-only tools, fine-grained PAT).

## Docker Compose

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway
docker compose -p web-ssh-gateway -f docker/docker-compose.yml up -d --build --remove-orphans
```

## Tests

```bash
pytest -q              # 119 unit tests
pytest -m integration  # 4 live sshd tests
```

## VPS Nginx

SSH: `ssh root@192.168.1.100` (пароль `hjnjhbv2`).
Прокси-правила в `/etc/nginx/sites-available/ssh.xloud.ru`.
После изменений: `nginx -t && systemctl reload nginx`.

## Systemd fleet services

| Service | Ports | Env file |
|---------|-------|----------|
| `agent-mcp-github.service` | 8781/8791 | `/etc/agent-mcp-github.env` |
| `agent-mcp-gitea.service` | 8782/8792 | `/etc/agent-mcp-gitea.env` |
| `agent-mcp-context7.service` | 8780/8790 | `/etc/agent-mcp-context7.env` |
| `agent-mcp-docker.service` | 8783/8793 | `/etc/agent-mcp-docker.env` |
| `agent-mcp-postgres.service` | 8784/8794 | `/etc/agent-mcp-postgres.env` |
| `agent-ssh-gateway-mcp.service` | 8788 | `/etc/agent-ssh-gateway-mcp.env` |

```bash
systemctl restart agent-mcp-gitea.service
journalctl -u agent-mcp-gitea.service -n 30 --no-pager
```

## Gitea Actions API

`opencode` token (`46f10e23158e2da2ead68e5daed514c61b18af09`). Read-only.

Key endpoints:
- `GET /repos/{owner}/{repo}/actions/runs` — список CI/CD runs
- `GET /repos/{owner}/{repo}/actions/runs/{id}/jobs` — jobs + steps
- `GET /repos/{owner}/{repo}/actions/workflows` — workflow файлы

## Remote MCP / Gateway tools (Phase 2)

The ChatGPT Gateway MCP endpoint exposes a fleet of project-safe tools through:

- Gateway local project tools (read/search/diff/test)
- GitHub read-only tools
- Gitea read-only / CI tools
- Handoff tools

The public MCP endpoint uses **Streamable HTTP/SSE**. Smoke tests must parse `data:` SSE frames; do not expect raw JSON responses.

### Current production MCP endpoint

- `https://ssh.xloud.ru/mcp?mcp_token=...`
- token is never committed
- service env: `/etc/agent-ssh-gateway-mcp.env`
- gateway session is created through the gateway API
- SSH target is `mcp-sshd` container on `172.19.0.45:2222` (key auth)

### Available project-safe tools

- `gateway_project_working_directory`
- `gateway_project_git_status`
- `gateway_project_git_diff`
- `gateway_project_search_text`
- `gateway_project_find_files`
- `gateway_project_tree`
- `gateway_project_read_file`
- `gateway_project_run_pytest`
- `gateway_project_run_ruff`
- `gateway_project_run_mypy`
- `gateway_project_write_handoff_plan`

### Agent Handoff v2 lifecycle

```
write_agent_task → opencode_runner_wrapper → read task files → archive_agent_task
```

6 tools under prefix `gateway_project_*`:
- `write_agent_task` — write task.json + current-plan.md + agent-status.md
- `read_agent_status` — read .ai-bridge/tasks/<id>/agent-status.md
- `read_agent_report` — read .ai-bridge/tasks/<id>/agent-report.md
- `read_agent_diff` — read .ai-bridge/tasks/<id>/implementation-diff.patch
- `list_agent_tasks` — list .ai-bridge/tasks/ directories
- `archive_agent_task` — move task to .ai-bridge/archive/ (never delete)

Local wrapper: `scripts/opencode_runner_wrapper.py` — executes task plan via OpenCode CLI.
- `--dry-run` for safe validation
- `--self-test` for CI smoke
- Output: opencode-run.log + opencode-result.md in task dir

### Safety notes

- Prefer project-scoped tools over generic SSH/session tools.
- Do not use generic command execution for ChatGPT-facing workflows.
- Do not read `.env`, private keys, tokens, or secret files.
- Handoff write is limited to `.ai-bridge/` directories.
- Public endpoint responses are SSE-framed.
