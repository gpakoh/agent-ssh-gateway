# Webhook Notifications — Design Spec

## Overview

Allow AI agents and external systems to receive real-time notifications about SSH session events via webhooks. Agents register a callback URL, and the gateway POSTs event payloads when commands complete, sessions connect/disconnect, etc.

## Events

```
session.connected      — new SSH session established
session.disconnected   — session closed (explicitly or by timeout)
command.started        — command execution begins
command.completed      — command finished with exit code 0
command.failed         — command finished with non-zero exit code
```

## Data Model

### Webhook ORM (table: `webhooks`)

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID (PK) | auto-generated |
| `url` | str | target URL for POST |
| `events` | `list[str]` | PostgreSQL ARRAY of event names |
| `session_id` | `str\|None` | optional filter — only fire for this session |
| `headers` | `dict\|None` | optional custom HTTP headers |
| `is_active` | bool | soft disable without deleting |
| `created_at` | datetime | auto |
| `updated_at` | datetime | auto |

### Pydantic models

- `WebhookCreate` — url, events, session_id?, headers?
- `WebhookUpdate` — same fields, all optional
- `WebhookResponse` — all fields + id/dates
- `WebhookListResponse` — `{webhooks: [...], count: int}`

## API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/webhooks` | API key | Create webhook |
| `GET` | `/api/webhooks` | API key | List all |
| `GET` | `/api/webhooks/{id}` | API key | Get one |
| `PUT` | `/api/webhooks/{id}` | API key | Update (full replace) |
| `DELETE` | `/api/webhooks/{id}` | API key | Delete |

All endpoints require API key auth (standard `X-API-Key` header).

## Delivery

- Fire-and-forget via `aiohttp.ClientSession`
- Timeout: 10s
- No retry
- Log success/failure at INFO/WARNING level
- Payload format:

```json
{
  "event": "command.completed",
  "timestamp": "2026-05-28T12:00:00Z",
  "session_id": "abc-123",
  "host": "10.0.0.1",
  "port": 22,
  "username": "root",
  "command": "uptime",
  "exit_code": 0,
  "stdout": " 12:00:00 up 30 days",
  "stderr": "",
  "duration": 1.23
}
```

Different events include relevant fields:
- `session.connected` — host, port, username, session_id
- `session.disconnected` — session_id, reason (timeout/manual), connected_seconds
- `command.*` — command, exit_code, stdout, stderr, duration

## File Structure

```
app/
  webhook_store.py      — WebhookStore: CRUD over SQLAlchemy (async)
  webhook_delivery.py   — deliver_webhook(event_type, payload): async POST + logging
  routers/
    webhooks.py         — 5 CRUD endpoints
  session_store.py      — +Webhook ORM model (added to existing Base)
  main.py               — +include_router(webhooks_router), +register lifespan hooks
```

## Wiring into existing flow

In `ssh_manager.py`:

1. `connect()` → after successful connection, call `fire_event("session.connected", ...)`
2. `disconnect()` → before/after cleanup, call `fire_event("session.disconnected", ...)`
3. `execute()` → `command.started` before, `command.completed`/`command.failed` after

`fire_event()` is a standalone async function that:
1. Queries active webhooks matching event type + session_id
2. Spawns background tasks for each match
3. Returns immediately (fire-and-forget)

No changes to existing response models or return values.

## Testing

- Unit tests for `WebhookStore` CRUD (using aiosqlite, same pattern as host key store tests)
- Unit tests for `deliver_webhook()` (mock aiohttp)
- Integration test: register webhook → trigger event → verify delivery

## Security

- Webhook URL validated: must be `https://` (reject `http://` and `file://`)
- Max 50 webhooks per gateway (configurable guardrail, returns 409 Conflict)
- HTTPS enforced by default; `http://` allowed if `WEBHOOK_ALLOW_HTTP=true`
- Custom headers allow setting `Authorization: Bearer ...` tokens
- No credentials/tokens logged in webhook payload
