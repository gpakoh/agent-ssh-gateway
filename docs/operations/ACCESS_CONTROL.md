# Access-Control Operator Runbook

## Quick reference

| Action | Endpoint | Auth |
|--------|----------|------|
| Set decision | `POST /api/admin/access-control/decision` | Master key |
| List decisions | `GET /api/admin/access-control/recent` | Master key |
| Clear decision | `POST /api/admin/access-control/clear` | Master key |

## Inspect recent decisions

```bash
curl -s http://localhost:8085/api/admin/access-control/recent \
  -H "X-API-Key: $MASTER_KEY" | python3 -m json.tool
```

- Default: newest first, limit 100.
- Filter by decision: `?decision=denied`.
- Response includes `ttl_seconds_remaining` for each entry.

## Set a deny

```bash
curl -s -X POST http://localhost:8085/api/admin/access-control/decision \
  -H "X-API-Key: $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"actor_fingerprint":"<12-char-prefix>","source_ip":"<ip>","decision":"deny","reason":"violated policy"}'
```

- Deny **immediately disconnects** matching active sessions.
- TTL defaults to `ACCESS_CONTROL_DENY_TTL` (24h).

## Set an allow

```bash
curl -s -X POST http://localhost:8085/api/admin/access-control/decision \
  -H "X-API-Key: $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"actor_fingerprint":"<12-char-prefix>","source_ip":"<ip>","decision":"allow"}'
```

## Clear a decision

```bash
curl -s -X POST http://localhost:8085/api/admin/access-control/clear \
  -H "X-API-Key: $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"actor_fingerprint":"<12-char-prefix>","source_ip":"<ip>","reason":"operator cleared"}'
```

After clear:
- Decision deleted from in-memory store and Redis (best-effort).
- Actor+IP tuple returns to **pending** semantics (profile capped to readonly).
- Active sessions are **NOT** killed by clear.

## Rollback

To rollback a deny: call the **clear** endpoint above, then optionally **allow** the actor.

```bash
# Clear the deny
curl -s -X POST http://localhost:8085/api/admin/access-control/clear \
  -H "X-API-Key: $MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"actor_fingerprint":"<fp>","source_ip":"<ip>","reason":"rollback"}'

# Optionally allow
curl -s -X POST http://localhost:8085/api/admin/access-control/decision \
  -H "X-API-Key: $MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"actor_fingerprint":"<fp>","source_ip":"<ip>","decision":"allow"}'
```

## Truth notes

- **In-memory store** is the source of truth for reads and writes.
- **Redis** is best-effort backing for crash recovery only. Redis failure does not affect live access control.
- **clear()** deletes from memory and Redis (best-effort). After clear, the tuple returns to **pending** semantics.
- No raw tokens, commands, hosts, paths, or session credentials are exposed in these endpoints.

## Smoke

```bash
GATEWAY_URL=http://localhost:8085 GATEWAY_API_KEY=$MASTER_KEY python3 scripts/access_control_smoke.py
```

Validates: health → deny → recent (contains denied) → clear (removed).
