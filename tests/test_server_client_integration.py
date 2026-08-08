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
