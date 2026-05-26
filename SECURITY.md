# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅ |

## Reporting a Vulnerability

**Do not open a public issue.** Vulnerability reports are handled through GitHub's private disclosure system.

### Preferred: GitHub Security Advisories

Submit via [github.com/gpakoh/web-ssh-gateway/security/advisories/new](https://github.com/gpakoh/web-ssh-gateway/security/advisories/new).  
This creates a private advisory that only the maintainer can see. No registration required beyond a GitHub account.

### Fallback: Email

If you cannot use GitHub, email the maintainer directly (contact details in the Git commit history).  
Encrypt with the maintainer's PGP key if available (published on GitHub profile).

### Disclosure Process

1. **Report received** → acknowledgement within 48 hours
2. **Triage** → initial assessment within 5 business days (confirmed / rejected / needs more info)
3. **Patch** → fix developed and tested (typically 7–14 days for medium severity)
4. **Release** → fixed version published, advisory publicly disclosed
5. **Credit** → reporter acknowledged in release notes (opt-out available)

### Response SLA

| Severity | Initial assessment | Fix target |
|----------|-------------------|------------|
| Critical | 24 hours | 48 hours |
| High | 3 business days | 7 days |
| Medium | 5 business days | 14 days |
| Low | 10 business days | Next release |

### What to include

- Project version and deployment mode (Docker / bare-metal)
- Steps to reproduce (minimal, reproducible test case preferred)
- Impact description (credential leak, RCE, privilege escalation, etc.)
- Optional: suggested fix or mitigation

### Scope

Only the gateway application itself (Python code under `app/`).  
Infrastructure-level issues (Docker, Nginx, PostgreSQL, Redis) should be reported to their respective projects.

No bug bounty program is currently offered. Thank-you credit in release notes is guaranteed.

---

## Security Architecture

### Credential Storage

- SSH passwords and private keys are encrypted at rest using **Fernet** (AES-128-CBC + HMAC-SHA256, keyed via `ENCRYPTION_KEY` environment variable).
- The encryption key must be provided at container startup — it is never embedded in code, config files, or images.
- Credentials are decrypted only in-memory at connection time and never written to disk.
- Encrypted credentials are stored in PostgreSQL. A database compromise without the `ENCRYPTION_KEY` yields no plaintext secrets.

### Transport Security

- **TLS 1.3** is required for production deployments.
- **Mutual TLS (mTLS)** is optional — agents present a client certificate that Nginx validates before proxying requests to the gateway.
- The gateway itself does not terminate TLS in production; it runs behind Nginx which handles SSL termination, mTLS verification, and rate limiting.

### Authentication

Three independent authentication layers, any of which can grant access:

| Layer | Mechanism | Typical User |
|-------|-----------|-------------|
| API Key | `X-API-Key` header, validated at application level | Scripts, CI/CD |
| mTLS | Client certificate validated by Nginx | Automated agents |
| SSO | Authelia / OpenID Connect, cookie-based session | Human operators |

Agent tokens (short-lived, TTL-based) are validated through the API Key path with separate key material.

### Authorization

- **CIDR allowlist** per endpoint — configured via `ALLOWED_CLIENT_CIDRS`.
- **Path traversal protection** — directory traversal (`../`), home shortcuts (`~`), and sensitive system paths (`/etc/passwd`, `/proc`, `/sys`, `/dev`) are blocked on all file operations.
- **Command safety filter** — dangerous shell patterns (`rm -rf /`, pipe-to-`sh`, fork bombs) are rejected before reaching SSH.

### Session Isolation

- Each `session_id` maps to exactly one SSH channel on exactly one target host.
- Sessions are isolated per connection — there is no cross-session access even with a valid API key.
- WebSocket PTY tokens are tied to the session and invalidated on disconnect.
- Stale sessions (5 minutes idle by default) are automatically cleaned.

### Audit Logging

All operations produce structured log entries with event type classification:

- `COMMAND` — executed command, exit code, output size, session_id
- `FILE` — file read/write/edit, target path, diff summary
- `AUTH` — authentication attempt, source IP, auth layer used, result
- `SECURITY` — blocked paths, rate limit threshold hits, CIDR deny events

Logs are emitted via Python's structured logging and can be forwarded to any standard log collector.

### Rate Limiting

All mutation endpoints are rate-limited per source IP:

| Endpoint | Limit |
|----------|-------|
| SSH connect | 10 requests/minute |
| Command execute | 60 requests/minute |
| File edit | 30 requests/minute |
| Bulk operations | 10 requests/minute |
| Batch operations | 20 requests/minute |

---

## Deployment Security Checklist

- [ ] Set a strong `ENCRYPTION_KEY` (64+ random bytes, base64-encoded)
- [ ] Set a strong `API_KEY` (32+ random alphanumeric characters)
- [ ] Enable TLS 1.3 in the reverse proxy
- [ ] Restrict `ALLOWED_CLIENT_CIDRS` to your infrastructure CIDR blocks
- [ ] Use `read_only: true` in the Docker Compose config (enabled by default)
- [ ] Generate separate agent tokens per automated tool rather than sharing `API_KEY`
- [ ] Monitor `AUTH` and `SECURITY` log events for anomalous patterns
- [ ] Keep PostgreSQL and Redis containers on an isolated Docker network (`internal_net`)

---

## Threat Model and Non-Goals

### In scope
- Protect SSH credentials at rest and in transit.
- Prevent cross-session access and command/path abuse.
- Provide auditable security events for incident response.

### Out of scope
- Compromise of the host OS running Docker.
- Full protection against a malicious root user on target SSH hosts.
- Data exfiltration from already-compromised upstream infrastructure.

---

## Key Rotation Runbook

### API_KEY rotation

1. Generate a new strong API key (32+ random alphanumeric characters).
2. Update the application and reverse proxy environment with the new key.
3. Deploy and reload services.
4. Revoke the old key.
5. Verify access logs for failed auth attempts using the retired key.

### ENCRYPTION_KEY rotation

1. Schedule a maintenance window (all sessions will be lost during rotation).
2. Export encrypted credential records from PostgreSQL (backup).
3. Re-encrypt all records with the new key using a migration script.
4. Restart services with the new `ENCRYPTION_KEY`.
5. Validate session restore path: connect, restart container, session survives.
6. Securely retire old key material.

> **Note:** `ENCRYPTION_KEY` rotation is a breaking operation because active sessions hold in-memory decrypted credentials. Plan for session drain before rotation.
