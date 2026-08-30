"""Lifecycle regression tests for durable OAuth access tokens.

Covers the AUTH-1 corrective: access tokens are persisted atomically
together with refresh tokens at issuance, refreshed access tokens are
durable, and refresh rotation is durable (old refresh becomes invalid
across process recreation).

A controlled fake clock drives the +15/+30/+120 minute restart windows:
the access lifetime is advertised as 7200s, so a bearer that was issued
before a restart must still verify inside that window and be rejected
once the persisted expiry passes.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthClientInformationFull

from examples.mcp_server.oauth_provider import (
    GatewayOAuthProvider,
    StoredClient,
    _generate_code_challenge,
    hash_token,
)
from examples.mcp_server.token_store import StoredTokenEntry, TokenStore

CALLBACK_URL = "https://example.com/callback"
SCOPES = ["mcp:read", "mcp:project"]
ACCESS_LIFETIME = 7200


class _FakeClock:
    """Deterministic clock for restart-window simulation."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FailingRotationStore(TokenStore):
    """Store whose rotation write always fails (fail-closed refresh test)."""

    def rotate(
        self, revoke_hash: str, new_entries: list[StoredTokenEntry]
    ) -> StoredTokenEntry | None:
        raise OSError("durable token store unavailable")


def _client(client_id: str) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[CALLBACK_URL],
        client_name="Lifecycle client",
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=" ".join(SCOPES),
    )


def _provider(store_path: Path | str) -> GatewayOAuthProvider:
    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(str(store_path)))
    return provider


def _issue_tokens(provider: GatewayOAuthProvider, client_id: str) -> dict[str, str]:
    provider._clients[client_id] = StoredClient(
        client_id=client_id,
        redirect_uris=[CALLBACK_URL],
        client_name="Lifecycle client",
        scopes=list(SCOPES),
    )
    verifier = secrets.token_urlsafe(64)
    authorization = provider.create_authorization_code(
        client_id=client_id,
        redirect_uri=CALLBACK_URL,
        code_challenge=_generate_code_challenge(verifier),
        state="lifecycle",
        scopes=list(SCOPES),
    )
    return provider.exchange_code_for_token(
        client_id=client_id,
        code=authorization["code"],
        code_verifier=verifier,
        redirect_uri=CALLBACK_URL,
    )


def test_issuance_persists_access_and_refresh_together(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    provider = _provider(store_path)
    tokens = _issue_tokens(provider, "lifecycle-atomic")

    entries = TokenStore(str(store_path)).load()

    assert len(entries) == 2
    by_type = {e.type for e in entries}
    assert by_type == {"access", "refresh"}
    hashes = {hash_token(raw) for raw in (tokens["access_token"], tokens["refresh_token"])}
    assert hashes == {e.token_hash for e in entries}
    assert all(e.client_id == "lifecycle-atomic" for e in entries)
    assert all(e.scopes == SCOPES for e in entries)


def test_access_bearer_survives_process_recreation(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    first_provider = _provider(store_path)
    tokens = _issue_tokens(first_provider, "lifecycle-recreation")

    recreated_provider = _provider(store_path)
    assert recreated_provider.load_tokens() == 2

    restored = recreated_provider.verify_access_token(tokens["access_token"])
    assert restored is not None
    assert restored.client_id == "lifecycle-recreation"
    assert restored.scopes == SCOPES


@pytest.mark.parametrize("minutes", [15, 30, 120])
def test_restart_within_lifetime_keeps_access_bearer_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, minutes: int
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("examples.mcp_server.oauth_provider.time.time", clock)

    store_path = tmp_path / "tokens.json"
    provider = _provider(store_path)
    tokens = _issue_tokens(provider, f"lifecycle-restart-{minutes}")
    issued_expiry = provider.verify_access_token(tokens["access_token"]).expires_at

    clock.advance(minutes * 60)

    recreated_provider = _provider(store_path)
    recreated_provider.load_tokens()
    restored = recreated_provider.verify_access_token(tokens["access_token"])

    assert restored is not None
    assert restored.expires_at == pytest.approx(issued_expiry, abs=0.001)


def test_restart_past_lifetime_rejects_access_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("examples.mcp_server.oauth_provider.time.time", clock)

    store_path = tmp_path / "tokens.json"
    provider = _provider(store_path)
    tokens = _issue_tokens(provider, "lifecycle-expired")

    clock.advance(ACCESS_LIFETIME + 60)

    recreated_provider = _provider(store_path)
    recreated_provider.load_tokens()

    assert recreated_provider.verify_access_token(tokens["access_token"]) is None


def test_refresh_rotates_durably_and_returns_new_refresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("examples.mcp_server.oauth_provider.time.time", clock)

    store_path = tmp_path / "tokens.json"
    provider = _provider(store_path)
    client_id = "lifecycle-rotation"
    tokens = _issue_tokens(provider, client_id)
    clock.advance(60)

    refreshed = provider.refresh_access_token(client_id, tokens["refresh_token"])

    assert "refresh_token" in refreshed
    assert refreshed["refresh_token"] != tokens["refresh_token"]
    assert refreshed["access_token"] != tokens["access_token"]
    assert refreshed["token_type"] == "Bearer"

    entries = TokenStore(str(store_path)).load()
    new_rt_hash = hash_token(refreshed["refresh_token"])
    old_rt = next(e for e in entries if e.token_hash == hash_token(tokens["refresh_token"]))
    assert old_rt.revoked_at is not None
    assert any(e.token_hash == new_rt_hash and e.revoked_at is None for e in entries)


def test_refreshed_access_survives_process_recreation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("examples.mcp_server.oauth_provider.time.time", clock)

    store_path = tmp_path / "tokens.json"
    provider = _provider(store_path)
    client_id = "lifecycle-refreshed-access"
    tokens = _issue_tokens(provider, client_id)
    clock.advance(60)
    refreshed = provider.refresh_access_token(client_id, tokens["refresh_token"])
    clock.advance(300)

    recreated_provider = _provider(store_path)
    recreated_provider.load_tokens()
    restored = recreated_provider.verify_access_token(refreshed["access_token"])

    assert restored is not None
    assert restored.client_id == client_id


def test_old_refresh_token_invalid_after_rotation_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("examples.mcp_server.oauth_provider.time.time", clock)

    store_path = tmp_path / "tokens.json"
    provider = _provider(store_path)
    client_id = "lifecycle-old-refresh"
    tokens = _issue_tokens(provider, client_id)
    clock.advance(60)
    refreshed = provider.refresh_access_token(client_id, tokens["refresh_token"])

    with pytest.raises(TokenError) as before:
        provider.refresh_access_token(client_id, tokens["refresh_token"])
    assert before.value.error == "invalid_grant"

    recreated_provider = _provider(store_path)
    recreated_provider.load_tokens()

    with pytest.raises(TokenError) as after:
        recreated_provider.refresh_access_token(client_id, tokens["refresh_token"])
    assert after.value.error == "invalid_grant"

    new_tokens = recreated_provider.refresh_access_token(client_id, refreshed["refresh_token"])
    assert "access_token" in new_tokens


def test_refresh_rotation_preserves_client_binding_and_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("examples.mcp_server.oauth_provider.time.time", clock)

    store_path = tmp_path / "tokens.json"
    provider = _provider(store_path)
    client_id = "lifecycle-binding"
    tokens = _issue_tokens(provider, client_id)
    clock.advance(60)
    refreshed = provider.refresh_access_token(client_id, tokens["refresh_token"])

    stored_access = provider.verify_access_token(refreshed["access_token"])
    stored_refresh_id = next(
        k
        for k, v in provider._tokens.items()
        if v.type == "refresh" and k == hash_token(refreshed["refresh_token"])
    )
    stored_refresh = provider._tokens[stored_refresh_id]

    assert stored_access is not None
    assert stored_access.client_id == client_id
    assert stored_access.scopes == SCOPES
    assert stored_refresh.client_id == client_id
    assert stored_refresh.scopes == SCOPES

    with pytest.raises(TokenError) as wrong_client:
        provider.refresh_access_token("lifecycle-other", refreshed["refresh_token"])
    assert wrong_client.value.error == "invalid_grant"


def test_refresh_fails_closed_when_rotation_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("examples.mcp_server.oauth_provider.time.time", clock)

    store_path = tmp_path / "tokens.json"
    provider = GatewayOAuthProvider()
    provider.set_token_store(_FailingRotationStore(str(store_path)))
    client_id = "lifecycle-rotation-failure"
    tokens = _issue_tokens(provider, client_id)
    clock.advance(60)

    all_hashes_before = set(provider._tokens)

    with pytest.raises(OSError, match="durable token store unavailable"):
        provider.refresh_access_token(client_id, tokens["refresh_token"])

    assert set(provider._tokens) == all_hashes_before
    stored = provider.verify_access_token(tokens["access_token"])
    assert stored is not None
