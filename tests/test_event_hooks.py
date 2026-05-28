"""Tests for event hook system."""

import pytest
import pytest_asyncio

from app.session_store import EventHook, WebhookDelivery
from app.event_hook_store import EventHookStore


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
