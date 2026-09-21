"""Integration tests: GatewayOAuthProvider + TokenStore."""

import time

import pytest

from examples.mcp_server.oauth_provider import GatewayOAuthProvider, hash_token
from examples.mcp_server.token_store import StoredTokenEntry, TokenStore


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "tokens.json")


def test_set_token_store(store_path):
    provider = GatewayOAuthProvider()
    store = TokenStore(store_path)
    provider.set_token_store(store)
    assert provider._token_store is store


def test_load_tokens_no_store():
    provider = GatewayOAuthProvider()
    assert provider.load_tokens() == 0


def test_load_tokens_empty_store(store_path):
    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    assert provider.load_tokens() == 0


def test_load_tokens_from_store(store_path):
    store = TokenStore(store_path)
    raw_token = "mcp_test_integration_token_12345"
    token_hash = hash_token(raw_token)
    store.add(
        StoredTokenEntry(
            id="tok_int_1",
            token_hash=token_hash,
            name="integration-test",
            profile="full",
            scopes=["mcp:read", "mcp:admin"],
            created_at="2026-06-26T12:00:00Z",
        )
    )

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    count = provider.load_tokens()
    assert count == 1

    # Verify token is usable
    stored = provider.verify_access_token(raw_token)
    assert stored is not None
    assert stored.scopes == ["mcp:read", "mcp:admin"]


def test_load_tokens_skips_revoked(store_path):
    store = TokenStore(store_path)
    h1 = hash_token("mcp_active")
    h2 = hash_token("mcp_revoked")
    store.add(
        StoredTokenEntry(
            id="tok_active",
            token_hash=h1,
            name="active",
            profile="full",
            scopes=["mcp:read"],
            created_at="2026-06-26T12:00:00Z",
        )
    )
    store.add(
        StoredTokenEntry(
            id="tok_revoked",
            token_hash=h2,
            name="revoked",
            profile="full",
            scopes=["mcp:read"],
            created_at="2026-06-26T12:00:00Z",
        )
    )
    store.revoke("tok_revoked")

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    count = provider.load_tokens()
    assert count == 1

    assert provider.verify_access_token("mcp_active") is not None
    assert provider.verify_access_token("mcp_revoked") is None


def test_load_tokens_multiple(store_path):
    store = TokenStore(store_path)
    tokens = []
    for i in range(3):
        raw = f"mcp_multi_{i}"
        h = hash_token(raw)
        store.add(
            StoredTokenEntry(
                id=f"tok_multi_{i}",
                token_hash=h,
                name=f"multi-{i}",
                profile="full",
                scopes=["mcp:read"],
                created_at="2026-06-26T12:00:00Z",
            )
        )
        tokens.append(raw)

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    assert provider.load_tokens() == 3
    for t in tokens:
        assert provider.verify_access_token(t) is not None


def test_revoke_token_syncs_to_store(store_path):
    store = TokenStore(store_path)
    raw = "mcp_revoke_sync"
    h = hash_token(raw)
    store.add(
        StoredTokenEntry(
            id="tok_sync",
            token_hash=h,
            name="sync-test",
            profile="full",
            scopes=["mcp:read"],
            created_at="2026-06-26T12:00:00Z",
        )
    )

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    provider.load_tokens()
    assert provider.verify_access_token(raw) is not None

    # Revoke via provider
    import asyncio

    asyncio.run(provider.revoke_token(raw))

    # Must be gone from provider
    assert provider.verify_access_token(raw) is None

    # Must be revoked in store
    store2 = TokenStore(store_path)
    entry = store2.find_by_hash(h)
    assert entry is not None
    assert entry.revoked_at is not None


def test_revoke_client_token_syncs_to_store(store_path):
    store = TokenStore(store_path)
    raw = "mcp_client_revoke"
    h = hash_token(raw)
    store.add(
        StoredTokenEntry(
            id="tok_client_sync",
            token_hash=h,
            name="client-sync",
            profile="full",
            scopes=["mcp:read"],
            created_at="2026-06-26T12:00:00Z",
        )
    )

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    provider.load_tokens()
    assert provider.verify_access_token(raw) is not None

    provider.revoke_client_token("mcp_static", raw)
    assert provider.verify_access_token(raw) is None

    store2 = TokenStore(store_path)
    entry = store2.find_by_hash(h)
    assert entry is not None
    assert entry.revoked_at is not None


# ── Startup filtering + durable compaction integration ──────────


def test_load_tokens_skips_expired_persisted(store_path):
    store = TokenStore(store_path)
    store.add(
        StoredTokenEntry(
            id="tok_expired",
            token_hash=hash_token("mcp_expired_persisted"),
            name="expired",
            profile="full",
            scopes=["mcp:read"],
            created_at="2026-06-26T12:00:00Z",
            expires_at="2025-06-26T12:00:00Z",
        )
    )

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    assert provider.load_tokens() == 0

    assert provider.verify_access_token("mcp_expired_persisted") is None


def test_load_tokens_restores_unexpired_with_exact_expiry(store_path):
    store = TokenStore(store_path)
    store.add(
        StoredTokenEntry(
            id="tok_near_expiry",
            token_hash=hash_token("mcp_near_expiry_persisted"),
            name="near-expiry",
            profile="full",
            scopes=["mcp:read"],
            created_at="2026-06-26T12:00:00Z",
            expires_at="2099-12-31T23:59:59Z",
        )
    )

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    assert provider.load_tokens() == 1

    restored = provider.verify_access_token("mcp_near_expiry_persisted")
    assert restored is not None


def test_load_tokens_skips_revoked_and_expired_then_compaction_removes_oauth(
    tmp_path,
):
    store_path = str(tmp_path / "tokens.json")
    store = TokenStore(store_path)
    store.add_many(
        [
            StoredTokenEntry(
                id="oauth_expired",
                token_hash=hash_token("mcp_oauth_expired"),
                name="oauth",
                profile="oauth",
                scopes=["mcp:read"],
                created_at="2026-06-26T12:00:00Z",
                client_id="mcp_client_1",
                type="refresh",
                expires_at="2025-06-26T12:00:00Z",
            ),
            StoredTokenEntry(
                id="profile_revoked",
                token_hash=hash_token("mcp_profile_revoked"),
                name="operator",
                profile="operator",
                scopes=["mcp:read"],
                created_at="2026-06-26T12:00:00Z",
                revoked_at="2026-06-27T12:00:00Z",
            ),
        ]
    )

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    assert provider.load_tokens() == 0

    removed = store.compact_expired_oauth(time.time())

    assert removed == 1
    remaining = {
        e.id for e in TokenStore(store_path).load()
    }
    assert remaining == {"profile_revoked"}
