# agent-ssh-gateway

**OpenAPI-first SSH control plane for AI agents, CI/CD pipelines and self-hosted infrastructure automation.**

![python](https://img.shields.io/badge/python-3.11%2B-blue)
![tests](https://img.shields.io/badge/tests-413%20passed-brightgreen)
![mypy](https://img.shields.io/badge/mypy-0%20errors-brightgreen)
![ruff](https://img.shields.io/badge/ruff-passing-brightgreen)
![license](https://img.shields.io/badge/license-MIT-blue)

> **Do not expose this service directly to the public Internet.** Read [SECURITY.md](SECURITY.md) before deploying.

---

## Project status

Early self-hosted MVP / alpha release. Intended for private/internal automation environments. The public API may change before v1.0.0.

---

## Quickstart

```bash
git clone https://github.com/gpakoh/web-ssh-gateway.git
cd web-ssh-gateway
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Start the server:

```bash
uvicorn app.main:app --reload
```

On Windows PowerShell:

```powershell
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

2. Execute a command:

```bash
curl -X POST http://127.0.0.1:8000/api/ssh/execute \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "<session_id>",
    "command": "uname -a"
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

`agent-ssh-gateway` allows clients to:

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

Not a replacement for Teleport, Apache Guacamole, or enterprise access platforms. Not a browser SSH terminal.

The goal: a lightweight, self-hosted SSH control plane for agents, automation and internal infrastructure workflows.

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
pytest -q                       # 413+ tests
ruff check app tests            # linting
mypy app --show-error-codes     # type checking
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
- Test suite: 413 passed, 1 skipped

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
* [ ] Enable output redaction for secrets.
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
app/
  routers/          API routers
  services/         SSH, jobs, audit and integration services
  models/           Data models and schemas
  security.py       Authentication, validation and security helpers
  config.py         Application configuration

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
