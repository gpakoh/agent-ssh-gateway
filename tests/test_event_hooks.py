"""Tests for event hook system."""

import pytest
import pytest_asyncio

from app.session_store import EventHook, WebhookDelivery
from app.event_hook_store import EventHookStore
from app.event_hook_security import (
    validate_webhook_url,
    validate_destination_ip,
    sign_payload,
    mask_sensitive_headers,
)


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
