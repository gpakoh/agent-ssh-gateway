"""Integration tests for event hooks — CRUD via HTTP + outbox delivery with SQLite."""

from __future__ import annotations

import json
import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_module
import app.state as state_module
from app.event_hook_store import EventHookStore
from app.event_hook_delivery import DeliveryService


@pytest.fixture
def _setup_globals():
    store = EventHookStore("sqlite+aiosqlite://")
    ds = DeliveryService("sqlite+aiosqlite://", instance_id="test-instance")
    state_module.event_hook_store = store
    state_module.delivery_service = ds
    from app.config import settings
    settings.event_hooks_max = 10
    yield store, ds
    state_module.event_hook_store = None
    state_module.delivery_service = None


@pytest.mark.asyncio
async def test_crud_create_and_list(_setup_globals):
    store, ds = _setup_globals
    await store.create_tables()
    await ds.create_tables()

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = {
            "url": "https://example.com/hook",
            "events": ["session.connected", "command.completed"],
            "secret": "my-secret",
        }
        r = await client.post("/api/event-hooks", json=body)
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["url"] == "https://example.com/hook"
        assert data["events"] == ["session.connected", "command.completed"]
        assert data["is_active"] is True
        assert "id" in data

        hook_id = data["id"]

        r2 = await client.get(f"/api/event-hooks/{hook_id}")
        assert r2.status_code == 200
        assert r2.json()["id"] == hook_id

        r3 = await client.get("/api/event-hooks")
        assert r3.status_code == 200
        assert r3.json()["count"] == 1


@pytest.mark.asyncio
async def test_crud_update_and_delete(_setup_globals):
    store, ds = _setup_globals
    await store.create_tables()
    await ds.create_tables()

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = {
            "url": "https://example.com/hook",
            "events": ["session.connected"],
        }
        r = await client.post("/api/event-hooks", json=body)
        hook_id = r.json()["id"]

        patch = {"events": ["session.connected", "session.disconnected"], "is_active": False}
        r2 = await client.patch(f"/api/event-hooks/{hook_id}", json=patch)
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert "session.disconnected" in data["events"]
        assert data["is_active"] is False

        r3 = await client.delete(f"/api/event-hooks/{hook_id}")
        assert r3.status_code == 200

        r4 = await client.get(f"/api/event-hooks/{hook_id}")
        assert r4.status_code == 404


@pytest.mark.asyncio
async def test_crud_rejects_invalid_url(_setup_globals):
    store, ds = _setup_globals
    await store.create_tables()
    await ds.create_tables()

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = {
            "url": "http://localhost:8080/evil",
            "events": ["session.connected"],
        }
        r = await client.post("/api/event-hooks", json=body)
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_crud_rejects_max_hooks(_setup_globals):
    store, ds = _setup_globals
    await store.create_tables()
    await ds.create_tables()

    from app.config import settings
    settings.event_hooks_max = 1

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = {"url": "https://example.com/hook1", "events": ["session.connected"]}
        r = await client.post("/api/event-hooks", json=body)
        assert r.status_code == 201

        body2 = {"url": "https://example.com/hook2", "events": ["session.connected"]}
        r2 = await client.post("/api/event-hooks", json=body2)
        assert r2.status_code == 409


@pytest.mark.asyncio
async def test_delivery_enqueue_and_claim(_setup_globals):
    store, ds = _setup_globals
    await store.create_tables()
    await ds.create_tables()

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/event-hooks", json={
            "url": "https://example.com/hook",
            "events": ["session.connected"],
            "secret": "test-secret",
        })
        assert r.status_code == 201

    from app.event_hook_emitter import emit_event
    await emit_event(
        event="session.connected",
        session_id="test-session",
        host="10.0.0.1",
        port=22,
        username="root",
    )

    claimed = await ds.claim_deliveries(limit=10, lease_ttl=30.0)
    assert len(claimed) == 0

    from sqlalchemy import select as sel
    from app.session_store import WebhookDelivery
    from datetime import datetime, timedelta, timezone
    async with ds._session_factory() as session:
        result = await session.execute(sel(WebhookDelivery).limit(1))
        rec = result.scalar_one()
        rec.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
        await session.commit()

    claimed = await ds.claim_deliveries(limit=10, lease_ttl=30.0)
    assert len(claimed) == 1
    assert claimed[0].status == "pending"
    assert claimed[0].url == "https://example.com/hook"
