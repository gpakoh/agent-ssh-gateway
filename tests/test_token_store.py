"""Tests for TokenStore persistence."""

import json
import os
import stat

import pytest

from examples.mcp_server import token_store as token_store_module
from examples.mcp_server.token_store import TOKEN_STORE_VERSION, StoredTokenEntry, TokenStore


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "tokens.json")


def _write_private_text(path, content):
    """Create a semantic token-store fixture with production-valid permissions."""
    path.write_text(content)
    path.chmod(0o600)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_token_store_create_empty(store_path):
    store = TokenStore(store_path)
    entries = store.load()
    assert entries == []


def test_token_store_constructor_does_not_create_missing_parent(tmp_path):
    store_path = tmp_path / "missing" / "nested" / "tokens.json"

    TokenStore(str(store_path))

    assert not store_path.parent.exists()


def test_token_store_constructor_does_not_require_parent_write_access(
    tmp_path,
    monkeypatch,
):
    store_path = tmp_path / "read-only-construction" / "tokens.json"

    def fail_if_parent_creation_is_attempted(_path: str) -> None:
        raise PermissionError("constructor attempted filesystem preparation")

    monkeypatch.setattr(token_store_module, "_ensure_parent", fail_if_parent_creation_is_attempted)

    TokenStore(str(store_path))


def test_existing_corrupt_token_store_still_fails_closed(tmp_path):
    store_path = tmp_path / "tokens.json"
    _write_private_text(store_path, "{not-json")

    with pytest.raises(ValueError, match="invalid JSON"):
        TokenStore(str(store_path)).load()


def test_prepare_durable_storage_creates_parent_and_supports_writes(tmp_path):
    store_path = tmp_path / "missing" / "nested" / "tokens.json"
    store = TokenStore(str(store_path))

    store.prepare_durable_storage()
    store.add(
        StoredTokenEntry(
            id="prepared-store",
            token_hash="sha256:prepared",
            name="prepared",
            profile="viewer",
            scopes=["mcp:read"],
            created_at="2026-08-29T00:00:00Z",
        )
    )

    assert store_path.parent.is_dir()
    assert [entry.id for entry in store.load()] == ["prepared-store"]


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
    store.add(
        StoredTokenEntry(
            id="tok_find",
            token_hash="sha256:findme",
            name="findable",
            profile="viewer",
            scopes=["mcp:read"],
            created_at="2026-06-26T12:00:00Z",
            expires_at=None,
            revoked_at=None,
            last_used_at=None,
        )
    )
    found = store.find_by_hash("sha256:findme")
    assert found is not None
    assert found.id == "tok_find"
    assert store.find_by_hash("sha256:nope") is None


def test_token_store_version_in_file(store_path):
    store = TokenStore(store_path)
    store.add(
        StoredTokenEntry(
            id="tok_v1",
            token_hash="sha256:v1",
            name="v1",
            profile="full",
            scopes=["mcp:read"],
            created_at="2026-06-26T12:00:00Z",
        )
    )
    with open(store_path) as f:
        data = json.load(f)
    assert data["version"] == TOKEN_STORE_VERSION == 2


def test_token_store_persisted_mode_is_secure_under_umask_000(store_path):
    previous_umask = os.umask(0o000)
    try:
        store = TokenStore(store_path)
        store.add(
            StoredTokenEntry(
                id="mode-first",
                token_hash="sha256:mode-first",
                name="mode-first",
                profile="viewer",
                scopes=["mcp:read"],
                created_at="2026-08-29T00:00:00Z",
            )
        )
        assert stat.S_IMODE(os.stat(store_path).st_mode) == 0o600

        # A subsequent mutation publishes a new tempfile inode via os.replace.
        store.add(
            StoredTokenEntry(
                id="mode-replacement",
                token_hash="sha256:mode-replacement",
                name="mode-replacement",
                profile="viewer",
                scopes=["mcp:read"],
                created_at="2026-08-29T00:00:01Z",
            )
        )
        assert stat.S_IMODE(os.stat(store_path).st_mode) == 0o600
    finally:
        os.umask(previous_umask)


def test_world_writable_existing_token_store_is_rejected(store_path):
    # Make store world-writable
    with open(store_path, "w") as f:
        json.dump({"version": 1, "tokens": []}, f)
    os.chmod(store_path, 0o666)
    with pytest.raises(PermissionError, match="world-writable"):
        TokenStore(store_path).load()


def test_token_store_add_revoke_race_no_resurrection(store_path):
    """Regression: add() and revoke() must run under the same lock.

    A stale read-modify-write cycle previously let add() overwrite a
    revoke() that landed between its load and save, resurrecting the
    revoked entry after a restart.

    The test forces the interleaving deterministically: the add worker
    reads the store, then blocks; the parent revokes the entry; only
    then is the add worker released. On the fixed code add() still
    holds the lock while reading, so the revoke lands *after* the
    add's read and the final store keeps the entry revoked. On the
    pre-fix code the add worker holds no lock while reading, revoke
    succeeds, and the stale add write resurrects the entry.
    """
    import multiprocessing

    def worker_add(store_path, released, proceed):
        store = TokenStore(store_path)
        entry = StoredTokenEntry(
            id="tok_add_race",
            token_hash="sha256:race2",
            name="added",
            profile="operator",
            scopes=["mcp:read"],
            created_at="2026-06-26T12:00:00Z",
            expires_at=None,
            revoked_at=None,
            last_used_at=None,
        )
        orig_load = TokenStore.load

        def slow_load(self):
            data = orig_load(self)
            released.set()
            if not proceed.wait(timeout=15):
                raise TimeoutError("add barrier timeout")
            return data

        TokenStore.load = slow_load
        store.add(entry)

    def worker_revoke(store_path):
        TokenStore(store_path).revoke("tok_revoke_race")

    store = TokenStore(store_path)
    revocable = StoredTokenEntry(
        id="tok_revoke_race",
        token_hash="sha256:race1",
        name="revocable",
        profile="operator",
        scopes=["mcp:read"],
        created_at="2026-06-26T12:00:00Z",
        expires_at=None,
        revoked_at=None,
        last_used_at=None,
    )
    store.add(revocable)

    released = multiprocessing.Event()
    proceed = multiprocessing.Event()
    p_add = multiprocessing.Process(
        target=worker_add, args=(store_path, released, proceed)
    )
    p_add.start()
    assert released.wait(timeout=15), "add worker never read the store"

    # Revoke races the add worker while it is paused between load and save.
    p_revoke = multiprocessing.Process(target=worker_revoke, args=(store_path,))
    p_revoke.start()
    proceed.set()
    p_add.join(timeout=15)
    p_revoke.join(timeout=15)
    assert p_add.exitcode == 0
    assert p_revoke.exitcode == 0

    # Fresh instance reads the persisted truth: the revoked entry must
    # stay revoked and the concurrently added entry must be present.
    store2 = TokenStore(store_path)
    loaded = store2.load()
    revoked = [e for e in loaded if e.id == "tok_revoke_race"]
    assert len(revoked) == 1
    assert revoked[0].revoked_at is not None
    assert any(e.id == "tok_add_race" for e in loaded)
