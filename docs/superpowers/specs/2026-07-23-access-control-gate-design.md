# Phase 12B — Agent Access Approval Gate (Design Spec)

> **Status:** Draft
> **Date:** 2026-07-23
> **Author:** opencode
> **Supersedes:** Phase 12A (decorative buttons, no enforcement)

---

## 1. Problem

Phase 12A added Telegram inline buttons (Allow/Deny) but they are decorative:
- Decisions stored in notifier-sidecar memory, gateway cannot read them
- Gateway routers (ssh, jobs, batch) never check access state
- `pending` state does not exist — unknown actors get full access
- Buttons not wired into alert flow (service.py sends text-only)

**Result:** Operator clicks buttons, nothing happens.

---

## 2. Design Goals

1. **Key:** `(actor_fingerprint, source_ip)` tuple — not pure IP, not pure session
2. **Three states:** `pending` / `allowed` / `denied`
3. **Pending = capability downgrade**, not 403 block
4. **Denied = hard block** + active session kill
5. **Gateway owns the store** — notifier writes via admin API
6. **Master identity exempt** by default (break-glass)
7. **source_ip from trusted-proxy helper**, not raw `request.client.host`

---

## 3. Access Control Store

### 3.1 Data Model

```python
@dataclass
class AccessDecision:
    key_hash: str            # SHA256(actor_fingerprint:source_ip), first 16 hex
    decision: str            # "pending" | "allowed" | "denied"
    actor_fingerprint: str   # stored for audit, not for key lookup
    source_ip: str           # stored for audit, not for key lookup
    reason: str
    decided_by: str          # "operator" | "system" | "auto"
    created_at: float
    expires_at: float
```

### 3.2 Key Format

Redis key: `ac:{key_hash}` where `key_hash = sha256(f"{actor_fingerprint}:{source_ip}")[:16]`

Rationale: never store raw actor fingerprint or source IP in Redis keys. Hash is non-reversible, fixed-length, safe for logs.

### 3.3 Storage Layers

| Layer | Read | Write | Purpose |
|-------|------|-------|---------|
| In-memory dict | Sync, hot path | Sync | Every request checks here first |
| Redis | Async, background | Async | Crash recovery, restart load |

- **Read path:** memory → if miss, return `None` (treated as `pending`)
- **Write path:** memory + Redis (best-effort Redis write, log warning on failure)
- **Startup:** load all non-expired entries from Redis into memory
- **If Redis unavailable:** gateway continues with in-memory state, logs `WARNING access_control.redis_unavailable`

### 3.4 TTL Defaults (env-configurable)

| State | Default TTL | Env Var |
|-------|-------------|---------|
| `pending` | 900s (15 min) | `ACCESS_CONTROL_PENDING_TTL` |
| `allowed` | 86400s (24h) | `ACCESS_CONTROL_ALLOW_TTL` |
| `denied` | 86400s (24h) | `ACCESS_CONTROL_DENY_TTL` |

### 3.5 Cleanup

Background task evicts expired entries from memory every 60s. Redis TTL handles Redis-side expiry.

---

## 4. Admin Endpoint

### `POST /api/admin/access-control/decision`

**Auth:** `require_master_key` only. Agent tokens rejected.

**Request body:**
```json
{
  "actor_fingerprint": "abc123def456",
  "source_ip": "<source-ip>",
  "decision": "allow",
  "reason": "operator approved via Telegram",
  "ttl_seconds": null,
  "request_id": "req-abc123"
}
```

`actor_fingerprint` is the existing 12-character SHA-256 prefix already emitted
in audit events (`_identity.fingerprint[:12]`). It is never a raw token and is
not a `sha256:`-prefixed full digest.

**Red lines:**
- No raw token, username, host, path, or command in request body
- Callback token is NOT authentication — master key is
- `actor_fingerprint` and `source_ip` are the only identity fields accepted

**Response:**
```json
{
  "decision_id": "dec_...",
  "key_hash": "a1b2c3d4e5f6a7b8",
  "decision": "allowed",
  "expires_at": "2026-07-24T12:00:00Z",
  "effective_now": true
}
```

**Side effects:**
- Logs structured audit event `access_control.decision` (without raw IP in key)
- If `decision == "deny"`: triggers `disconnect_sessions_for_actor_source()`

**Error codes:**
- `ACCESS_DENIED` — actor+IP tuple is denied
- `ACCESS_PENDING_APPROVAL` — operation requires operator approval first
- `ACCESS_CONTROL_DISABLED` — endpoint called while access control is disabled

### `GET /api/admin/access-control/recent` (optional, later slice)

Returns last N decisions for operator review.

---

## 5. Source IP Resolution

For HTTP requests, reuse existing `get_client_ip()` from `app/auth_middleware.py`:

```python
def get_client_ip(
    request: Request, trusted_proxy_networks: list[...]
) -> str:
```

- Reads `X-Forwarded-For` header when `request.client.host` is in trusted proxy networks
- Returns the first non-trusted IP from the chain
- Falls back to `request.client.host` if no proxy header

**Env:** `TRUSTED_PROXY_CIDRS` (already exists in config)

For WebSocket routes, add an equivalent helper that accepts a `WebSocket` and
uses the same trusted-proxy logic against `websocket.headers` and
`websocket.client.host`. Do not use raw `websocket.client.host` directly in the
access gate.

---

## 6. Identity Exemptions

| Identity type | Access gate enforced | Rationale |
|---------------|---------------------|-----------|
| `master` | **No** (exempt by default) | Break-glass admin access |
| `agent` | **Yes** | Normal agent operations |

**Config:** `ACCESS_CONTROL_ENFORCE_MASTER=false` (default). Set `true` to enforce gate on master too.

**Implementation:** `resolve_access_policy()` checks `identity.token_type` before querying the store.

---

## 7. Policy Engine Integration

### 7.1 `resolve_access_policy()`

New function in `app/access_control.py`:

```python
@dataclass
class AccessPolicyResult:
    state: str              # "pending" | "allowed" | "denied" | "exempt"
    effective_profile: str  # capped or original profile
    reason: str
    key_hash: str

def resolve_access_policy(
    *,
    identity: AuthIdentity,
    source_ip: str,
    requested_profile: str,
    operation: str,
    enforce_master: bool = False,
) -> AccessPolicyResult:
```

**Logic:**
1. If `identity.token_type == "master"` and not `enforce_master` → return `state="exempt"`, `effective_profile=requested_profile`
2. Compute `key_hash = sha256(f"{identity.fingerprint}:{source_ip}")[:16]`
3. Look up in memory store
4. If not found or expired → `state="pending"`
5. If `state == "denied"` → raise `AccessDeniedError`
6. If `operation == "connect"` and state is `pending` or `allowed` → allow session creation
7. If `operation == "pty"` and state is `pending` → raise `AccessPendingApprovalError`
8. If `state == "allowed"` → `effective_profile=requested_profile` (passthrough)
9. If `state == "pending"` → `effective_profile=capped_profile(requested_profile)`

### 7.2 Profile Capping

```python
def capped_profile(requested: str) -> str:
    """Downgrade profile to readonly/testlint for pending actors."""
    if requested in ("readonly", "testlint"):
        return requested
    return "readonly"
```

**Semantics:** pending actors can open an SSH session, but command execution is
capped to `readonly`/`testlint` and PTY is blocked. This keeps safe read/test
tools available while preventing interactive shell escape.

### 7.3 Router Integration

**Call site:** before `evaluate_command_policy()` in each router:

```python
# Before (current):
decision = evaluate_command_policy(command, mode=mode, profile=profile)

# After (Phase 12B):
access = resolve_access_policy(
    identity=identity,
    source_ip=source_ip,
    requested_profile=profile,
)
if access.state == "denied":
    raise HTTPException(403, "ACCESS_DENIED")
decision = evaluate_command_policy(
    command, mode=mode, profile=access.effective_profile
)
```

### 7.4 Affected Routers

| Router | Endpoint | Enforcement point |
|--------|----------|-------------------|
| `ssh.py` | `POST /api/ssh/connect` | Before session creation; `denied` blocks, `pending` allowed |
| `ssh.py` | `POST /api/ssh/execute` | Before command execution |
| `ssh.py` | `POST /api/ssh/execute-argv` | Before command execution |
| `ssh.py` | `WS /api/ssh/execute/stream` | Before command policy evaluation |
| `ssh.py` | `WS /api/ssh/pty/{session_id}/stream` | Before PTY creation; `pending` blocked |
| `jobs.py` | `POST /api/jobs/run` | Before job submission |
| `jobs.py` | `POST /api/bulk/execute` | Before bulk execution |
| `batch.py` | `POST /api/batch/execute` | Before batch execution |

---

## 8. Active Session Kill

### 8.1 Prerequisite: SessionRecord fields

`SessionRecord` currently tracks `owner_token_fingerprint` but not `source_ip`. Two additions needed:

1. Add `source_ip: str | None = None` to `SessionRecord` dataclass
2. In `ssh_connect()`, capture `source_ip` from `get_client_ip()` and store in record

### 8.2 Internal function

```python
async def disconnect_sessions_for_actor_source(
    manager: SSHSessionManager,
    actor_fingerprint: str,
    source_ip: str,
) -> int:
    """Disconnect all sessions matching actor+IP. Returns count."""
```

**Implementation:** iterate `manager._sessions`, match `session.owner_token_fingerprint == actor_fingerprint and session.source_ip == source_ip`, call `manager.disconnect(session_id)` for each.

### 8.3 Trigger

Called from admin endpoint handler when `decision == "deny"`.

### 8.4 Logging

Each disconnection logs: `session.killed_by_access_control` with `session_id`, `actor_fingerprint`, `source_ip`.

---

## 9. Notifier Callback Flow

### 9.1 Alert → Button Attachment

In `GatewayNotifierService._poll_events()` (service.py:190):

```python
# Current:
await self._telegram.send_message(text)

# Phase 12B:
reply_markup = None
if event_type in self._action_event_types:
    actor_fp = event.get("actor_fingerprint")
    src_ip = event.get("source_ip")
    if actor_fp and src_ip:
        reply_markup = _build_keyboard(
            actor_fingerprint=actor_fp,
            source_ip=src_ip,
            event_type=event_type,
            request_id=event.get("request_id", ""),
        )
await self._telegram.send_message(text, reply_markup=reply_markup)
```

### 9.2 Keyboard Builder

```python
def _build_keyboard(
    *, actor_fingerprint: str, source_ip: str, event_type: str, request_id: str
) -> dict:
    allow_token = create_action(
        action_type="allow_actor",
        actor_fingerprint=actor_fingerprint,
        source_ip=source_ip,
        event_type=event_type,
        request_id=request_id,
    )
    deny_token = create_action(
        action_type="deny_actor",
        actor_fingerprint=actor_fingerprint,
        source_ip=source_ip,
        event_type=event_type,
        request_id=request_id,
    )
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Allow", "callback_data": allow_token},
                {"text": "⛔ Deny", "callback_data": deny_token},
            ]
        ]
    }
```

### 9.3 Config

```python
# config.py additions:
action_event_types: set[str] = {"command.deny", "workspace.readonly_block"}
```

**Env:** `GATEWAY_NOTIFIER_ACTION_EVENT_TYPES=command.deny,workspace.readonly_block`

### 9.4 Callback Handler (callbacks.py)

```python
async def handle_callback_query(callback_query, *, gateway_api_url, gateway_api_key):
    data = callback_query.get("data", "")
    from_user = callback_query.get("from", {})

    if not data:
        return {"action_taken": False, "reason": "no_data"}

    payload = pop_action(data)
    if payload is None:
        return {"action_taken": False, "reason": "invalid_or_expired_token"}

    # 1. Write decision to gateway admin API
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"{gateway_api_url}/api/admin/access-control/decision",
            json={
                "actor_fingerprint": payload.actor_fingerprint,
                "source_ip": payload.source_ip,
                "decision": "allow" if payload.action_type == "allow_actor" else "deny",
                "reason": f"operator:{from_user.get('username', 'unknown')}",
                "ttl_seconds": None,
                "request_id": payload.request_id,
            },
            headers={"X-API-Key": gateway_api_key},
        )

    # 2. Answer callback query (removes loading spinner)
    await answer_callback_query(callback_query["id"])

    # 3. Send follow-up message
    decision = "allow" if payload.action_type == "allow_actor" else "deny"
    icon = "✅" if decision == "allow" else "⛔"
    await send_message(f"{icon} {decision.title()} actor {payload.actor_fingerprint[:12]}... from {payload.source_ip} for 24h")

    return {"action_taken": True, "decision": decision}
```

### 9.5 `answerCallbackQuery`

New helper in `telegram.py`:

```python
async def answer_callback_query(self, callback_query_id: str) -> bool:
    """Answer a callback query to remove loading indicator."""
    if self._dry_run:
        return True
    url = f"{self._api_base}/bot{self._token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    try:
        async with self._session.post(url, json=payload, proxy=self._proxy) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
```

### 9.6 Button State After Decision

After callback is processed, the original inline keyboard is replaced with a text indicator. Implementation: `editMessageText` with the follow-up text and no keyboard.

```python
async def edit_message_text(self, chat_id: str, message_id: int, text: str) -> bool:
    """Edit an existing message to remove inline keyboard."""
    ...
```

---

## 10. CallbackPoller Integration

### 10.1 Startup

In `__main__.py`:

```python
async def _main():
    ...
    tasks = [service.run_forever()]
    if settings.can_send_telegram:
        poller = CallbackPoller(
            token=settings.telegram_token,
            proxy=settings.proxy,
            handle_callback_fn=lambda cb: handle_callback_query(
                cb,
                gateway_api_url=settings.gateway_url,
                gateway_api_key=settings.gateway_api_key,
            ),
        )
        tasks.append(poller.run_forever())
    await asyncio.gather(*tasks)
```

The poller must not start in dry-run mode. Dry-run smoke must never call
Telegram `getUpdates`.

### 10.2 Polling

`CallbackPoller.run_forever()` calls `poll_once()` every 2 seconds. Each update increments `_offset`. Callbacks are processed concurrently.

---

## 11. Testing Strategy

### Unit Tests

| Test file | Coverage |
|-----------|----------|
| `test_access_control.py` | Store CRUD, TTL expiry, key hashing, capped profiles |
| `test_access_control_redis.py` | Redis load/save, graceful degradation |
| `test_callbacks.py` | Callback → gateway API call, answerCallbackQuery, follow-up |
| `test_notifier_buttons.py` | Button attachment logic, event type filtering |
| `test_admin_access.py` | Admin endpoint auth, validation, side effects |
| `test_access_gate.py` | `resolve_access_policy()` for all states + identity types |

### Integration Tests

| Test | What it verifies |
|------|------------------|
| Pending actor gets `readonly` profile | `ssh_execute` with pending actor → command capped to readonly |
| Denied actor gets 403 | `ssh_connect` with denied actor → connection refused |
| Allowed actor passes through | `ssh_execute` with allowed actor → normal profile |
| Master exempt by default | `ssh_execute` with master key → full access |
| Pending actor cannot open PTY | `pty_stream` with pending actor → `ACCESS_PENDING_APPROVAL` |
| WebSocket execute is capped | `execute/stream` pending actor → readonly profile |
| Session kill on deny | Deny decision → active sessions for that actor+IP disconnected |
| Redis crash recovery | Restart gateway → decisions restored from Redis |

### Regression

All existing 122 notifier tests + 25 Phase 12A tests must pass.

---

## 12. Env Vars Summary

| Variable | Default | Purpose |
|----------|---------|---------|
| `ACCESS_CONTROL_ENABLED` | `true` | Master switch |
| `ACCESS_CONTROL_ENFORCE_MASTER` | `false` | Enforce gate on master identity |
| `ACCESS_CONTROL_PENDING_TTL` | `900` | Pending state TTL (seconds) |
| `ACCESS_CONTROL_ALLOW_TTL` | `86400` | Allow state TTL (seconds) |
| `ACCESS_CONTROL_DENY_TTL` | `86400` | Deny state TTL (seconds) |
| `ACCESS_CONTROL_REDIS_URL` | (uses `REDIS_URL`) | Optional Redis URL override for persistence |
| `GATEWAY_NOTIFIER_ACTION_EVENT_TYPES` | `command.deny,workspace.readonly_block` | Event types with inline buttons |

---

## 13. Files Summary

| File | Action | Purpose |
|------|--------|---------|
| `app/access_control.py` | NEW | Store, TTL, `resolve_access_policy()`, `capped_profile()`, `disconnect_sessions_for_actor_source()` |
| `app/routers/admin_access.py` | NEW | `POST /api/admin/access-control/decision` |
| `app/main.py` | MODIFY | Include admin access router and initialize access-control store |
| `app/api_help.py` | MODIFY | Document admin decision endpoint and access error codes |
| `app/notifier/callbacks.py` | REWRITE | Wire to gateway admin API, answerCallbackQuery, editMessageText |
| `app/notifier/get_updates.py` | KEEP | CallbackPoller (already correct) |
| `app/notifier/service.py` | MODIFY | Attach buttons based on configurable event types |
| `app/notifier/telegram.py` | MODIFY | Add `answer_callback_query()`, `edit_message_text()` |
| `app/notifier/__main__.py` | MODIFY | Start CallbackPoller alongside service |
| `app/notifier/config.py` | MODIFY | Add `action_event_types` |
| `app/routers/ssh.py` | MODIFY | Add `resolve_access_policy()` before connect/execute |
| `app/routers/jobs.py` | MODIFY | Add `resolve_access_policy()` before run/bulk |
| `app/routers/batch.py` | MODIFY | Add `resolve_access_policy()` before execute |
| `app/ssh_manager.py` | MODIFY | Add `source_ip` field to `SessionRecord`, capture in `ssh_connect()` |
| `app/command_policy.py` | NO CHANGE | `evaluate_command_policy()` already accepts profile param |

---

## 14. Migration / Rollback

- **Forward:** deploy new code, set `ACCESS_CONTROL_ENABLED=true`
- **Rollback:** set `ACCESS_CONTROL_ENABLED=false` — gate disabled, all requests pass through
- **No schema migration** — Redis keys are new, no existing data to migrate

---

## 15. Public Hygiene

All IP references in spec use `<source-ip>` / `<example-ip>` placeholders. No real IPs, no RFC 5737 ranges that could be confused with real addresses.
