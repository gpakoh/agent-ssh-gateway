# Notifier Operator Runbook

Gateway Telegram notifier sidecar — opt-in, dry-run by default.

## Architecture

```
gateway-notifier (sidecar)
  → polls GET /health + /api/admin/audit/recent
  → sends Telegram alerts (dry_run=true by default)
  → tracks health transitions (ok ↔ degraded/recovered)
```

The notifier is a **separate container** that runs alongside the gateway. It is NOT included in the main `docker-compose.yml`. It is deployed via the overlay `docker-compose.notifier.yml`.

## Quick Start

### 1. Dry-run smoke (safe, no Telegram sends)

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway

# Run dry-run smoke locally (no Docker required)
python3 scripts/notifier_dry_run_smoke.py
```

This reads gateway `/health` + audit recent. Telegram always dry_run.

### 2. Enable via overlay

```bash
# Start notifier with overlay (dry-run by default)
docker compose -f docker/docker-compose.yml -f docker/docker-compose.notifier.yml up -d gateway-notifier

# Check logs
docker logs -f gateway-notifier
```

### 3. Enable real Telegram sends

Set these env vars before starting:

```bash
export GATEWAY_NOTIFIER_ENABLED=true
export GATEWAY_NOTIFIER_DRY_RUN=false          # ⚠️ sends real messages
export GATEWAY_NOTIFIER_API_KEY=<your-api-key>
export GATEWAY_NOTIFIER_TELEGRAM_TOKEN=<bot-token>
export GATEWAY_NOTIFIER_CHAT_IDS=<chat-id>
```

Then restart:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.notifier.yml up -d gateway-notifier
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_NOTIFIER_ENABLED` | `false` | Master switch |
| `GATEWAY_NOTIFIER_DRY_RUN` | `true` | Telegram sends are logged, not delivered |
| `GATEWAY_NOTIFIER_GATEWAY_URL` | `http://web-ssh-gateway:8085` | Gateway URL |
| `GATEWAY_NOTIFIER_API_KEY` | (empty) | API key for gateway admin endpoints |
| `GATEWAY_NOTIFIER_TELEGRAM_TOKEN` | (empty) | Telegram bot token |
| `GATEWAY_NOTIFIER_CHAT_IDS` | (empty) | Comma-separated Telegram chat IDs |
| `GATEWAY_NOTIFIER_POLL_INTERVAL_SECONDS` | `5` | Polling interval |
| `GATEWAY_NOTIFIER_TIMEOUT_SECONDS` | `10` | HTTP timeout |
| `GATEWAY_NOTIFIER_EVENT_TYPES` | `command.deny,workspace.readonly_block,session.connect,session.disconnect,system.error` | Events to notify |

## Health Transitions

The notifier tracks gateway health state transitions:

- **ok → degraded/recovered**: sends `health.degraded` alert
- **degraded/recovered → ok**: sends `health.recovered` alert
- **non-ok → non-ok** (e.g. unreachable → degraded): no notification (avoids alert spam)

First poll records baseline — no notification sent.

## Alert Safety

- All alert fields go through `redact_secrets()` before sending
- No hostnames, IPs, raw commands, paths, or secrets in alert text
- Event types are a bounded set (5 types by default)

## Rollback / Off-Switch

```bash
# Stop notifier (gateway continues unaffected)
docker compose -f docker/docker-compose.yml -f docker/docker-compose.notifier.yml stop gateway-notifier

# Remove notifier completely
docker compose -f docker/docker-compose.yml -f docker/docker-compose.notifier.yml rm gateway-notifier

# Or just disable via env (container stays, no alerts sent)
docker compose -f docker/docker-compose.yml -f docker/docker-compose.notifier.yml \
  -e GATEWAY_NOTIFIER_ENABLED=false up -d gateway-notifier
```

## First Real Telegram Send (Manual Gate)

Before enabling real Telegram delivery:

1. Run dry-run smoke: `python3 scripts/notifier_dry_run_smoke.py`
2. Verify gateway health: `curl http://localhost:8085/health`
3. Set `GATEWAY_NOTIFIER_DRY_RUN=false` with real token + chat ID
4. Start overlay: `docker compose ... up -d gateway-notifier`
5. Trigger a test event (e.g. `command.deny`)
6. Verify Telegram message arrives
7. If issues: set `GATEWAY_NOTIFIER_DRY_RUN=true` or stop the container

**No auto-deploy for first real send.** This is a manual gate.

## Compose Contract

- Main `docker/docker-compose.yml` does NOT contain `gateway-notifier`
- Overlay `docker/docker-compose.notifier.yml` adds `gateway-notifier` with dry-run defaults
- Overlay is applied manually: `-f docker-compose.notifier.yml`

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Notifier exits immediately | `GATEWAY_NOTIFIER_ENABLED=false` | Set `GATEWAY_NOTIFIER_ENABLED=true` |
| No Telegram messages | `DRY_RUN=true` | Set `DRY_RUN=false` + provide token/chat IDs |
| `gateway_notifier_not_ready` | Missing API key or gateway URL | Set `GATEWAY_NOTIFIER_API_KEY` and `GATEWAY_NOTIFIER_GATEWAY_URL` |
| `health.degraded` alert | Gateway health non-ok | Check gateway: `curl http://localhost:8085/health` |
| No health alerts | First poll (baseline) | Wait for next poll cycle |
