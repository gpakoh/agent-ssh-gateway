"""Integration tests for event hooks — CRUD via HTTP + outbox delivery with SQLite."""

from __future__ import annotations

from datetime import UTC

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_module
import app.state as state_module
from app.event_hook_delivery import DeliveryService
from app.event_hook_store import EventHookStore

TEST_API_KEY = "test-event-hook-key-789"


@pytest.fixture(autouse=True)
def _configure_auth():
    from app.config import settings

    settings.api_key = TEST_API_KEY
    yield


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


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": TEST_API_KEY}


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
        headers = _auth_headers()
        r = await client.post("/api/event-hooks", json=body, headers=headers)
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["url"] == "https://example.com/hook"
        assert data["events"] == ["session.connected", "command.completed"]
        assert data["is_active"] is True
        assert "id" in data

        hook_id = data["id"]

        r2 = await client.get(f"/api/event-hooks/{hook_id}", headers=headers)
        assert r2.status_code == 200
        assert r2.json()["id"] == hook_id

        r3 = await client.get("/api/event-hooks", headers=headers)
        assert r3.status_code == 200
        assert r3.json()["count"] == 1


@pytest.mark.asyncio
async def test_crud_update_and_delete(_setup_globals):
    store, ds = _setup_globals
    await store.create_tables()
    await ds.create_tables()

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = _auth_headers()
        body = {
            "url": "https://example.com/hook",
            "events": ["session.connected"],
        }
        r = await client.post("/api/event-hooks", json=body, headers=headers)
        hook_id = r.json()["id"]

        patch = {"events": ["session.connected", "session.disconnected"], "is_active": False}
        r2 = await client.patch(f"/api/event-hooks/{hook_id}", json=patch, headers=headers)
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert "session.disconnected" in data["events"]
        assert data["is_active"] is False

        r3 = await client.delete(f"/api/event-hooks/{hook_id}", headers=headers)
        assert r3.status_code == 200

        r4 = await client.get(f"/api/event-hooks/{hook_id}", headers=headers)
        assert r4.status_code == 404


@pytest.mark.asyncio
async def test_crud_rejects_invalid_url(_setup_globals):
    store, ds = _setup_globals
    await store.create_tables()
    await ds.create_tables()

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = _auth_headers()
        body = {
            "url": "http://localhost:8080/evil",
            "events": ["session.connected"],
        }
        r = await client.post("/api/event-hooks", json=body, headers=headers)
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
        headers = _auth_headers()
        body = {"url": "https://example.com/hook1", "events": ["session.connected"]}
        r = await client.post("/api/event-hooks", json=body, headers=headers)
        assert r.status_code == 201

        body2 = {"url": "https://example.com/hook2", "events": ["session.connected"]}
        r2 = await client.post("/api/event-hooks", json=body2, headers=headers)
        assert r2.status_code == 409


@pytest.mark.asyncio
async def test_delivery_enqueue_and_claim(_setup_globals):
    store, ds = _setup_globals
    await store.create_tables()
    await ds.create_tables()

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/event-hooks",
            json={
                "url": "https://example.com/hook",
                "events": ["session.connected"],
                "secret": "test-secret",
            },
            headers=_auth_headers(),
        )
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

    from datetime import datetime, timedelta

    from sqlalchemy import select as sel

    from app.session_store import WebhookDelivery

    async with ds._session_factory() as session:
        result = await session.execute(sel(WebhookDelivery).limit(1))
        rec = result.scalar_one()
        rec.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10)
        await session.commit()

    claimed = await ds.claim_deliveries(limit=10, lease_ttl=30.0)
    assert len(claimed) == 1
    assert claimed[0].status == "pending"
    assert claimed[0].url == "https://example.com/hook"


@pytest.mark.asyncio
async def test_agent_token_cannot_manage_event_hooks(_setup_globals):
    """Agent tokens (even with ssh:execute scope) must not be able to manage event hooks."""
    store, ds = _setup_globals
    await store.create_tables()
    await ds.create_tables()

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        master_headers = _auth_headers()

        # Create an agent token via the API
        from app.config import settings

        settings.agent_token = "agent-canary"
        from datetime import datetime, timedelta

        settings.agent_token_expires_at = datetime.now(UTC) + timedelta(hours=1)
        settings.agent_token_scopes = ["ssh:execute"]

        # Ensure no connected token store — want fallback to settings.agent_token
        state_module.agent_token_store = None

        agent_headers = {"Authorization": "Bearer agent-canary"}

        # GET /api/event-hooks — should reject agent token
        r = await client.get("/api/event-hooks", headers=agent_headers)
        assert r.status_code in (401, 403), (
            f"Expected 401/403 for agent token, got {r.status_code}: {r.text}"
        )

        # POST /api/event-hooks — should reject agent token
        r = await client.post(
            "/api/event-hooks",
            json={
                "url": "https://example.com/hook",
                "events": ["session.connected"],
            },
            headers=agent_headers,
        )
        assert r.status_code in (401, 403), (
            f"Expected 401/403 for agent token, got {r.status_code}: {r.text}"
        )

        # Verify master can still manage (sanity check)
        r = await client.get("/api/event-hooks", headers=master_headers)
        assert r.status_code == 200
