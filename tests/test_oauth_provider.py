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


def test_dcr_register(provider):
    result = provider.register_client(
        redirect_uris=["https://chatgpt.com/callback"],
        client_name="Test Client",
    )
    assert "client_id" in result
    assert result["client_secret"] is None
    assert result["token_endpoint_auth_method"] == "none"
    assert result["redirect_uris"] == ["https://chatgpt.com/callback"]


def test_dcr_requires_redirect_uri(provider):
    with pytest.raises(ValueError, match="redirect_uri"):
        provider.register_client(redirect_uris=[])


def test_get_client(provider):
    reg = provider.register_client(
        redirect_uris=["https://chatgpt.com/callback"],
        client_name="Test",
    )
    client = provider.get_client(reg["client_id"])
    assert client is not None
    assert client.client_name == "Test"


def test_get_client_unknown(provider):
    assert provider.get_client("nonexistent") is None


def test_authorization_code_flow(provider):
    reg = provider.register_client(
        redirect_uris=["https://chatgpt.com/callback"],
        client_name="Test",
    )
    client_id = reg["client_id"]
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
    reg = provider.register_client(redirect_uris=["https://example.com/cb"])
    client_id = reg["client_id"]
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code(
        client_id, "https://example.com/cb", cc, "s", ["mcp:read"]
    )
    provider.exchange_code_for_token(client_id, auth["code"], cv, "https://example.com/cb")
    with pytest.raises(ValueError, match="already used"):
        provider.exchange_code_for_token(client_id, auth["code"], cv, "https://example.com/cb")


def test_pkce_verification_rejects_wrong_verifier(provider):
    reg = provider.register_client(redirect_uris=["https://example.com/cb"])
    client_id = reg["client_id"]
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code(
        client_id, "https://example.com/cb", cc, "s", ["mcp:read"]
    )
    with pytest.raises(ValueError, match="PKCE verification"):
        provider.exchange_code_for_token(
            client_id, auth["code"], "wrong_verifier", "https://example.com/cb"
        )


def test_access_token_verification(provider):
    reg = provider.register_client(redirect_uris=["https://example.com/cb"])
    client_id = reg["client_id"]
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code(client_id, "https://example.com/cb", cc, "s")
    tokens = provider.exchange_code_for_token(client_id, auth["code"], cv, "https://example.com/cb")

    stored = provider.verify_access_token(tokens["access_token"])
    assert stored is not None
    assert stored.client_id == client_id
    assert stored.type == "access"


def test_refresh_token(provider):
    reg = provider.register_client(redirect_uris=["https://example.com/cb"])
    client_id = reg["client_id"]
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code(client_id, "https://example.com/cb", cc, "s")
    tokens = provider.exchange_code_for_token(client_id, auth["code"], cv, "https://example.com/cb")

    refreshed = provider.refresh_access_token(client_id, tokens["refresh_token"])
    assert "access_token" in refreshed
    assert refreshed["token_type"] == "Bearer"


def test_revoke_token(provider):
    reg = provider.register_client(redirect_uris=["https://example.com/cb"])
    client_id = reg["client_id"]
    cv = secrets.token_urlsafe(64)
    cc = _generate_code_challenge(cv)
    auth = provider.create_authorization_code(client_id, "https://example.com/cb", cc, "s")
    tokens = provider.exchange_code_for_token(client_id, auth["code"], cv, "https://example.com/cb")

    provider.revoke_token(client_id, tokens["access_token"])
    assert provider.verify_access_token(tokens["access_token"]) is None


def test_scope_validation():
    assert _parse_scopes("mcp:read mcp:project") == ["mcp:read", "mcp:project"]
    assert _parse_scopes(None) == ["mcp:read", "mcp:project"]
    assert _parse_scopes("") == ["mcp:read", "mcp:project"]
    with pytest.raises(ValueError, match="Unsupported scope"):
        _parse_scopes("mcp:admin")
