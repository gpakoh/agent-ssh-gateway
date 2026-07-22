# Phase 7 - Gateway Telegram Notifier

## Goal

Add a Telegram notifier for **web-ssh-gateway** only. This is not the separate VPN bot service and must not touch neighboring repositories.

The notifier is an optional sidecar that observes gateway metadata and sends operator alerts. It never executes SSH commands, never mutates workspace files, and never receives RW privileges.

## Architecture

```mermaid
flowchart LR
  G[web-ssh-gateway] --> A[/api/admin/audit/recent]
  G --> H[/health and /metrics]
  A --> N[gateway-notifier sidecar]
  H --> N
  N --> T[Telegram Bot API]
```

## Phase 7.1 scope

- Add `app/notifier/*` sidecar modules.
- Poll `/api/admin/audit/recent` using a gateway API key.
- Format safe alerts for existing audit events:
  - `command.deny`
  - `workspace.readonly_block`
  - `session.connect`
  - `session.disconnect`
  - `system.error`
- Telegram delivery is dry-run by default.
- Add `docker/docker-compose.notifier.yml` dry-run sidecar overlay (not used by default).

## Safety contract

Telegram messages may include only metadata:

- event type
- decision
- command root
- route
- profile
- error code
- request id
- actor fingerprint
- short reason

Telegram messages must not include:

- raw command text
- stdout/stderr
- file content or patches
- hostnames/IPs
- full paths
- API keys/tokens/passwords

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `GATEWAY_NOTIFIER_ENABLED` | `false` | Enable sidecar loop |
| `GATEWAY_NOTIFIER_DRY_RUN` | `true` | Do not call Telegram when true |
| `GATEWAY_NOTIFIER_GATEWAY_URL` | `http://localhost:8085` | Gateway base URL |
| `GATEWAY_NOTIFIER_API_KEY` | empty | Gateway read/admin key for audit endpoint |
| `GATEWAY_NOTIFIER_TELEGRAM_TOKEN` | empty | Telegram bot token |
| `GATEWAY_NOTIFIER_CHAT_IDS` | empty | Comma-separated allowlisted chats |
| `GATEWAY_NOTIFIER_POLL_INTERVAL_SECONDS` | `5` | Poll cadence |
| `GATEWAY_NOTIFIER_EVENT_TYPES` | security subset | Comma-separated event type allowlist |

## Future slices

- Emit structured audit events for `session.connect` and `session.disconnect` directly in the audit ring buffer.
- Add `/status`, `/audit`, `/silence`, and `/digest` bot commands.
- Add deploy/release notifications.
- Promote the compose overlay into deploy only after dry-run smoke passes.
