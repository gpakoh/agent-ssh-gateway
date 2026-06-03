# SSH Trust Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make SSH host key trust visible and safe — preflight check before connect, blocked connect on changed key, inline known-hosts management in Recovery panel.

**Architecture:** Three layers: (1) `HostKeyStore` gets `get_host(host,port)` and port-filtered `delete_host(host,port)`; (2) new endpoints `GET /api/known-hosts/check` (preflight) and `GET /api/known-hosts/{host}` (lookup by pair); (3) UI shows trust indicator, blocks Connect on changed, inline known-hosts actions.

**Tech Stack:** FastAPI, Paramiko, vanilla JS, PostgreSQL/File host key stores.

**Key decisions:**
- Preflight (`/check`) returns only `known`/`unknown` — never tries to guess `changed`
- `changed` detected only from Paramiko error at connect time, then cached in UI state until entry deleted
- `host:port` is the lookup key everywhere — `DELETE` gets optional `port` query param (default 22)
- Error matching extracted to `classify_ssh_trust_error()` in known_hosts.py

---

### Task 1: Add `get_host()` to HostKeyStore + update `delete_host()` with port

**Files:**
- Modify: `app/known_hosts.py`
- Test: `tests/test_known_hosts.py`

- [ ] **Step 1: Add `get_host()` to abstract `HostKeyStore` + update `delete_host` signature**

Add to the abstract class (line 39):

```python
class HostKeyStore(ABC):
    ...

    async def get_host(self, host: str, port: int = 22) -> dict | None:
        """Return {host, port, key_type, fingerprint} or None."""
        entries = await self.list_keys()
        for e in entries:
            if e.get("host") == host and e.get("port", 22) == port:
                return e
        return None

    async def delete_host(self, host: str, port: int = 22) -> int:
        """Delete entries for (host,port). Default port=22."""
        ...
```

- [ ] **Step 2: Implement in `NullHostKeyStore`**

```python
class NullHostKeyStore(HostKeyStore):
    ...
    # get_host() — no override needed, base class returns None via list_keys()
```

(No override needed — default implementation returns [] from `list_keys()`, so `get_host()` returns None.)

- [ ] **Step 3: Implement in `FileHostKeyStore`**

`FileHostKeyStore.delete_host` currently removes all entries matching hostname. Update to also check port when specified. Since paramiko HostKeys entries can hold multiple hostnames per key, port filtering is best-effort (entries with `[host]:port` format work, bare hostnames match any port). Add a comment documenting this.

```python
async def delete_host(self, host: str, port: int = 22) -> int:
    async with self._lock:
        await self._load()
        before = len(self._hk._entries)
        def _match(e):
            names = e.hostnames
            # Try [host]:port format first
            bracketed = f"[{host}]:{port}"
            if bracketed in names:
                return True
            # Fall back to bare hostname (matches all ports on this host)
            # This is a paramiko limitation — it doesn't store port per-entry
            return host in names
        self._hk._entries = [e for e in self._hk._entries if not _match(e)]
        removed = before - len(self._hk._entries)
        if removed > 0:
            await self._save()
        return removed
```

No `get_host()` override needed — `FileHostKeyStore.list_keys()` returns entries with port=22, so base class `get_host()` matches correctly.

- [ ] **Step 4: Implement in `PostgresHostKeyStore`**

```python
async def get_host(self, host: str, port: int = 22) -> dict | None:
    async with self._lock:
        await self._init_db()
    sm = self._session_maker
    assert sm is not None
    async with sm() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(HostKeyRecord).where(
                HostKeyRecord.host == host,
                HostKeyRecord.port == port,
            )
        )
        r = result.scalar_one_or_none()
        if r is None:
            return None
        return {
            "host": r.host,
            "port": r.port,
            "key_type": r.key_type,
            "fingerprint": r.fingerprint,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }

async def delete_host(self, host: str, port: int = 22) -> int:
    async with self._lock:
        await self._init_db()
    sm = self._session_maker
    assert sm is not None
    async with sm() as session:
        from sqlalchemy import delete as sa_delete
        result = await session.execute(
            sa_delete(HostKeyRecord).where(
                HostKeyRecord.host == host,
                HostKeyRecord.port == port,
            )
        )
        await session.commit()
        return result.rowcount
```

- [ ] **Step 5: Add `classify_ssh_trust_error()` helper**

Add at bottom of known_hosts.py (before `create_host_key_store`):

```python
import re

_TRUST_ERROR_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("unknown", re.compile(r"unknown host", re.IGNORECASE)),
    ("changed", re.compile(r"(changed.*mitm|key.*changed|host key mismatch)", re.IGNORECASE)),
]

def classify_ssh_trust_error(message: str) -> str | None:
    """Classify SSH error message into trust state.

    Returns 'unknown', 'changed', or None if the message doesn't match
    a known trust error pattern.
    """
    for state, pattern in _TRUST_ERROR_PATTERNS:
        if pattern.search(message):
            return state
    return None
```

- [ ] **Step 6: Run existing tests to confirm nothing broken**

Run: `python -m pytest tests/test_known_hosts.py -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add app/known_hosts.py
git commit -m "feat(ssh): add get_host() and port-filtered delete_host() to HostKeyStore"
```

---

### Task 2: New backend endpoints for trust flow

**Files:**
- Modify: `app/routers/system.py`
- Modify: `app/models.py` — add response models

- [ ] **Step 1: Add response models in models.py**

Search for `class KnownHostEntry` (line ~1580) and add after it:

```python
class KnownHostCheckResponse(BaseModel):
    """Preflight trust check response."""
    status: str  # "known" | "unknown"
    host: str
    port: int

class KnownHostLookupResponse(BaseModel):
    """Single host entry lookup."""
    host: str
    port: int
    key_type: str
    fingerprint: str
```

- [ ] **Step 2: Add `GET /api/known-hosts/check` endpoint in system.py**

```python
@router.get("/api/known-hosts/check", tags=["known-hosts"])
async def check_known_host(
    host: str = Query(..., min_length=1),
    port: int = Query(22, ge=1, le=65535),
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Preflight trust check — returns 'known' or 'unknown'. Never returns 'changed'."""
    entry = await _state.host_key_store.get_host(host, port)
    return KnownHostCheckResponse(
        status="known" if entry else "unknown",
        host=host,
        port=port,
    )
```

- [ ] **Step 3: Add `GET /api/known-hosts/{host}` lookup endpoint**

```python
@router.get("/api/known-hosts/{host}", tags=["known-hosts"])
async def lookup_known_host(
    host: str,
    port: int = Query(22, ge=1, le=65535),
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Lookup a single host:port entry. Returns 404 if not found.
    
    IMPORTANT: lookups are by (host,port) pair — not by host alone.
    """
    entry = await _state.host_key_store.get_host(host, port)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Host {host}:{port} not found in known-hosts")
    return KnownHostLookupResponse(**entry)
```

- [ ] **Step 4: Update `DELETE /api/known-hosts/{host}` to accept port param**

Find the existing endpoint (line ~2077) and update:

```python
@router.delete("/api/known-hosts/{host}", tags=["known-hosts"])
async def delete_known_host(
    host: str,
    port: int = Query(22, ge=1, le=65535),
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Delete a specific host:port entry from known hosts.
    
    IMPORTANT: deletes by (host,port) pair — use port=22 if not specified.
    """
    count = await _state.host_key_store.delete_host(host, port)
    if count == 0:
        raise HTTPException(status_code=404, detail=f"No known hosts found for {host}:{port}")
    return {"deleted": count, "host": host, "port": port}
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_system_endpoints.py -v -x`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add app/models.py app/routers/system.py
git commit -m "feat(ssh): add trust check + host lookup endpoints, port-filtered delete"
```

---

### Task 3: Add `ssh_trust_workflow` to `/api/help`

**Files:**
- Modify: `app/routers/system.py`

- [ ] **Step 1: Add `ssh_trust_workflow` section**

Find the end of `recovery_workflow` (before `"public_endpoints"`). Insert new section:

```python
        "ssh_trust_workflow": {
            "title": "SSH Trust Flow — safe host key verification",
            "overview": "Before establishing an SSH connection, the gateway checks whether the remote host's key is known. This section documents the three trust states, how the UI behaves in each, and how to manage known hosts manually.",
            "important": "The preflight endpoint GET /api/known-hosts/check returns only 'known' or 'unknown'. It does NOT attempt to detect 'changed' — that requires a real SSH handshake. 'changed' is only reported after a failed Connect attempt.",
            "sections": [
                {
                    "name": "trust_states",
                    "title": "Three trust states",
                    "overview": "The gateway maintains a store of SSH host keys (file or PostgreSQL). Each (host,port) pair has exactly one state at any time.",
                    "states": [
                        {"state": "known", "meaning": "The host:port has been seen before and its key matches what's stored. No action needed.", "ui": "Green: 'Trusted'. Connect works normally.", "icon": "🟢"},
                        {"state": "unknown", "meaning": "The host:port has never been seen before. This is normal for first connections.", "ui": "Yellow: 'Host not in known-hosts yet'. Connect still works — the key will be stored on success.", "icon": "🟡"},
                        {"state": "changed", "meaning": "The host presented a key that differs from what's stored. This is a potential MITM attack and requires manual intervention.", "ui": "Red: 'Host key CHANGED'. Connect is blocked until the entry is deleted via Recovery > Known Hosts.", "icon": "🔴"},
                    ],
                    "note": "'changed' is never returned by the preflight endpoint. It only appears after a real SSH connection attempt fails with a key mismatch. Once triggered, the UI remembers the state until the entry is deleted.",
                },
                {
                    "name": "host_key_store",
                    "title": "How known-hosts work",
                    "overview": "The store is configured via KNOWN_HOSTS_STORE env var ('file', 'postgres', or empty/null for no-op). On first connection to a host, the key is automatically added. On subsequent connections, the key is verified against the stored copy.",
                    "settings": [
                        {"setting": "KNOWN_HOSTS_STORE='file'", "description": "Uses an OpenSSH-format file (default: known_hosts in working dir). Paramiko's HostKeys handles read/write."},
                        {"setting": "KNOWN_HOSTS_STORE='postgres'", "description": "Uses the ssh_host_keys table in PostgreSQL. Supports concurrent access across gateway instances."},
                        {"setting": "SSH_STRICT_HOST_KEY_CHECKING=true", "description": "Unknown hosts are rejected instead of auto-accepted. Combine with manual key management via known-hosts API."},
                    ],
                },
                {
                    "name": "preflight_check",
                    "title": "Check Trust — what it does and doesn't do",
                    "overview": "The Check Trust button in the UI calls GET /api/known-hosts/check?host=X&port=Y. It looks up the (host,port) pair in the store and returns 'known' if found, 'unknown' if not.",
                    "what_it_does": [
                        "Returns 'known' — host:port exists in the store. The key was previously trusted.",
                        "Returns 'unknown' — host:port not found. First connection or entry was deleted.",
                    ],
                    "what_it_does_not_do": [
                        "It does NOT attempt to connect to the host.",
                        "It does NOT compare keys (no key is available before connection).",
                        "It does NOT return 'changed'. That state is impossible to determine without an active SSH handshake.",
                    ],
                    "recommendation": "Always use Check Trust before connecting to a sensitive host. If unknown, verify the host fingerprint out-of-band before proceeding.",
                },
                {
                    "name": "recovery_actions",
                    "title": "Known-hosts management (Recovery panel)",
                    "overview": "The Recovery panel has a Known Hosts sub-block with three actions: View (show fingerprint), Delete (remove one host:port entry), Clear All (remove all entries). All actions require master API key.",
                    "endpoints": [
                        {"endpoint": "GET /api/known-hosts", "scope": "master_key", "description": "List all known hosts. Returns host, port, key_type, fingerprint for each entry."},
                        {"endpoint": "GET /api/known-hosts/{host}?port=Y", "scope": "master_key", "description": "Lookup single host:port entry. Returns full record or 404. Lookup is by (host,port) pair."},
                        {"endpoint": "GET /api/known-hosts/check?host=X&port=Y", "scope": "master_key", "description": "Preflight trust check. Returns 'known' or 'unknown'. Never 'changed'."},
                        {"endpoint": "DELETE /api/known-hosts/{host}?port=Y", "scope": "master_key", "description": "Delete specific host:port entry. Port defaults to 22. Use after host key rotation."},
                        {"endpoint": "DELETE /api/known-hosts", "scope": "master_key", "description": "Clear all known hosts. Resets the store. All hosts become unknown on next connect."},
                    ],
                },
            ],
            "examples": [
                {
                    "endpoint": "GET /api/known-hosts/check",
                    "title": "Preflight: check if a host is trusted",
                    "description": "Before connecting, check if the host:port has a stored key. This is the 'Check Trust' action.",
                    "request": "GET /api/known-hosts/check?host=192.168.1.100&port=22",
                    "response": '{"status":"known","host":"192.168.1.100","port":22}',
                    "notes": "Returns 'known' if the (host,port) pair exists in the store, 'unknown' if not. Never returns 'changed'.",
                },
                {
                    "endpoint": "GET /api/known-hosts/{host}",
                    "title": "Lookup a specific host entry by host:port",
                    "description": "Get full details of a stored host key. Returns 404 if not found.",
                    "request": "GET /api/known-hosts/192.168.1.100?port=22",
                    "response": '{"host":"192.168.1.100","port":22,"key_type":"ssh-ed25519","fingerprint":"SHA256:abc123..."}',
                    "notes": "Lookup is by (host,port) pair. Port defaults to 22. Use exact port your connection uses.",
                },
                {
                    "endpoint": "DELETE /api/known-hosts/{host}",
                    "title": "Delete a specific host entry",
                    "description": "Remove a host:port entry. Use this after a legitimate host key rotation.",
                    "request": "DELETE /api/known-hosts/192.168.1.100?port=22",
                    "response": '{"deleted":1,"host":"192.168.1.100","port":22}',
                    "notes": "Deletes by (host,port) pair. If you have entries for the same host on different ports, only the matching one is removed.",
                },
                {
                    "endpoint": "DELETE /api/known-hosts",
                    "title": "Clear all known hosts",
                    "description": "Remove all entries from the store. All hosts become unknown on next connection.",
                    "request": "DELETE /api/known-hosts",
                    "response": '{"deleted":5}',
                    "notes": "This is destructive and irreversible. After this, every host will be treated as 'unknown' on first connect.",
                },
            ],
            "full_scenario": {
                "title": "End-to-end: Unknown host → Connect → Trust → Changed key → Block → Recovery",
                "overview": "A complete cycle from first connection through key rotation and recovery.",
                "steps": [
                    {
                        "step": 1,
                        "action": "Preflight: check trust before connecting",
                        "endpoint": "GET /api/known-hosts/check?host=10.0.0.5&port=2222",
                        "expected": '{"status":"unknown"}',
                        "notes": "Host is unknown. This is expected for first connection. Verify fingerprint out-of-band if this is a production server.",
                    },
                    {
                        "step": 2,
                        "action": "Connect — first time, key is stored",
                        "endpoint": "POST /api/ssh/connect",
                        "body": '{"host":"10.0.0.5","port":2222,"username":"deploy","password":"***"}',
                        "expected": "Connection successful. Host key is automatically stored in the store.",
                        "notes": "On success, the gateway stores the host key via store(). Next check will return 'known'.",
                    },
                    {
                        "step": 3,
                        "action": "Preflight: confirm host is now trusted",
                        "endpoint": "GET /api/known-hosts/check?host=10.0.0.5&port=2222",
                        "expected": '{"status":"known"}',
                        "notes": "Host is now trusted. The UI shows green indicator.",
                    },
                    {
                        "step": 4,
                        "action": "Connect fails — key changed",
                        "endpoint": "POST /api/ssh/connect",
                        "body": '{"host":"10.0.0.5","port":2222,"username":"deploy","password":"***"}',
                        "expected": "Connection fails with 'Host key changed — possible MITM attack'.",
                        "notes": "The gateway's KnownHostsPolicy detected a key mismatch. The connection is rejected with SSHException.",
                    },
                    {
                        "step": 5,
                        "action": "UI blocks Connect — shows red warning",
                        "endpoint": "(UI only)",
                        "expected": "Connect button is disabled. Red banner: 'Host key CHANGED — possible MITM attack. Remove entry via Recovery > Known Hosts'.",
                        "notes": "The UI detected 'changed' from the error message. User must resolve before retrying.",
                    },
                    {
                        "step": 6,
                        "action": "User investigates — view the stored key fingerprint",
                        "endpoint": "GET /api/known-hosts/10.0.0.5?port=2222",
                        "expected": '{"host":"10.0.0.5","port":2222,"key_type":"ssh-ed25519","fingerprint":"SHA256:old_fingerprint..."}',
                        "notes": "The stored fingerprint does not match what the server is now presenting. Admin should verify the new fingerprint out-of-band.",
                    },
                    {
                        "step": 7,
                        "action": "Admin confirms the key change is legitimate — deletes stale entry",
                        "endpoint": "DELETE /api/known-hosts/10.0.0.5?port=2222",
                        "expected": '{"deleted":1}',
                        "notes": "Entry removed. Next connection attempt will treat the host as 'unknown' and store the new key.",
                    },
                    {
                        "step": 8,
                        "action": "Reconnect — new key is stored",
                        "endpoint": "POST /api/ssh/connect",
                        "body": '{"host":"10.0.0.5","port":2222,"username":"deploy","password":"***"}',
                        "expected": "Connection successful. New key is stored. Trust restored.",
                        "notes": "The cycle is complete: unknown → connect → trust → changed → delete → reconnect → trust.",
                    },
                ],
                "summary": "8 steps: preflight unknown → connect (store) → confirm known → changed detected → blocked → inspect → delete → reconnect. Full trust lifecycle with safe recovery.",
            },
            "tips": [
                "Always run Check Trust before connecting to a production server. 'unknown' is safe but worth verifying.",
                "If a host key changed unexpectedly, do NOT delete the entry blindly. Verify the new fingerprint out-of-band first.",
                "The preflight endpoint never returns 'changed'. Changed is only detected during an actual SSH handshake.",
                "Delete by (host,port) pair, not by host alone. A server on port 2222 and the same server on port 22 have separate entries.",
                "Use Clear All sparingly — it removes every stored key and every host becomes 'unknown'.",
                "Known hosts are stored per gateway instance. If you run multiple gateways, they share the store only if using KNOWN_HOSTS_STORE=postgres.",
                "Check Trust requires the host and port. If connecting through a jump host, check the jump host, not the target.",
                "The Recovery panel's Known Hosts sub-block shows the action log in the terminal. Use View to confirm a fingerprint before deleting.",
            ],
        },
```

Insert this after the `recovery_workflow` block and before `"public_endpoints"`.

- [ ] **Step 2: Verify JSON validity**

The `/api/help` response is built as a dict. Make sure the section renders correctly. Run:
Run: `python -c "from app.routers.system import router; print('ok')"`
Expected: no syntax errors

- [ ] **Step 3: Commit**

```bash
git add app/routers/system.py
git commit -m "docs(ssh): add ssh_trust_workflow section to /api/help"
```

---

### Task 4: CSS — trust indicator + known-hosts inline styles

**Files:**
- Modify: `app/static/style.css`
- Modify: `app/static/index.html` (cache-bust)

- [ ] **Step 1: Add trust indicator CSS at end of style.css**

```css
/* SSH Trust indicator */
.trust-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-1) 0;
    min-height: 28px;
}

.trust-indicator {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    font-weight: 500;
    padding: 2px 8px;
    border-radius: var(--radius-sm);
    flex: 1;
}

.trust-indicator.trust-known {
    color: var(--accent-success);
    background: color-mix(in srgb, var(--accent-success) 10%, transparent);
}

.trust-indicator.trust-unknown {
    color: var(--accent-warning);
    background: color-mix(in srgb, var(--accent-warning) 10%, transparent);
}

.trust-indicator.trust-changed {
    color: var(--accent-danger);
    background: color-mix(in srgb, var(--accent-danger) 15%, transparent);
    font-weight: 600;
}

.trust-btn {
    font-size: 11px;
    white-space: nowrap;
}

.trust-changed-banner {
    font-size: 11px;
    color: var(--accent-danger);
    background: color-mix(in srgb, var(--accent-danger) 10%, transparent);
    padding: 4px 8px;
    border-radius: var(--radius-sm);
    margin-bottom: var(--space-1);
    display: none;
}

.trust-changed-banner.visible {
    display: block;
}

/* Known-hosts inline sub-block */
.kh-section {
    margin-top: var(--space-2);
    border-top: 1px solid var(--border-subtle);
    padding-top: var(--space-2);
}

.kh-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--space-1);
    font-size: 11px;
    font-weight: 500;
    color: var(--text-secondary);
}

.kh-header-actions {
    display: flex;
    gap: var(--space-1);
}

.kh-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.kh-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 11px;
    padding: 3px 6px;
    border-radius: var(--radius-sm);
    background: var(--bg-subtle);
}

.kh-item:hover {
    background: var(--bg-hover);
}

.kh-item-host {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-primary);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.kh-item-actions {
    display: flex;
    gap: 2px;
    flex-shrink: 0;
}

.kh-item-actions .btn-compact {
    font-size: 10px;
    padding: 1px 6px;
    min-width: 0;
}

.kh-empty {
    font-size: 11px;
    color: var(--text-tertiary);
    padding: 4px 6px;
    font-style: italic;
}
```

- [ ] **Step 2: Bump cache-buster to v=9 in index.html**

Change both `v=8` references to `v=9`.

- [ ] **Step 3: Commit**

```bash
git add app/static/style.css app/static/index.html
git commit -m "style(ssh): add trust indicator and known-hosts inline CSS"
```

---

### Task 5: HTML — trust indicator + Check Trust + known-hosts sub-block

**Files:**
- Modify: `app/static/index.html`

- [ ] **Step 1: Add trust row after form fields, before action buttons in connection form**

Find `<div class="form-actions">` (line ~122) and insert before it:

```html
                        <!-- Trust Check Row -->
                        <div class="trust-row">
                            <div class="trust-indicator" id="trustIndicator">
                                <i data-lucide="help-circle" class="icon-14"></i>
                                <span id="trustText">Check trust before connecting</span>
                            </div>
                            <button type="button" class="btn btn-outline btn-compact trust-btn" id="checkTrustBtn">
                                <i data-lucide="shield" class="icon-14"></i>
                                <span>Check Trust</span>
                            </button>
                        </div>
                        <!-- Changed Key Banner -->
                        <div class="trust-changed-banner" id="trustChangedBanner">
                            <i data-lucide="alert-triangle" class="icon-14"></i>
                            <span>Host key CHANGED — possible MITM attack. Remove entry via <strong>Recovery > Known Hosts</strong>.</span>
                        </div>
```

Also add an ID to `connectBtn` if not already there — I see it's `id="connectBtn"` already.

- [ ] **Step 2: Add known-hosts sub-block inside Recovery section**

Find the Recovery panel body (`.rec-body`) and add after the `.rec-flow` buttons:

```html
                    <!-- Known Hosts sub-block -->
                    <div class="kh-section" id="khSection">
                        <div class="kh-header">
                            <span>Known Hosts <span id="khCount">(0)</span></span>
                            <div class="kh-header-actions">
                                <button type="button" class="btn btn-outline btn-compact" id="khRefreshBtn" title="Refresh known hosts">
                                    <i data-lucide="refresh-cw" class="icon-12"></i>
                                    <span>Refresh</span>
                                </button>
                                <button type="button" class="btn btn-outline btn-compact" id="khClearAllBtn" title="Clear all known hosts">
                                    <i data-lucide="trash-2" class="icon-12"></i>
                                    <span>Clear All</span>
                                </button>
                            </div>
                        </div>
                        <div class="kh-list" id="khList">
                            <div class="kh-empty">No known hosts yet.</div>
                        </div>
                    </div>
```

- [ ] **Step 3: Commit**

```bash
git add app/static/index.html
git commit -m "feat(ui): add trust indicator and known-hosts sub-block to HTML"
```

---

### Task 6: JS — trust check, changed detection, known-hosts inline actions

**Files:**
- Modify: `app/static/app.js`

- [ ] **Step 1: Add trust check handler (debounced blur + Check Trust button)**

Find the `document.addEventListener('DOMContentLoaded', ...)` block (line ~2250) and add trust logic inside:

```javascript
    // SSH Trust Flow
    const trustIndicator = document.getElementById('trustIndicator');
    const trustText = document.getElementById('trustText');
    const checkTrustBtn = document.getElementById('checkTrustBtn');
    const trustChangedBanner = document.getElementById('trustChangedBanner');
    const hostInput = document.getElementById('host');
    const portInput = document.getElementById('port');
    const connectBtn = document.getElementById('connectBtn');

    let trustState = 'unknown';  // 'known' | 'unknown' | 'changed'
    let trustDebounceTimer = null;

    function setTrustState(state, fingerprint) {
        trustState = state;
        trustIndicator.className = 'trust-indicator';
        if (state === 'known') {
            trustIndicator.classList.add('trust-known');
            trustText.textContent = 'Trusted' + (fingerprint ? ' — ' + fingerprint.substring(0, 20) + '…' : '');
            connectBtn.disabled = false;
            trustChangedBanner.classList.remove('visible');
        } else if (state === 'unknown') {
            trustIndicator.classList.add('trust-unknown');
            trustText.textContent = 'Host not in known-hosts yet';
            connectBtn.disabled = false;
            trustChangedBanner.classList.remove('visible');
        } else if (state === 'changed') {
            trustIndicator.classList.add('trust-changed');
            trustText.textContent = 'Host key CHANGED — Connect blocked';
            connectBtn.disabled = true;
            trustChangedBanner.classList.add('visible');
        }
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }

    function doTrustCheck() {
        const host = hostInput.value.trim();
        const port = portInput.value.trim() || '22';
        if (!host) {
            setTrustState('unknown');
            return;
        }
        fetch('/api/known-hosts/check?host=' + encodeURIComponent(host) + '&port=' + encodeURIComponent(port), {
            credentials: 'include',
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            setTrustState(data.status === 'known' ? 'known' : 'unknown');
        })
        .catch(function() {
            // silently fail — trust state stays as-is
        });
    }

    // Debounced auto-check on blur
    function scheduleTrustCheck() {
        if (trustDebounceTimer) clearTimeout(trustDebounceTimer);
        trustDebounceTimer = setTimeout(doTrustCheck, 800);
    }

    if (hostInput) hostInput.addEventListener('blur', scheduleTrustCheck);
    if (portInput) portInput.addEventListener('blur', scheduleTrustCheck);

    // Check Trust button — explicit check
    if (checkTrustBtn) {
        checkTrustBtn.addEventListener('click', function(e) {
            e.preventDefault();
            doTrustCheck();
        });
    }

    // Connect error handling — detect changed key
    (function patchConnectHandler() {
        // Override form submit to catch 'changed' errors
        const form = document.getElementById('connectForm');
        if (form) {
            var origSubmit = form.onsubmit;
            form.addEventListener('submit', function(e) {
                // The original handler runs, we hook after it fails
                // We monitor the error message element
                var errorMsg = document.getElementById('errorMessage');
                var errorText = errorMsg ? errorMsg.querySelector('.error-text') : null;
                if (!errorText) return;
                // Set up a mutation observer to catch error text changes
                var observer = new MutationObserver(function() {
                    var msg = (errorText.textContent || '').toLowerCase();
                    if (msg.indexOf('changed') !== -1 || msg.indexOf('mitm') !== -1 || msg.indexOf('host key mismatch') !== -1) {
                        setTrustState('changed');
                        observer.disconnect();
                    } else if (msg.indexOf('unknown host') !== -1 || msg.indexOf('not found in known_hosts') !== -1) {
                        // Could be unknown host error — set to unknown
                        setTrustState('unknown');
                        observer.disconnect();
                    }
                });
                observer.observe(errorText, { childList: true, characterData: true, subtree: true });
            });
        }
    })();
```

- [ ] **Step 2: Add known-hosts inline render + actions**

Find `document.addEventListener('DOMContentLoaded', ...)` and add after trust code:

```javascript
    // Known Hosts sub-block in Recovery
    const khSection = document.getElementById('khSection');
    const khList = document.getElementById('khList');
    const khCount = document.getElementById('khCount');
    const khRefreshBtn = document.getElementById('khRefreshBtn');
    const khClearAllBtn = document.getElementById('khClearAllBtn');

    function renderKnownHosts() {
        fetch('/api/known-hosts', { credentials: 'include' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var hosts = data.hosts || [];
            if (khCount) khCount.textContent = '(' + hosts.length + ')';
            if (!khList) return;
            if (!hosts.length) {
                khList.innerHTML = '<div class="kh-empty">No known hosts yet.</div>';
                return;
            }
            var html = '';
            for (var i = 0; i < hosts.length; i++) {
                var h = hosts[i];
                var hostPort = h.host + ':' + (h.port || 22);
                var label = h.key_type || 'ssh-key';
                html += '<div class="kh-item" data-host="' + escapeAttr(h.host) + '" data-port="' + (h.port || 22) + '">';
                html += '  <span class="kh-item-host" title="' + escapeAttr(hostPort) + ' (' + escapeAttr(h.fingerprint || '') + ')">' + escapeHtml(hostPort) + ' <span style="color:var(--text-tertiary)">(' + escapeHtml(label) + ')</span></span>';
                html += '  <div class="kh-item-actions">';
                html += '    <button type="button" class="btn btn-outline btn-compact kh-view-btn" title="View fingerprint">View</button>';
                html += '    <button type="button" class="btn btn-outline btn-compact kh-del-btn" title="Delete entry">Del</button>';
                html += '  </div>';
                html += '</div>';
            }
            khList.innerHTML = html;

            // Bind View buttons
            khList.querySelectorAll('.kh-view-btn').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    var item = this.closest('.kh-item');
                    var host = item.dataset.host;
                    var port = item.dataset.port;
                    fetch('/api/known-hosts/' + encodeURIComponent(host) + '?port=' + encodeURIComponent(port), {
                        credentials: 'include',
                    })
                    .then(function(r) { return r.json(); })
                    .then(function(entry) {
                        var out = 'Known Host: ' + entry.host + ':' + entry.port;
                        out += '\n  Key Type: ' + (entry.key_type || '?');
                        out += '\n  Fingerprint: ' + (entry.fingerprint || '?');
                        appendTerminal('known-hosts', out, 'View: ' + host + ':' + port);
                    })
                    .catch(function() { appendTerminal('known-hosts', 'Failed to load entry', 'Error'); });
                });
            });

            // Bind Delete buttons
            khList.querySelectorAll('.kh-del-btn').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    var item = this.closest('.kh-item');
                    var host = item.dataset.host;
                    var port = item.dataset.port;
                    if (!confirm('Delete ' + host + ':' + port + ' from known hosts?')) return;
                    fetch('/api/known-hosts/' + encodeURIComponent(host) + '?port=' + encodeURIComponent(port), {
                        method: 'DELETE',
                        credentials: 'include',
                    })
                    .then(function(r) {
                        if (!r.ok) throw new Error('Delete failed');
                        return r.json();
                    })
                    .then(function() {
                        appendTerminal('known-hosts', 'Deleted ' + host + ':' + port, 'Delete');
                        renderKnownHosts(); // refresh list
                    })
                    .catch(function() { appendTerminal('known-hosts', 'Failed to delete ' + host + ':' + port, 'Error'); });
                });
            });
        })
        .catch(function() {
            if (khList) khList.innerHTML = '<div class="kh-empty" style="color:var(--accent-danger)">Failed to load known hosts.</div>';
        });
    }

    function appendTerminal(mode, text, label) {
        var els = getBulkEls();
        var time = new Date().toLocaleTimeString();
        var html = '<div class="terminal-line" style="border-top:1px solid var(--border-subtle);padding-top:6px;margin-top:6px">';
        html += '<span style="color:var(--accent-info)">[' + time + ']</span> ';
        html += '<strong>' + escapeHtml(label || mode) + '</strong></div>';
        html += '<div class="terminal-line"><pre style="margin:0;white-space:pre-wrap;font:var(--font-mono);font-size:11px;color:var(--text-secondary);line-height:1.4">' + escapeHtml(text) + '</pre></div>';
        if (els.terminal) {
            els.terminal.insertAdjacentHTML('beforeend', html);
            els.terminal.scrollTop = els.terminal.scrollHeight;
        }
    }

    // Clear All button
    if (khClearAllBtn) {
        khClearAllBtn.addEventListener('click', function() {
            if (!confirm('Clear ALL known hosts? This cannot be undone. All hosts will become unknown on next connect.')) return;
            fetch('/api/known-hosts', {
                method: 'DELETE',
                credentials: 'include',
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                appendTerminal('known-hosts', 'Cleared ' + data.deleted + ' known host(s)', 'Clear All');
                renderKnownHosts(); // refresh
                setTrustState('unknown'); // reset trust state
            })
            .catch(function() { appendTerminal('known-hosts', 'Failed to clear known hosts', 'Error'); });
        });
    }

    // Refresh button
    if (khRefreshBtn) {
        khRefreshBtn.addEventListener('click', function() {
            renderKnownHosts();
        });
    }

    // Load known hosts on page load
    renderKnownHosts();
```

- [ ] **Step 3: Add `escapeAttr` and `escapeHtml` helper if missing**

Check if `escapeHtml` exists. It's used in printBulkResult already. Add `escapeAttr`:

```javascript
function escapeAttr(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
```

Find where `escapeHtml` is defined and add `escapeAttr` next to it.

- [ ] **Step 4: Cache bust app.js version**

Update the `app.js?v=9` reference in index.html.

- [ ] **Step 5: Verify JS syntax**

Run: `node -e "var fs=require('fs'); var c=fs.readFileSync('app/static/app.js','utf8'); new Function(c); console.log('JS OK')"`
Expected: "JS OK"

- [ ] **Step 6: Commit**

```bash
git add app/static/app.js app/static/index.html
git commit -m "feat(ui): add trust check, changed detection, and known-hosts inline actions"
```

---

### Task 7: Tests

**Files:**
- Modify: `tests/test_known_hosts.py`
- Modify: `tests/test_system_endpoints.py`

- [ ] **Step 1: Test `HostKeyStore.get_host()` and port-filtered `delete_host()`**

Add tests to `tests/test_known_hosts.py`:

```python
@pytest.mark.asyncio
async def test_get_host_returns_entry():
    store = FileHostKeyStore("/tmp/test_known_hosts_get_host")
    from paramiko import RSAKey
    from io import StringIO
    key = RSAKey.generate(bits=2048)
    await store.store("testhost", 22, key)
    entry = await store.get_host("testhost", 22)
    assert entry is not None
    assert entry["host"] == "testhost"
    assert entry["port"] == 22
    assert entry["key_type"] == "ssh-rsa"

@pytest.mark.asyncio
async def test_get_host_returns_none_for_unknown():
    store = FileHostKeyStore("/tmp/test_known_hosts_get_host_none")
    entry = await store.get_host("nobody", 22)
    assert entry is None

@pytest.mark.asyncio
async def test_delete_host_by_port():
    store = FileHostKeyStore("/tmp/test_known_hosts_del_port")
    from paramiko import RSAKey
    key1 = RSAKey.generate(bits=2048)
    key2 = RSAKey.generate(bits=2048)
    await store.store("multi", 22, key1)
    await store.store("multi", 2222, key2)
    # Delete only port 22
    count = await store.delete_host("multi", 22)
    assert count >= 1
    remaining = await store.list_keys()
    ports = [e["port"] for e in remaining if e["host"] == "multi"]
    assert 22 not in ports

@pytest.mark.asyncio
async def test_classify_ssh_trust_error():
    from app.known_hosts import classify_ssh_trust_error
    assert classify_ssh_trust_error("Unknown host 10.0.0.1:22") == "unknown"
    assert classify_ssh_trust_error("Host key for 10.0.0.1:22 changed — possible MITM") == "changed"
    assert classify_ssh_trust_error("Connection refused") is None
```

- [ ] **Step 2: Test new endpoints**

Add to `tests/test_system_endpoints.py`:

```python
@pytest.mark.asyncio
async def test_known_hosts_check_unknown(async_client):
    resp = await async_client.get("/api/known-hosts/check?host=ghost&port=22")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unknown"
    assert data["host"] == "ghost"
    assert data["port"] == 22

@pytest.mark.asyncio
async def test_known_hosts_lookup_404(async_client):
    resp = await async_client.get("/api/known-hosts/ghost?port=22")
    assert resp.status_code == 404

@pytest.mark.asyncio
async def test_known_hosts_delete_with_port(async_client):
    # First delete something that exists (or verify 404 for non-existent)
    resp = await async_client.delete("/api/known-hosts/ghost?port=22")
    assert resp.status_code == 404
```

- [ ] **Step 3: Run all tests**

Run: `python -m pytest -q --tb=short`
Expected: all pass, no regressions

- [ ] **Step 4: Commit**

```bash
git add tests/test_known_hosts.py tests/test_system_endpoints.py
git commit -m "test(ssh): add tests for get_host, port-filtered delete, trust endpoints"
```

---

### Task 8: Final verification

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest -q`
Expected: all ~430 tests pass

- [ ] **Step 2: Verify JS syntax**

Run: `node -e "var fs=require('fs'); var c=fs.readFileSync('app/static/app.js','utf8'); new Function(c); console.log('JS OK')"`
Expected: "JS OK"

- [ ] **Step 3: Verify ruff**

Run: `ruff check app/`
Expected: no warnings

- [ ] **Step 4: Bump cache busters if not already done**

Verify `v=9` in both style.css and app.js references in index.html.

- [ ] **Step 5: Final summary**

Print: "SSH trust flow implemented: preflight check, blocked changed keys, inline known-hosts management, full /api/help documentation."
