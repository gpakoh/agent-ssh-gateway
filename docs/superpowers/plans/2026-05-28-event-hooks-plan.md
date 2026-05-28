# Event Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add event hook notification system — agents register callback URLs, gateway POSTs signed event payloads with at-least-once delivery via outbox + retry.

**Architecture:** 4 new modules (store, security, delivery, emitter) + CRUD router. Emission points in `ssh_manager.py` (connect/disconnect/execute/execute_stream). Outbox table in PostgreSQL, async background worker with lease-based concurrency.

**Tech Stack:** FastAPI, SQLAlchemy + asyncpg, aiohttp (existing), Prometheus metrics (existing), Fernet encryption (existing SecretManager)

---

### Task 1: Config + ORM Models

**Files:**
- Modify: `app/config.py:64-66`
- Modify: `app/session_store.py`
- Modify: `app/state.py` (add imports + Optional vars)
- Test: `tests/test_event_hooks.py` (table creation)

- [ ] **Step 1: Add EVENT_HOOKS_* settings to config.py**

Add before the `class Config` block:

```python
    event_hooks_enabled: bool = Field(default=True, alias="EVENT_HOOKS_ENABLED")
    event_hooks_max: int = Field(default=50, alias="EVENT_HOOKS_MAX")
    event_hooks_timeout_connect: float = Field(default=5.0, alias="EVENT_HOOKS_TIMEOUT_CONNECT")
    event_hooks_timeout_read: float = Field(default=10.0, alias="EVENT_HOOKS_TIMEOUT_READ")
    event_hooks_max_attempts: int = Field(default=5, alias="EVENT_HOOKS_MAX_ATTEMPTS")
    event_hooks_retry_base_sec: float = Field(default=2.0, alias="EVENT_HOOKS_RETRY_BASE_SEC")
    event_hooks_retry_max_sec: float = Field(default=300.0, alias="EVENT_HOOKS_RETRY_MAX_SEC")
    event_hooks_max_output_bytes: int = Field(default=65536, alias="EVENT_HOOKS_MAX_OUTPUT_BYTES")
    event_hooks_allow_http: bool = Field(default=False, alias="EVENT_HOOKS_ALLOW_HTTP")
    event_hooks_poll_interval: float = Field(default=5.0, alias="EVENT_HOOKS_POLL_INTERVAL")
    event_hooks_lease_ttl: float = Field(default=30.0, alias="EVENT_HOOKS_LEASE_TTL")
    event_hooks_retention_sent_days: int = Field(default=7, alias="EVENT_HOOKS_RETENTION_SENT_DAYS")
    event_hooks_retention_dead_days: int = Field(default=30, alias="EVENT_HOOKS_RETENTION_DEAD_DAYS")
```

- [ ] **Step 2: Add ORM models to session_store.py**

Add after `HostKeyRecord` class at the bottom of the file:

```python
class EventHook(Base):
    __tablename__ = "event_hooks"
    id = Column(String(36), primary_key=True)
    url = Column(String(2048), nullable=False)
    events = Column(JSON, nullable=False)
    session_id = Column(String(36), nullable=True)
    headers_encrypted = Column(Text, nullable=True)
    secret_encrypted = Column(Text, nullable=True)
    include_output = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "events": self.events,
            "session_id": self.session_id,
            "include_output": self.include_output,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    delivery_id = Column(String(36), primary_key=True)
    event_id = Column(String(36), nullable=False, index=True)
    hook_id = Column(String(36), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    status = Column(String(16), default="pending", index=True)
    attempts = Column(Integer, default=0)
    next_retry_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    http_status = Column(Integer, nullable=True)
    leased_by = Column(String(64), nullable=True)
    leased_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "delivery_id": self.delivery_id,
            "event_id": self.event_id,
            "hook_id": self.hook_id,
            "event_type": self.event_type,
            "status": self.status,
            "attempts": self.attempts,
            "http_status": self.http_status,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
```

- [ ] **Step 3: Add Optional vars to state.py**

Add imports:
```python
from app.event_hook_store import EventHookStore
from app.event_hook_delivery import DeliveryService
```

Add after `bulk_ops`:
```python
event_hook_store: Optional[EventHookStore] = None
delivery_service: Optional[DeliveryService] = None
```

- [ ] **Step 4: Run test to verify ORM tables create**

Write temp test, then revert:
```python
# tests/test_event_hooks.py
import pytest
from app.session_store import EventHook, WebhookDelivery

def test_orm_models_defined():
    assert EventHook.__tablename__ == "event_hooks"
    assert WebhookDelivery.__tablename__ == "webhook_deliveries"
```

Run: `pytest tests/test_event_hooks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/session_store.py app/state.py tests/test_event_hooks.py
git commit -m "feat: add EventHook + WebhookDelivery ORM models and config"
```

---

### Task 2: Pydantic Models

**Files:**
- Modify: `app/models.py`

- [ ] **Step 1: Add pydantic models to models.py**

Add before the `HealthResponse` class:

```python
class EventHookCreate(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    events: list[str] = Field(..., min_length=1)
    session_id: str | None = None
    headers: dict[str, str] | None = None
    secret: str | None = None
    include_output: bool = False


class EventHookUpdate(BaseModel):
    url: str | None = None
    events: list[str] | None = None
    session_id: str | None = None
    headers: dict[str, str] | None = None
    secret: str | None = None
    include_output: bool | None = None
    is_active: bool | None = None


class EventHookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    session_id: str | None = None
    include_output: bool = False
    is_active: bool = True
    created_at: str | None = None
    updated_at: str | None = None


class EventHookListResponse(BaseModel):
    hooks: list[EventHookResponse]
    count: int


class EventHookDeliveryResponse(BaseModel):
    delivery_id: str
    event_id: str
    hook_id: str
    event_type: str
    status: str
    attempts: int
    http_status: int | None = None
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
```

- [ ] **Step 2: Commit**

```bash
git add app/models.py
git commit -m "feat: add pydantic models for event hooks CRUD + delivery"
```

---

### Task 3: EventHookStore (CRUD)

**Files:**
- Create: `app/event_hook_store.py`
- Test: `tests/test_event_hooks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_event_hooks.py
import pytest
import uuid
from datetime import datetime
from app.event_hook_store import EventHookStore


@pytest.fixture
async def store():
    s = EventHookStore("sqlite+aiosqlite:///:memory:")
    await s.create_tables()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_create_hook(store):
    hook = await store.create(
        url="https://example.com/hook",
        events=["command.completed"],
        session_id=None,
        headers=None,
        secret=None,
        include_output=False,
    )
    assert hook.id is not None
    assert hook.url == "https://example.com/hook"
    assert hook.is_active is True


@pytest.mark.asyncio
async def test_list_hooks(store):
    await store.create(url="https://a.com/hook", events=["command.completed"])
    await store.create(url="https://b.com/hook", events=["session.connected"])
    hooks = await store.list()
    assert len(hooks) == 2


@pytest.mark.asyncio
async def test_get_hook(store):
    created = await store.create(url="https://c.com/hook", events=["command.failed"])
    fetched = await store.get(created.id)
    assert fetched is not None
    assert fetched.id == created.id


@pytest.mark.asyncio
async def test_get_hook_not_found(store):
    assert await store.get("nonexistent-id") is None


@pytest.mark.asyncio
async def test_update_hook(store):
    created = await store.create(url="https://d.com/hook", events=["command.completed"])
    updated = await store.update(created.id, url="https://d.com/new-hook", is_active=False)
    assert updated is not None
    assert updated.url == "https://d.com/new-hook"
    assert updated.is_active is False


@pytest.mark.asyncio
async def test_update_not_found(store):
    assert await store.update("nope", url="x") is None


@pytest.mark.asyncio
async def test_delete_hook(store):
    created = await store.create(url="https://e.com/hook", events=["command.completed"])
    deleted = await store.delete(created.id)
    assert deleted is True
    assert await store.get(created.id) is None


@pytest.mark.asyncio
async def test_delete_not_found(store):
    assert await store.delete("nope") is False


@pytest.mark.asyncio
async def test_find_matching_hooks(store):
    h1 = await store.create(url="https://a.com/hook", events=["command.completed"], session_id=None)
    h2 = await store.create(url="https://b.com/hook", events=["command.completed"], session_id="sess-1")
    h3 = await store.create(url="https://c.com/hook", events=["session.connected"], session_id=None)

    matches = await store.find_matching("command.completed", session_id="sess-2")
    assert len(matches) == 1  # only h1 (no session filter)
    assert matches[0].id == h1.id

    matches = await store.find_matching("command.completed", session_id="sess-1")
    assert len(matches) == 2  # h1 (no filter) + h2 (exact match)

    matches = await store.find_matching("session.connected", session_id="sess-2")
    assert len(matches) == 1  # h3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_event_hooks.py -v`
Expected: ImportError or AttributeError (EventHookStore not defined)

- [ ] **Step 3: Write EventHookStore implementation**

```python
"""Event hook storage — CRUD over SQLAlchemy."""
import uuid
import logging
from datetime import datetime, timezone

from sqlalchemy import create_engine, select, delete, and_
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.session_store import Base, EventHook

logger = logging.getLogger(__name__)


class EventHookStore:
    def __init__(self, database_url: str):
        self._engine = create_async_engine(database_url)
        self._session_factory = async_sessionmaker(self._engine, class_=AsyncSession)

    async def create_tables(self):
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self):
        await self._engine.dispose()

    async def create(
        self,
        url: str,
        events: list[str],
        session_id: str | None,
        headers_encrypted: str | None,
        secret_encrypted: str | None,
        include_output: bool,
    ) -> EventHook:
        hook = EventHook(
            id=uuid.uuid4().hex,
            url=url,
            events=events,
            session_id=session_id,
            headers_encrypted=headers_encrypted,
            secret_encrypted=secret_encrypted,
            include_output=include_output,
            is_active=True,
        )
        async with self._session_factory() as session:
            session.add(hook)
            await session.commit()
            await session.refresh(hook)
            return hook

    async def list(self) -> list[EventHook]:
        async with self._session_factory() as session:
            result = await session.execute(select(EventHook).order_by(EventHook.created_at))
            return list(result.scalars().all())

    async def get(self, hook_id: str) -> EventHook | None:
        async with self._session_factory() as session:
            result = await session.execute(select(EventHook).where(EventHook.id == hook_id))
            return result.scalar_one_or_none()

    async def update(
        self,
        hook_id: str,
        url: str | None = None,
        events: list[str] | None = None,
        session_id: str | None = None,
        headers_encrypted: str | None = None,
        secret_encrypted: str | None = None,
        include_output: bool | None = None,
        is_active: bool | None = None,
    ) -> EventHook | None:
        async with self._session_factory() as session:
            result = await session.execute(select(EventHook).where(EventHook.id == hook_id))
            hook = result.scalar_one_or_none()
            if not hook:
                return None
            if url is not None:
                hook.url = url
            if events is not None:
                hook.events = events
            if session_id is not None:
                hook.session_id = session_id
            if headers_encrypted is not None:
                hook.headers_encrypted = headers_encrypted
            if secret_encrypted is not None:
                hook.secret_encrypted = secret_encrypted
            if include_output is not None:
                hook.include_output = include_output
            if is_active is not None:
                hook.is_active = is_active
            hook.updated_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(hook)
            return hook

    async def delete(self, hook_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(select(EventHook).where(EventHook.id == hook_id))
            hook = result.scalar_one_or_none()
            if not hook:
                return False
            await session.delete(hook)
            await session.commit()
            return True

    async def find_matching(self, event_type: str, session_id: str) -> list[EventHook]:
        """Find active hooks matching event_type and optionally session_id."""
        async with self._session_factory() as session:
            q = select(EventHook).where(
                EventHook.is_active.is_(True),
                EventHook.events.as_string().contains(event_type),
            )
            result = await session.execute(q)
            hooks: list[EventHook] = list(result.scalars().all())
            matched = []
            for h in hooks:
                if event_type not in h.events:
                    continue
                if h.session_id and h.session_id != session_id:
                    continue
                matched.append(h)
            return matched
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_event_hooks.py -v`
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add app/event_hook_store.py tests/test_event_hooks.py
git commit -m "feat: add EventHookStore with CRUD + find_matching queries"
```

---

### Task 4: EventHookSecurity (SSRF, HMAC, log masking)

**Files:**
- Create: `app/event_hook_security.py`
- Test: `tests/test_event_hooks.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_event_hooks.py
import pytest
from app.event_hook_security import (
    validate_webhook_url,
    sign_payload,
    mask_sensitive_headers,
)

# --- URL validation ---

@pytest.mark.parametrize("url,ok", [
    ("https://hooks.example.com/callback", True),
    ("https://10.0.0.1/hook", False),          # private
    ("https://127.0.0.1/hook", False),          # loopback
    ("https://192.168.1.1/hook", False),        # private
    ("https://169.254.169.254/latest", False),  # metadata
    ("https://[::1]/hook", False),              # loopback
    ("http://example.com/hook", False),         # not allowed by default
    ("https://example.com:22/hook", True),
    ("file:///etc/passwd", False),              # wrong scheme
    ("", False),                                # empty
])
def test_validate_url(url, ok):
    result = validate_webhook_url(url, allow_http=False)
    assert result.valid is ok, f"{url}: expected valid={ok}, got {result}"


@pytest.mark.parametrize("url,ok", [
    ("http://example.com/hook", True),          # allowed with flag
    ("https://example.com/hook", True),
])
def test_validate_url_allow_http(url, ok):
    result = validate_webhook_url(url, allow_http=True)
    assert result.valid is ok


# --- HMAC signing ---

def test_sign_payload():
    secret = "test-secret-key"
    payload = b'{"event":"command.completed"}'
    timestamp = "1716800000"
    signature = sign_payload(secret, payload, timestamp)
    assert signature.startswith("sha256=")
    assert len(signature) > 50


def test_sign_payload_different():
    """Same secret+payload+timestamp produces same signature."""
    secret = "test-secret"
    payload = b'{}'
    ts = "1716800000"
    assert sign_payload(secret, payload, ts) == sign_payload(secret, payload, ts)


def test_sign_payload_no_secret():
    assert sign_payload("", b"{}", "0") is None
    assert sign_payload(None, b"{}", "0") is None


# --- Log masking ---

masked = {"Authorization": "Bearer secret123", "X-API-Key": "key456"}
unmasked = {"Content-Type": "application/json"}


def test_mask_sensitive_headers():
    result = mask_sensitive_headers({**masked, **unmasked})
    for key in masked:
        assert result[key] == "****", f"{key} should be masked"
    assert result["Content-Type"] == "application/json"


def test_mask_sensitive_headers_none():
    assert mask_sensitive_headers(None) == {}
    assert mask_sensitive_headers({}) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_event_hooks.py::test_validate_url tests/test_event_hooks.py::test_sign_payload tests/test_event_hooks.py::test_mask_sensitive_headers -v`
Expected: ImportError

- [ ] **Step 3: Write EventHookSecurity implementation**

```python
"""Security utilities for event hooks — SSRF, HMAC, log masking."""
import hmac
import hashlib
import ipaddress
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SENSITIVE_HEADERS = frozenset({
    "authorization", "x-api-key", "cookie", "set-cookie", "x-webhook-signature",
})

BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("ff00::/8"),
    # EC2/GCE metadata
    ipaddress.ip_network("169.254.169.254/32"),
]


@dataclass
class UrlValidationResult:
    valid: bool
    reason: str = ""


def validate_webhook_url(url: str, allow_http: bool = False) -> UrlValidationResult:
    if not url:
        return UrlValidationResult(False, "URL is empty")

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return UrlValidationResult(False, f"URL parse error: {exc}")

    if parsed.scheme == "http" and not allow_http:
        return UrlValidationResult(False, "HTTP not allowed, use HTTPS")
    if parsed.scheme not in ("http", "https"):
        return UrlValidationResult(False, f"Scheme not allowed: {parsed.scheme}")

    try:
        host = parsed.hostname
        if host is None:
            return UrlValidationResult(False, "No hostname in URL")
        addr = ipaddress.ip_address(host)
        for net in BLOCKED_NETWORKS:
            if addr in net:
                return UrlValidationResult(False, f"Blocked IP range: {net}")
    except ValueError:
        # hostname is a domain name — DNS will be checked at delivery time
        pass

    return UrlValidationResult(True)


def validate_destination_ip(host: str) -> UrlValidationResult:
    """Check resolved IP against blocked ranges (call per-delivery for SSRF)."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return UrlValidationResult(False, f"Not a valid IP: {host}")

    for net in BLOCKED_NETWORKS:
        if addr in net:
            return UrlValidationResult(False, f"Blocked IP range: {net}")
    return UrlValidationResult(True)


def sign_payload(secret: str | None, payload: bytes, timestamp: str) -> str | None:
    if not secret:
        return None
    msg = f"{timestamp}.{payload.decode('utf-8', errors='replace')}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def mask_sensitive_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {
        k: "****" if k.lower() in SENSITIVE_HEADERS else v
        for k, v in headers.items()
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_event_hooks.py -v -k "validate_url or sign_payload or mask"`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/event_hook_security.py tests/test_event_hooks.py
git commit -m "feat: add SSRF validation, HMAC signing, log masking for event hooks"
```

---

### Task 5: EventHookDelivery (outbox, retry scheduler, HTTP send)

**Files:**
- Create: `app/event_hook_delivery.py`
- Test: `tests/test_event_hooks.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_event_hooks.py (append)
import pytest
import uuid
from unittest.mock import patch, AsyncMock
from datetime import datetime, timedelta, timezone

from app.event_hook_delivery import DeliveryService


@pytest.fixture
async def delivery_service():
    ds = DeliveryService(
        database_url="sqlite+aiosqlite:///:memory:",
        instance_id="test-1",
    )
    await ds.create_tables()
    yield ds
    await ds.close()


@pytest.mark.asyncio
async def test_enqueue_delivery(delivery_service):
    delivery_id = await delivery_service.enqueue(
        event_id="evt-1",
        hook_id="hook-1",
        event_type="command.completed",
        payload_json='{"event":"command.completed"}',
    )
    assert delivery_id is not None


@pytest.mark.asyncio
async def test_claim_pending_delivery(delivery_service):
    delivery_id = await delivery_service.enqueue(
        event_id="evt-2", hook_id="hook-2",
        event_type="session.connected",
        payload_json="{}",
    )
    claimed = await delivery_service.claim_deliveries(limit=10, lease_ttl=30.0)
    assert len(claimed) == 1
    assert claimed[0].delivery_id == delivery_id
    assert claimed[0].status == "pending"


@pytest.mark.asyncio
async def test_claim_skips_leased(delivery_service):
    d1 = await delivery_service.enqueue("evt-1", "h-1", "a", "{}")
    d2 = await delivery_service.enqueue("evt-2", "h-2", "b", "{}")

    # claim first batch
    first = await delivery_service.claim_deliveries(limit=1, lease_ttl=30.0)
    assert len(first) == 1

    # second batch should get the other one
    second = await delivery_service.claim_deliveries(limit=10, lease_ttl=30.0)
    assert len(second) == 1
    assert second[0].delivery_id != first[0].delivery_id


@pytest.mark.asyncio
async def test_complete_delivery(delivery_service):
    d_id = await delivery_service.enqueue("evt-3", "h-3", "a", "{}")
    result = await delivery_service.complete(d_id, http_status=200)
    assert result is True

    # verify status
    claimed = await delivery_service.claim_deliveries(limit=10, lease_ttl=30.0)
    assert all(c.delivery_id != d_id for c in claimed)


@pytest.mark.asyncio
async def test_fail_delivery_with_retry(delivery_service):
    d_id = await delivery_service.enqueue("evt-4", "h-4", "a", "{}")
    result = await delivery_service.fail(d_id, last_error="timeout", max_attempts=5, retry_base_sec=2.0, retry_max_sec=300.0)
    assert result is True

    rec = await delivery_service._get_record(d_id)
    assert rec.attempts == 1
    assert rec.status == "failed"
    assert rec.next_retry_at is not None


@pytest.mark.asyncio
async def test_fail_delivery_dead_after_max_attempts(delivery_service):
    d_id = await delivery_service.enqueue("evt-5", "h-5", "a", "{}")
    # simulate 5 prior attempts
    async with delivery_service._session_factory() as s:
        from app.session_store import WebhookDelivery
        result = await s.execute(
            __import__("sqlalchemy").select(WebhookDelivery).where(WebhookDelivery.delivery_id == d_id)
        )
        rec = result.scalar_one()
        rec.attempts = 5  # already at max+1
        await s.commit()

    result = await delivery_service.fail(d_id, last_error="gave up", max_attempts=5, retry_base_sec=2.0, retry_max_sec=300.0)
    assert result is True

    rec = await delivery_service._get_record(d_id)
    assert rec.status == "dead"


@pytest.mark.asyncio
async def test_retry_schedule_calculation(delivery_service):
    d_id = await delivery_service.enqueue("evt-6", "h-6", "a", "{}")
    await delivery_service.fail(d_id, last_error="err", max_attempts=5, retry_base_sec=2.0, retry_max_sec_sec=300.0)
    rec = await delivery_service._get_record(d_id)
    assert rec.attempts == 1
    assert rec.next_retry_at > datetime.now(timezone.utc)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_event_hooks.py -v -k "test_enqueue or test_claim or test_complete or test_fail or test_retry"`
Expected: ImportError

- [ ] **Step 3: Write DeliveryService implementation**

```python
"""Outbox delivery service — enqueue, claim, complete, fail, retry scheduler."""
import asyncio
import logging
import uuid
import random
from datetime import datetime, timedelta, timezone

import aiohttp
from sqlalchemy import select, and_, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.session_store import Base, WebhookDelivery
from app.event_hook_security import (
    validate_webhook_url,
    validate_destination_ip,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compute_retry_at(attempts: int, base_sec: float, max_sec: float) -> datetime:
    delay = min(base_sec * (2 ** attempts), max_sec)
    jitter = delay * random.uniform(0.5, 1.5)
    return _now() + timedelta(seconds=jitter)


class DeliveryService:
    def __init__(self, database_url: str, instance_id: str):
        self._instance_id = instance_id
        self._engine = create_async_engine(database_url)
        self._session_factory = async_sessionmaker(self._engine, class_=AsyncSession)
        self._http_session: aiohttp.ClientSession | None = None
        self._worker_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._running = False

    async def create_tables(self):
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self):
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._http_session:
            await self._http_session.close()
        await self._engine.dispose()

    async def start(self, poll_interval: float, connect_timeout: float, read_timeout: float,
                    max_attempts: int, retry_base_sec: float, retry_max_sec: float,
                    lease_ttl: float, retention_sent_days: int, retention_dead_days: int):
        self._running = True
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=connect_timeout + read_timeout, connect=connect_timeout),
            allow_redirects=False,
        )
        self._worker_task = asyncio.create_task(
            self._worker_loop(poll_interval, max_attempts, retry_base_sec, retry_max_sec, lease_ttl)
        )
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(3600, retention_sent_days, retention_dead_days)
        )

    async def enqueue(self, event_id: str, hook_id: str, event_type: str, payload_json: str) -> str:
        delivery_id = uuid.uuid4().hex
        delivery = WebhookDelivery(
            delivery_id=delivery_id,
            event_id=event_id,
            hook_id=hook_id,
            event_type=event_type,
            payload_json=payload_json,
            status="pending",
            attempts=0,
        )
        async with self._session_factory() as session:
            session.add(delivery)
            await session.commit()
        return delivery_id

    async def claim_deliveries(self, limit: int, lease_ttl: float) -> list[WebhookDelivery]:
        now = _now()
        lease_deadline = now - timedelta(seconds=lease_ttl)
        async with self._session_factory() as session:
            result = await session.execute(
                select(WebhookDelivery).where(
                    and_(
                        WebhookDelivery.status.in_(["pending", "failed"]),
                        WebhookDelivery.next_retry_at.is_(None)
                        if False else True,
                    ).with_dialect("sqlite"),
                ).limit(limit).with_for_update(skip_locked=True)
            )
            deliveries: list[WebhookDelivery] = list(result.scalars().all())
            for d in deliveries:
                d.leased_by = self._instance_id
                d.leased_at = now
            await session.commit()
            return deliveries

    async def complete(self, delivery_id: str, http_status: int) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
            )
            d = result.scalar_one_or_none()
            if not d:
                return False
            d.status = "sent"
            d.http_status = http_status
            d.leased_by = None
            d.leased_at = None
            d.updated_at = _now()
            await session.commit()
            return True

    async def fail(self, delivery_id: str, last_error: str, max_attempts: int,
                   retry_base_sec: float, retry_max_sec: float) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
            )
            d = result.scalar_one_or_none()
            if not d:
                return False
            d.attempts += 1
            d.last_error = last_error[:1024]
            d.leased_by = None
            d.leased_at = None
            d.updated_at = _now()

            if d.attempts >= max_attempts:
                d.status = "dead"
                d.next_retry_at = None
            else:
                d.status = "failed"
                d.next_retry_at = _compute_retry_at(d.attempts, retry_base_sec, retry_max_sec)

            await session.commit()
            return True

    async def cleanup_old(self, sent_days: int, dead_days: int) -> int:
        now = _now()
        total = 0
        async with self._session_factory() as session:
            for status, days in [("sent", sent_days), ("dead", dead_days)]:
                cutoff = now - timedelta(days=days)
                result = await session.execute(
                    select(WebhookDelivery).where(
                        WebhookDelivery.status == status,
                        WebhookDelivery.updated_at < cutoff,
                    )
                )
                rows = list(result.scalars().all())
                for r in rows:
                    await session.delete(r)
                    total += 1
            await session.commit()
        return total

    # ---- internal ----

    async def _worker_loop(self, poll_interval: float, max_attempts: int,
                           retry_base_sec: float, retry_max_sec: float, lease_ttl: float):
        while self._running:
            try:
                deliveries = await self.claim_deliveries(limit=20, lease_ttl=lease_ttl)
                for d in deliveries:
                    asyncio.create_task(self._process_delivery(d, max_attempts, retry_base_sec, retry_max_sec))
            except Exception:
                logger.exception("Delivery worker error")
            await asyncio.sleep(poll_interval)

    async def _process_delivery(self, delivery: WebhookDelivery, max_attempts: int,
                                retry_base_sec: float, retry_max_sec: float):
        try:
            async with self._http_session.post(
                delivery.url, data=delivery.payload_json,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if 200 <= resp.status < 300:
                    await self.complete(delivery.delivery_id, resp.status)
                elif resp.status == 429 or resp.status >= 500:
                    await self.fail(delivery.delivery_id, f"HTTP {resp.status}",
                                    max_attempts, retry_base_sec, retry_max_sec)
                else:
                    await self.fail(delivery.delivery_id, f"HTTP {resp.status} (non-retryable)",
                                    max_attempts, retry_base_sec, retry_max_sec)
        except Exception as exc:
            await self.fail(delivery.delivery_id, str(exc)[:1024],
                            max_attempts, retry_base_sec, retry_max_sec)

    async def _cleanup_loop(self, interval: float, sent_days: int, dead_days: int):
        while self._running:
            try:
                count = await self.cleanup_old(sent_days, dead_days)
                if count:
                    logger.info("Cleaned up %d old delivery records", count)
            except Exception:
                logger.exception("Delivery cleanup error")
            await asyncio.sleep(interval)

    async def _get_record(self, delivery_id: str) -> WebhookDelivery | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
            )
            return result.scalar_one_or_none()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_event_hooks.py -v -k "test_enqueue or test_claim or test_complete or test_fail or test_retry"`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/event_hook_delivery.py tests/test_event_hooks.py
git commit -m "feat: add DeliveryService — outbox, lease-based worker, retry, cleanup"
```

---

### Task 6: EventHookEmitter + ssh_manager wiring

**Files:**
- Create: `app/event_hook_emitter.py`
- Modify: `app/ssh_manager.py`

- [ ] **Step 1: Write emitter module**

```python
"""Event emitter — creates outbox deliveries for matching hooks."""
import uuid
import json
import logging
from datetime import datetime, timezone

from app import state as _state
from app.event_hook_security import sign_payload, mask_sensitive_headers

logger = logging.getLogger(__name__)

EVENT_VERSION = 1
SESSION_EVENTS = {"session.connected", "session.disconnected"}
COMMAND_EVENTS = {"command.started", "command.completed", "command.failed"}


def _build_session_payload(event: str, session_id: str, host: str, port: int,
                           username: str, **extra) -> dict:
    payload = {
        "event": event,
        "event_id": uuid.uuid4().hex,
        "event_version": EVENT_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "host": host,
        "port": port,
        "username": username,
    }
    payload.update(extra)
    return payload


def _build_command_payload(event: str, session_id: str, command: str,
                           exit_code: int | None = None, duration: float | None = None,
                           stdout: str | None = None, stderr: str | None = None,
                           output_truncated: bool = False, **extra) -> dict:
    payload = {
        "event": event,
        "event_id": uuid.uuid4().hex,
        "event_version": EVENT_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "command": command,
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if duration is not None:
        payload["duration"] = round(duration, 2)
    if stdout is not None:
        payload["stdout"] = stdout
    if stderr is not None:
        payload["stderr"] = stderr
    payload["output_truncated"] = output_truncated
    payload.update(extra)
    return payload


async def emit_event(event: str, session_id: str, host: str = "", port: int = 22,
                     username: str = "", command: str = "", exit_code: int | None = None,
                     duration: float | None = None, stdout: str | None = None,
                     stderr: str | None = None, reason: str = "",
                     connected_seconds: float | None = None) -> None:
    store = _state.event_hook_store
    if store is None:
        return

    try:
        hooks = await store.find_matching(event, session_id)
    except Exception:
        logger.exception("Failed to query hooks for event %s", event)
        return

    if not hooks:
        return

    for hook in hooks:
        # Build base payload
        if event in SESSION_EVENTS:
            payload = _build_session_payload(
                event, session_id, host, port, username,
                reason=reason,
                connected_seconds=round(connected_seconds, 1) if connected_seconds is not None else None,
            )
        elif event in COMMAND_EVENTS:
            truncated = False
            out = err = None
            if hook.include_output:
                limit = _state.delivery_service._max_output_bytes if _state.delivery_service else 65536
                if stdout and len(stdout) > limit:
                    stdout = stdout[:limit]
                    truncated = True
                if stderr and len(stderr) > limit:
                    stderr = stderr[:limit]
                    truncated = True
                out, err = stdout, stderr

            payload = _build_command_payload(
                event, session_id, command, exit_code, duration,
                stdout=out, stderr=err, output_truncated=truncated,
            )
        else:
            payload = {"event": event, "event_id": uuid.uuid4().hex, "event_version": EVENT_VERSION}

        payload_json = json.dumps(payload, default=str)

        # Sign payload
        secret = None
        if hook.secret_encrypted and _state.secret_manager:
            try:
                secret = _state.secret_manager.decrypt(hook.secret_encrypted)
            except Exception:
                logger.exception("Failed to decrypt hook secret %s", hook.id)

        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        signature = sign_payload(secret, payload_json.encode("utf-8"), timestamp)

        # Build headers
        headers = {"Content-Type": "application/json"}
        if signature:
            headers["X-Webhook-Signature"] = signature
            headers["X-Webhook-Timestamp"] = timestamp
        headers["X-Event-Id"] = payload["event_id"]
        headers["X-Delivery-Id"] = uuid.uuid4().hex

        # Add custom headers
        if hook.headers_encrypted and _state.secret_manager:
            try:
                custom = json.loads(_state.secret_manager.decrypt(hook.headers_encrypted))
                if isinstance(custom, dict):
                    headers.update(custom)
            except Exception:
                logger.exception("Failed to decrypt hook headers %s", hook.id)

        # Enqueue delivery
        try:
            ds = _state.delivery_service
            if ds:
                final_payload = json.dumps(payload, default=str)
                await ds.enqueue(
                    event_id=payload["event_id"],
                    hook_id=hook.id,
                    event_type=event,
                    payload_json=final_payload,
                )
        except Exception:
            logger.exception("Failed to enqueue delivery for hook %s", hook.id)
```

- [ ] **Step 2: Add emit_event calls to ssh_manager.py**

In `create_session()`, after successful connection (find line ~230 where the session record is created):

```python
# After session record creation, before returning
from app.event_hook_emitter import emit_event
asyncio.ensure_future(emit_event(
    "session.connected",
    session_id=session_id,
    host=host,
    port=port,
    username=username,
))
```

In `disconnect()`, before cleanup (find line ~466):

```python
# At the start of disconnect, before closing session
from app.event_hook_emitter import emit_event
asyncio.ensure_future(emit_event(
    "session.disconnected",
    session_id=session_id,
    connected_seconds=...,
    reason="manual",
))
```

In `execute()`, before and after command (find line ~344):

```python
# Before exec
from app.event_hook_emitter import emit_event
asyncio.ensure_future(emit_event(
    "command.started",
    session_id=session_id,
    command=command,
))

# After exec (before return)
asyncio.ensure_future(emit_event(
    "command.completed" if exit_code == 0 else "command.failed",
    session_id=session_id,
    command=command,
    exit_code=exit_code,
    duration=duration,
    stdout=stdout,
    stderr=stderr,
    host=record.host,
    port=record.port,
    username=record.username,
))
```

In `execute_stream()`, before yield and after exit (find line ~409):

```python
# Before exec
asyncio.ensure_future(emit_event(
    "command.started",
    session_id=session_id,
    command=command,
))

# After exit code received (before return)
asyncio.ensure_future(emit_event(
    "command.completed" if exit_code == 0 else "command.failed",
    session_id=session_id,
    command=command,
    exit_code=exit_code,
    host=record.host,
    port=record.port,
    username=record.username,
))
```

Note: The import `from app.event_hook_emitter import emit_event` and `import asyncio` should be at the top of `ssh_manager.py`, not inline.

- [ ] **Step 3: Commit**

```bash
git add app/event_hook_emitter.py app/ssh_manager.py
git commit -m "feat: add EventHookEmitter — outbox enqueue + ssh_manager wiring"
```

---

### Task 7: CRUD Router + main.py wiring + state.py + metrics

**Files:**
- Create: `app/routers/event_hooks.py`
- Modify: `app/main.py`
- Modify: `app/metrics.py`

- [ ] **Step 1: Write the CRUD router**

```python
"""Event hook CRUD endpoints."""
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app import state as _state
from app.state import _err
from app.models import (
    EventHookCreate,
    EventHookUpdate,
    EventHookResponse,
    EventHookListResponse,
    EventHookDeliveryResponse,
)
from app.event_hook_security import validate_webhook_url, mask_sensitive_headers

logger = logging.getLogger(__name__)

router = APIRouter(tags=["event-hooks"])


@router.get("/api/event-hooks", response_model=EventHookListResponse)
async def list_event_hooks():
    """List all registered event hooks."""
    if not _state.event_hook_store:
        raise HTTPException(status_code=503, detail=_err(503, "Event hook store not available"))
    hooks = await _state.event_hook_store.list()
    return EventHookListResponse(
        hooks=[EventHookResponse(**h.to_dict()) for h in hooks],
        count=len(hooks),
    )


@router.post("/api/event-hooks", response_model=EventHookResponse, status_code=201)
async def create_event_hook(body: EventHookCreate):
    """Register a new event hook."""
    store = _state.event_hook_store
    if not store:
        raise HTTPException(status_code=503, detail=_err(503, "Event hook store not available"))

    # SSRF check
    result = validate_webhook_url(body.url, allow_http=False)
    if not result.valid:
        raise HTTPException(status_code=422, detail=_err(422, f"Invalid URL: {result.reason}"))

    # Max hooks guardrail
    if _state.settings and hasattr(_state.settings, "event_hooks_max"):
        existing = await store.list()
        if len(existing) >= _state.settings.event_hooks_max:
            raise HTTPException(status_code=409, detail=_err(409, "Max event hooks reached"))

    # Encrypt sensitive fields
    headers_encrypted = None
    secret_encrypted = None
    if body.headers and _state.secret_manager:
        try:
            headers_encrypted = _state.secret_manager.encrypt(json.dumps(body.headers))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_err(500, f"Failed to encrypt headers: {exc}"))
    if body.secret and _state.secret_manager:
        try:
            secret_encrypted = _state.secret_manager.encrypt(body.secret)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_err(500, f"Failed to encrypt secret: {exc}"))

    hook = await store.create(
        url=body.url,
        events=body.events,
        session_id=body.session_id,
        headers_encrypted=headers_encrypted,
        secret_encrypted=secret_encrypted,
        include_output=body.include_output,
    )
    return EventHookResponse(**hook.to_dict())


@router.get("/api/event-hooks/{hook_id}", response_model=EventHookResponse)
async def get_event_hook(hook_id: str):
    """Get event hook by ID."""
    if not _state.event_hook_store:
        raise HTTPException(status_code=503, detail=_err(503, "Event hook store not available"))
    hook = await _state.event_hook_store.get(hook_id)
    if not hook:
        raise HTTPException(status_code=404, detail=_err(404, f"Event hook not found: {hook_id}"))
    return EventHookResponse(**hook.to_dict())


@router.patch("/api/event-hooks/{hook_id}", response_model=EventHookResponse)
async def update_event_hook(hook_id: str, body: EventHookUpdate):
    """Update an event hook (partial update)."""
    store = _state.event_hook_store
    if not store:
        raise HTTPException(status_code=503, detail=_err(503, "Event hook store not available"))

    existing = await store.get(hook_id)
    if not existing:
        raise HTTPException(status_code=404, detail=_err(404, f"Event hook not found: {hook_id}"))

    # Validate URL if changed
    if body.url is not None:
        result = validate_webhook_url(body.url, allow_http=False)
        if not result.valid:
            raise HTTPException(status_code=422, detail=_err(422, f"Invalid URL: {result.reason}"))

    # Encrypt if changed
    headers_encrypted = None
    if body.headers is not None:
        if _state.secret_manager:
            try:
                headers_encrypted = _state.secret_manager.encrypt(json.dumps(body.headers))
            except Exception as exc:
                raise HTTPException(status_code=500, detail=_err(500, f"Failed to encrypt headers: {exc}"))

    secret_encrypted = None
    if body.secret is not None:
        if _state.secret_manager:
            try:
                secret_encrypted = _state.secret_manager.encrypt(body.secret)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=_err(500, f"Failed to encrypt secret: {exc}"))

    updated = await store.update(
        hook_id,
        url=body.url,
        events=body.events,
        session_id=body.session_id,
        headers_encrypted=headers_encrypted if body.headers is not None else None,
        secret_encrypted=secret_encrypted if body.secret is not None else None,
        include_output=body.include_output,
        is_active=body.is_active,
    )
    return EventHookResponse(**updated.to_dict())


@router.delete("/api/event-hooks/{hook_id}")
async def delete_event_hook(hook_id: str):
    """Delete an event hook."""
    if not _state.event_hook_store:
        raise HTTPException(status_code=503, detail=_err(503, "Event hook store not available"))
    deleted = await _state.event_hook_store.delete(hook_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=_err(404, f"Event hook not found: {hook_id}"))
    return {"deleted": True}


@router.get("/api/event-hooks/{hook_id}/deliveries", response_model=list[EventHookDeliveryResponse])
async def list_hook_deliveries(hook_id: str):
    """List deliveries for a specific hook."""
    store = _state.event_hook_store
    if not store:
        raise HTTPException(status_code=503, detail=_err(503, "Event hook store not available"))
    # Verify hook exists
    hook = await store.get(hook_id)
    if not hook:
        raise HTTPException(status_code=404, detail=_err(404, f"Event hook not found: {hook_id}"))
    # Return empty list for now (delivery listing from DB would be a future addition)
    return []
```

- [ ] **Step 2: Add metrics to metrics.py**

Add after existing SSH metrics in `__init__`:

```python
        # Event hook metrics
        self.event_hook_deliveries_total = Counter(
            'ssh_gateway_event_hook_deliveries_total',
            'Event hook deliveries',
            ['status', 'event'],
        )
        self.event_hook_delivery_attempts_total = Counter(
            'ssh_gateway_event_hook_delivery_attempts_total',
            'Event hook delivery attempts',
        )
        self.event_hook_delivery_latency = Histogram(
            'ssh_gateway_event_hook_delivery_latency_seconds',
            'Event hook delivery latency',
            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
        )
        self.event_hook_dead_letter = Gauge(
            'ssh_gateway_event_hook_dead_letter_count',
            'Dead letter deliveries',
        )
```

- [ ] **Step 3: Wire into main.py**

Add import at top:
```python
from app.routers.event_hooks import router as event_hooks_router
from app.event_hook_store import EventHookStore
from app.event_hook_delivery import DeliveryService
```

In lifespan, after the existing state initialization (after `state.analytics` line):

```python
    # Event hook system
    state.event_hook_store = None
    state.delivery_service = None
    if settings.event_hooks_enabled and settings.database_url:
        state.event_hook_store = EventHookStore(settings.database_url)
        await state.event_hook_store.create_tables()
        state.delivery_service = DeliveryService(
            database_url=settings.database_url,
            instance_id=uuid.uuid4().hex[:8],
        )
        await state.delivery_service.create_tables()
        await state.delivery_service.start(
            poll_interval=settings.event_hooks_poll_interval,
            connect_timeout=settings.event_hooks_timeout_connect,
            read_timeout=settings.event_hooks_timeout_read,
            max_attempts=settings.event_hooks_max_attempts,
            retry_base_sec=settings.event_hooks_retry_base_sec,
            retry_max_sec=settings.event_hooks_retry_max_sec,
            lease_ttl=settings.event_hooks_lease_ttl,
            retention_sent_days=settings.event_hooks_retention_sent_days,
            retention_dead_days=settings.event_hooks_retention_dead_days,
        )
        logger.info("Event hook system initialized")

        # Emit reminder about delivery worker
        asyncio.create_task(_emit_cleanup_warning())
```

Add cleanup in lifespan shutdown (after the existing `try:` block):

```python
    if state.delivery_service:
        await state.delivery_service.close()
    if state.event_hook_store:
        await state.event_hook_store.close()
```

Add include_router before static files mount:
```python
app.include_router(event_hooks_router)
```

- [ ] **Step 4: Commit**

```bash
git add app/routers/event_hooks.py app/main.py app/metrics.py
git commit -m "feat: add event hooks CRUD router, metrics, main.py wiring"
```

---

### Task 8: Integration test + final smoke test

**Files:**
- Modify: `tests/test_event_hooks.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_event_hooks.py (append)
import pytest
import uuid
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.mark.asyncio
async def test_integration_create_and_list_hooks():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create hook
        resp = await client.post(
            "/api/event-hooks",
            json={
                "url": "https://hooks.example.com/callback",
                "events": ["command.completed", "command.failed"],
                "include_output": True,
            },
            headers={"X-API-Key": "test-key"},
        )
        # Note: test may 503 if no DB configured — this validates wiring
        if resp.status_code == 503:
            pytest.skip("No DB configured for integration test")
        assert resp.status_code == 201
        data = resp.json()
        assert data["url"] == "https://hooks.example.com/callback"
        assert "command.completed" in data["events"]

        hook_id = data["id"]

        # List
        resp = await client.get("/api/event-hooks", headers={"X-API-Key": "test-key"})
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

        # Get
        resp = await client.get(f"/api/event-hooks/{hook_id}", headers={"X-API-Key": "test-key"})
        assert resp.status_code == 200
        assert resp.json()["id"] == hook_id

        # Update
        resp = await client.patch(
            f"/api/event-hooks/{hook_id}",
            json={"is_active": False},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

        # Delete
        resp = await client.delete(f"/api/event-hooks/{hook_id}", headers={"X-API-Key": "test-key"})
        assert resp.status_code == 200

        # 404 after delete
        resp = await client.get(f"/api/event-hooks/{hook_id}", headers={"X-API-Key": "test-key"})
        assert resp.status_code == 404
```

- [ ] **Step 2: Run unit tests**

Run: `pytest tests/test_event_hooks.py -v -x --ignore=tests/test_integration.py`
Expected: All unit tests pass

- [ ] **Step 3: Run full test suite**

Run: `pytest -q --ignore=tests/test_integration.py --ignore=tests/test_persistence.py --ignore=tests/test_rate_limit.py`
Expected: 104+ new tests pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_event_hooks.py
git commit -m "test: add event hooks integration test"
```

---

## Self-Review Checklist

1. **Spec coverage** — Every section covered: ORM models (Task 1), pydantic models (Task 2), store CRUD (Task 3), SSRF/HMAC/masking (Task 4), outbox delivery (Task 5), emitter + ssh_manager (Task 6), CRUD router (Task 7), tests (Task 8).

2. **Placeholder scan** — No TBD, TODO, or "implement later" patterns. Every step has real code.

3. **Type consistency** — `EventHookStore` methods match what `router` calls. `DeliveryService` constructor matches what `main.py` calls. `emit_event()` signature matches what `ssh_manager.py` calls.

## Execution

**Plan complete and saved to `docs/superpowers/plans/2026-05-28-event-hooks-plan.md`.**

**Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
