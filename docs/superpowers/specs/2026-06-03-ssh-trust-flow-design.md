# SSH Trust Flow — Safe Host Key Verification

## Problem

Before SSH connection, the user (human or agent) has no visibility into whether the remote host key is known, unknown, or changed. Paramiko blocks on changed keys with a generic exception, but the UI gives no preflight feedback and no guided recovery.

## Design

### Backend: new endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/known-hosts/check?host=X&port=Y` | GET | Preflight: returns `{status: "known"|"unknown", host, port, fingerprint?}`. No key comparison — only checks if `host:port` exists in the store. |
| `/api/known-hosts/{host}?port=Y` | GET | Full entry lookup: returns `{host, port, key_type, fingerprint}` or 404. Used by `View` action. |
| `/api/known-hosts/{host}?port=Y` | DELETE | Delete single `host:port` entry. Port defaults to 22. Returns `{deleted: 1\|0}`. |

**Contract:** all lookups are by `host + port` pair — never by host alone.

### Helper: `HostKeyStore.get_host(host, port)`

Add to the abstract interface and all implementations:

```python
async def get_host(self, host: str, port: int) -> dict | None:
    """Return {host, port, key_type, fingerprint} for (host,port) or None."""
```

Default implementations iterate `list_keys()` and filter. Postgres variant uses a direct SQL query.

### SSH Trust States (UI contract)

| State | Meaning | Indicator | Connect blocked? |
|-------|---------|-----------|------------------|
| `known` | `host:port` found in store | Green | No |
| `unknown` | `host:port` not in store | Yellow | No |
| `changed` | Paramiko raised SSHException with "key changed" | Red | Yes (until entry deleted) |

- `known` / `unknown` come from preflight (`/check`).
- `changed` is **never** returned by preflight. It only appears after a failed `Connect` where the exception message contains "key changed" / "changed".
- Once `changed`, the UI blocks `Connect` until the entry is deleted via Recovery > Known Hosts.

### `ssh_trust_workflow` in `/api/help`

Three sections:
1. **How known-hosts work** — the store (file or postgres), auto-add on first connect, `SSH_STRICT_HOST_KEY_CHECKING` 
2. **Three trust states** — `known` (trusted), `unknown` (first connect — safe), `changed` (MITM alert — block)
3. **Decision table** — what the UI does in each state
4. **Examples** — check trust, view entry, delete entry, clear all, connect flow with changed key
5. **Safety notes** — always check trust before sensitive connects, remove stale entries, audit periodically

### UI: Connection Section

**New element: trust indicator** between form fields and action buttons.

```
[ ⚪ Check Trust ]  [ 🟢 Trusted | 🟡 Unknown | 🔴 Key Changed ]
```

- **Check Trust** button: calls `GET /api/known-hosts/check?host=X&port=Y`. Always enabled.
- **Auto-check**: debounced (800 ms) on `blur` of host/port fields — updates indicator silently.
- **Indicator colors**:
  - `known` — green text "Trusted"
  - `unknown` — yellow text "Host not in known-hosts yet"
  - `changed` — red text "Host key CHANGED — possible MITM attack". Connect disabled. Banner with instructions.
- **Changed detection**: on Connect failure, parse error message. If contains "changed" → set `changed` state, disable Connect, show red banner.

### UI: Recovery > Known Hosts

Inline block inside the Recovery section:

```
├─ Known Hosts (2)              [Clear All]
│  ├─ 192.0.2.10:22 (ssh-ed25519)   [View] [Del]
│  └─ 10.0.0.5:2222 (ssh-rsa)          [View] [Del]
```

- **Clear All**: confirm dialog → `DELETE /api/known-hosts` → refresh list.
- **View**: `GET /api/known-hosts/{host}?port=Y` → show fingerprint in terminal output.
- **Delete**: confirm "Delete 192.0.2.10:22?" → `DELETE /api/known-hosts/{host}?port=Y` → refresh list.
- Empty state: "No known hosts yet."

### Implementation order

1. `HostKeyStore.get_host()` — all three implementations (Null, File, Postgres)
2. Backend endpoints: `check`, `{host}` GET, `{host}` DELETE with port
3. `ssh_trust_workflow` in `/api/help`
4. CSS: `.trust-indicator`, `.trust-*`, `.kh-*`
5. JS: `BULK_TEMPLATES` for trust-check inline, debounced blur, changed detection in connect handler, known‑hosts inline render
6. HTML: trust indicator + Check Trust button in connection form, known-hosts sub-block in recovery
7. Tests + verification

### Files touched

- `app/routers/system.py` — new endpoints + help section
- `app/known_hosts.py` — `get_host()` on all stores
- `app/static/index.html` — trust indicator, known-host inline block
- `app/static/style.css` — trust/kh styles (+ cache-bust v=9)
- `app/static/app.js` — trust check, changed detection, kh inline actions (+ cache-bust v=9)
- `app/models.py` — new response models if needed
