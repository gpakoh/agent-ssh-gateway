# Security Policy

## Supported versions

Current active development version: `main`.

## Security model

agent-ssh-gateway is a sensitive infrastructure component. It can open SSH sessions and execute commands on remote hosts.

Do not expose it directly to the Internet without authentication, network restrictions and reverse proxy protection.

## Recommended deployment

- Reverse proxy
- SSO for browser access
- API key or short-lived agent tokens for API access
- Target host allowlist
- Command policy
- Audit logging
- Secret redaction
- Least-privilege SSH users

## Secrets

Never commit:

- `.env`
- private SSH keys
- API keys
- agent tokens
- webhook secrets
- production IPs/domains if they reveal private infrastructure

## Agent tokens

Agent tokens are scoped and short-lived. They cannot create or refresh other agent tokens.

## Session ownership

Agent-created sessions are bound to the token fingerprint that created them.

## Reporting vulnerabilities

Open a private security advisory on GitHub or contact the maintainer.
