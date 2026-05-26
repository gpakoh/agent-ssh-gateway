# Web SSH Gateway

A stateful SSH session runtime with API control for AI agents and automation systems.

## What this IS

A stateful SSH session runtime with API access for AI agents and automation systems.

## What this is NOT

- Not a web SSH client (Guacamole, Sshwifty)
- Not a Kubernetes tool
- Not a full orchestration system
- Not a bastion/jump host (Warpgate)
- Not an infrastructure automation platform

## Mental model

- A session is a persistent API resource stored in PostgreSQL.
- A command is an HTTP request executed over SSH.
- A PTY is a WebSocket stream attached to a session.
- A job is a Redis-backed background execution task.

## Why this exists

AI agents and CI/CD pipelines need SSH access, but raw SSH doesn't fit their model. They need:

- **API-driven** — connect, execute, stream, disconnect over HTTP/WS
- **Stateful** — session survives agent restart (PostgreSQL, Fernet-encrypted)
- **Restart-safe** — reconnect to live sessions without credential re-entry
- **Auditable** — every command is logged; host keys are pinned

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌───────────┐
│  AI Agent   │────▶│  FastAPI     │────▶│  SSH Host │
│  CI/CD      │     │  Gateway     │     │           │
│  API Client │     │  + Paramiko  │     │  (any)    │
└─────────────┘     └──────────────┘     └───────────┘
                         │
                    ┌────┴────┐
                    │         │
               ┌────────┐ ┌───────┐
               │ Redis  │ │ PG   │
               │ (queue)│ │(sess)│
               └────────┘ └───────┘
```

## API at a glance

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/api/capabilities` | Feature flags & config |
| `POST` | `/api/ssh/connect` | Create SSH session |
| `POST` | `/api/ssh/execute` | Execute command (REST) |
| `WS` | `/api/ssh/execute/stream` | Execute command (stream) |
| `WS` | `/api/ssh/pty` | Interactive PTY |
| `POST` | `/api/ssh/disconnect` | Close session |
| `GET` | `/api/ssh/sessions` | List active sessions |
| `GET` | `/api/known-hosts` | List pinned host keys |
| `DELETE` | `/api/known-hosts/{host}` | Remove host key |
| `DELETE` | `/api/known-hosts` | Clear all host keys |

## SDK

Python SDK for AI agents: [`sdk/ssh_gateway.py`](sdk/ssh_gateway.py)

```python
from sdk.ssh_gateway import SSHGateway

gw = SSHGateway("https://ssh.example.com", api_key="...")
session = gw.connect("my-server", username="root", password="...")
output = gw.execute(session, "uptime")
```

## Features

- **API-first SSH** — connect, execute, stream over HTTP/WebSocket
- **Restart-safe sessions** — credentials encrypted at rest (Fernet), survive gateway restart
- **Pluggable host key verification** — file-backed or PostgreSQL store, auto-update on key change
- **3-layer auth** — API key, mTLS (nginx), SSO (Authelia)
- **PTY streaming** — interactive terminal sessions over WebSocket
- **Session management** — list, disconnect, timeout-based cleanup, reconnect
- **Audit logging** — every command recorded
- **Command guardrails** — blocklist for dangerous operations (not a security boundary)
- **Rate limiting** — per-IP, configurable
- **Persistent sessions** — optional PostgreSQL backend restores session context after gateway restart
- **CI/CD integration** — Gitea Actions pipeline with SBOM scanning

## Dependencies

| Component | Role |
|-----------|------|
| FastAPI | HTTP/WS framework |
| Paramiko | SSH protocol |
| PostgreSQL 16 | Session persistence, host key store |
| Redis 7 | Job queue |
| Nginx | TLS termination, mTLS verification, proxy |

## Quick start

```bash
# Start the demo stack with a test SSH server
docker compose --profile demo -f docker/docker-compose.yml up -d

# Connect to the test server
curl -X POST -H "X-API-Key: $API_KEY" \
  -d '{"host":"ssh-gateway-test-sshd","username":"root","password":"test123","port":22}' \
  http://localhost:8085/api/ssh/connect

# Execute a command
curl -X POST -H "X-API-Key: $API_KEY" \
  -d '{"session_id":"<id>","command":"uname -a"}' \
  http://localhost:8085/api/ssh/execute
```

## Security

- **Encryption**: credentials encrypted with Fernet (symmetric, key required at startup)
- **Host key verification**: configurable — `RejectPolicy` (strict) or known_hosts store (warn + auto-update)
- **mTLS**: nginx verifies client certificates before requests reach the gateway
- **Read-only filesystem** in production container
- **No-new-privileges**, all capabilities dropped
- **Rate limiting**: per-IP, CIDR allowlist
- **Command guardrails**: blocklist-based protection against dangerous commands

See [SECURITY.md](SECURITY.md) for threat model and disclosure process.

## Deployment

Production deployment uses Docker Compose with separate containers for the gateway, PostgreSQL, and Redis behind an nginx reverse proxy with mTLS.

```bash
docker compose -p web-ssh-gateway -f docker/docker-compose.yml up -d --build
```

Environment configuration via `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | — | Required. Auth key for API access |
| `ENCRYPTION_KEY` | — | Required if `PERSISTENT_SESSIONS_ENABLED=true` |
| `KNOWN_HOSTS_STORE` | `""` | `file`, `postgres`, or empty (auto-add) |
| `SSH_STRICT_HOST_KEY_CHECKING` | `false` | Reject unknown host keys when `true` |

## Project status

Active development. Public preview — API may change.
