"""Regression tests for persisted OAuth token expiry across restarts.

Proves that `GatewayOAuthProvider.load_tokens()` applies persisted
`expires_at` records at startup: entries whose persistence expiry has
already passed are skipped entirely (filtered out, never registered in
memory), entries still inside their window are restored with their exact
expiry, and legacy `expires_at=None` records keep acting as "never
expires". Revoked persisted entries also remain rejected.
"""

import pytest

from examples.mcp_server.oauth_provider import (
    GatewayOAuthProvider,
    hash_token,
)
from examples.mcp_server.token_store import StoredTokenEntry, TokenStore


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "tokens.json")


@pytest.fixture
def expired_entry():
    """Persisted token entry with expires_at in the past."""
    raw = "mcp_test_expired_persisted_token"
    return StoredTokenEntry(
        id="tok_expired_1",
        token_hash=hash_token(raw),
        name="expired-token",
        profile="full",
        scopes=["mcp:read", "mcp:admin"],
        created_at="2025-01-01T00:00:00Z",
        expires_at="2025-06-01T00:00:00Z",
    ), raw


@pytest.fixture
def valid_entry():
    """Persisted token entry with expires_at far in the future."""
    raw = "mcp_test_valid_persisted_token"
    return StoredTokenEntry(
        id="tok_valid_1",
        token_hash=hash_token(raw),
        name="valid-token",
        profile="full",
        scopes=["mcp:read"],
        created_at="2025-01-01T00:00:00Z",
        expires_at="2099-12-31T23:59:59Z",
    ), raw


@pytest.fixture
def no_expiry_entry():
    """Persisted token entry with expires_at=None (old format)."""
    raw = "mcp_test_no_expiry_persisted_token"
    return StoredTokenEntry(
        id="tok_no_expiry_1",
        token_hash=hash_token(raw),
        name="no-expiry-token",
        profile="full",
        scopes=["mcp:read"],
        created_at="2025-01-01T00:00:00Z",
        expires_at=None,
    ), raw


# ── Startup filtering regression (post-fix) ─────────────────────


def test_expired_token_rejected_after_reload(store_path, expired_entry):
    """Persisted token with past expires_at must be skipped at reload."""
    entry, raw_token = expired_entry
    store = TokenStore(store_path)
    store.add(entry)

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    count = provider.load_tokens()
    assert count == 0, (
        "BUG: expired persisted token was registered at load; "
        "expected it to be skipped because expires_at is in the past"
    )
    assert provider._tokens == {}

    result = provider.verify_access_token(raw_token)
    assert result is None


def test_not_expired_token_rejected_after_its_persisted_expiry(
    store_path, expired_entry, valid_entry
):
    """Token with future expires_at must be rejected once that time passes."""
    expired, _ = expired_entry
    store = TokenStore(store_path)
    store.add(expired)

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    provider.load_tokens()

    assert provider._tokens == {}, (
        "BUG: token with past expires_at registered in memory at load; "
        "should have been filtered out once persisted expiry has passed"
    )
    raw_expired = "mcp_test_expired_persisted_token"
    result = provider.verify_access_token(raw_expired)
    assert result is None


def test_startup_skips_only_already_expired_using_one_captured_now(
    store_path, monkeypatch
):
    """Startup filters against one captured ``now``: future entries load."""
    import time as time_module

    now = 1_700_000_000.0
    monkeypatch.setattr("examples.mcp_server.oauth_provider.time.time", lambda: now)

    store = TokenStore(store_path)
    store.add(
        StoredTokenEntry(
            id="tok_just_expired",
            token_hash=hash_token("mcp_startup_expired"),
            name="startup-expired",
            profile="full",
            scopes=["mcp:read"],
            created_at="2025-01-01T00:00:00Z",
            expires_at=time_module.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time_module.gmtime(now - 1)
            ),
        )
    )
    store.add(
        StoredTokenEntry(
            id="tok_about_to_expire",
            token_hash=hash_token("mcp_startup_boundary"),
            name="startup-boundary",
            profile="full",
            scopes=["mcp:read"],
            created_at="2025-01-01T00:00:00Z",
            expires_at=time_module.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time_module.gmtime(now + 300)
            ),
        )
    )

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    assert provider.load_tokens() == 1

    assert provider.verify_access_token("mcp_startup_expired") is None
    assert provider.verify_access_token("mcp_startup_boundary") is not None


# ── Passing regression tests (should pass before and after fix) ─


def test_valid_token_accepted_after_reload(store_path, valid_entry):
    """Token with future expires_at must be accepted after reload."""
    entry, raw_token = valid_entry
    store = TokenStore(store_path)
    store.add(entry)

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    provider.load_tokens()

    result = provider.verify_access_token(raw_token)
    assert result is not None
    assert result.scopes == ["mcp:read"]


def test_no_expiry_token_accepted_after_reload(store_path, no_expiry_entry):
    """Old persisted record with expires_at=None must behave as 'no expiry'."""
    entry, raw_token = no_expiry_entry
    store = TokenStore(store_path)
    store.add(entry)

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    provider.load_tokens()

    result = provider.verify_access_token(raw_token)
    assert result is not None, (
        "Old persisted entry with expires_at=None should behave as 'never expires'"
    )


def test_revoked_token_stays_rejected_after_reload(store_path):
    """A revoked persisted token must remain rejected after restart."""
    raw = "mcp_test_revoked_persisted_token"
    entry = StoredTokenEntry(
        id="tok_revoked_persisted",
        token_hash=hash_token(raw),
        name="revoked-token",
        profile="full",
        scopes=["mcp:read"],
        created_at="2025-01-01T00:00:00Z",
        expires_at="2099-12-31T23:59:59Z",
    )
    store = TokenStore(store_path)
    store.add(entry)
    store.revoke("tok_revoked_persisted")

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    count = provider.load_tokens()
    assert count == 0  # revoked entries are skipped

    assert provider.verify_access_token(raw) is None


def test_no_raw_token_in_persisted_store(store_path):
    """Persisted store file must never contain raw token values."""
    raw = "mcp_test_raw_token_hygiene"
    token_hash = hash_token(raw)
    entry = StoredTokenEntry(
        id="tok_hygiene_1",
        token_hash=token_hash,
        name="hygiene-test",
        profile="full",
        scopes=["mcp:read"],
        created_at="2025-01-01T00:00:00Z",
    )
    store = TokenStore(store_path)
    store.add(entry)

    with open(store_path) as f:
        file_content = f.read()

    assert raw not in file_content, (
        "Raw token value found in persisted store file; only sha256 hash should be stored"
    )
    assert token_hash in file_content


def test_no_raw_token_in_log_output(store_path, capsys):
    """Loading persisted tokens must not emit raw token values to stdout/stderr."""
    raw = "mcp_test_log_hygiene_token"
    entry = StoredTokenEntry(
        id="tok_log_hygiene",
        token_hash=hash_token(raw),
        name="log-hygiene",
        profile="full",
        scopes=["mcp:read"],
        created_at="2025-01-01T00:00:00Z",
    )
    store = TokenStore(store_path)
    store.add(entry)

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(store_path))
    provider.load_tokens()

    captured = capsys.readouterr()
    assert raw not in captured.out
    assert raw not in captured.err


def test_dcr_pkce_flow_unaffected(tmp_path):
    """DCR + PKCE + authorization-code exchange must work unchanged.

    This exercises token issuance with durable refresh storage attached,
    proving the expiry fix remains compatible with authorization-code/PKCE flow.
    """
    import secrets

    from examples.mcp_server.oauth_provider import (
        _generate_code_challenge,
    )

    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(str(tmp_path / "oauth-tokens.json")))

    # Register a client
    client_id = "mcp_client_expiry_test"
    provider._clients[client_id] = type(
        "StoredClient",
        (),
        {
            "client_id": client_id,
            "redirect_uris": ["https://example.com/cb"],
            "client_name": "Expiry Test",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scopes": ["mcp:read"],
            "created_at": 1700000000.0,
        },
    )()

    # Create auth code
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code(
        client_id=client_id,
        redirect_uri="https://example.com/cb",
        code_challenge=cc,
        state="expiry-test",
        scopes=["mcp:read"],
    )
    assert "code" in auth

    # Exchange for tokens
    tokens = provider.exchange_code_for_token(
        client_id=client_id,
        code=auth["code"],
        code_verifier=cv,
        redirect_uri="https://example.com/cb",
    )
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 7200

    # Verify access token works
    stored = provider.verify_access_token(tokens["access_token"])
    assert stored is not None
    assert stored.scopes == ["mcp:read"]

    # Refresh works
    refreshed = provider.refresh_access_token(client_id, tokens["refresh_token"])
    assert "access_token" in refreshed
    assert refreshed["expires_in"] == 7200

    # Old access token still valid (refresh doesn't revoke old)
    assert provider.verify_access_token(tokens["access_token"]) is not None
