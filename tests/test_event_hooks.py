"""Tests for event hook system."""

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone

from app.session_store import EventHook, WebhookDelivery
from app.event_hook_store import EventHookStore
from app.event_hook_security import (
    validate_webhook_url,
    validate_destination_ip,
    sign_payload,
    mask_sensitive_headers,
)
from app.event_hook_delivery import DeliveryService


def test_orm_models_defined():
    assert EventHook.__tablename__ == "event_hooks"
    assert WebhookDelivery.__tablename__ == "webhook_deliveries"


@pytest_asyncio.fixture
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
        headers_encrypted=None,
        secret_encrypted=None,
        include_output=False,
    )
    assert hook.id is not None
    assert hook.url == "https://example.com/hook"
    assert hook.is_active is True


@pytest.mark.asyncio
async def test_list_hooks(store):
    await store.create(
        url="https://a.com/hook", events=["command.completed"],
        session_id=None, headers_encrypted=None, secret_encrypted=None, include_output=False,
    )
    await store.create(
        url="https://b.com/hook", events=["session.connected"],
        session_id=None, headers_encrypted=None, secret_encrypted=None, include_output=False,
    )
    hooks = await store.list()
    assert len(hooks) == 2


@pytest.mark.asyncio
async def test_get_hook(store):
    created = await store.create(
        url="https://c.com/hook", events=["command.failed"],
        session_id=None, headers_encrypted=None, secret_encrypted=None, include_output=False,
    )
    fetched = await store.get(created.id)
    assert fetched is not None
    assert fetched.id == created.id


@pytest.mark.asyncio
async def test_get_hook_not_found(store):
    assert await store.get("nonexistent-id") is None


@pytest.mark.asyncio
async def test_update_hook(store):
    created = await store.create(
        url="https://d.com/hook", events=["command.completed"],
        session_id=None, headers_encrypted=None, secret_encrypted=None, include_output=False,
    )
    updated = await store.update(created.id, url="https://d.com/new-hook", is_active=False)
    assert updated is not None
    assert updated.url == "https://d.com/new-hook"
    assert updated.is_active is False


@pytest.mark.asyncio
async def test_update_not_found(store):
    assert await store.update("nope", url="x") is None


@pytest.mark.asyncio
async def test_delete_hook(store):
    created = await store.create(
        url="https://e.com/hook", events=["command.completed"],
        session_id=None, headers_encrypted=None, secret_encrypted=None, include_output=False,
    )
    deleted = await store.delete(created.id)
    assert deleted is True
    assert await store.get(created.id) is None


@pytest.mark.asyncio
async def test_delete_not_found(store):
    assert await store.delete("nope") is False


@pytest.mark.asyncio
async def test_find_matching_hooks(store):
    h1 = await store.create(
        url="https://a.com/hook", events=["command.completed"],
        session_id=None, headers_encrypted=None, secret_encrypted=None, include_output=False,
    )
    h2 = await store.create(
        url="https://b.com/hook", events=["command.completed"],
        session_id="sess-1", headers_encrypted=None, secret_encrypted=None, include_output=False,
    )
    h3 = await store.create(
        url="https://c.com/hook", events=["session.connected"],
        session_id=None, headers_encrypted=None, secret_encrypted=None, include_output=False,
    )

    matches = await store.find_matching("command.completed", session_id="sess-2")
    assert len(matches) == 1
    assert matches[0].id == h1.id

    matches = await store.find_matching("command.completed", session_id="sess-1")
    assert len(matches) == 2

    matches = await store.find_matching("session.connected", session_id="sess-2")
    assert len(matches) == 1
    assert matches[0].id == h3.id


# ---------------------------------------------------------------------------
# Security — URL validation, HMAC, log masking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,ok", [
    ("https://hooks.example.com/callback", True),
    ("https://10.0.0.1/hook", False),
    ("https://127.0.0.1/hook", False),
    ("https://192.168.1.1/hook", False),
    ("https://169.254.169.254/latest", False),
    ("https://[::1]/hook", False),
    ("http://example.com/hook", False),
    ("https://example.com:22/hook", True),
    ("file:///etc/passwd", False),
    ("", False),
])
def test_validate_url(url, ok):
    result = validate_webhook_url(url, allow_http=False)
    assert result.valid is ok, f"{url}: expected valid={ok}, got {result.reason}"


@pytest.mark.parametrize("url,ok", [
    ("http://example.com/hook", True),
    ("https://example.com/hook", True),
])
def test_validate_url_allow_http(url, ok):
    result = validate_webhook_url(url, allow_http=True)
    assert result.valid is ok


def test_validate_destination_ip_loopback():
    assert validate_destination_ip("127.0.0.1").valid is False
    assert validate_destination_ip("::1").valid is False


def test_validate_destination_ip_private():
    assert validate_destination_ip("10.10.10.10").valid is False
    assert validate_destination_ip("192.168.1.1").valid is False


def test_validate_destination_ip_public():
    assert validate_destination_ip("8.8.8.8").valid is True
    assert validate_destination_ip("93.184.216.34").valid is True


def test_validate_destination_ip_invalid():
    assert validate_destination_ip("not-an-ip").valid is False


def test_sign_payload():
    secret = "test-secret-key"
    payload = b'{"event":"command.completed"}'
    timestamp = "1716800000"
    signature = sign_payload(secret, payload, timestamp)
    assert signature.startswith("sha256=")
    assert len(signature) > 50


def test_sign_payload_deterministic():
    secret = "test-secret"
    payload = b"{}"
    ts = "1716800000"
    assert sign_payload(secret, payload, ts) == sign_payload(secret, payload, ts)


def test_sign_payload_no_secret():
    assert sign_payload("", b"{}", "0") is None
    assert sign_payload(None, b"{}", "0") is None


def test_mask_sensitive_headers():
    masked = {"Authorization": "Bearer secret123", "X-API-Key": "key456"}
    unmasked = {"Content-Type": "application/json"}
    result = mask_sensitive_headers({**masked, **unmasked})
    for key in masked:
        assert result[key] == "****"
    assert result["Content-Type"] == "application/json"


def test_mask_sensitive_headers_none():
    assert mask_sensitive_headers(None) == {}
    assert mask_sensitive_headers({}) == {}


# ---------------------------------------------------------------------------
# Delivery Service — outbox, lease, retry, cleanup
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def delivery_service():
    ds = DeliveryService(
        database_url="sqlite+aiosqlite:///:memory:",
        instance_id="test-1",
    )
    await ds.create_tables()
    yield ds
    await ds.close()


@pytest.mark.asyncio
async def test_delivery_enqueue(delivery_service):
    delivery_id = await delivery_service.enqueue(
        event_id="evt-1",
        hook_id="hook-1",
        event_type="command.completed",
        url="http://example.com/hook",
        payload_json='{"event":"command.completed"}',
    )
    assert delivery_id is not None


@pytest.mark.asyncio
async def test_delivery_claim_pending(delivery_service):
    delivery_id = await delivery_service.enqueue(
        event_id="evt-2", hook_id="hook-2",
        event_type="session.connected",
        url="http://example.com/hook",
        payload_json="{}",
    )
    # Manually age the record so it's claimable (>2s old)
    async with delivery_service._session_factory() as session:
        from sqlalchemy import select as sel
        result = await session.execute(
            sel(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
        )
        rec = result.scalar_one()
        rec.created_at = datetime.utcnow() - timedelta(seconds=10)
        await session.commit()

    claimed = await delivery_service.claim_deliveries(limit=10, lease_ttl=30.0)
    assert len(claimed) == 1
    assert claimed[0].delivery_id == delivery_id
    assert claimed[0].status == "pending"


@pytest.mark.asyncio
async def test_delivery_complete(delivery_service):
    d_id = await delivery_service.enqueue("evt-3", "h-3", "a", "http://x.co/h", "{}")
    result = await delivery_service.complete(d_id, http_status=200)
    assert result is True

    rec = await delivery_service._get_record(d_id)
    assert rec.status == "sent"


@pytest.mark.asyncio
async def test_delivery_fail_with_retry(delivery_service):
    d_id = await delivery_service.enqueue("evt-4", "h-4", "a", "http://x.co/h", "{}")
    result = await delivery_service.fail(
        d_id, last_error="timeout",
        max_attempts=5, retry_base_sec=2.0, retry_max_sec=300.0,
    )
    assert result is True

    rec = await delivery_service._get_record(d_id)
    assert rec.attempts == 1
    assert rec.status == "failed"
    assert rec.next_retry_at is not None


@pytest.mark.asyncio
async def test_delivery_fail_dead_after_max(delivery_service):
    d_id = await delivery_service.enqueue("evt-5", "h-5", "a", "http://x.co/h", "{}")
    await delivery_service.fail(
        d_id, last_error="first",
        max_attempts=3, retry_base_sec=2.0, retry_max_sec=300.0,
    )
    await delivery_service.fail(
        d_id, last_error="second",
        max_attempts=3, retry_base_sec=2.0, retry_max_sec=300.0,
    )
    await delivery_service.fail(
        d_id, last_error="third",
        max_attempts=3, retry_base_sec=2.0, retry_max_sec=300.0,
    )
    rec = await delivery_service._get_record(d_id)
    assert rec.status == "dead"
    assert rec.attempts == 3


@pytest.mark.asyncio
async def test_delivery_claim_skips_young_pending(delivery_service):
    await delivery_service.enqueue("evt-6", "h-6", "a", "http://x.co/h", "{}")


    claimed = await delivery_service.claim_deliveries(limit=10, lease_ttl=30.0)
    assert len(claimed) == 0


@pytest.mark.asyncio
async def test_delivery_cleanup(delivery_service):
    d_id = await delivery_service.enqueue("evt-7", "h-7", "a", "http://x.co/h", "{}")
    await delivery_service.complete(d_id, 200)

    # Should not be cleaned (not old enough)
    count = await delivery_service.cleanup_old(sent_days=7, dead_days=30)
    assert count == 0

    # Manually age the record
    async with delivery_service._session_factory() as s:
        from sqlalchemy import select as sel
        result = await s.execute(sel(WebhookDelivery).where(
            WebhookDelivery.delivery_id == d_id
        ))
        d = result.scalar_one()
        d.updated_at = datetime.now(timezone.utc) - timedelta(days=10)
        await s.commit()

    # Now it should be cleaned
    count = await delivery_service.cleanup_old(sent_days=7, dead_days=30)
    assert count == 1
