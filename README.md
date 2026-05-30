# agent-ssh-gateway

**OpenAPI-first SSH control plane for AI agents, CI/CD pipelines and self-hosted infrastructure automation.**

`agent-ssh-gateway` gives automation tools, internal services and AI agents a structured, auditable and policy-controlled way to access remote machines over SSH.

It is not just a browser terminal.
It is an API layer between your agents and your servers.

```text
AI Agent / CI Runner / Internal Tool
              ↓
        HTTP API / SDK
              ↓
 Policy • Audit • Sessions • Jobs
              ↓
          SSH Targets
```

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

Examples:

* inspect a remote service;
* read logs;
* restart a container;
* check disk usage;
* run deployment commands;
* collect diagnostics.

### CI/CD pipelines

Use the gateway as a central SSH execution layer for build and deployment jobs.

Examples:

* deploy to a remote host;
* run migrations;
* upload release artifacts;
* restart services;
* collect post-deploy status.

### Internal infrastructure tools

Build dashboards, admin panels and automation services on top of a single SSH API.

Examples:

* one-click maintenance actions;
* controlled server operations;
* internal support tools;
* repeatable operational playbooks.

### Self-hosted environments

Useful for homelabs, small infrastructure clusters, internal DevOps setups and private automation platforms.

---

## Key features

* **API-first design**
  SSH operations are exposed through a documented HTTP API.

* **OpenAPI contract**
  Designed to be usable by agents, SDKs and generated clients.

* **Persistent SSH sessions**
  Create, reuse and close sessions through API calls.

* **Command execution**
  Run commands remotely and capture structured results.

* **WebSocket terminal**
  Optional interactive terminal access through the browser or custom clients.

* **Background jobs**
  Run longer tasks without blocking the initial API request.

* **File operations**
  Upload, download and manage files over SSH.

* **Agent tokens**
  Use short-lived tokens for automation instead of long-lived master credentials.

* **Audit logging**
  Track who connected, where, when and what was executed.

* **Event hooks**
  Send structured events to external systems.

* **Security-focused deployment model**
  Designed to run behind SSO, reverse proxy, mTLS, API keys and network policies.

---

## What this project is not

`agent-ssh-gateway` is not intended to replace mature enterprise access platforms such as Teleport or Apache Guacamole.

It is also not just another web SSH terminal.

The goal is different:

> provide a lightweight, self-hosted SSH control plane for agents, automation and internal infrastructure workflows.

If you only need a browser-based SSH client, this may be more than you need.

If you need controlled SSH access for automation, agents and CI/CD, this project is designed for that.

---

## Example workflow

```bash
# 1. Create SSH session
curl -X POST https://gateway.example.com/api/ssh/connect \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "host": "10.0.0.25",
    "port": 22,
    "username": "deploy"
  }'

# 2. Execute command
curl -X POST https://gateway.example.com/api/ssh/sessions/<session_id>/exec \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "command": "docker ps"
  }'
```

---

## Example agent use

An AI agent or automation service can use the gateway like this:

```text
1. Request short-lived agent token
2. Create SSH session to an allowed target
3. Run diagnostic command
4. Parse structured output
5. Trigger remediation job if needed
6. Store audit trail
7. Close session
```

This gives agents a safer operational boundary than direct SSH access.

---

## Security model

SSH gateways are sensitive infrastructure components.

Do not expose this service directly to the Internet without proper protection.

Recommended deployment:

```text
Internet
   ↓
Reverse Proxy
   ↓
SSO / Authelia / OAuth2 Proxy
   ↓
mTLS / API Key / Agent Token
   ↓
agent-ssh-gateway
   ↓
Allowed SSH Targets
```

Recommended protections:

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

## Target allowlist

A production deployment should restrict which hosts the gateway can access.

Example:

```env
ALLOWED_TARGET_CIDRS=10.0.0.0/8,192.168.0.0/16,172.16.0.0/12
DENIED_TARGET_CIDRS=127.0.0.0/8,::1/128,169.254.0.0/16,0.0.0.0/8,224.0.0.0/4
```

This prevents the gateway from becoming an internal port scanner or SSRF-style pivot.

---

## Configuration

Create an `.env` file from the example:

```bash
cp .env.example .env
```

Minimal configuration:

```env
API_KEY=change-me-to-a-long-random-secret
ENCRYPTION_KEY=change-me
DATABASE_URL=postgresql://gateway:gateway@postgres:5432/gateway
REDIS_URL=redis://redis:6379/0

ALLOWED_CLIENT_CIDRS=10.0.0.0/8,192.168.0.0/16
ALLOWED_TARGET_CIDRS=10.0.0.0/8,192.168.0.0/16
DENIED_TARGET_CIDRS=127.0.0.0/8,::1/128,169.254.0.0/16
```

Never commit real `.env` files.

---

## Running with Docker Compose

```bash
docker compose up -d
```

Then check service health:

```bash
curl http://localhost:8085/health
```

OpenAPI schema should be available at:

```text
http://localhost:8085/openapi.json
```

Interactive API documentation may be available at:

```text
http://localhost:8085/docs
```

depending on your deployment settings.

---

## Development

Install dependencies:

```bash
pip install -r requirements.txt
```

Run tests:

```bash
pytest -q
```

Run the application locally:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8085 --reload
```

---

## Suggested production checklist

Before using this in production:

* [ ] Change all default secrets.
* [ ] Put the service behind a reverse proxy.
* [ ] Enable SSO for browser access.
* [ ] Use API keys or short-lived tokens for automation.
* [ ] Configure allowed client networks.
* [ ] Configure allowed target networks.
* [ ] Deny loopback, link-local and metadata IP ranges.
* [ ] Use dedicated low-privilege SSH users.
* [x] Private key upload disabled by default (`SSH_KEY_UPLOAD_ENABLED=false`).
* [ ] Enable audit logging.
* [ ] Enable output redaction for secrets.
* [ ] Limit command execution by role or profile.
* [ ] Rotate tokens regularly.
* [ ] Review event hooks before enabling command output forwarding.
* [ ] Keep deployment-specific files out of the public repository.

---

## Roadmap

Planned or recommended improvements:

* target host allowlist and denylist;
* role-based command policies;
* stronger agent token lifecycle;
* command output redaction;
* secret-safe audit logs;
* safer file transfer policies;
* session recording;
* job queue improvements;
* SDK examples;
* Gitea/GitHub Actions examples;
* MCP/AI-agent integration examples;
* production hardening guide.

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
  docker-compose.example.yml
  Dockerfile

tests/
  Unit and integration tests

docs/
  Deployment and security documentation
```

---

## Public repository hygiene

This repository should contain only generic example configuration.

Do not commit:

* real `.env` files;
* private SSH keys;
* API keys;
* agent tokens;
* webhook secrets;
* production IP addresses;
* internal domains;
* real reverse proxy configs;
* customer data;
* deployment files containing private infrastructure details.

Keep real deployment configuration in a private repository or secret manager.

---

## License

MIT License.

---

## Project status

Early-stage but functional.

The project is suitable for experimentation, internal automation and controlled self-hosted environments.

Production use requires careful security review, strict network policies and secret management.

---

## Short description

**agent-ssh-gateway** is a lightweight self-hosted SSH control plane that lets AI agents, CI/CD pipelines and internal tools execute SSH operations through a controlled, auditable OpenAPI-based gateway.
