"""Regression coverage for OAuth refresh representation and durability."""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from examples.mcp_server.oauth_provider import (
    GatewayOAuthProvider,
    StoredClient,
    StoredToken,
    _generate_code_challenge,
    hash_token,
)
from examples.mcp_server.token_store import StoredTokenEntry, TokenStore

CALLBACK_URL = "https://example.com/callback"
SCOPES = ["mcp:read", "mcp:project"]


class _ClientAuthenticator:
    def __init__(self, client: OAuthClientInformationFull) -> None:
        self._client = client

    async def authenticate_request(self, _request: Request) -> OAuthClientInformationFull:
        return self._client


class _RecordingTokenMap(dict[str, StoredToken]):
    def __init__(self, values: dict[str, StoredToken]) -> None:
        super().__init__(values)
        self.lookups: list[str] = []

    def get(self, key: str, default: Any = None) -> StoredToken | Any:
        self.lookups.append(key)
        return super().get(key, default)


class _FailingTokenStore(TokenStore):
    def add(self, entry: StoredTokenEntry) -> None:
        raise OSError("durable token store unavailable")


def _client(client_id: str) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[CALLBACK_URL],
        client_name="Refresh regression client",
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=" ".join(SCOPES),
    )


def _provider(store_path: Path) -> GatewayOAuthProvider:
    provider = GatewayOAuthProvider()
    provider.set_token_store(TokenStore(str(store_path)))
    return provider


def _issue_tokens(
    provider: GatewayOAuthProvider,
    client_id: str,
) -> dict[str, Any]:
    provider._clients[client_id] = StoredClient(
        client_id=client_id,
        redirect_uris=[CALLBACK_URL],
        client_name="Refresh regression client",
        scopes=list(SCOPES),
    )
    verifier = secrets.token_urlsafe(64)
    authorization = provider.create_authorization_code(
        client_id=client_id,
        redirect_uri=CALLBACK_URL,
        code_challenge=_generate_code_challenge(verifier),
        state="refresh-regression",
        scopes=list(SCOPES),
    )
    return provider.exchange_code_for_token(
        client_id=client_id,
        code=authorization["code"],
        code_verifier=verifier,
        redirect_uri=CALLBACK_URL,
    )


def _issue_refresh_token(provider: GatewayOAuthProvider, client_id: str) -> str:
    return _issue_tokens(provider, client_id)["refresh_token"]


def _token_client(
    provider: GatewayOAuthProvider,
    client: OAuthClientInformationFull,
) -> TestClient:
    authenticator = _ClientAuthenticator(client)
    handler = TokenHandler(provider=provider, client_authenticator=authenticator)  # type: ignore[arg-type]
    app = Starlette(routes=[Route("/token", handler.handle, methods=["POST"])])
    return TestClient(app, raise_server_exceptions=False)


def _refresh_request(client_id: str, raw_refresh_token: str) -> dict[str, str]:
    return {
        "grant_type": "refresh_token",
        "refresh_token": raw_refresh_token,
        "client_id": client_id,
    }


def _single_hash_lookups_only(lookups: list[str], raw_refresh_token: str) -> bool:
    hash1 = hash_token(raw_refresh_token)
    hash2 = hash_token(hash1)
    return lookups == [hash1, hash1] and hash2 not in lookups


def _contains_secret(serialized: str, raw_token: str) -> bool:
    return raw_token in serialized


def test_sdk_refresh_round_trip_hashes_lookup_exactly_once(tmp_path: Path) -> None:
    """Both validation lookups derive HASH1 from RAW; neither derives HASH2."""
    provider = _provider(tmp_path / "tokens.json")
    client = _client("refresh-sdk-chain")
    raw_refresh_token = _issue_refresh_token(provider, client.client_id)
    recording_tokens = _RecordingTokenMap(provider._tokens)
    provider._tokens = recording_tokens

    with _token_client(provider, client) as http:
        response = http.post(
            "/token",
            data=_refresh_request(client.client_id, raw_refresh_token),
        )

    assert response.status_code == 200
    assert _single_hash_lookups_only(recording_tokens.lookups, raw_refresh_token)


@pytest.mark.anyio
async def test_load_refresh_token_preserves_raw_sdk_representation(tmp_path: Path) -> None:
    provider = _provider(tmp_path / "tokens.json")
    client = _client("refresh-raw-contract")
    raw_refresh_token = _issue_refresh_token(provider, client.client_id)

    loaded = await provider.load_refresh_token(client, raw_refresh_token)

    assert loaded is not None
    representation_is_raw = secrets.compare_digest(loaded.token, raw_refresh_token)
    representations_are_safe = all(
        not _contains_secret(serialized, raw_refresh_token)
        for serialized in (repr(loaded), str(loaded), loaded.model_dump_json())
    )
    assert representation_is_raw
    assert representations_are_safe
    assert not loaded.token.startswith("sha256:")


def test_refresh_record_survives_provider_recreation(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    first_provider = _provider(store_path)
    client = _client("refresh-recreation")
    raw_refresh_token = _issue_refresh_token(first_provider, client.client_id)

    recreated_provider = _provider(store_path)
    assert recreated_provider.load_tokens() == 1

    with _token_client(recreated_provider, client) as http:
        response = http.post(
            "/token",
            data=_refresh_request(client.client_id, raw_refresh_token),
        )

    assert response.status_code == 200


def test_persisted_refresh_restores_client_type_scopes_and_expiry(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    first_provider = _provider(store_path)
    client = _client("refresh-metadata")
    raw_refresh_token = _issue_refresh_token(first_provider, client.client_id)
    token_hash = hash_token(raw_refresh_token)
    original = first_provider._tokens[token_hash]

    entries = TokenStore(str(store_path)).load()
    recreated_provider = _provider(store_path)
    recreated_provider.load_tokens()
    restored = recreated_provider._tokens.get(token_hash)

    assert len(entries) == 1
    assert restored is not None
    assert entries[0].client_id == client.client_id
    assert entries[0].type == "refresh"
    assert entries[0].scopes == SCOPES
    assert restored.client_id == original.client_id
    assert restored.type == original.type == "refresh"
    assert restored.scopes == original.scopes
    assert restored.expires_at == pytest.approx(original.expires_at, abs=0.001)


def test_persisted_refresh_token_preserves_client_isolation(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    first_provider = _provider(store_path)
    owner = _client("refresh-owner")
    other = _client("refresh-other")
    raw_refresh_token = _issue_refresh_token(first_provider, owner.client_id)

    recreated_provider = _provider(store_path)
    assert recreated_provider.load_tokens() == 1
    with _token_client(recreated_provider, other) as http:
        response = http.post(
            "/token",
            data=_refresh_request(other.client_id, raw_refresh_token),
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_dynamic_oauth_store_never_contains_raw_tokens(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    provider = _provider(store_path)
    tokens = _issue_tokens(provider, "refresh-secret-hygiene")
    serialized = store_path.read_text()
    persisted_repr = repr(TokenStore(str(store_path)).load())
    runtime_repr = repr(provider._tokens)

    secrets_absent = all(
        not _contains_secret(representation, raw_token)
        for representation in (serialized, persisted_repr, runtime_repr)
        for raw_token in (tokens["access_token"], tokens["refresh_token"])
    )
    assert secrets_absent


@pytest.mark.anyio
async def test_persisted_refresh_revocation_survives_recreation(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    provider = _provider(store_path)
    client = _client("refresh-revocation")
    raw_refresh_token = _issue_refresh_token(provider, client.client_id)

    await provider.revoke_token(raw_refresh_token)
    recreated_provider = _provider(store_path)

    assert recreated_provider.load_tokens() == 0
    with _token_client(recreated_provider, client) as http:
        response = http.post(
            "/token",
            data=_refresh_request(client.client_id, raw_refresh_token),
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_oauth_issuance_fails_closed_when_refresh_persistence_fails(tmp_path: Path) -> None:
    provider = GatewayOAuthProvider()
    provider.set_token_store(_FailingTokenStore(str(tmp_path / "tokens.json")))
    client_id = "refresh-persistence-failure"
    provider._clients[client_id] = StoredClient(
        client_id=client_id,
        redirect_uris=[CALLBACK_URL],
        scopes=list(SCOPES),
    )
    verifier = secrets.token_urlsafe(64)
    authorization = provider.create_authorization_code(
        client_id,
        CALLBACK_URL,
        _generate_code_challenge(verifier),
        "state",
        list(SCOPES),
    )

    with pytest.raises(OSError, match="durable token store unavailable"):
        provider.exchange_code_for_token(
            client_id,
            authorization["code"],
            verifier,
            CALLBACK_URL,
        )

    assert provider._tokens == {}


def test_oauth_issuance_requires_durable_token_store() -> None:
    provider = GatewayOAuthProvider()
    client_id = "refresh-store-required"
    provider._clients[client_id] = StoredClient(
        client_id=client_id,
        redirect_uris=[CALLBACK_URL],
        scopes=list(SCOPES),
    )
    verifier = secrets.token_urlsafe(64)
    authorization = provider.create_authorization_code(
        client_id,
        CALLBACK_URL,
        _generate_code_challenge(verifier),
        "state",
        list(SCOPES),
    )

    with pytest.raises(RuntimeError, match="persistence is not configured"):
        provider.exchange_code_for_token(
            client_id,
            authorization["code"],
            verifier,
            CALLBACK_URL,
        )

    assert provider._tokens == {}


def test_refresh_handler_missing_token_returns_invalid_grant_400(tmp_path: Path) -> None:
    provider = _provider(tmp_path / "tokens.json")
    client = _client("refresh-missing")

    with _token_client(provider, client) as http:
        response = http.post(
            "/token",
            data=_refresh_request(client.client_id, "unknown-refresh-credential"),
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_refresh_handler_expired_token_returns_invalid_grant_400(tmp_path: Path) -> None:
    provider = _provider(tmp_path / "tokens.json")
    client = _client("refresh-expired")
    raw_refresh_token = "expired-refresh-credential"
    provider._tokens[hash_token(raw_refresh_token)] = StoredToken(
        token=hash_token(raw_refresh_token),
        client_id=client.client_id,
        scopes=list(SCOPES),
        expires_at=time.time() - 1,
        type="refresh",
    )

    with _token_client(provider, client) as http:
        response = http.post(
            "/token",
            data=_refresh_request(client.client_id, raw_refresh_token),
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_refresh_expiry_race_never_returns_500(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(tmp_path / "tokens.json")
    client = _client("refresh-expiry-race")
    raw_refresh_token = _issue_refresh_token(provider, client.client_id)
    original_load = provider.load_refresh_token

    async def expire_after_load(
        client_info: OAuthClientInformationFull,
        token: str,
    ) -> Any:
        loaded = await original_load(client_info, token)
        provider._tokens[hash_token(token)].expires_at = time.time() - 1
        return loaded

    monkeypatch.setattr(provider, "load_refresh_token", expire_after_load)

    with _token_client(provider, client) as http:
        response = http.post(
            "/token",
            data=_refresh_request(client.client_id, raw_refresh_token),
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_refresh_access_token_uses_typed_invalid_grant(tmp_path: Path) -> None:
    provider = _provider(tmp_path / "tokens.json")

    with pytest.raises(TokenError) as exc_info:
        provider.refresh_access_token("refresh-client", "unknown-refresh-credential")

    assert exc_info.value.error == "invalid_grant"


def test_corrupt_token_store_is_infrastructure_failure(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    store_path.write_text("{not-json")

    with pytest.raises(ValueError, match="invalid JSON"):
        TokenStore(str(store_path)).load()


def test_oauth_setup_fails_closed_on_corrupt_token_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server.mcp_infra.auth_setup import setup

    store_path = tmp_path / "tokens.json"
    store_path.write_text("{not-json")
    monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
    monkeypatch.setenv("MCP_TOKEN_STORE_FILE", str(store_path))
    monkeypatch.setenv("MCP_CLIENT_STORE_FILE", str(tmp_path / "clients.json"))
    monkeypatch.setenv("MCP_AGENT_BACKEND_ROUTER_ENABLED", "false")

    with pytest.raises(RuntimeError, match="OAuth token store initialization failed"):
        setup()


def test_v1_static_token_entry_remains_access_mcp_static(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    raw_static_token = "legacy-static-credential"
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "tokens": [
                    {
                        "id": "legacy-static",
                        "token_hash": hash_token(raw_static_token),
                        "name": "legacy",
                        "profile": "viewer",
                        "scopes": ["mcp:read"],
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        )
    )

    entries = TokenStore(str(store_path)).load()
    provider = _provider(store_path)
    provider.load_tokens()
    stored = provider.verify_access_token(raw_static_token)

    assert entries[0].client_id == "mcp_static"
    assert entries[0].type == "access"
    assert stored is not None
    assert stored.client_id == "mcp_static"
    assert stored.type == "access"
