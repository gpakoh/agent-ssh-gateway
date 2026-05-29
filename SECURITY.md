# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please do **not** open a public issue.

Contact: **security@example.com**  
PGP key: _available at https://example.com/.well-known/pgp-key.txt_

We aim to acknowledge receipt within 48 hours and provide an initial assessment within 5 business days.

## Security Measures

- **Encryption at rest**: session credentials encrypted with Fernet (symmetric key required at startup)
- **Host key verification**: `RejectPolicy` by default (`SSH_STRICT_HOST_KEY_CHECKING=true`)
- **mTLS**: nginx verifies client certificates before requests reach the gateway
- **Rate limiting**: per-IP, configurable limits with CIDR allowlist
- **Fail-closed host keys**: unknown host keys rejected in strict mode
- **CIDR allowlist**: `ALLOWED_CLIENT_CIDRS` restricts which IPs can connect
- **Read-only root filesystem** in production container
- **No-new-privileges**, all capabilities dropped
- **Command guardrails**: blocklist-based (not a security boundary — see README)

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.x     | ✔️        |

## Threat Model

This gateway is designed for **trusted network environments** (internal VPC, CI/CD runners).  
It is not intended for direct exposure to the public internet without additional controls (mTLS, CIDR restrictions, WAF).
