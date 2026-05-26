# Known Hosts Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `AutoAddPolicy` with a pluggable host key store (file or PostgreSQL) that auto-adds new keys and auto-updates changed keys with a warning.

**Architecture:** `HostKeyStore` ABC with `FileHostKeyStore` (OpenSSH known_hosts format via paramiko.hostkeys.HostKeys) and `PostgresHostKeyStore` (SQLAlchemy table `ssh_host_keys`). Custom `KnownHostsPolicy(paramiko.MissingHostKeyPolicy)` bridges the sync Paramiko callback to the async store via `run_coroutine_threadsafe`.

**Tech Stack:** Python 3.12, FastAPI, Paramiko, SQLAlchemy async, PostgreSQL 16

---

### Task 1: Config settings + HostKeyStore ABC + factory

**Files:**
- Modify: `app/config.py`
- Create: `app/known_hosts.py`
- Create: `tests/test_host_key_store.py`

- [ ] **Step 1: Add settings to `app/config.py`**

Add after line 29 (`ssh_strict_host_key_checking`):
```python
known_hosts_store: str = Field(default="", alias="KNOWN_HOSTS_STORE")
known_hosts_file: str = Field(default="/app/known_hosts", alias="KNOWN_HOSTS_FILE")
```

- [ ] **Step 2: Write failing test for HostKeyStore ABC and NullHostKeyStore**

```python
"""Tests for host key store backends."""

import pytest
from app.known_hosts import HostKeyStore, NullHostKeyStore, create_host_key_store


class TestHostKeyStoreAbc:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            HostKeyStore()  # noqa


class TestNullHostKeyStore:
    @pytest.mark.asyncio
    async def test_check_returns_none(self):
        store = NullHostKeyStore()
        result = await store.check("host", 22, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_store_is_noop(self):
        store = NullHostKeyStore()
        await store.store("host", 22, None)  # should not raise
```

- [ ] **Step 3: Run to confirm failure**

```
cd /media/1TB/Python/web_ssh/web-ssh-gateway
python -m pytest tests/test_host_key_store.py -v
Expected: ModuleNotFoundError for app.known_hosts
```

- [ ] **Step 4: Create `app/known_hosts.py` with ABC + NullHostKeyStore + factory stub**

```python
"""Host key storage backends and custom MissingHostKeyPolicy."""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional

import paramiko

from app.config import settings

logger = logging.getLogger(__name__)


class HostKeyStore(ABC):
    """Abstract host key store. check() returns:
    None = unknown host, True = key matches, False = key changed.
    """

    @abstractmethod
    async def check(self, host: str, port: int, key: paramiko.PKey) -> Optional[bool]:
        ...

    @abstractmethod
    async def store(self, host: str, port: int, key: paramiko.PKey) -> None:
        ...


class NullHostKeyStore(HostKeyStore):
    """No-op store — every host is unknown, store() does nothing."""

    async def check(self, host: str, port: int, key: paramiko.PKey) -> Optional[bool]:
        return None

    async def store(self, host: str, port: int, key: paramiko.PKey) -> None:
        pass


def create_host_key_store(settings) -> HostKeyStore:
    kind = settings.known_hosts_store
    if kind == "file":
        from app.known_hosts import FileHostKeyStore
        return FileHostKeyStore(settings.known_hosts_file)
    if kind == "postgres":
        from app.known_hosts import PostgresHostKeyStore
        return PostgresHostKeyStore(settings.database_url)
    return NullHostKeyStore()
```

- [ ] **Step 5: Run tests to verify they pass**

```
python -m pytest tests/test_host_key_store.py -v
Expected: 2 passed
```

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/known_hosts.py tests/test_host_key_store.py
git commit -m "feat: add HostKeyStore ABC, NullHostKeyStore, config settings, factory"
```

---

### Task 2: FileHostKeyStore

**Files:**
- Modify: `app/known_hosts.py`
- Modify: `tests/test_host_key_store.py`

- [ ] **Step 1: Write failing test for FileHostKeyStore**

Add after existing tests:
```python
import tempfile
import os
import paramiko


class TestFileHostKeyStore:
    @pytest.mark.asyncio
    async def test_unknown_host_returns_none(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp") as f:
            os.unlink(f.name)  # file doesn't exist yet
            store = FileHostKeyStore(f.name)
            result = await store.check("10.0.0.1", 22, None)
            assert result is None

    @pytest.mark.asyncio
    async def test_store_and_check_match(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp") as f:
            os.unlink(f.name)
            store = FileHostKeyStore(f.name)
            key = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key)
            result = await store.check("10.0.0.1", 22, key)
            assert result is True

    @pytest.mark.asyncio
    async def test_changed_key_returns_false(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp") as f:
            os.unlink(f.name)
            store = FileHostKeyStore(f.name)
            key1 = paramiko.RSAKey.generate(2048)
            key2 = paramiko.RSAKey.generate(2048)
            await store.store("10.0.0.1", 22, key1)
            result = await store.check("10.0.0.1", 22, key2)
            assert result is False
```

- [ ] **Step 2: Run to confirm failure**

```
python -m pytest tests/test_host_key_store.py -v
Expected: ImportError for FileHostKeyStore or NotImplementedError
```

- [ ] **Step 3: Implement FileHostKeyStore in `app/known_hosts.py`**

Add after NullHostKeyStore:
```python
class FileHostKeyStore(HostKeyStore):
    """OpenSSH-format known_hosts file via paramiko.hostkeys.HostKeys."""

    def __init__(self, path: str):
        self._path = path
        self._hk = paramiko.hostkeys.HostKeys()
        self._loaded = False
        self._lock = asyncio.Lock()

    async def _load(self):
        if self._loaded:
            return
        loop = asyncio.get_event_loop()
        self._hk = await loop.run_in_executor(
            None, lambda: paramiko.hostkeys.HostKeys(self._path)
        )
        self._loaded = True

    async def _save(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._hk.save, self._path)

    async def check(self, host: str, port: int, key: paramiko.PKey) -> Optional[bool]:
        async with self._lock:
            await self._load()
        host_key = self._hk.lookup(host)
        if host_key is None:
            return None
        for known_type, known_key in host_key:
            if known_key == key:
                return True
        return False

    async def store(self, host: str, port: int, key: paramiko.PKey) -> None:
        async with self._lock:
            await self._load()
            self._hk.add(host, key, port=port)
            try:
                await self._save()
            except Exception as exc:
                logger.warning("Failed to write known_hosts file %s: %s", self._path, exc)
```

- [ ] **Step 4: Run to verify pass**

```
python -m pytest tests/test_host_key_store.py -v
Expected: 5 passed
```

- [ ] **Step 5: Commit**

```bash
git add app/known_hosts.py tests/test_host_key_store.py
git commit -m "feat: add FileHostKeyStore"
```

---

### Task 3: PostgresHostKeyStore

**Files:**
- Modify: `app/known_hosts.py`
- Modify: `tests/test_host_key_store.py`

- [ ] **Step 1: Write failing test for PostgresHostKeyStore**

Add after TestFileHostKeyStore:
```python
class TestPostgresHostKeyStore:
    @pytest.mark.asyncio
    async def test_unknown_host_returns_none(self):
        store = PostgresHostKeyStore("sqlite+aiosqlite:///:memory:")
        await store._init_db()
        result = await store.check("10.0.0.1", 22, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_store_and_check_match(self):
        store = PostgresHostKeyStore("sqlite+aiosqlite:///:memory:")
        await store._init_db()
        key = paramiko.RSAKey.generate(2048)
        await store.store("10.0.0.1", 22, key)
        result = await store.check("10.0.0.1", 22, key)
        assert result is True

    @pytest.mark.asyncio
    async def test_changed_key_returns_false(self):
        store = PostgresHostKeyStore("sqlite+aiosqlite:///:memory:")
        await store._init_db()
        key1 = paramiko.RSAKey.generate(2048)
        key2 = paramiko.RSAKey.generate(2048)
        await store.store("10.0.0.1", 22, key1)
        result = await store.check("10.0.0.1", 22, key2)
        assert result is False
```

Note: `aiosqlite` is required for testing. Add to `tests/requirements.txt` or ensure dev install.

- [ ] **Step 2: Run to confirm failure**

```
python -m pytest tests/test_host_key_store.py -v
Expected: ImportError for PostgresHostKeyStore
```

- [ ] **Step 3: Implement PostgresHostKeyStore in `app/known_hosts.py`**

Add after FileHostKeyStore. Import block becomes:
```python
from abc import ABC, abstractmethod
from typing import Optional
import base64
import hashlib

import paramiko

from app.config import settings

try:
    from sqlalchemy import (
        Column, String, Integer, Text, DateTime, create_engine, UniqueConstraint,
    )
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
    from sqlalchemy.orm import declarative_base
    _sa_available = True
except ImportError:
    _sa_available = False
```

Add at end of file:
```python
class PostgresHostKeyStore(HostKeyStore):
    """PostgreSQL-backed host key store using SQLAlchemy async."""

    def __init__(self, database_url: str):
        if not _sa_available:
            raise RuntimeError("SQLAlchemy is not installed")
        self._database_url = database_url
        self._engine = None
        self._session_maker = None
        self._lock = asyncio.Lock()

    async def _init_db(self):
        if self._engine is not None:
            return
        self._engine = create_async_engine(self._database_url, echo=False)
        self._session_maker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False,
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def _key_data(self, key: paramiko.PKey) -> str:
        return base64.b64encode(key.asbytes()).decode()

    def _key_type(self, key: paramiko.PKey) -> str:
        return key.get_name()

    def _fingerprint(self, key: paramiko.PKey) -> str:
        return hashlib.sha256(key.asbytes()).hexdigest()

    async def check(self, host: str, port: int, key: paramiko.PKey) -> Optional[bool]:
        async with self._lock:
            await self._init_db()
        async with self._session_maker() as session:
            from sqlalchemy import select
            result = await session.execute(
                select(HostKeyRecord).where(
                    HostKeyRecord.host == host,
                    HostKeyRecord.port == port,
                    HostKeyRecord.key_type == self._key_type(key),
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                return None
            if record.key_data == self._key_data(key):
                return True
            return False

    async def store(self, host: str, port: int, key: paramiko.PKey) -> None:
        async with self._lock:
            await self._init_db()
        async with self._session_maker() as session:
            from sqlalchemy import select
            result = await session.execute(
                select(HostKeyRecord).where(
                    HostKeyRecord.host == host,
                    HostKeyRecord.port == port,
                    HostKeyRecord.key_type == self._key_type(key),
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                record = HostKeyRecord(
                    host=host, port=port,
                    key_type=self._key_type(key),
                    key_data=self._key_data(key),
                    fingerprint=self._fingerprint(key),
                )
                session.add(record)
            else:
                record.key_data = self._key_data(key)
                record.fingerprint = self._fingerprint(key)
            await session.commit()

    async def list_keys(self) -> list[dict]:
        async with self._lock:
            await self._init_db()
        async with self._session_maker() as session:
            from sqlalchemy import select
            result = await session.execute(select(HostKeyRecord))
            records = result.scalars().all()
            return [
                {"host": r.host, "port": r.port, "key_type": r.key_type,
                 "fingerprint": r.fingerprint, "updated_at": r.updated_at.isoformat() if r.updated_at else None}
                for r in records
            ]

    async def delete_host(self, host: str) -> int:
        async with self._lock:
            await self._init_db()
        async with self._session_maker() as session:
            from sqlalchemy import delete as sa_delete
            result = await session.execute(
                sa_delete(HostKeyRecord).where(HostKeyRecord.host == host)
            )
            await session.commit()
            return result.rowcount

    async def delete_all(self) -> int:
        async with self._lock:
            await self._init_db()
        async with self._session_maker() as session:
            from sqlalchemy import delete as sa_delete
            result = await session.execute(sa_delete(HostKeyRecord))
            await session.commit()
            return result.rowcount

    async def disconnect(self):
        if self._engine:
            await self._engine.dispose()


# SQLAlchemy model for host keys
if _sa_available:
    from datetime import datetime, timezone

    Base = declarative_base()

    class HostKeyRecord(Base):
        __tablename__ = "ssh_host_keys"

        host = Column(String(255), primary_key=True)
        port = Column(Integer, primary_key=True, default=22)
        key_type = Column(String(32), primary_key=True)
        key_data = Column(Text, nullable=False)
        fingerprint = Column(String(128), nullable=False)
        updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

        __table_args__ = (
            UniqueConstraint("host", "port", "key_type", name="uq_host_key_type"),
        )
```

Also add `list_keys`, `delete_host`, `delete_all` to the ABC as optional (or add them only on PostgresHostKeyStore since FileHostKeyStore exposes via the known_hosts file directly).

Actually, for the API routes to work consistently, the host key store should have a common interface for listing and deleting. Let me add those methods to the HostKeyStore protocol.

Actually, to keep things simpler — let me add `list_keys`, `delete_host`, `delete_all` only to `PostgresHostKeyStore` and expose them via the router directly. The FileHostKeyStore API can return empty list (or be read from the file). Simpler: add `list_keys()` and `delete_host()` to the ABC with default no-op, override in PostgresHostKeyStore.

Let me add to ABC:
```python
class HostKeyStore(ABC):
    ...
    async def list_keys(self) -> list[dict]:
        return []

    async def delete_host(self, host: str) -> int:
        return 0

    async def delete_all(self) -> int:
        return 0

    async def disconnect(self):
        pass
```

And update `FileHostKeyStore` to override `list_keys` by reading the file.

Actually, for the file store, listing keys means reading the HostKeys and iterating. That's reasonable. Let me add that to FileHostKeyStore too.

Add to FileHostKeyStore:
```python
    async def list_keys(self) -> list[dict]:
        async with self._lock:
            await self._load()
        results = []
        for host, entries in self._hk.items():
            for key_type, key in entries:
                results.append({
                    "host": host,
                    "port": 22,  # port not stored in HostKeys format directly
                    "key_type": key_type,
                    "fingerprint": hashlib.sha256(key.asbytes()).hexdigest(),
                })
        return results

    async def delete_host(self, host: str) -> int:
        async with self._lock:
            await self._load()
            if host in self._hk:
                del self._hk._entries[host]
                await self._save()
                return 1
        return 0
```

- [ ] **Step 4: Run to verify pass**

```
python -m pytest tests/test_host_key_store.py -v
Expected: 8 passed
```

- [ ] **Step 5: Commit**

```bash
git add app/known_hosts.py tests/test_host_key_store.py
git commit -m "feat: add PostgresHostKeyStore with SQLAlchemy model"
```

---

### Task 4: KnownHostsPolicy

**Files:**
- Modify: `app/known_hosts.py`
- Create: `tests/test_known_hosts_policy.py`

- [ ] **Step 1: Write failing test for KnownHostsPolicy**

```python
"""Tests for KnownHostsPolicy integration."""

import asyncio
import pytest
import paramiko

from app.known_hosts import KnownHostsPolicy, NullHostKeyStore, InMemoryHostKeyStore


class InMemoryHostKeyStore:
    """Simple in-memory store for policy tests."""
    def __init__(self):
        self._keys: dict[tuple[str, int], str] = {}

    async def check(self, host: str, port: int, key: paramiko.PKey):
        key_data = base64.b64encode(key.asbytes()).decode()
        stored = self._keys.get((host, port))
        if stored is None:
            return None
        if stored == key_data:
            return True
        return False

    async def store(self, host: str, port: int, key: paramiko.PKey):
        self._keys[(host, port)] = base64.b64encode(key.asbytes()).decode()


class TestKnownHostsPolicy:
    def test_first_visit_stores_key(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=22)
        hostname = "10.0.0.1"
        key = paramiko.RSAKey.generate(2048)
        policy.missing_host_key(None, hostname, key)
        assert store._keys.get((hostname, 22)) is not None

    def test_same_key_does_not_raise(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=22)
        hostname = "10.0.0.1"
        key = paramiko.RSAKey.generate(2048)
        policy.missing_host_key(None, hostname, key)  # first visit
        policy.missing_host_key(None, hostname, key)  # second visit — no error

    def test_changed_key_updates_and_returns(self):
        store = InMemoryHostKeyStore()
        policy = KnownHostsPolicy(store, port=22)
        hostname = "10.0.0.1"
        key1 = paramiko.RSAKey.generate(2048)
        key2 = paramiko.RSAKey.generate(2048)
        policy.missing_host_key(None, hostname, key1)  # store key1
        policy.missing_host_key(None, hostname, key2)  # update to key2
        assert store._keys[(hostname, 22)] == base64.b64encode(key2.asbytes()).decode()
```

- [ ] **Step 2: Run to confirm failure**

```
python -m pytest tests/test_known_hosts_policy.py -v
Expected: ImportError for KnownHostsPolicy
```

- [ ] **Step 3: Implement KnownHostsPolicy in `app/known_hosts.py`**

Design note: `missing_host_key` is called by Paramiko when `_host_keys` has
no entry for the host. Our store is independent of Paramiko's
`_host_keys`. We keep `_host_keys` empty so `missing_host_key` is called
on every connection, giving us full control. The port comes from the
SSHSessionManager that creates the policy.

Add before `create_host_key_store`:
```python
class KnownHostsPolicy(paramiko.MissingHostKeyPolicy):
    """Paramiko host key policy backed by an async HostKeyStore.

    Called by Paramiko when _host_keys has no entry for the host.
    Since we keep _host_keys empty, this is called on every connection.
    """

    def __init__(self, store: HostKeyStore, port: int = 22):
        self._store = store
        self._port = port
        self._loop = asyncio.get_event_loop()

    def missing_host_key(self, client, hostname, key):
        future = asyncio.run_coroutine_threadsafe(
            self._check_or_store(hostname, key), self._loop
        )
        future.result()  # raises if store fails

    async def _check_or_store(self, hostname, key):
        known = await self._store.check(hostname, self._port, key)
        if known is None:
            await self._store.store(hostname, self._port, key)
            logger.info("Stored host key for %s (%s)", hostname, key.get_name())
        elif not known:
            logger.warning(
                "Host key for %s changed (%s). Auto-updating.",
                hostname, key.get_name(),
            )
            await self._store.store(hostname, self._port, key)
```

- [ ] **Step 4: Run to verify pass**

```
python -m pytest tests/test_known_hosts_policy.py -v
Expected: 3 passed
```

- [ ] **Step 5: Commit**

```bash
git add app/known_hosts.py tests/test_known_hosts_policy.py
git commit -m "feat: add KnownHostsPolicy (sync->async bridge)"
```

---

### Task 5: Integrate into SSHSessionManager

**Files:**
- Modify: `app/ssh_manager.py`
- Modify: `tests/test_ssh_manager.py`

- [ ] **Step 1: Update SSHSessionManager.__init__ to accept host_key_store**

```python
    def __init__(self, session_timeout: int = 300, cleanup_interval: int = 60,
                 max_sessions: int = 100,
                 host_key_store: Optional[HostKeyStore] = None) -> None:
        ...
        self._host_key_store = host_key_store or NullHostKeyStore()
```

Update `_get_host_key_policy` to accept port:
```python
    def _get_host_key_policy(self, port=22):
        if self._strict_host_key:
            return paramiko.RejectPolicy()
        if not isinstance(self._host_key_store, NullHostKeyStore):
            from app.known_hosts import KnownHostsPolicy
            return KnownHostsPolicy(self._host_key_store, port=port)
        return paramiko.AutoAddPolicy()
```

Update all calls to `_get_host_key_policy()` (in `create_session`, `restore_session`, `reconnect`):
```python
client.set_missing_host_key_policy(self._get_host_key_policy(port=port))
```

Need to add import at top of `ssh_manager.py`:
```python
from app.known_hosts import HostKeyStore, NullHostKeyStore
```

- [ ] **Step 2: Update existing SSH manager tests**

In `tests/test_ssh_manager.py`, all tests that instantiate `SSHSessionManager` should continue to work because `host_key_store` defaults to `None` which becomes `NullHostKeyStore` → `AutoAddPolicy`.

Run existing tests:
```
python -m pytest tests/test_ssh_manager.py -v
Expected: all pass (no changes needed)
```

- [ ] **Step 3: Commit**

```bash
git add app/ssh_manager.py
git commit -m "feat: integrate KnownHostsPolicy into SSHSessionManager"
```

---

### Task 6: Integrate into lifespan and state

**Files:**
- Modify: `app/state.py`
- Modify: `app/main.py`

- [ ] **Step 1: Add host_key_store to state.py**

```python
# After session_store line:
from app.known_hosts import HostKeyStore
...
host_key_store: Optional[HostKeyStore] = None
```

- [ ] **Step 2: Create store in main.py lifespan**

After `state.session_store` block (or before `state.manager` creation), add:
```python
    # Initialize Host Key Store
    state.host_key_store = create_host_key_store(settings)
    if not isinstance(state.host_key_store, NullHostKeyStore):
        logger.info("Host key store initialized: %s", type(state.host_key_store).__name__)
```

Pass to SSHManager:
```python
    state.manager = SSHSessionManager(
        session_timeout=settings.session_timeout,
        cleanup_interval=settings.cleanup_interval,
        host_key_store=state.host_key_store,
    )
```

Ensure `create_host_key_store` and `NullHostKeyStore` are imported in main.py:
```python
from app.known_hosts import create_host_key_store, NullHostKeyStore
```

- [ ] **Step 3: Add shutdown for PostgresHostKeyStore**

In shutdown section of lifespan, after `state.session_store` disconnect:
```python
    if state.host_key_store:
        await state.host_key_store.disconnect()
```

- [ ] **Step 4: Verify app starts**

```
cd /media/1TB/Python/web_ssh/web-ssh-gateway
python -c "from app.main import app; print('OK')"
Expected: OK
```

- [ ] **Step 5: Commit**

```bash
git add app/state.py app/main.py
git commit -m "feat: initialize host key store in lifespan"
```

---

### Task 7: API routes for known-hosts management

**Files:**
- Modify: `app/routers/system.py`
- Modify: `tests/test_rate_limit.py` (if needed)

- [ ] **Step 1: Add known-hosts endpoint models to `app/models.py`**

Add somewhere:
```python
class KnownHostEntry(BaseModel):
    host: str
    port: int
    key_type: str
    fingerprint: str
```

- [ ] **Step 2: Add routes to system.py**

Add after the health endpoint or at end of file:
```python
@router.get("/api/known-hosts")
async def list_known_hosts():
    entries = await _state.host_key_store.list_keys()
    return {"hosts": entries}


@router.delete("/api/known-hosts/{host}")
async def delete_known_host(host: str):
    count = await _state.host_key_store.delete_host(host)
    if count == 0:
        raise HTTPException(404, detail=_err(404, f"No known hosts found for {host}"))
    return {"deleted": count}


@router.delete("/api/known-hosts")
async def clear_known_hosts():
    count = await _state.host_key_store.delete_all()
    return {"deleted": count}
```

- [ ] **Step 3: Run all non-integration tests to verify**

```
python -m pytest tests/ -q --ignore=tests/test_ssh_integration.py --ignore=tests/test_mtls_e2e.py --ignore=tests/test_openapi_contract.py --ignore=tests/test_nginx_ssh_template.py
Expected: all pass
```

- [ ] **Step 4: Update `_path_tag` and `TAGS_META` in main.py**

Add to TAGS_META:
```python
"known-hosts": "Host key store management",
```

In `_path_tag`:
```python
    if path.startswith("/api/known-hosts"):
        return "known-hosts"
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/system.py app/main.py
git commit -m "feat: add known-hosts API routes (list, delete, clear)"
```

---

### Task 8: Full verification

- [ ] **Step 1: Run all non-integration tests**

```
cd /media/1TB/Python/web_ssh/web-ssh-gateway
python -m pytest tests/ -q --ignore=tests/test_ssh_integration.py --ignore=tests/test_mtls_e2e.py --ignore=tests/test_openapi_contract.py --ignore=tests/test_nginx_ssh_template.py
Expected: all pass
```

- [ ] **Step 2: Verify route registration**

```
python -c "
from app.main import app
from collections import Counter
routes = [(r.path, list(r.methods) if hasattr(r, 'methods') else 'WS') for r in app.routes if hasattr(r, 'path')]
prefixes = Counter()
for path, _ in routes:
    parts = path.split('/')
    if len(parts) >= 3 and parts[1] == 'api':
        prefixes[parts[2]] += 1
for k, v in sorted(prefixes.items()):
    print(f'  /api/{k}: {v}')
"
Expected: known-hosts listed with 3 routes
```

- [ ] **Step 3: Commit final**

```bash
git add -A
git commit -m "feat: known-hosts store with File and PostgreSQL backends"
```
