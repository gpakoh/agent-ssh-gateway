# Event Hooks — Design Spec v2

## Overview

External event notification system for SSH session lifecycle events. AI agents register callback URLs that receive signed, retried HTTP POSTs when commands finish, sessions connect/disconnect.

Independent from existing CI/CD webhooks at `/api/webhooks/*`.

## Events

| Event | Emitted when | Payload extras |
|-------|-------------|----------------|
| `session.connected` | SSH session established | host, port, username |
| `session.disconnected` | Session closed | reason, connected_seconds |
| `command.started` | Command begins | command |
| `command.completed` | Exit code 0 | command, exit_code, duration, stdout?, stderr? |
| `command.failed` | Exit code != 0 | command, exit_code, duration, stdout?, stderr? |

## API — `/api/event-hooks` (tag: `event-hooks`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/event-hooks` | Create hook |
| `GET` | `/api/event-hooks` | List all |
| `GET` | `/api/event-hooks/{id}` | Get one |
| `PATCH` | `/api/event-hooks/{id}` | Partial update |
| `DELETE` | `/api/event-hooks/{id}` | Delete |

## Data Model — `event_hooks` table

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID (PK) | auto |
| `url` | str | target URL |
| `events` | JSON | list of event type strings |
| `session_id` | str\|null | filter to one session |
| `headers_encrypted` | bytes\|null | Fernet-encrypted custom HTTP headers |
| `secret_encrypted` | bytes\|null | Fernet-encrypted secret for HMAC signing |
| `include_output` | bool | default false |
| `is_active` | bool | soft-disable |
| `created_at` | datetime | auto |
| `updated_at` | datetime | auto |

## Outbox — `webhook_deliveries` table

One row per logical delivery; `attempts` counter tracks retries. `event_id` is the dedup key (same UUID across retries), `delivery_id` is the PK.

| Field | Type | Notes |
|-------|------|-------|
| `delivery_id` | UUID (PK) | auto |
| `event_id` | UUID | dedup key, same across retries |
| `hook_id` | UUID | FK to event_hooks |
| `event_type` | str | e.g. `command.completed` |
| `payload_json` | str | serialised payload |
| `status` | enum | `pending` / `sent` / `failed` / `dead` |
| `attempts` | int | 0-based, incremented on each try |
| `next_retry_at` | datetime\|null | next scheduled attempt |
| `last_error` | str\|null | error message |
| `http_status` | int\|null | response HTTP code |
| `leased_by` | str\|null | worker instance ID |
| `leased_at` | datetime\|null | when lease acquired |
| `created_at` | datetime | auto |
| `updated_at` | datetime | auto |

Index: `(status, next_retry_at)` for worker polling.

## Delivery Semantics — at-least-once

1. Event fires → delivery row created synchronously (status=`pending`)
2. Background worker polls for `pending` (age > 2s, to avoid races) and `failed` where `next_retry_at < now()`
3. Worker acquires lease via `FOR UPDATE SKIP LOCKED`, sets `leased_by` + `leased_at` (lease TTL: 30s)
4. HTTP POST sent; success → `sent`; failure → `failed`, `attempts`++, `next_retry_at` calculated
5. After `max_attempts` → `dead`, no more retries
6. Stale leases (expired) are reclaimed by any worker next poll cycle

## HTTP Status → Action Mapping

| Response | Action |
|----------|--------|
| 2xx | status=`sent`, done |
| 429 | status=`failed`, retry (rate-limited, backoff respects Retry-After) |
| 5xx | status=`failed`, retry |
| Timeout / DNS / connection error | status=`failed`, retry |
| 4xx (except 429) | status=`dead` (client error, retry won't help) |

## Retry Policy

| Param | Default | Notes |
|-------|---------|-------|
| `max_attempts` | 5 | incl. first attempt |
| `base_delay` | 2s | exponential backoff |
| `max_delay` | 300s | cap |
| jitter | ±50% | randomise |
| Formula | `min(base * 2^attempt, max) * random(0.5, 1.5)` |

## SSRF Protection

- Allowed schemes: `https://` only (`http://` allowed if `EVENT_HOOKS_ALLOW_HTTP=true`)
- Blocked IP ranges (checked every delivery):
  - Loopback: `127.0.0.0/8`, `::1/128`
  - Private: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
  - Link-local: `169.254.0.0/16`, `fe80::/10`
  - Metadata: `169.254.169.254/32`
  - Multicast: `224.0.0.0/4`, `ff00::/8`
- `allow_redirects=False` (aiohttp)
- IP re-resolved on each delivery (DNS rebinding protection)
- Blocked URL returns 422 with `EVENT_HOOK_URL_BLOCKED`

## Signing — HMAC-SHA256

Headers on every delivery:
- `X-Event-Id` — UUID
- `X-Delivery-Id` — UUID
- `X-Webhook-Timestamp` — Unix timestamp
- `X-Webhook-Signature` — `sha256=hex(signature)`

Signature payload: `{timestamp}.{body}` signed with HMAC-SHA256 using hook's `secret`. If no secret configured, signature header omitted.

Receiver verifies: recompute `sha256=hmac(secret, timestamp + "." + body)` and compare.

## Payload Format

```json
{
  "event": "command.completed",
  "event_id": "0195f2e1-...",
  "event_version": 1,
  "timestamp": "2026-05-28T12:00:00Z",
  "session_id": "abc-123",
  "host": "10.0.0.1",
  "port": 22,
  "username": "root",
  "command": "uptime",
  "exit_code": 0,
  "duration": 1.23,
  "stdout": " 12:00:00 up 30 days",
  "stderr": "",
  "output_truncated": false
}
```

`stdout`/`stderr` included only when `include_output=true`, truncated at `EVENT_HOOKS_MAX_OUTPUT_BYTES` (default 65536). `output_truncated=true` if truncated.

## Wiring into existing flow

Emission points (both REST and WebSocket paths):

| File | Function | Event |
|------|----------|-------|
| `ssh_manager.create_session()` | after connect | `session.connected` |
| `ssh_manager.disconnect()` | before cleanup | `session.disconnected` |
| `ssh_manager.execute()` | before/after exec | `command.started / completed / failed` |
| `main.py` `execute_stream()` WS | before/after exec | `command.started / completed / failed` |

The WebSocket `execute/stream` handler calls the same underlying `SSHSessionManager` method — events emit from the manager, not the route handler, so both paths are covered.

`emit_event(event_type, session_data, command_data=None)`:
1. Query active hooks matching `event_type` + `session_id`
2. Build payload
3. Create `webhook_deliveries` rows (status=`pending`)
4. Return immediately (outbox created synchronously)
5. Background worker picks up pending deliveries

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `EVENT_HOOKS_ENABLED` | `true` | master switch |
| `EVENT_HOOKS_MAX` | `50` | max hooks per gateway |
| `EVENT_HOOKS_TIMEOUT_CONNECT` | `5` | connect timeout (s) |
| `EVENT_HOOKS_TIMEOUT_READ` | `10` | read timeout (s) |
| `EVENT_HOOKS_MAX_ATTEMPTS` | `5` | retry attempts |
| `EVENT_HOOKS_RETRY_BASE_SEC` | `2` | base delay |
| `EVENT_HOOKS_RETRY_MAX_SEC` | `300` | max delay |
| `EVENT_HOOKS_MAX_OUTPUT_BYTES` | `65536` | truncation limit |
| `EVENT_HOOKS_ALLOW_HTTP` | `false` | allow http:// URLs |
| `EVENT_HOOKS_POLL_INTERVAL` | `5` | retry scheduler interval (s) |
| `EVENT_HOOKS_RETENTION_SENT_DAYS` | `7` | delete sent deliveries after |
| `EVENT_HOOKS_RETENTION_DEAD_DAYS` | `30` | delete dead deliveries after |
| `EVENT_HOOKS_LEASE_TTL` | `30` | worker lease TTL (s) |

## Security

- Headers/secret encrypted at rest via `SecretManager`
- Log masking for: `Authorization`, `X-API-Key`, `Cookie`, `Set-Cookie`, `X-Webhook-Signature`
- No credentials in payload
- SSRF protection on every delivery (re-resolve IP)

## Outbox Retention

Background cleanup runs every hour:

| Status | Retention | Action |
|--------|-----------|--------|
| `sent` | 7 days | DELETE |
| `dead` | 30 days | DELETE |
| `pending` / `failed` | — | kept (worker retries) |

Configurable via `EVENT_HOOKS_RETENTION_SENT_DAYS` (default 7) and `EVENT_HOOKS_RETENTION_DEAD_DAYS` (default 30).

## Observability

Prometheus metrics (existing `metrics.py`):
- `event_hook_deliveries_total{status,event}` — counter
- `event_hook_delivery_attempts_total` — counter
- `event_hook_delivery_latency_ms` — histogram
- `event_hook_dead_letter_count` — gauge

Structured logging with `event_id`, `delivery_id`, `hook_id` on every delivery attempt.

## File Structure

```
app/
  event_hook_store.py       — EventHookStore: CRUD over SQLAlchemy + filtering
  event_hook_delivery.py    — DeliveryService: outbox, retry scheduler, HTTP send
  event_hook_emitter.py     — emit_event(): create deliveries, wire into ssh_manager
  event_hook_security.py    — SSRF checker, URL validator, HMAC signer, log masker
  routers/
    event_hooks.py          — 5 CRUD endpoints
  models.py                 — pydantic models (EventHookCreate, EventHookResponse, etc.)
  session_store.py          — EventHook ORM model, WebhookDelivery ORM model
  main.py                   — +include_router, +lifespan event_hooks.start/stop
  config.py                 — +EVENT_HOOKS_* settings
  metrics.py                — +event hook metrics
```

## Tests

Unit:
- URL validation (SSRF, allowed schemes, blocked IPs)
- HMAC signing and verification
- Retry schedule calculation
- EventHookStore CRUD (aiosqlite)
- Delivery status transitions
- Log masking

Integration:
- Register hook → trigger event → verify HTTP delivery (mock server)
- Retry → failed delivery eventually becomes dead
- Output truncation

DoD:
- All 5 CRUD endpoints work
- Event → outbox → delivery → retry loop functional
- SSRF blocks private IPs
- Signing headers present and verifiable
- Output truncation works
- Metrics visible on `/metrics`
- Tests pass
