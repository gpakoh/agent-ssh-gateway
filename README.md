# agent-ssh-gateway

**A self-hosted control plane for policy-controlled access from AI agents and automation to remote infrastructure over SSH.**

![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![status](https://img.shields.io/badge/status-alpha-orange)
[![MCP](https://img.shields.io/badge/interface-MCP-7B2FF7?logo=modelcontextprotocol)](https://modelcontextprotocol.io)

`agent-ssh-gateway` exposes SSH operations through structured HTTP/OpenAPI and
Model Context Protocol (MCP) interfaces. Instead of giving every agent, CI job,
or internal tool raw SSH credentials and an unrestricted shell, the gateway
centralizes sessions, permissions, policies, execution, and audit data.

It supports remote operations, project-scoped engineering workflows, background
jobs, repository and infrastructure integrations, and coordinated agent
handoffs.

> [!WARNING]
> This project is an alpha release for private and internal environments. Do not
> expose it directly to the public Internet. Read [SECURITY.md](SECURITY.md)
> before deployment.

## Why use a gateway?

Direct SSH works well for humans, but becomes difficult to control across
agents and automation:

- credentials are copied between clients;
- long-running commands lack a shared job lifecycle;
- file, Git, test, and infrastructure actions are hidden inside shell scripts;
- access policies and audit trails are inconsistent;
- a generic shell grants more authority than most tasks require.

The gateway provides one controlled boundary between clients and remote
systems. Clients receive task-oriented capabilities; operators retain control
over credentials, targets, scopes, and execution policy.

## Capabilities

| Area | What the gateway provides |
|---|---|
| Remote operations | Persistent SSH sessions, structured command execution, argv-safe execution, WebSocket terminal, file transfer, and server profiles |
| Jobs and observability | Background jobs, status and result retrieval, output streaming, health checks, metrics, audit events, and event hooks |
| Project workflows | Scoped file read/search/write, Git inspection, diffs, tests, linting, type checks, preview, hash verification, and patch application |
| MCP integrations | Gateway tools plus GitHub, Gitea, Docker, PostgreSQL, and Context7 adapters |
| Agent coordination | Structured task handoff, isolated worktrees, agent status/report artifacts, and controlled runner workflows |
| Access control | Master and short-lived agent tokens, scopes, access profiles, session ownership, target allowlists, command policies, and confirmation flows |

Tool visibility depends on the selected MCP mode and access profile. The project
ships minimal, standard, full, and ChatGPT-oriented modes, including a safe mode
that removes mutation, privileged Docker, and agent-launch tools.

## Architecture

```text
AI agents / CI/CD / internal tools
                |
          HTTP/OpenAPI or MCP
                |
        agent-ssh-gateway
        |       |        |
   policies   jobs     audit
        |
   approved SSH targets
```

Optional MCP adapters expose repository providers, Docker, PostgreSQL, and
documentation lookup through the same control-plane model.

## Security boundaries

The gateway is designed to reduce authority, not to make unrestricted remote
execution inherently safe.

- SSH targets can be restricted with allow and deny CIDRs.
- Commands can be evaluated against `readonly`, `testlint`,
  `project-automation`, `ops`, or other policy profiles.
- Agent tokens are scoped, short-lived, and isolated by session ownership.
- Project tools resolve paths under registered roots and reject traversal.
- Dangerous Docker operations require an explicit confirmation flow.
- Secret redaction can be enabled for returned command and job output.
- Workspace mutation is disabled by default.

The gateway is not a replacement for Teleport, an enterprise zero-trust
platform, a hardened multi-tenant sandbox, or a browser-based SSH client.

See [SECURITY.md](SECURITY.md) for the threat model and deployment checklist.

## Quickstart

Requirements:

- Python 3.11 or newer;
- access to an SSH target;
- Redis and PostgreSQL only when their optional persistence features are used.

Clone and install:

```bash
git clone https://github.com/gpakoh/agent-ssh-gateway.git
cd agent-ssh-gateway

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
```

Generate strong values for `API_KEY`, `AGENT_TOKEN`, and `JWT_SECRET`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Generate `ENCRYPTION_KEY`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add the generated values to `.env`, review the allowed target networks, and
start the API:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8085
```

Verify the service:

```bash
curl http://127.0.0.1:8085/health
```

The authenticated OpenAPI UI is available at:

```text
http://127.0.0.1:8085/docs
```

For Docker Compose and private deployment overlays, see
[Deployment overlays](docs/operations/DEPLOYMENT_OVERLAYS.md).

## Minimal REST workflow

Set the master API key:

```bash
export API_KEY="<your-api-key>"
```

Create an SSH session:

```bash
curl -X POST http://127.0.0.1:8085/api/ssh/connect \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "host": "your-server",
    "username": "automation-user",
    "password": "your-password"
  }'
```

Use the returned `session_id` to execute a command:

```bash
curl -X POST http://127.0.0.1:8085/api/ssh/execute \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "<session-id>",
    "command": "uname -a"
  }'
```

For long-running work, pass `"async_mode": true` and use the job status, result,
wait, or stream endpoints.

For agent clients, create a scoped agent token instead of sharing the master
key.

## MCP

The MCP server turns gateway operations into task-oriented tools for compatible
AI clients. Tool modes control visibility; token scopes and command policies
control authority.

Typical MCP workflows include:

- inspecting projects and Git state;
- reading, searching, previewing, and verifying files;
- running tests, Ruff, and mypy;
- inspecting GitHub or Gitea repositories;
- querying Docker and PostgreSQL through guarded tools;
- coordinating structured agent tasks and reviewing their results.

Start with:

- [MCP server guide](examples/mcp_server/README.md)
- [Remote MCP adapter](examples/mcp_client_remote/README.md)
- [MCP operator runbook](docs/operations/MCP_OPERATOR_RUNBOOK.md)
- [ChatGPT attachment guide](docs/operations/CHATGPT_TOOL_ATTACH.md)

## Agent handoff

Agent Handoff provides a file-based lifecycle for delegating independent tasks
to coding agents:

```text
create task -> run agent -> inspect status/report/diff -> review -> archive
```

Each task records its agent, allowed and forbidden files, worktree, and
commit/push permissions. Runner tools are excluded from ChatGPT safe mode and
must be enabled deliberately.

See the [Agent Handoff runbook](docs/operations/AGENT_HANDOFF_RUNBOOK.md).

## Core configuration

Copy `.env.example` and review every security-sensitive value.

| Variable | Purpose |
|---|---|
| `API_KEY` | Master API credential |
| `AGENT_TOKEN` / `AGENT_TOKEN_SCOPES` | Restricted automation credential and scopes |
| `ALLOWED_TARGET_CIDRS` / `DENIED_TARGET_CIDRS` | Networks available as SSH targets |
| `COMMAND_POLICY_MODE` | `off`, `audit`, or `enforce` |
| `COMMAND_POLICY_PROFILE` | Command capability profile |
| `WORKSPACE_READONLY` | Global gate for workspace mutation |
| `COMMAND_OUTPUT_REDACTION_ENABLED` | Best-effort response redaction |
| `SSH_STRICT_HOST_KEY_CHECKING` | SSH host identity verification |
| `ENCRYPTION_KEY` | Encryption of persisted session credentials |

`.env.example` is the canonical configuration reference. Never commit real
tokens, private keys, infrastructure addresses, or deployment overlays.

## Development

```bash
source .venv/bin/activate
pip install -e ".[dev]"

pytest -q
ruff check .
mypy app
python -m compileall app examples
```

Host-dependent smoke tests are marked separately and are not run in portable
CI:

```bash
pytest -m host_smoke -v
```

## Documentation

- [Security policy and threat model](SECURITY.md)
- [Practical REST API guide](SSH_GATEWAY_GUIDE.md)
- [MCP server](examples/mcp_server/README.md)
- [Deployment overlays](docs/operations/DEPLOYMENT_OVERLAYS.md)
- [Audit logging](docs/operations/AUDIT_LOGGING.md)
- [Access control](docs/operations/ACCESS_CONTROL.md)
- [Notifier](docs/operations/NOTIFIER.md)
- [Maintainer workflows](docs/OSS_MAINTAINER_WORKFLOWS.md)
- [Architecture decisions](docs/architecture/)
- [Changelog](CHANGELOG.md)
- [Roadmap](docs/roadmap.md)

## Project status

`agent-ssh-gateway` is under active development and its public interfaces may
change before version 1.0. Production use requires an independent security
review and environment-specific hardening.

## License

MIT License.
