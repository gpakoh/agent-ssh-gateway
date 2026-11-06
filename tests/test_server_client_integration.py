"""Integration tests: GatewayOAuthProvider + ClientStore.

Regression coverage for the "Client ID ... not found" bug: dynamically
registered OAuth clients (RFC 7591) lived only in
GatewayOAuthProvider._clients, an in-memory dict -- every process
restart silently forgot every client a connector had ever registered,
so the next reconnection attempt failed even though nothing about the
connection itself had changed.
"""

import asyncio
import os
import tempfile

import pytest

from examples.mcp_server.client_store import ClientStore
from examples.mcp_server.oauth_provider import GatewayOAuthProvider, StoredClient

pytestmark = pytest.mark.integration


@pytest.fixture
def store_path():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.unlink(path)
    lock_path = path + ".lock"
    if os.path.exists(lock_path):
        os.unlink(lock_path)


def _client_info(client_name: str = "test-connector"):
    class _Info:
        redirect_uris = ["https://chat.example.com/callback"]
        client_name = "test-connector"
        token_endpoint_auth_method = "none"
        scope = "mcp:read mcp:project"

    info = _Info()
    info.client_name = client_name
    return info


def test_set_client_store(store_path):
    provider = GatewayOAuthProvider()
    store = ClientStore(store_path)
    provider.set_client_store(store)
    assert provider._client_store is store


def test_load_clients_no_store():
    provider = GatewayOAuthProvider()
    assert provider.load_clients() == 0


def test_load_clients_empty_store(store_path):
    provider = GatewayOAuthProvider()
    provider.set_client_store(ClientStore(store_path))
    assert provider.load_clients() == 0


def test_register_client_persists_to_store(store_path):
    """register_client() (the DCR handler) must persist immediately, not
    just hold the registration in memory.
    """
    provider = GatewayOAuthProvider()
    provider.set_client_store(ClientStore(store_path))

    info = _client_info()
    asyncio.run(provider.register_client(info))
    client_id = info.client_id

    # A second, freshly constructed store reading the same path must see it.
    store2 = ClientStore(store_path)
    loaded = store2.load()
    assert len(loaded) == 1
    assert loaded[0].client_id == client_id
    assert loaded[0].redirect_uris == ["https://chat.example.com/callback"]


def test_client_store_lock_contention_fails_closed_and_recovers(
    store_path, monkeypatch
):
    """A stuck writer cannot hold registration indefinitely or corrupt data."""
    import fcntl
    import time

    from examples.mcp_server import client_store as store_module

    monkeypatch.setattr(store_module, "CLIENT_STORE_LOCK_TIMEOUT_SECONDS", 0.15)
    store = ClientStore(store_path)
    client = StoredClient(client_id="mcp_client_busy", redirect_uris=["https://example.com/cb"])
    with open(store_path + ".lock", "w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        began = time.monotonic()
        with pytest.raises(store_module.ClientStoreBusyError, match="temporarily busy"):
            store.add(client)
        assert time.monotonic() - began < 2
        assert store.load() == []
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)

    store.add(client)
    assert len(store.load()) == 1
    assert store.load()[0].client_id == "mcp_client_busy"


@pytest.mark.asyncio
async def test_register_client_store_write_does_not_block_oauth_event_loop(
    store_path,
):
    """A stalled filesystem writer must not prevent another OAuth coroutine."""
    import threading

    class DelayedStore(ClientStore):
        def __init__(self, path):
            super().__init__(path)
            self.entered = threading.Event()
            self.release = threading.Event()

        def add(self, client):
            self.entered.set()
            self.release.wait(timeout=4)
            super().add(client)

    store = DelayedStore(store_path)
    provider = GatewayOAuthProvider()
    provider.set_client_store(store)
    info = _client_info("concurrent-oauth")
    task = asyncio.create_task(provider.register_client(info))
    try:
        # With the old synchronous call in the event loop, add() would block
        # this coroutine until its 4-second emergency release elapsed.
        assert await asyncio.wait_for(asyncio.to_thread(store.entered.wait, 2), 3)
        assert not task.done()
        assert "client_id" not in vars(info)
        await asyncio.wait_for(asyncio.sleep(0), 1)
    finally:
        store.release.set()
    await asyncio.wait_for(task, 4)
    assert await provider.get_client(info.client_id) is not None
    assert len(ClientStore(store_path).load()) == 1


@pytest.mark.asyncio
async def test_failed_client_store_write_does_not_publish_ephemeral_client(
    store_path, monkeypatch
):
    from examples.mcp_server.client_store import ClientStoreBusyError

    store = ClientStore(store_path)

    def refused(_client):
        raise ClientStoreBusyError("OAuth client store temporarily busy")

    monkeypatch.setattr(store, "add", refused)
    provider = GatewayOAuthProvider()
    provider.set_client_store(store)
    info = _client_info("failed-oauth")
    with pytest.raises(ClientStoreBusyError):
        await provider.register_client(info)
    assert not provider._clients
    assert "client_id" not in vars(info)


def test_client_survives_provider_restart(store_path):
    """The actual bug scenario: register a client on one provider
    instance, then simulate a process restart (a brand-new provider +
    store instance reading the same file) and confirm the client is
    still known -- this is exactly what a systemd/container restart
    does between a connector's registration and its next reconnection.
    """
    provider1 = GatewayOAuthProvider()
    provider1.set_client_store(ClientStore(store_path))
    info = _client_info()
    asyncio.run(provider1.register_client(info))
    client_id = info.client_id

    # Simulate restart: brand-new provider, brand-new store, same file.
    provider2 = GatewayOAuthProvider()
    provider2.set_client_store(ClientStore(store_path))
    loaded_count = provider2.load_clients()

    assert loaded_count == 1
    found = asyncio.run(provider2.get_client(client_id))
    assert found is not None
    assert found.client_id == client_id


def test_load_clients_multiple(store_path):
    store = ClientStore(store_path)
    store.add(StoredClient(client_id="mcp_client_a", redirect_uris=["https://a.example.com/cb"]))
    store.add(StoredClient(client_id="mcp_client_b", redirect_uris=["https://b.example.com/cb"]))

    provider = GatewayOAuthProvider()
    provider.set_client_store(store)
    assert provider.load_clients() == 2
    assert set(provider._clients.keys()) == {"mcp_client_a", "mcp_client_b"}


def test_add_same_client_id_replaces_not_duplicates(store_path):
    store = ClientStore(store_path)
    store.add(StoredClient(client_id="mcp_client_x", redirect_uris=["https://old.example.com/cb"]))
    store.add(StoredClient(client_id="mcp_client_x", redirect_uris=["https://new.example.com/cb"]))

    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].redirect_uris == ["https://new.example.com/cb"]
