# Known Hosts Store for SSH Gateway

## Problem

`AutoAddPolicy` accepts any host key on first connection (MITM
vulnerability). `RejectPolicy` rejects all unknown hosts, making the
gateway unusable until a host key is somehow provisioned. Neither
supports multi-replica deployments (the file-based known_hosts is
per-container), nor does Paramiko natively store keys in a database.

## Solution

Pluggable `HostKeyStore` backend (file or PostgreSQL) + custom Paramiko
`MissingHostKeyPolicy` that auto-adds new keys and auto-updates changed
keys with a warning.

`SSH_STRICT_HOST_KEY_CHECKING=true` still bypasses the store and uses
`RejectPolicy` (fail-closed for compliance environments).

---

## Architecture

### HostKeyStore (protocol / ABC)

```python
class HostKeyStore(ABC):
    @abstractmethod
    async def check(self, host: str, port: int, key: paramiko.PKey) -> bool | None:
        """None=unknown, True=matches, False=key changed."""

    @abstractmethod
    async def store(self, host: str, port: int, key: paramiko.PKey) -> None:
        """Save or update the host key."""
```

Two implementations:

**`FileHostKeyStore`** — reads/writes an OpenSSH-format `known_hosts`
file at `KNOWN_HOSTS_FILE` path (default `/app/known_hosts`). Uses
`paramiko.hostkeys.HostKeys` internally. Flushes immediately.

**`PostgresHostKeyStore`** — table `ssh_host_keys`:

| Column | Type | Notes |
|--------|------|-------|
| `host` | VARCHAR(255) | |
| `port` | INTEGER | default 22 |
| `key_type` | VARCHAR(32) | e.g. ssh-rsa, ssh-ed25519 |
| `key_data` | TEXT | base64-encoded key bytes |
| `fingerprint` | VARCHAR(64) | SHA256 fingerprint for lookup |
| `updated_at` | TIMESTAMP | |
| `created_at` | TIMESTAMP | |

Unique constraint on `(host, port, key_type)`.

### KnownHostsPolicy

```python
class KnownHostsPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, store: HostKeyStore):
        self._store = store

    def missing_host_key(self, client, hostname, key):
        # synchronous — runs in executor
        loop = asyncio.get_event_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._check_or_store(hostname, key), loop
        )
        result = future.result()
        if result is not None:
            raise result

    async def _check_or_store(self, hostname, key):
        known = await self._store.check(hostname, …)
        if known is None:
            await self._store.store(hostname, …, key)
        elif not known:
            logger.warning(...)
            await self._store.store(hostname, …, key)
```

`MissingHostKeyPolicy.missing_host_key` is called *synchronously* by
Paramiko inside `client.connect()`. The policy bridges to the async
store via `run_coroutine_threadsafe`.

### Integration in SSHSessionManager

```python
class SSHSessionManager:
    def __init__(self, …, host_key_store: HostKeyStore | None = None):
        self._host_key_store = host_key_store or NullHostKeyStore()
        self._strict_host_key = settings.ssh_strict_host_key_checking

    def _get_host_key_policy(self):
        if self._strict_host_key:
            return paramiko.RejectPolicy()
        if self._host_key_store:
            return KnownHostsPolicy(self._host_key_store)
        return paramiko.AutoAddPolicy()
```

`NullHostKeyStore.check()` returns `None` (unknown → auto-add), `store()`
is no-op — equivalent to current `AutoAddPolicy`.

### Config

```python
known_hosts_store: str = Field(default="", alias="KNOWN_HOSTS_STORE")
known_hosts_file: str = Field(default="/app/known_hosts", alias="KNOWN_HOSTS_FILE")
```

- `KNOWN_HOSTS_STORE=postgres` → `PostgresHostKeyStore`
- `KNOWN_HOSTS_STORE=file` → `FileHostKeyStore`
- `KNOWN_HOSTS_STORE` empty or unset → `NullHostKeyStore` (AutoAddPolicy as before)

### API

```
GET  /api/known-hosts             → list[host, port, key_type, fingerprint]
DELETE /api/known-hosts           → clear all
DELETE /api/known-hosts/{host}    → remove entries for host (any port)
```

Read-only management. Writes happen automatically via policy.

### Startup

In `app.main` lifespan, after all other managers:

```python
host_key_store = create_host_key_store(settings)
```

File store: initializes `paramiko.hostkeys.HostKeys`, loads file from
disk, no error if file missing.

Postgres store: ensures table exists via
`Base.metadata.create_all`.

Passed to `SSHSessionManager(host_key_store=host_key_store)`.

### Testing

- `tests/test_host_key_store.py` — FileHostKeyStore CRUD, Postgres HostKeyStore
  CRUD, KnownHostsPolicy: first visit → stores, second visit same key → pass,
  changed key → warn + update
- Existing SSH manager tests updated to inject `NullHostKeyStore`
  (preserves current AutoAddPolicy behaviour)

### Error handling

- File store: write errors → log + fallback to in-memory cache, no crash
- Postgres store: connection errors → log + fallback to accept (fail-open
  or fail-closed via config)
- Key change (host rotated its host key): stored key is overwritten with
  a warning. Operator can audit via `/api/known-hosts`

---

## Changes summary

| File | Change |
|------|--------|
| `app/config.py` | +`known_hosts_store`, +`known_hosts_file` |
| `app/known_hosts.py` | New — `HostKeyStore`, `FileHostKeyStore`, `PostgresHostKeyStore`, `KnownHostsPolicy`, `create_host_key_store()` |
| `app/main.py` | Create store in lifespan, inject into SSHManager |
| `app/ssh_manager.py` | `__init__` accepts `host_key_store`, `_get_host_key_policy` uses `KnownHostsPolicy` when store available |
| `app/routers/system.py` | Add `GET/DELETE /api/known-hosts` routes |
| `app/state.py` | +`host_key_store` |
| `tests/test_host_key_store.py` | New — unit tests |
| Existing tests | Inject `NullHostKeyStore` or no change |

No changes to `session_store.py` or `security.py`.

## Out of scope

- `ssh-keyscan`-style probe — user manually pre-populates from CLI
- host key expiry / rotation alerts
- Multi-host wildcards (e.g. `*.example.com`)
