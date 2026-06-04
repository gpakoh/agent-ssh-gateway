# Security Policy

## Security status

This project is an early self-hosted MVP.

It is designed for private/internal environments and should not be exposed directly to the public Internet without an additional trusted reverse proxy, TLS termination, network-level restrictions, and strong operational controls.

Security review is ongoing.

## Supported versions

Current active development version: `main`.

## Token model

The gateway uses two types of tokens for API authentication.

### Master token

- Administrative access.
- Can create, list, and manage all SSH sessions, agent tokens, event hooks, and templates.
- Intended for internal platform operators or CI/CD pipelines.

### Agent token

- Scoped and short-lived.
- Cannot create or refresh other agent tokens.
- Bound to a set of scopes that restrict which API actions it can perform (e.g., `ssh:execute`, `ssh:files`, `ssh:admin`).
- Agent-created sessions are bound to the token fingerprint that created them.

### Session ownership

Agent tokens are not intended to access sessions created by other tokens. Session ownership checks are enforced for:

- HTTP execute, disconnect, heartbeat, health, and env endpoints
- HTTP file read, edit, write, upload, download, and patch operations
- WebSocket execute stream
- WebSocket PTY stream
- File watch stream
- Session listing (agents see only their own sessions)

Scopes restrict API surface but do not replace ownership checks. Ownership is enforced at the session level regardless of scope.

## Threat model

### In scope

- Accidental cross-agent session access — prevented by session ownership checks.
- Unauthorized session use via leaked `session_id` without a matching token — ownership verified on every session-bound request.
- Basic command execution authorization through token scopes.
- Target host restrictions via `ALLOWED_TARGET_CIDRS` and `DENIED_TARGET_CIDRS`.
- Private key upload disabled by default (`SSH_KEY_UPLOAD_ENABLED=false`).
- Audit logging of failed authentication and denied ownership attempts.

### Out of scope / not guaranteed

- Protection after host OS compromise — if the host running the gateway is compromised, all sessions and credentials are exposed.
- Protection if the master token is leaked — the master token has full administrative access.
- Full sandboxing of commands on remote SSH hosts — command execution on remote hosts is subject to remote host policy, not the gateway's.
- Multi-tenant enterprise isolation — the gateway does not provide hard tenant boundaries.
- Public Internet exposure without additional controls — a reverse proxy, TLS, and network restrictions are required.
- Protection from malicious administrators — anyone with host-level access can bypass gateway controls.

## Deployment hardening checklist

- Run behind TLS. Do not expose the service directly to the public Internet.
- Use long random tokens for `API_KEY`, `AGENT_TOKEN`, and any webhook secrets.
- Store tokens in environment variables or a secrets manager. Never commit `.env`.
- Rotate tokens after suspected exposure.
- Keep `SSH_KEY_UPLOAD_ENABLED=false` unless you explicitly need the feature.
- Restrict SSH targets with `ALLOWED_TARGET_CIDRS` and `DENIED_TARGET_CIDRS`.
- Prefer non-root containers with read-only filesystems and dropped capabilities.
- Use firewall rules or private networks to limit access to the gateway port.
- Review audit logs for failed authentication and denied session ownership attempts.
- Keep the gateway and its dependencies up to date.

## Secrets

Never commit:

- `.env`
- private SSH keys
- API keys
- agent tokens
- webhook secrets
- production IPs/domains if they reveal private infrastructure

## Reporting vulnerabilities

Please do not open public GitHub issues for security vulnerabilities.

Report vulnerabilities using GitHub Security Advisories when available, or contact the maintainer through the repository profile.
