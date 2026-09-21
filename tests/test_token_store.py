"""Tests for TokenStore persistence."""

import json
import os
import stat
from pathlib import Path

import pytest

from examples.mcp_server import token_store as token_store_module
from examples.mcp_server.oauth_provider import hash_token
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


# ── Retention compaction (profile=oauth only) ───────────────────

COMPACTION_NOW = 1_800_000_000.0


def _oauth_entry(
    id_,
    expires_at,
    revoked_at=None,
    client_id="mcp_client_1",
    token_type="access",
):
    return StoredTokenEntry(
        id=id_,
        token_hash=f"sha256:{id_}",
        name="oauth-grant",
        profile="oauth",
        scopes=["mcp:read"],
        created_at="2026-01-01T00:00:00Z",
        client_id=client_id,
        type=token_type,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


def _non_oauth_entry(id_, profile="operator", revoked_at=None, expires_at=None):
    return StoredTokenEntry(
        id=id_,
        token_hash=f"sha256:{id_}",
        name=f"{profile}-record",
        profile=profile,
        scopes=["mcp:read"],
        created_at="2026-01-01T00:00:00Z",
        client_id="mcp_static",
        type="access",
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


def test_compact_expired_oauth_removes_expired_and_revoked_oauth_records(
    store_path,
):
    store = TokenStore(store_path)
    store.add_many(
        [
            _oauth_entry(
                "oauth-expired",
                expires_at="2025-01-01T00:00:00Z",
            ),
            _oauth_entry(
                "oauth-revoked",
                expires_at="2099-12-31T23:59:59Z",
                revoked_at="2026-01-01T00:00:00Z",
                token_type="refresh",
            ),
            _oauth_entry(
                "oauth-unparseable-expiry",
                expires_at="not-a-real-date",
            ),
        ]
    )

    removed = store.compact_expired_oauth(COMPACTION_NOW)

    assert removed == 3
    assert store.load() == []


def test_compact_skips_unexpired_oauth_records(store_path):
    store = TokenStore(store_path)
    store.add_many(
        [
            _oauth_entry(
                "oauth-live-access",
                expires_at="2099-12-31T23:59:59Z",
            ),
            _oauth_entry(
                "oauth-live-refresh",
                expires_at="2099-12-31T23:59:59Z",
                token_type="refresh",
            ),
            _oauth_entry(
                "oauth-no-expiry",
                expires_at=None,
            ),
        ]
    )
    before = [e.id for e in store.load()]

    removed = store.compact_expired_oauth(COMPACTION_NOW)

    assert removed == 0
    assert [e.id for e in store.load()] == before


def test_compact_expired_oauth_noop_writes_no_store(tmp_path):
    store_path = tmp_path / "tokens.json"
    store = TokenStore(str(store_path))

    assert store.compact_expired_oauth(COMPACTION_NOW) == 0
    assert not store_path.exists()


def test_compact_preserves_non_oauth_records_and_revoked_history(store_path):
    """Static/profile/admin records — including revoked audit history — survive."""
    store = TokenStore(store_path)
    store.add_many(
        [
            _oauth_entry("oauth-expired", expires_at="2025-01-01T00:00:00Z"),
            _non_oauth_entry(
                "operator-revoked",
                profile="operator",
                revoked_at="2026-06-01T00:00:00Z",
            ),
            _non_oauth_entry(
                "admin-live",
                profile="admin",
                expires_at="2099-12-31T23:59:59Z",
            ),
            _non_oauth_entry(
                "viewer-revoked-no-expiry",
                profile="viewer",
                revoked_at="2026-06-01T00:00:00Z",
            ),
        ]
    )

    removed = store.compact_expired_oauth(COMPACTION_NOW)

    assert removed == 1
    loaded = store.load()
    remaining = {e.id for e in loaded}
    assert remaining == {
        "operator-revoked",
        "admin-live",
        "viewer-revoked-no-expiry",
    }
    operator_revoked = next(e for e in loaded if e.id == "operator-revoked")
    assert operator_revoked.revoked_at == "2026-06-01T00:00:00Z"
    viewer_revoked = next(e for e in loaded if e.id == "viewer-revoked-no-expiry")
    assert viewer_revoked.revoked_at == "2026-06-01T00:00:00Z"


def test_compact_expired_oauth_never_writes_raw_tokens(store_path):
    raw = "mcp_compaction_raw_secret"
    store = TokenStore(store_path)
    store.add_many(
        [
            StoredTokenEntry(
                id="oauth-live",
                token_hash=hash_token(raw),
                name="oauth-grant",
                profile="oauth",
                scopes=["mcp:read"],
                created_at="2026-01-01T00:00:00Z",
                client_id="mcp_client_1",
                type="refresh",
                expires_at="2099-12-31T23:59:59Z",
            ),
            StoredTokenEntry(
                id="oauth-expired",
                token_hash="sha256:oauth-expired-hash",
                name="oauth-grant",
                profile="oauth",
                scopes=["mcp:read"],
                created_at="2026-01-01T00:00:00Z",
                client_id="mcp_client_1",
                type="access",
                expires_at="2025-01-01T00:00:00Z",
            ),
            _non_oauth_entry(
                "operator-revoked",
                revoked_at="2026-06-01T00:00:00Z",
            ),
        ]
    )

    store.compact_expired_oauth(COMPACTION_NOW)

    content = Path(store_path).read_text()
    assert raw not in content
    assert hash_token(raw) in content  # only the hash is ever serialized


def test_prepare_durable_storage_auto_compacts_expired_oauth(tmp_path):
    """Durable startup preparation sweeps expired/revoked OAuth records."""
    store_path = tmp_path / "tokens.json"
    store = TokenStore(str(store_path))
    store.add_many(
        [
            _oauth_entry("oauth-expired", expires_at="2025-01-01T00:00:00Z"),
            _oauth_entry(
                "oauth-revoked",
                expires_at="2099-12-31T23:59:59Z",
                revoked_at="2026-01-01T00:00:00Z",
                token_type="refresh",
            ),
            _oauth_entry("oauth-live", expires_at="2099-12-31T23:59:59Z"),
            _non_oauth_entry("static-revoked", revoked_at="2026-06-01T00:00:00Z"),
        ]
    )

    store.prepare_durable_storage()

    loaded = TokenStore(str(store_path)).load()
    remaining = {e.id for e in loaded}
    assert remaining == {"oauth-live", "static-revoked"}
    static_revoked = next(e for e in loaded if e.id == "static-revoked")
    assert static_revoked.revoked_at == "2026-06-01T00:00:00Z"


def test_compact_oauth_race_no_entries_lost(store_path):
    """Compaction holds the flock across its read-modify-write window.

    A concurrent revoke landing while compaction is between load and write
    must be preserved, never clobbered by a stale compacted write.
    """
    import multiprocessing

    def worker_compact(store_path, released, proceed):
        store = TokenStore(store_path)
        orig_load = TokenStore.load

        def slow_load(self):
            data = orig_load(self)
            released.set()
            if not proceed.wait(timeout=15):
                raise TimeoutError("compact barrier timeout")
            return data

        TokenStore.load = slow_load
        store.compact_expired_oauth(COMPACTION_NOW)

    def worker_revoke(store_path):
        TokenStore(store_path).revoke("oauth-live")

    store = TokenStore(store_path)
    store.add_many(
        [
            _oauth_entry("oauth-expired", expires_at="2025-01-01T00:00:00Z"),
            _oauth_entry("oauth-live", expires_at="2099-12-31T23:59:59Z"),
            _non_oauth_entry("static-revoked", revoked_at="2026-06-01T00:00:00Z"),
        ]
    )

    released = multiprocessing.Event()
    proceed = multiprocessing.Event()
    p_compact = multiprocessing.Process(
        target=worker_compact, args=(store_path, released, proceed)
    )
    p_compact.start()
    assert released.wait(timeout=15), "compact worker never read the store"

    p_revoke = multiprocessing.Process(target=worker_revoke, args=(store_path,))
    p_revoke.start()
    proceed.set()
    p_compact.join(timeout=15)
    p_revoke.join(timeout=15)
    assert p_compact.exitcode == 0
    assert p_revoke.exitcode == 0

    store2 = TokenStore(store_path)
    loaded = store2.load()
    assert all(e.id != "oauth-expired" for e in loaded)
    live = [e for e in loaded if e.id == "oauth-live"]
    assert len(live) == 1
    assert live[0].revoked_at is not None
    static = [e for e in loaded if e.id == "static-revoked"]
    assert len(static) == 1
    assert static[0].revoked_at == "2026-06-01T00:00:00Z"


# ── In-transaction compaction during add_many / rotate ──────────


def test_add_many_without_oauth_compaction_keeps_append_only(store_path):
    """Generic add_many (no oauth now) keeps the previous append-only behaviour."""
    store = TokenStore(store_path)
    store.add_many([_oauth_entry("oauth-expired", expires_at="2025-01-01T00:00:00Z")])

    store.add_many([_oauth_entry("oauth-new", expires_at="2099-12-31T23:59:59Z")])

    remaining = {e.id for e in store.load()}
    assert remaining == {"oauth-expired", "oauth-new"}


def test_add_many_with_oauth_compaction_filters_stale_oauth_records(store_path):
    """OAuth issuance compacts stale OAuth history in the same transaction."""
    store = TokenStore(store_path)
    store.add_many(
        [
            _oauth_entry("oauth-expired", expires_at="2025-01-01T00:00:00Z"),
            _oauth_entry(
                "oauth-revoked",
                expires_at="2099-12-31T23:59:59Z",
                revoked_at="2026-01-01T00:00:00Z",
                token_type="refresh",
            ),
            _oauth_entry("oauth-live", expires_at="2099-12-31T23:59:59Z"),
            _non_oauth_entry("operator-revoked", revoked_at="2026-06-01T00:00:00Z"),
        ]
    )

    store.add_many(
        [_oauth_entry("oauth-new-at", expires_at="2099-12-31T23:59:59Z")],
        compact_oauth_now=COMPACTION_NOW,
    )

    remaining = {e.id for e in store.load()}
    assert remaining == {"oauth-live", "operator-revoked", "oauth-new-at"}
    operator_revoked = next(e for e in store.load() if e.id == "operator-revoked")
    assert operator_revoked.revoked_at == "2026-06-01T00:00:00Z"


def test_rotate_without_oauth_compaction_keeps_revoked_old_refresh(store_path):
    """Generic rotate (no oauth now) keeps the old revoked refresh in place."""
    store = TokenStore(store_path)
    old_rt_hash = "sha256:oauth-old-rt"
    store.add_many(
        [
            _oauth_entry(
                "oauth-old-rt",
                expires_at="2099-12-31T23:59:59Z",
                token_type="refresh",
            ),
            _oauth_entry("oauth-stale", expires_at="2025-01-01T00:00:00Z"),
        ]
    )

    store.rotate(
        old_rt_hash,
        [
            _oauth_entry("oauth-new-at", expires_at="2099-12-31T23:59:59Z"),
            _oauth_entry(
                "oauth-new-rt",
                expires_at="2099-12-31T23:59:59Z",
                token_type="refresh",
            ),
        ],
    )

    loaded = store.load()
    old_rt = next(e for e in loaded if e.id == "oauth-old-rt")
    assert old_rt.revoked_at is not None
    assert any(e.id == "oauth-stale" for e in loaded)
    assert any(e.id == "oauth-new-at" and e.revoked_at is None for e in loaded)


def test_rotate_with_oauth_compaction_removes_revoked_old_refresh_and_stale(store_path):
    """Rotation compacts stale OAuth history including the old refresh it revokes."""
    old_rt_hash = "sha256:oauth-old-rt"
    store = TokenStore(store_path)
    store.add_many(
        [
            _oauth_entry("oauth-expired-access", expires_at="2025-01-01T00:00:00Z"),
            _oauth_entry(
                "oauth-old-rt",
                expires_at="2099-12-31T23:59:59Z",
                token_type="refresh",
            ),
            _oauth_entry("oauth-live-access", expires_at="2099-12-31T23:59:59Z"),
            _non_oauth_entry("operator-revoked", revoked_at="2026-06-01T00:00:00Z"),
        ]
    )

    store.rotate(
        old_rt_hash,
        [
            _oauth_entry("oauth-new-at", expires_at="2099-12-31T23:59:59Z"),
            _oauth_entry(
                "oauth-new-rt",
                expires_at="2099-12-31T23:59:59Z",
                token_type="refresh",
            ),
        ],
        compact_oauth_now=COMPACTION_NOW,
    )

    loaded = store.load()
    remaining = {e.id for e in loaded}
    assert remaining == {
        "oauth-live-access",
        "oauth-new-at",
        "oauth-new-rt",
        "operator-revoked",
    }
    assert all(e.token_hash != old_rt_hash for e in loaded)
    live_access = next(e for e in loaded if e.id == "oauth-live-access")
    assert live_access.revoked_at is None
    assert all(e.revoked_at is None for e in loaded if e.profile == "oauth")
    operator_revoked = next(e for e in loaded if e.id == "operator-revoked")
    assert operator_revoked.revoked_at == "2026-06-01T00:00:00Z"


def test_rotate_with_oauth_compaction_never_writes_raw_tokens(store_path):
    """Rotation compaction only ever serializes hashes, never raw credentials."""
    raw = "mcp_rotate_raw_secret"
    store = TokenStore(store_path)
    store.add_many(
        [
            StoredTokenEntry(
                id="oauth-old-rt",
                token_hash=hash_token(raw),
                name="oauth-grant",
                profile="oauth",
                scopes=["mcp:read"],
                created_at="2026-01-01T00:00:00Z",
                client_id="mcp_client_1",
                type="refresh",
                expires_at="2099-12-31T23:59:59Z",
            ),
            _oauth_entry("oauth-expired", expires_at="2025-01-01T00:00:00Z"),
        ]
    )

    store.rotate(
        hash_token(raw),
        [
            _oauth_entry("oauth-new-at", expires_at="2099-12-31T23:59:59Z"),
            _oauth_entry(
                "oauth-new-rt",
                expires_at="2099-12-31T23:59:59Z",
                token_type="refresh",
            ),
        ],
        compact_oauth_now=COMPACTION_NOW,
    )

    content = Path(store_path).read_text()
    assert raw not in content
    assert hash_token(raw) not in content  # the revoked old refresh was compacted


# ── Rotate no-match is zero-write / no append (replay race) ─────


def test_rotate_no_match_returns_none_and_writes_nothing(store_path):
    """Rotating a hash with no non-revoked record mutates nothing.

    A replayed refresh (hash already rotated/compacted, or never
    persisted) must return ``None`` without appending a replacement pair
    or touching the store file at all.
    """
    store = TokenStore(store_path)
    store.add_many(
        [
            _oauth_entry("oauth-existing", expires_at="2099-12-31T23:59:59Z"),
            _non_oauth_entry(
                "operator-revoked",
                revoked_at="2026-06-01T00:00:00Z",
            ),
        ]
    )
    before = Path(store_path).read_bytes()

    result = store.rotate(
        "sha256:no-such-hash",
        [
            _oauth_entry("oauth-missing-at", expires_at="2099-12-31T23:59:59Z"),
            _oauth_entry(
                "oauth-missing-rt",
                expires_at="2099-12-31T23:59:59Z",
                token_type="refresh",
            ),
        ],
        compact_oauth_now=COMPACTION_NOW,
    )

    assert result is None
    assert Path(store_path).read_bytes() == before
    assert [e.id for e in store.load()] == ["oauth-existing", "operator-revoked"]


def test_rotate_no_match_on_empty_store_writes_no_file(tmp_path):
    """No-match rotate against an empty store creates no file state."""
    store_path = tmp_path / "tokens.json"
    store = TokenStore(str(store_path))

    result = store.rotate(
        "sha256:nope",
        [
            _oauth_entry("oauth-new-at", expires_at="2099-12-31T23:59:59Z"),
            _oauth_entry(
                "oauth-new-rt",
                expires_at="2099-12-31T23:59:59Z",
                token_type="refresh",
            ),
        ],
        compact_oauth_now=COMPACTION_NOW,
    )

    assert result is None
    assert not store_path.exists()
    assert store.load() == []


def test_rotate_already_revoked_hash_is_no_match_and_writes_nothing(store_path):
    """A fully-rotated (revoked) refresh is treated as a no-match.

    Even when the hash still exists in the store but is revoked, rotate
    must return ``None`` without appending a replacement pair; otherwise
    a concurrent replay of one refresh would leave multiple unexpired
    replacement pairs behind.
    """
    store = TokenStore(store_path)
    store.add_many(
        [
            _oauth_entry(
                "oauth-already-revoked",
                expires_at="2099-12-31T23:59:59Z",
                revoked_at="2026-06-01T00:00:00Z",
                token_type="refresh",
            )
        ]
    )
    before = Path(store_path).read_bytes()

    result = store.rotate(
        "sha256:oauth-already-revoked",
        [
            _oauth_entry("oauth-replay-at", expires_at="2099-12-31T23:59:59Z"),
            _oauth_entry(
                "oauth-replay-rt",
                expires_at="2099-12-31T23:59:59Z",
                token_type="refresh",
            ),
        ],
        compact_oauth_now=COMPACTION_NOW,
    )

    assert result is None
    assert Path(store_path).read_bytes() == before
    assert [e.id for e in store.load()] == ["oauth-already-revoked"]


def test_rotate_no_match_without_compaction_still_no_write(store_path):
    """No-match is zero-write regardless of the compaction flag."""
    store = TokenStore(store_path)
    store.add_many([_oauth_entry("oauth-existing", expires_at="2099-12-31T23:59:59Z")])
    before = Path(store_path).read_bytes()

    result = store.rotate(
        "sha256:no-such-hash",
        [_oauth_entry("oauth-new-at", expires_at="2099-12-31T23:59:59Z")],
    )

    assert result is None
    assert Path(store_path).read_bytes() == before
    assert [e.id for e in store.load()] == ["oauth-existing"]


def test_same_refresh_concurrent_rotate_exactly_one_winner_and_pair(store_path):
    """Two processes racing to rotate the same refresh produce one winner.

    Rotation serialises on the single flock, so exactly one process may
    revoke the old refresh and persist its replacement pair; the loser
    observes no non-revoked record and performs a zero-write no-match.
    Only one replacement pair can ever survive a same-refresh replay.
    """
    import multiprocessing

    def worker_rotate(store_path_arg, pair_prefix, start, out):
        store = TokenStore(store_path_arg)
        if not start.wait(timeout=30):
            raise TimeoutError("rotate start barrier timeout")
        result = store.rotate(
            "sha256:race-old-rt",
            [
                _oauth_entry(f"{pair_prefix}-at", expires_at="2099-12-31T23:59:59Z"),
                _oauth_entry(
                    f"{pair_prefix}-rt",
                    expires_at="2099-12-31T23:59:59Z",
                    token_type="refresh",
                ),
            ],
            compact_oauth_now=COMPACTION_NOW,
        )
        out.put(result is not None)

    store = TokenStore(store_path)
    store.add_many(
        [
            _oauth_entry(
                "race-old-rt",
                expires_at="2099-12-31T23:59:59Z",
                token_type="refresh",
            )
        ]
    )

    start = multiprocessing.Event()
    out_a = multiprocessing.Queue()
    out_b = multiprocessing.Queue()
    p_a = multiprocessing.Process(
        target=worker_rotate, args=(store_path, "race-pair-a", start, out_a)
    )
    p_b = multiprocessing.Process(
        target=worker_rotate, args=(store_path, "race-pair-b", start, out_b)
    )
    p_a.start()
    p_b.start()
    start.set()
    p_a.join(timeout=30)
    p_b.join(timeout=30)
    assert p_a.exitcode == 0
    assert p_b.exitcode == 0

    results = [out_a.get(timeout=10), out_b.get(timeout=10)]
    assert results.count(True) == 1, f"expected exactly one winner, got {results}"
    assert results.count(False) == 1

    loaded = TokenStore(store_path).load()
    oauth = [e for e in loaded if e.profile == "oauth"]
    assert len(oauth) == 2  # exactly one replacement pair, never two
    assert all(e.revoked_at is None for e in oauth)
    assert all(e.token_hash != "sha256:race-old-rt" for e in oauth)
