# agent-ssh-gateway

**AI engineering control plane — SSH gateway with MCP Fleet for agents, CI/CD and self-hosted automation.**

![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![status](https://img.shields.io/badge/status-alpha-orange)
![tests](https://img.shields.io/badge/tests-571%20passed-brightgreen)
[![MCP](https://img.shields.io/badge/MCP-Fleet-7B2FF7?logo=modelcontextprotocol)](https://modelcontextprotocol.io)

> **Do not expose this service directly to the public Internet.** Read [SECURITY.md](SECURITY.md) before deploying.

### Why not just SSH?

- Agents need structured APIs, not interactive terminals.
- Long-running commands need jobs, status and output streams.
- File operations should be separate from shell commands.
- Access must be scoped, audited and redactable.

### How is this different?

Similar tools are often terminal-first or MCP-only.
agent-ssh-gateway is OpenAPI-first and can be used by agents, CI/CD, dashboards and internal automation.

### Gateway MCP Fleet

The project now ships a **multi-adapter MCP Fleet** that exposes the gateway and its ecosystem through the Model Context Protocol:

| Adapter | Tools | Access |
|---------|-------|--------|
| **Gateway** | 62 | SSH commands, project-safe code tools, job management, handoff |
| **GitHub** | 8 | Read-only: repo info, commits, branches, search, PRs, issues |
| **Gitea** | 12 | Read-only + CI/CD: repos, branches, PRs, Actions runs, jobs |
| **Context7** | 2 | Documentation lookup for AI coding agents |
| **Docker** | 7 | Read-only: ps, images, inspect, logs, stats, compose_ps, services |
| **Postgres** | 6 | Read-only: health, list_schemas, list_tables, describe_table, select (guardrailed), vector_status |

All adapters are deployed behind an nginx reverse proxy and use **Streamable HTTP/SSE** transport with per-adapter API token auth.

```text
ChatGPT / AI agents
    ↓
Gateway MCP Fleet
    ├── Gateway (SSH + project tools)
    ├── GitHub (read-only)
    ├── Gitea (read-only + CI/CD)
    ├── Docker (read-only fleet ops)
    ├── Postgres (read-only SQL guardrails)
    └── Context7 (docs)
```

---

## Project status

Early self-hosted MVP / alpha release. Intended for private/internal automation environments. The public API may change before v1.0.0.

---

## Quickstart

```bash
git clone https://github.com/gpakoh/agent-ssh-gateway.git
cd agent-ssh-gateway
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Generate API keys and add them to `.env`:

```bash
python -c "
import secrets
with open('.env', 'a') as f:
    f.write(f'API_KEY={secrets.token_urlsafe(48)}\n')
    f.write(f'AGENT_TOKEN={secrets.token_urlsafe(48)}\n')
"
```

Start the server:

```bash
uvicorn app.main:app --reload
```

On Windows PowerShell:

```powershell
python -c "import secrets; open('.env', 'a').write('API_KEY=' + secrets.token_urlsafe(48) + '\n' + 'AGENT_TOKEN=' + secrets.token_urlsafe(48) + '\n')"
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Verify it is running:

```bash
curl http://127.0.0.1:8000/health

curl http://127.0.0.1:8000/api/capabilities
```

OpenAPI UI:

```text
http://127.0.0.1:8000/docs
```

---

## MCP Fleet usage

The Fleet exposes a **Streamable HTTP/SSE** endpoint per adapter. Each requires initialization via the MCP protocol:

```text
POST /mcp?mcp_token=<token>  →  initialize → Mcp-Session-Id → tools/list → tools/call
```

### Gateway (project-safe tools)

Execute commands and use project-scoped tools through the gateway MCP adapter:

```python
# Initialize session
POST /mcp?mcp_token=<token>
{"jsonrpc":"2.0","id":"1","method":"initialize",...}

# List 62 tools
POST /mcp?mcp_token=<token>
{"jsonrpc":"2.0","id":"2","method":"tools/list"}
```

The gateway adapter includes project-safe tools — read file, search text, find files, tree, git diff, pytest, ruff, mypy, remotes, branch info, and handoff read/write/status. These are scoped to the project root and block path traversal, shell injection, and unauthorized writes.

### GitHub (read-only)

8 read-only tools for repository inspection: repo info, commit log, branch list, file contents, code search, PR list, issues, user info.

### Gitea (read-only + CI/CD)

12 tools covering repository info, branches, file tree, file read, issues, PRs, CI/CD runs, job status, workflow details, and commit search.

---

## Minimal SSH flow

Set a master API key (required by the auth middleware):

```bash
export API_KEY=change-me-generate-long-random-api-key
```

1. Create an SSH session:

```bash
curl -X POST http://127.0.0.1:8000/api/ssh/connect \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "host": "your-server",
    "username": "root",
    "password": "your-password"
  }'
```

Save the returned `session_id`.

2. Execute a command (sync):

```bash
curl -X POST http://127.0.0.1:8000/api/ssh/execute \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "<session_id>",
    "command": "uname -a"
  }'
```

   For long-running commands, use `async_mode=true` to run as a background job:

```bash
curl -X POST http://127.0.0.1:8000/api/ssh/execute \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "<session_id>",
    "command": "docker compose build",
    "async_mode": true
  }'
```

   When command output may contain tokens or secrets, add `redact_output=true`:

```bash
curl -X POST http://127.0.0.1:8000/api/ssh/execute \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "<session_id>",
    "command": "kubectl get secrets -o yaml",
    "redact_output": true
  }'
```

3. Disconnect:

```bash
curl -X POST http://127.0.0.1:8000/api/ssh/disconnect \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "<session_id>"
  }'
```

For AI agent use, request a scoped agent token instead of using the master key directly.

---

## Why this project exists

Traditional SSH access works well for humans, but it is often awkward and risky for automation:

* agents need SSH credentials directly;
* commands are hard to audit consistently;
* CI jobs often duplicate SSH logic;
* long-running sessions are difficult to manage;
* access policies are usually hidden inside scripts;
* file transfer, command execution and logs are scattered across tools.

`agent-ssh-gateway` solves this by exposing SSH operations through a controlled API.

Instead of giving every automation component direct SSH access, you can place one gateway in front of your infrastructure and control how SSH is used.

---

## What it does

The gateway MCP adapter also includes a **handoff protocol** (`.ai-bridge/current-plan.md`) that lets AI agents leave structured plan documents behind for other agents, with write access controlled by `MCP_GATEWAY_WRITE_MODE`.

`agent-ssh-gateway` allows clients to:

* work through an **MCP Fleet** with per-adapter tool sets and Streamable HTTP/SSE transport;
* create SSH sessions through an HTTP API;
* execute commands on remote machines;
* stream terminal sessions through WebSocket;
* run background jobs;
* transfer files;
* inspect basic system information;
* use short-lived agent tokens;
* log and audit SSH activity;
* integrate with CI/CD pipelines and internal automation;
* expose a structured OpenAPI contract for SDKs and agents.

---

## Main use cases

### AI agents

Give AI agents a controlled way to execute infrastructure tasks without handing them raw SSH access.

Examples: inspect a remote service, read logs, restart a container, check disk usage, run deployment commands, collect diagnostics.

### CI/CD pipelines

Use the gateway as a central SSH execution layer for build and deployment jobs.

Examples: deploy to a remote host, run migrations, upload release artifacts, restart services, collect post-deploy status.

### Internal infrastructure tools

Build dashboards, admin panels and automation services on top of a single SSH API.

Examples: one-click maintenance actions, controlled server operations, internal support tools, repeatable operational playbooks.

### Self-hosted environments

Useful for homelabs, small infrastructure clusters, internal DevOps setups and private automation platforms.

---

## Key features

* **MCP Fleet** — multi-adapter Model Context Protocol deployment: SSH gateway, GitHub read-only, Gitea + CI/CD, Context7 docs.
* **Project-safe tools** — 16 scoped tools for file read, text search, diff, test run, lint, and handoff (path traversal and injection blocked).
* **API-first design** — SSH operations are exposed through a documented HTTP API.
* **OpenAPI contract** — usable by agents, SDKs and generated clients.
* **Persistent SSH sessions** — create, reuse and close sessions through API calls.
* **Command execution** — run commands remotely and capture structured results.
* **WebSocket terminal** — optional interactive terminal access.
* **Background jobs** — run longer tasks without blocking the initial API request.
* **File operations** — upload, download and manage files over SSH.
* **Agent tokens** — short-lived tokens for automation instead of long-lived master credentials.
* **Session ownership** — each session is bound to the token that created it.
* **Audit logging** — track who connected, where, when and what was executed.
* **Event hooks** — send structured events to external systems.
* **Security-focused deployment model** — designed to run behind SSO, reverse proxy, mTLS, API keys and network policies.

---

## What this project is not

Not a replacement for Teleport, Apache Guacamole, or enterprise zero-trust platforms. Not a browser SSH terminal.

The goal: a lightweight, self-hosted control plane for AI agents, CI/CD and internal automation — built on an SSH gateway and extended through an MCP Fleet of project-safe, read-only and documentation adapters.

If you only need a browser-based SSH client, this may be more than you need.

---

## Configuration

Create an `.env` file from the example:

```bash
cp .env.example .env
```

All env vars are documented in `.env.example`. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | `change-me-...` | Master API key for authentication |
| `AGENT_TOKEN` | `change-me-...` | Agent token for scoped access |
| `ALLOWED_TARGET_CIDRS` | `10.0.0.0/8,...` | SSH targets the gateway may connect to |
| `DENIED_TARGET_CIDRS` | `127.0.0.0/8,...` | SSH targets always denied |
| `SSH_KEY_UPLOAD_ENABLED` | `false` | Private key upload via API |
| `COMMAND_POLICY_MODE` | `audit` | Command policy mode |
| `COMMAND_OUTPUT_REDACTION_ENABLED` | `false` | Redact secrets (tokens, passwords, keys) from command responses |
| `PERSISTENT_SESSIONS_ENABLED` | `false` | Persist sessions across restarts |
| `ENCRYPTION_KEY` | `change-me-...` | Fernet key for credential encryption |

Never commit real `.env` files.

---

## Running with Docker Compose

```bash
docker compose up -d
```

Health check:

```bash
curl http://localhost:8085/health
```

OpenAPI:

```text
http://localhost:8085/docs
```

---

## Development

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                       # 570+ tests
ruff check app tests            # linting
mypy app                     # type checking
uvicorn app.main:app --reload   # run locally
```

---

## Command policy

```env
COMMAND_POLICY_MODE=audit
COMMAND_POLICY_PROFILE=default
```

Modes: `off` (disabled), `audit` (log, do not block), `enforce` (block).

Profiles: `default` (blocks dangerous root commands), `readonly` (inspection only), `ops` (read-only + limited systemctl/service/docker).

Recommended rollout: start with `audit`, review logs, then move to `enforce` on selected environments.

---

## Output redaction

```env
COMMAND_OUTPUT_REDACTION_ENABLED=true
```

Optional redaction of secrets (API keys, tokens, passwords, private key material) from command stdout/stderr responses. Disabled by default — set to `true` or pass `redact_output=true` per-request to enable.

When enabled, secrets are replaced with `[REDACTED]` on the response/stream side. **Raw job output is never mutated** — redaction applies only to the response or SSE stream data.

Covered endpoints:
- `POST /api/ssh/execute` — field `redact_output` in request body
- `GET /api/jobs/{job_id}/result` — query param `?redact_output=true`
- `GET /api/jobs/{job_id}/stream` and `/events` — query param `?redact_output=true`

Redaction is a best-effort regex-based pass, not a full DLP solution. It catches common patterns (`api_key=...`, `token=...`, `password=...`, `Authorization: Bearer ...`) but will not catch every possible secret format. Use as a safety net, not a security boundary.

---

## Web UI authentication

```env
JWT_SECRET=...
AUTH_DB_PATH=/app/data/auth.sqlite3
JWT_EXPIRES_MINUTES=1440
```

The web UI uses a single-admin bootstrap flow powered by JWT:

1. On first start, open `http://localhost:8085` and the UI shows a registration form.
2. Create the first admin account — this is the only user the system will ever have.
3. After registration, public registration is automatically disabled. Subsequent visits show a login form.
4. The JWT token is stored in browser `localStorage` and sent as `Authorization: Bearer <token>` on every API request.

Generate a `JWT_SECRET`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The auth database file lives at `AUTH_DB_PATH` (default `/app/data/auth.sqlite3`). In Docker, this path is inside the `known_hosts` volume mounted at `/app/data`.

Behind the scenes:
- Passwords are hashed with **bcrypt**.
- Credential validation uses **bcrypt.checkpw** — no timing side channels.
- Registration is guarded by an `asyncio.Lock` to prevent race conditions on first-user creation.
- If `JWT_SECRET` is empty, the gateway refuses to start with a `RuntimeError`.
- The `verify_jwt` function enforces `type: "web-ui"` in the token payload to prevent cross-context token reuse.
- The global auth middleware falls back to JWT verification when no `X-API-Key` header is present.

---

## Target allowlist

Production deployments should restrict which hosts the gateway can reach:

```env
ALLOWED_TARGET_CIDRS=10.0.0.0/8,192.168.0.0/16,172.16.0.0/12
DENIED_TARGET_CIDRS=127.0.0.0/8,::1/128,169.254.0.0/16,0.0.0.0/8,224.0.0.0/4
```

This prevents the gateway from becoming an internal port scanner or SSRF-style pivot.

---

## Security model

SSH gateways are sensitive infrastructure components.

Do not expose this service directly to the Internet without proper protection.

### Current hardening status

- Target allowlist/denylist: enabled
- Command policy: audit by default, enforce available
- Route auth contract: enabled
- Agent token scopes: enabled
- Session ownership: enabled
- Secret redaction: enabled
- Private key upload: disabled by default
- Full mypy: 0 errors
- Test suite: 571 passed, 1 skipped

### Recommended deployment topology

```text
Internet
   ↓
Reverse Proxy (TLS termination)
   ↓
SSO / Authelia / OAuth2 Proxy
   ↓
mTLS / API Key / Agent Token
   ↓
agent-ssh-gateway
   ↓
Allowed SSH Targets
```

### Recommended protections

* run behind a reverse proxy;
* require SSO for human access;
* require API keys or short-lived agent tokens for automation;
* use mTLS where possible;
* restrict client IP ranges;
* restrict allowed SSH target networks;
* deny loopback, link-local and metadata service ranges;
* use least-privilege SSH users;
* avoid storing private SSH keys in the gateway;
* rotate all secrets regularly;
* enable audit logs;
* redact sensitive command output;
* never expose raw production secrets in logs, hooks or events.

---

## Suggested production checklist

Before using this in production:

* [ ] Change all default secrets.
* [ ] Put the service behind a reverse proxy.
* [ ] Enable SSO for browser access.
* [ ] Use API keys or short-lived tokens for automation.
* [ ] Configure allowed client networks.
* [x] Configure allowed target networks (`ALLOWED_TARGET_CIDRS` / `DENIED_TARGET_CIDRS`).
* [x] Deny loopback, link-local and metadata IP ranges (built into default `DENIED_TARGET_CIDRS`).
* [ ] Use dedicated low-privilege SSH users.
* [x] Private key upload disabled by default (`SSH_KEY_UPLOAD_ENABLED=false`).
* [ ] Enable audit logging.
* [x] Enable output redaction for secrets (`COMMAND_OUTPUT_REDACTION_ENABLED=true`).
* [x] Command policy engine with `readonly`/`ops`/`default` profiles (`COMMAND_POLICY_MODE=enforce`).
* [ ] Rotate tokens regularly.
* [ ] Review event hooks before enabling command output forwarding.
* [ ] Keep deployment-specific files out of the public repository.

---

## Public repository hygiene

This repository should contain only generic example configuration.

Do not commit: real `.env` files, private SSH keys, API keys, agent tokens, webhook secrets, production IP addresses, internal domains, real reverse proxy configs, customer data, or deployment files containing private infrastructure details.

Keep real deployment configuration in a private repository or secret manager.

---

## Repository structure

```text
app/                  Core SSH gateway application
  routers/            API routers (see [docs/ROUTERS.md](docs/ROUTERS.md))
  services/           SSH, jobs, audit and integration services
  models/             Data models and schemas
  security.py         Authentication, validation and security helpers
  config.py           Application configuration

examples/             MCP Fleet adapters for AI agents
  mcp_server/         Gateway MCP adapter (62 tools, project-safe, SSH)
  chatgpt_remote_mcp/ ChatGPT remote MCP deployment
    fleet/            GitHub, Gitea, Context7 remote adapters

docker/
  docker-compose.yml
  Dockerfile

tests/
  Unit and integration tests

docs/
  Deployment and security documentation
```

---

## Project documents

- [Security model](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Roadmap](docs/roadmap.md)

---

## License

MIT License.
