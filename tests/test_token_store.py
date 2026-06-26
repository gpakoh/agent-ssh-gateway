"""Tests for TokenStore persistence."""
import json
import os
import tempfile

import pytest

from examples.mcp_server.token_store import StoredTokenEntry, TokenStore


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


def test_token_store_create_empty(store_path):
    store = TokenStore(store_path)
    entries = store.load()
    assert entries == []


def test_token_store_add_and_load(store_path):
    store = TokenStore(store_path)
    entry = StoredTokenEntry(
        id="tok_20260626_test",
        token_hash="sha256:abc123",
        name="test-token",
        profile="full",
        scopes=["mcp:read", "mcp:admin"],
        created_at="2026-06-26T12:00:00Z",
        expires_at=None,
        revoked_at=None,
        last_used_at=None,
    )
    store.add(entry)

    # Read from a new instance to verify persistence
    store2 = TokenStore(store_path)
    loaded = store2.load()
    assert len(loaded) == 1
    assert loaded[0].id == "tok_20260626_test"
    assert loaded[0].token_hash == "sha256:abc123"
    assert loaded[0].scopes == ["mcp:read", "mcp:admin"]


def test_token_store_revoke(store_path):
    store = TokenStore(store_path)
    entry = StoredTokenEntry(
        id="tok_revoke_me",
        token_hash="sha256:xyz",
        name="revocable",
        profile="operator",
        scopes=["mcp:read"],
        created_at="2026-06-26T12:00:00Z",
        expires_at=None,
        revoked_at=None,
        last_used_at=None,
    )
    store.add(entry)
    revoked = store.revoke("tok_revoke_me")
    assert revoked is not None
    assert revoked.revoked_at is not None

    store2 = TokenStore(store_path)
    loaded = store2.load()
    assert loaded[0].revoked_at is not None


def test_token_store_revoke_nonexistent(store_path):
    store = TokenStore(store_path)
    assert store.revoke("nonexistent") is None


def test_token_store_find_by_hash(store_path):
    store = TokenStore(store_path)
    store.add(StoredTokenEntry(
        id="tok_find",
        token_hash="sha256:findme",
        name="findable",
        profile="viewer",
        scopes=["mcp:read"],
        created_at="2026-06-26T12:00:00Z",
        expires_at=None,
        revoked_at=None,
        last_used_at=None,
    ))
    found = store.find_by_hash("sha256:findme")
    assert found is not None
    assert found.id == "tok_find"
    assert store.find_by_hash("sha256:nope") is None


def test_token_store_version_in_file(store_path):
    store = TokenStore(store_path)
    store.add(StoredTokenEntry(
        id="tok_v1", token_hash="sha256:v1", name="v1",
        profile="full", scopes=["mcp:read"],
        created_at="2026-06-26T12:00:00Z",
    ))
    with open(store_path) as f:
        data = json.load(f)
    assert data["version"] == 1


def test_token_store_enforces_permissions(store_path):
    # Make store world-writable
    with open(store_path, "w") as f:
        json.dump({"version": 1, "tokens": []}, f)
    os.chmod(store_path, 0o666)
    with pytest.raises(PermissionError, match="world-writable"):
        TokenStore(store_path).load()
