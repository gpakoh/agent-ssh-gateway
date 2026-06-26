"""Tests for GatewayOAuthProvider."""

import secrets

import pytest

from examples.mcp_server.oauth_provider import (
    DEFAULT_SCOPES,
    SUPPORTED_SCOPES,
    GatewayOAuthProvider,
    _generate_code_challenge,
    _parse_scopes,
    _verify_pkce,
)


def test_pkce_verification_valid():
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _generate_code_challenge(code_verifier)
    assert _verify_pkce(code_verifier, code_challenge) is True


def test_pkce_verification_invalid():
    code_verifier = secrets.token_urlsafe(64)
    wrong_challenge = "AAAA" + _generate_code_challenge(code_verifier)[4:]
    assert _verify_pkce(code_verifier, wrong_challenge) is False


def test_pkce_verifier_too_short():
    with pytest.raises(ValueError):
        _verify_pkce("short", "challenge")


def test_generate_code_challenge_deterministic():
    verifier = secrets.token_urlsafe(64)
    c1 = _generate_code_challenge(verifier)
    c2 = _generate_code_challenge(verifier)
    assert c1 == c2


def test_generate_code_challenge_differs():
    v1 = secrets.token_urlsafe(64)
    v2 = secrets.token_urlsafe(64)
    assert _generate_code_challenge(v1) != _generate_code_challenge(v2)


def test_scope_constants():
    assert "mcp:read" in SUPPORTED_SCOPES
    assert "mcp:admin" not in DEFAULT_SCOPES


@pytest.fixture
def provider():
    return GatewayOAuthProvider()


@pytest.mark.anyio
async def test_dcr_register(provider):
    from mcp.shared.auth import OAuthClientInformationFull

    client_info = OAuthClientInformationFull(
        redirect_uris=["https://chatgpt.com/callback"],
        client_name="Test Client",
        token_endpoint_auth_method="none",
    )
    await provider.register_client(client_info)
    assert client_info.client_id is not None
    assert client_info.client_id.startswith("mcp_client_")
    assert client_info.client_secret is None


@pytest.mark.anyio
async def test_dcr_requires_redirect_uri(provider):
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OAuthClientInformationFull(
            redirect_uris=[],
            client_name="No Redirect",
        )


@pytest.mark.anyio
async def test_get_client(provider):
    from mcp.shared.auth import OAuthClientInformationFull

    client_info = OAuthClientInformationFull(
        redirect_uris=["https://chatgpt.com/callback"],
        client_name="Test",
    )
    await provider.register_client(client_info)
    stored = await provider.get_client(client_info.client_id)
    assert stored is not None
    assert stored.client_name == "Test"


@pytest.mark.anyio
async def test_get_client_unknown(provider):
    assert await provider.get_client("nonexistent") is None


def test_authorization_code_flow(provider):
    reg_client = provider._clients
    client_id = "mcp_client_test_1"
    reg_client[client_id] = type("StoredClient", (), {
        "client_id": client_id, "redirect_uris": ["https://chatgpt.com/callback"],
        "client_name": "Test",
    })()
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _generate_code_challenge(code_verifier)

    auth = provider.create_authorization_code(
        client_id=client_id,
        redirect_uri="https://chatgpt.com/callback",
        code_challenge=code_challenge,
        state="test-state",
        scopes=["mcp:read"],
    )
    assert "code" in auth
    assert auth["state"] == "test-state"

    tokens = provider.exchange_code_for_token(
        client_id=client_id,
        code=auth["code"],
        code_verifier=code_verifier,
        redirect_uri="https://chatgpt.com/callback",
    )
    assert "access_token" in tokens
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 7200
    assert "refresh_token" in tokens


def test_code_reuse_rejected(provider):
    provider._clients["cid"] = type("S", (), {
        "client_id": "cid",
        "redirect_uris": ["https://example.com/cb"],
        "client_name": "T",
    })()
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code("cid", "https://example.com/cb", cc, "s", ["mcp:read"])
    provider.exchange_code_for_token("cid", auth["code"], cv, "https://example.com/cb")
    with pytest.raises(ValueError, match="already used"):
        provider.exchange_code_for_token("cid", auth["code"], cv, "https://example.com/cb")


def test_pkce_verification_rejects_wrong_verifier(provider):
    provider._clients["cid2"] = type("S", (), {
        "client_id": "cid2",
        "redirect_uris": ["https://example.com/cb"],
        "client_name": "T",
    })()
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code("cid2", "https://example.com/cb", cc, "s", ["mcp:read"])
    with pytest.raises(ValueError, match="PKCE verification"):
        provider.exchange_code_for_token("cid2", auth["code"], "wrong_verifier", "https://example.com/cb")


def test_access_token_verification(provider):
    provider._clients["cid3"] = type("S", (), {
        "client_id": "cid3",
        "redirect_uris": ["https://example.com/cb"],
        "client_name": "T",
    })()
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code("cid3", "https://example.com/cb", cc, "s")
    tokens = provider.exchange_code_for_token("cid3", auth["code"], cv, "https://example.com/cb")
    stored = provider.verify_access_token(tokens["access_token"])
    assert stored is not None
    assert stored.client_id == "cid3"
    assert stored.type == "access"


def test_refresh_token(provider):
    provider._clients["cid4"] = type("S", (), {
        "client_id": "cid4",
        "redirect_uris": ["https://example.com/cb"],
        "client_name": "T",
    })()
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code("cid4", "https://example.com/cb", cc, "s")
    tokens = provider.exchange_code_for_token("cid4", auth["code"], cv, "https://example.com/cb")
    refreshed = provider.refresh_access_token("cid4", tokens["refresh_token"])
    assert "access_token" in refreshed
    assert refreshed["token_type"] == "Bearer"


def test_revoke_token(provider):
    provider._clients["cid5"] = type("S", (), {
        "client_id": "cid5",
        "redirect_uris": ["https://example.com/cb"],
        "client_name": "T",
    })()
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code("cid5", "https://example.com/cb", cc, "s")
    tokens = provider.exchange_code_for_token("cid5", auth["code"], cv, "https://example.com/cb")
    provider.revoke_client_token("cid5", tokens["access_token"])
    assert provider.verify_access_token(tokens["access_token"]) is None


def test_scope_validation():
    assert _parse_scopes("mcp:read mcp:project") == ["mcp:read", "mcp:project"]
    assert _parse_scopes(None) == ["mcp:read", "mcp:project"]
    assert _parse_scopes("") == ["mcp:read", "mcp:project"]
    with pytest.raises(ValueError, match="Unsupported scope"):
        _parse_scopes("mcp:admin")
