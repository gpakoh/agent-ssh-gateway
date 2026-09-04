"""Tests for MCP auth middleware (oauth mode)."""

import os
from unittest.mock import patch

import pytest
from starlette.responses import JSONResponse


@pytest.fixture
def valid_token():
    return "test-token-123"


async def _mock_proxy(request):
    """Stub upstream — the middleware is what we test, not the real MCP server."""
    return JSONResponse({"ok": True})


@pytest.fixture
def token_client(valid_token):
    from starlette.testclient import TestClient

    with patch.dict(os.environ, {"MCP_PUBLIC_TOKEN": valid_token, "MCP_AUTH_MODE": "token"}):
        import importlib

        import examples.mcp_client_remote.server as srv

        importlib.reload(srv)
        srv.proxy_request = _mock_proxy
        app = srv.create_proxy_app()
        yield TestClient(app)


@pytest.fixture
def oauth_client(valid_token):
    from starlette.testclient import TestClient

    with patch.dict(os.environ, {"MCP_PUBLIC_TOKEN": valid_token, "MCP_AUTH_MODE": "oauth"}):
        import importlib

        import examples.mcp_client_remote.server as srv

        importlib.reload(srv)
        srv.proxy_request = _mock_proxy
        app = srv.create_proxy_app()
        yield TestClient(app)


def test_oauth_public_paths():
    from examples.mcp_client_remote.server import _is_oauth_public_path

    assert _is_oauth_public_path("/.well-known/oauth-authorization-server")
    assert _is_oauth_public_path("/oauth/authorize")
    assert _is_oauth_public_path("/oauth/token")
    assert _is_oauth_public_path("/oauth/register")
    assert not _is_oauth_public_path("/mcp")
    assert _is_oauth_public_path("/health")
    # Bare top-level forms (advertised by openid_configuration()'s
    # authorization_endpoint/token_endpoint/registration_endpoint) must
    # also stay public, exactly and with nested subpaths.
    assert _is_oauth_public_path("/authorize")
    assert _is_oauth_public_path("/token")
    assert _is_oauth_public_path("/register")
    assert _is_oauth_public_path("/token/refresh")


def test_oauth_public_paths_require_a_boundary_not_a_bare_prefix():
    """Regression (R5): path.startswith(("/authorize", "/token",
    "/register", "/health")) treated ANY path merely starting with one of
    those substrings as public -- a future route named e.g.
    /authorized_keys, /tokens-export, /registered-hosts, or
    /health-debug-internal would silently skip Bearer/mcp_token auth. Each
    must require an exact match or a "/"-bounded nested path.
    """
    from examples.mcp_client_remote.server import _is_oauth_public_path

    assert not _is_oauth_public_path("/authorized_keys")
    assert not _is_oauth_public_path("/authorize-legacy")
    assert not _is_oauth_public_path("/tokens-export")
    assert not _is_oauth_public_path("/token_leak")
    assert not _is_oauth_public_path("/registered-hosts")
    assert not _is_oauth_public_path("/registration-bypass")
    assert not _is_oauth_public_path("/health-debug-internal")
    assert not _is_oauth_public_path("/healthcheck-secret")


def test_token_mode_no_auth(token_client):
    """Token mode rejects requests without auth."""
    resp = token_client.get("/")
    assert resp.status_code == 401


def test_token_mode_mcp_token_valid(token_client, valid_token):
    """Token mode accepts valid mcp_token query param."""
    resp = token_client.get(f"/?mcp_token={valid_token}")
    assert resp.status_code not in (401, 403)


def test_token_mode_mcp_token_invalid(token_client):
    """Token mode rejects invalid mcp_token query param."""
    resp = token_client.get("/?mcp_token=wrong")
    assert resp.status_code in (401, 403)


def test_token_mode_bearer_valid(token_client, valid_token):
    """Token mode accepts valid Bearer token."""
    resp = token_client.get("/", headers={"Authorization": f"Bearer {valid_token}"})
    assert resp.status_code not in (401, 403)


def test_token_mode_bearer_invalid(token_client):
    """Token mode rejects invalid Bearer token."""
    resp = token_client.get("/", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code in (401, 403)


def test_oauth_mode_bearer_passthrough(oauth_client):
    """Bearer token is passed through in oauth mode."""
    resp = oauth_client.get("/", headers={"Authorization": "Bearer some-token"})
    assert resp.status_code not in (401, 403)


def test_oauth_mode_rejects_mcp_token(oauth_client, valid_token):
    """mcp_token is rejected in oauth mode."""
    resp = oauth_client.get(f"/?mcp_token={valid_token}")
    assert resp.status_code == 401


def test_oauth_mode_no_auth(oauth_client):
    """Missing auth in oauth mode returns 401."""
    resp = oauth_client.get("/")
    assert resp.status_code == 401


def test_oauth_endpoints_public_without_token(token_client):
    """OAuth discovery endpoints must work without any auth."""
    resp = token_client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code not in (401, 403)


def test_openid_configuration_exposes_docker_admin_scope(token_client):
    """OAuth discovery metadata must advertise mcp:docker:admin so a
    connector can request/register it (ACCESS_PROFILES['full'] includes it)."""
    resp = token_client.get("/.well-known/openid-configuration")
    assert resp.status_code not in (401, 403)
    scopes = resp.json()["scopes_supported"]
    assert "mcp:docker:admin" in scopes


def test_oauth_authorization_server_metadata_exposes_docker_admin_scope():
    """Cover the actual FastMCP authorization-server metadata path: the public
    proxy forwards /.well-known/oauth-authorization-server to the internal
    FastMCP server, whose create_auth_routes()/build_metadata() advertises
    valid_scopes (= SUPPORTED_SCOPES). Both discovery surfaces must expose
    mcp:docker:admin."""
    from mcp.server.auth.routes import create_auth_routes
    from mcp.server.auth.settings import ClientRegistrationOptions
    from pydantic import AnyHttpUrl
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from examples.mcp_server.oauth_provider import (
        SUPPORTED_SCOPES,
        GatewayOAuthProvider,
    )

    provider = GatewayOAuthProvider()
    options = ClientRegistrationOptions(
        enabled=True,
        valid_scopes=SUPPORTED_SCOPES,
        default_scopes=list(SUPPORTED_SCOPES),
    )
    routes = create_auth_routes(
        provider=provider,
        issuer_url=AnyHttpUrl("https://gateway.example.com"),
        service_documentation_url=AnyHttpUrl("https://github.com/gpakoh/agent-ssh-gateway"),
        client_registration_options=options,
    )
    app = Starlette(routes=routes)
    resp = TestClient(app).get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    body = resp.json()
    assert "scopes_supported" in body
    assert "mcp:docker:admin" in body["scopes_supported"]


@pytest.fixture
def oauth_registration(monkeypatch, tmp_path):
    """The real production DCR surface.

    Drives auth_setup.setup() -- the exact composition root the deployed
    MCP OAuth server uses -- and mounts the SDK /register handler, so the
    stored client scopes reflect the production ClientRegistrationOptions
    (default_scopes). Store paths are redirected to tmp so the test never
    touches /var/lib state.
    """
    monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
    monkeypatch.setenv("MCP_TEST_TOKEN_STORE", str(tmp_path))
    monkeypatch.setenv("MCP_TOKENS_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_TOKEN_STORE_FILE", str(tmp_path / "mcp_tokens.json"))
    monkeypatch.setenv("MCP_CLIENT_STORE_FILE", str(tmp_path / "mcp_clients.json"))
    monkeypatch.delenv("MCP_HEALTHCHECK_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("MCP_EXTRA_TOKENS_JSON", raising=False)

    from mcp.server.auth.routes import create_auth_routes
    from pydantic import AnyHttpUrl
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from examples.mcp_server.mcp_infra import auth_setup

    settings, provider, _ = auth_setup.setup()
    options = settings.client_registration_options
    routes = create_auth_routes(
        provider=provider,
        issuer_url=AnyHttpUrl(settings.issuer_url),
        service_documentation_url=AnyHttpUrl(settings.service_documentation_url),
        client_registration_options=options,
    )
    client = TestClient(Starlette(routes=routes))
    yield provider, client, list(options.default_scopes)


def test_dcr_register_without_scope_defaults_to_full_scopes(oauth_registration):
    """A registration that omits the optional scope field must resolve to
    DEFAULT_SCOPES (= SUPPORTED_SCOPES) so connector clients like ChatGPT
    get full access to all tools including Gitea Actions (gitea_get_action_run).

    The provider's own _parse_scopes() contract says exactly this; the
    defect was that the SDK RegistrationHandler substituted default_scopes
    (= SUPPORTED_SCOPES) before the provider could apply its safe default.
    This drives the real auth_setup.setup() wiring, so a revert of the
    default_scopes change fails this test.
    """
    provider, client, default_scopes = oauth_registration
    resp = client.post(
        "/register",
        json={
            "redirect_uris": ["http://localhost:9999/callback"],
            "client_name": "no-scope-repro",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    client_id = body["client_id"]
    stored = provider._clients[client_id].scopes
    assert stored == list(default_scopes), (
        f"DCR without scope registered {stored}; expected DEFAULT_SCOPES {default_scopes}"
    )
    assert "mcp:repo" in stored


def test_dcr_explicit_admin_scope_still_registrable(oauth_registration):
    """Corrective must not forbid explicit privileged registration: a client
    that deliberately asks for mcp:admin keeps working through the existing
    consent policy. Only the *omitted* scope must default safe."""
    provider, client, _ = oauth_registration
    resp = client.post(
        "/register",
        json={
            "redirect_uris": ["http://localhost:9999/callback"],
            "client_name": "explicit-admin-repro",
            "scope": "mcp:read mcp:project mcp:admin",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    stored = provider._clients[body["client_id"]].scopes
    assert "mcp:admin" in stored


def test_dcr_empty_and_unknown_scope_fail_closed(oauth_registration):
    """scope='' resolves to DEFAULT_SCOPES, whitespace-only to the empty set
    (both fail-safe -- never widened beyond DEFAULT_SCOPES), and an unknown
    scope is rejected outright. None of these may widen grants beyond what
    DEFAULT_SCOPES allows."""
    provider, client, default_scopes = oauth_registration
    for scope in ("", "   "):
        resp = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost:9999/callback"],
                "client_name": "empty-scope-repro",
                "scope": scope,
            },
        )
        assert resp.status_code == 201
        stored = provider._clients[resp.json()["client_id"]].scopes
        assert set(stored) <= set(default_scopes), f"scope {scope!r} widened grants to {stored}"
    empty_stored = provider._clients[
        client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost:9999/callback"],
                "client_name": "empty-scope-repro-2",
                "scope": "",
            },
        ).json()["client_id"]
    ].scopes
    assert empty_stored == list(default_scopes)

    bad = client.post(
        "/register",
        json={
            "redirect_uris": ["http://localhost:9999/callback"],
            "client_name": "bogus-scope-repro",
            "scope": "mcp:bogus",
        },
    )
    assert bad.status_code == 400
