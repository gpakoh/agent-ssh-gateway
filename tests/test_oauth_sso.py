"""Tests for OAuth2/SSO: provider abstraction, PKCE, authorize/callback flow."""

import os

os.environ.setdefault("AUTH_DB_PATH", "/tmp/test_oauth_sso.sqlite3")
os.environ.setdefault("JWT_SECRET", "test-secret-for-oauth-sso-tests")
os.environ.setdefault("API_KEY", "test-key-oauth")
os.environ.setdefault("API_AUTH_ENABLED", "true")
os.environ.setdefault("ALLOWED_CLIENT_CIDRS", "0.0.0.0/0,::1/128")
os.environ.setdefault("TRUSTED_PROXY_CIDRS", "127.0.0.1/32")

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import oauth_sso
from app.config import settings
from app.main import app
from app.oauth_sso import (
    BUILTIN_PROVIDERS,
    OAuthConfigError,
    OAuthProviderConfig,
    OAuthUserInfo,
    PendingAuth,
    build_authorize_url,
    discover_oidc,
    exchange_code,
    fetch_userinfo,
    generate_code_challenge,
    generate_code_verifier,
    get_provider_config,
    is_email_allowed,
    is_oauth_enabled,
    normalize_provider_name,
    state_store,
)
from app.user_auth import init_auth_db, verify_jwt

DB_PATH = os.environ["AUTH_DB_PATH"]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Isolate settings and state between tests."""
    monkeypatch.setattr(oauth_sso.state_store, "_pending", {})
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    asyncio.run(init_auth_db())
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "test-key-oauth")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(settings, "oauth_provider", "github")
    monkeypatch.setattr(settings, "oauth_client_id", "client-id-test")
    monkeypatch.setattr(settings, "oauth_client_secret", "client-secret-test")
    monkeypatch.setattr(settings, "oauth_issuer_url", "https://idp.example.com")
    monkeypatch.setattr(settings, "oauth_redirect_uri", "https://gw.example.com/api/auth/oauth/callback")
    monkeypatch.setattr(settings, "oauth_allowed_emails", "")
    yield


class TestPkce:
    def test_code_verifier_valid_rfc7636(self):
        verifier = generate_code_verifier()
        assert 43 <= len(verifier) <= 128
        assert verifier.isascii()

    def test_code_challenge_stable_and_s256(self):
        verifier = "a" * 64
        assert generate_code_challenge(verifier) == generate_code_challenge(verifier)
        assert len(generate_code_challenge(verifier)) == 43


class TestProviderRegistry:
    def test_builtin_providers_present(self):
        assert set(BUILTIN_PROVIDERS) == {"github", "gitlab", "google"}
        gh = BUILTIN_PROVIDERS["github"]
        assert "github.com/login/oauth" in gh.authorize_url
        assert gh.username_field == "login"

    def test_get_provider_config_unknown_raises(self):
        with pytest.raises(OAuthConfigError):
            get_provider_config("myspace")

    def test_normalize_provider_oidc_requires_issuer(self, monkeypatch):
        monkeypatch.setattr(settings, "oauth_issuer_url", "")
        with pytest.raises(OAuthConfigError):
            normalize_provider_name("oidc")

    def test_normalize_unknown_provider_raises(self):
        with pytest.raises(OAuthConfigError):
            normalize_provider_name("not-a-provider")

    def test_is_oauth_enabled_requires_full_config(self, monkeypatch):
        assert is_oauth_enabled() is True
        monkeypatch.setattr(settings, "oauth_client_secret", "")
        assert is_oauth_enabled() is False


class TestAuthorizeUrl:
    def test_build_authorize_url_contains_all_params(self):
        url = build_authorize_url(
            BUILTIN_PROVIDERS["github"],
            state="st-123",
            code_challenge="challenge-abc",
            redirect_uri="https://gw.example.com/cb",
        )
        assert "client_id=client-id-test" in url
        assert "redirect_uri=https%3A%2F%2Fgw.example.com%2Fcb" in url
        assert "response_type=code" in url
        assert "state=st-123" in url
        assert "code_challenge=challenge-abc" in url
        assert "code_challenge_method=S256" in url
        assert url.startswith("https://github.com/login/oauth/authorize")


class TestStateStore:
    def test_take_roundtrip(self):
        state_store.put("s1", PendingAuth(provider="github", code_verifier="verifier"))
        pending = state_store.take("s1")
        assert pending is not None
        assert pending.provider == "github"
        assert pending.code_verifier == "verifier"

    def test_take_unknown_state_returns_none(self):
        assert state_store.take("missing") is None

    def test_take_is_single_use(self):
        state_store.put("s2", PendingAuth(provider="github", code_verifier="v"))
        assert state_store.take("s2") is not None
        assert state_store.take("s2") is None

    def test_take_expired_state_returns_none(self):
        old = datetime.now(UTC) - timedelta(seconds=3600)
        state_store.put("s3", PendingAuth(provider="github", code_verifier="v"))
        state_store._pending["s3"].created_at = old
        assert state_store.take("s3") is None


class TestDiscovery:
    async def test_discover_oidc_builds_config(self, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "authorization_endpoint": "https://idp.example.com/auth",
                    "token_endpoint": "https://idp.example.com/token",
                    "userinfo_endpoint": "https://idp.example.com/userinfo",
                    "scopes_supported": ["openid", "email", "profile"],
                }

        class FakeClient:
            def __init__(self):
                self.last_url = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, **kwargs):
                self.last_url = url
                return FakeResponse()

        fake = FakeClient()
        cfg = await discover_oidc(fake, "https://idp.example.com")
        assert cfg.name == "oidc"
        assert cfg.token_url == "https://idp.example.com/token"
        assert cfg.username_field == "preferred_username"
        assert fake.last_url == "https://idp.example.com/.well-known/openid-configuration"

    async def test_discover_oidc_missing_endpoints_raises(self):
        class BadResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {}

        class BadClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, **kwargs):
                return BadResponse()

        with pytest.raises(OAuthConfigError):
            await discover_oidc(BadClient(), "https://idp.example.com")


class TestExchangeAndUserinfo:
    async def test_exchange_code_returns_token(self):
        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"access_token": "tok-123"}

        calls = {}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, **kwargs):
                calls["url"] = url
                calls["data"] = kwargs.get("data", {})
                return Resp()

        client = Client()
        token = await exchange_code(
            client, BUILTIN_PROVIDERS["github"], "code-x", "verifier", "https://gw.example.com/cb"
        )
        assert token == "tok-123"
        assert calls["data"]["grant_type"] == "authorization_code"
        assert calls["data"]["code"] == "code-x"
        assert calls["data"]["code_verifier"] == "verifier"
        assert "client_secret" in calls["data"]

    async def test_exchange_code_no_token_raises(self):
        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"error": "invalid_grant", "error_description": "bad code"}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, **kwargs):
                return Resp()

        with pytest.raises(OAuthConfigError):
            await exchange_code(
                Client(), BUILTIN_PROVIDERS["github"], "code-x", "v", "https://gw.example.com/cb"
            )

    async def test_fetch_userinfo_normalises_github(self):
        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"login": "octocat", "id": 42, "email": "octo@example.com"}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, **kwargs):
                assert kwargs["headers"]["Authorization"] == "Bearer tok"
                return Resp()

        info = await fetch_userinfo(Client(), BUILTIN_PROVIDERS["github"], "tok")
        assert info.username == "octocat"
        assert info.email == "octo@example.com"
        assert info.subject == "42"

    async def test_fetch_userinfo_normalises_google_email(self):
        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"sub": "12345", "email": "user@gmail.com"}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, **kwargs):
                return Resp()

        info = await fetch_userinfo(Client(), BUILTIN_PROVIDERS["google"], "tok")
        assert info.username == "user@gmail.com"
        assert info.subject == "12345"

    async def test_fetch_userinfo_missing_username_raises(self):
        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"sub": ""}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, **kwargs):
                return Resp()

        with pytest.raises(OAuthConfigError):
            await fetch_userinfo(Client(), BUILTIN_PROVIDERS["github"], "tok")


class TestEmailAllowlist:
    def test_empty_allowlist_denies_all(self):
        assert is_email_allowed("anyone@example.com") is False

    def test_allowlist_allows_member(self, monkeypatch):
        monkeypatch.setattr(settings, "oauth_allowed_emails", "team@example.com, dev@example.com")
        assert is_email_allowed("DEV@example.com") is True

    def test_allowlist_blocks_other(self, monkeypatch):
        monkeypatch.setattr(settings, "oauth_allowed_emails", "team@example.com")
        assert is_email_allowed("attacker@evil.com") is False


class TestSsoEndpoints:
    def test_authorize_redirects_to_provider(self, monkeypatch):
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/authorize?provider=github",
                headers={"X-API-Key": "test-key-oauth"},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        loc = resp.headers["location"]
        assert "github.com/login/oauth/authorize" in loc
        assert "code_challenge_method=S256" in loc
        assert "state=" in loc

    def test_authorize_rejects_unknown_provider(self):
        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/authorize?provider=myspace",
                headers={"X-API-Key": "test-key-oauth"},
            )
        assert resp.status_code == 503

    def test_authorize_unconfigured_returns_503(self, monkeypatch):
        monkeypatch.setattr(settings, "oauth_provider", "")
        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/authorize",
                headers={"X-API-Key": "test-key-oauth"},
            )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "OAUTH_NOT_CONFIGURED"

    def test_callback_requires_code(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/oauth/callback", headers={"X-API-Key": "test-key-oauth"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "OAUTH_MISSING_CODE"

    def test_callback_invalid_state(self):
        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/callback?code=x&state=nope",
                headers={"X-API-Key": "test-key-oauth"},
            )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "OAUTH_INVALID_STATE"

    def test_callback_full_flow_issues_gateway_jwt(self, monkeypatch):
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "oauth_allowed_emails", "octo@example.com")

        async def fake_exchange(client, cfg, code, verifier, redirect_uri):
            return "access-tok"

        async def fake_userinfo(client, cfg, token):
            return OAuthUserInfo(username="octocat", email="octo@example.com", subject="42")

        monkeypatch.setattr("app.routers.oauth.exchange_code", fake_exchange)
        monkeypatch.setattr("app.routers.oauth.fetch_userinfo", fake_userinfo)

        state = "st-full-flow"
        state_store.put(state, PendingAuth(provider="github", code_verifier="verifier"))

        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/callback?code=abc&state=" + state,
                headers={"X-API-Key": "test-key-oauth", "Accept": "application/json"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "octocat"
        assert data["provider"] == "github"
        payload = verify_jwt(data["token"])
        assert payload is not None
        assert payload["sub"] == "octocat"
        assert payload["type"] == "web-ui"

    def test_callback_defaults_to_operator_role_not_admin(self, monkeypatch):
        """Regression: SSO logins used to silently default to create_jwt's
        admin default because the callback never passed role= explicitly.
        SSO is multi-user by construction (anyone matching
        OAUTH_ALLOWED_EMAILS) — a safe non-admin default is required.
        """
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "oauth_allowed_emails", "octo@example.com")

        async def fake_exchange(client, cfg, code, verifier, redirect_uri):
            return "access-tok"

        async def fake_userinfo(client, cfg, token):
            return OAuthUserInfo(username="octocat", email="octo@example.com", subject="42")

        monkeypatch.setattr("app.routers.oauth.exchange_code", fake_exchange)
        monkeypatch.setattr("app.routers.oauth.fetch_userinfo", fake_userinfo)

        state = "st-role-default"
        state_store.put(state, PendingAuth(provider="github", code_verifier="verifier"))

        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/callback?code=abc&state=" + state,
                headers={"X-API-Key": "test-key-oauth", "Accept": "application/json"},
            )
        assert resp.status_code == 200
        payload = verify_jwt(resp.json()["token"])
        assert payload is not None
        assert payload["role"] == "operator"
        assert payload["role"] != "admin"

    def test_callback_honors_configured_oauth_default_role(self, monkeypatch):
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "oauth_allowed_emails", "octo@example.com")
        monkeypatch.setattr(settings, "oauth_default_role", "viewer")

        async def fake_exchange(client, cfg, code, verifier, redirect_uri):
            return "access-tok"

        async def fake_userinfo(client, cfg, token):
            return OAuthUserInfo(username="octocat", email="octo@example.com", subject="42")

        monkeypatch.setattr("app.routers.oauth.exchange_code", fake_exchange)
        monkeypatch.setattr("app.routers.oauth.fetch_userinfo", fake_userinfo)

        state = "st-role-viewer"
        state_store.put(state, PendingAuth(provider="github", code_verifier="verifier"))

        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/callback?code=abc&state=" + state,
                headers={"X-API-Key": "test-key-oauth", "Accept": "application/json"},
            )
        assert resp.status_code == 200
        payload = verify_jwt(resp.json()["token"])
        assert payload is not None
        assert payload["role"] == "viewer"

    def test_callback_invalid_oauth_default_role_falls_back_safely(self, monkeypatch):
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "oauth_allowed_emails", "octo@example.com")
        monkeypatch.setattr(settings, "oauth_default_role", "super-root-god-mode")

        async def fake_exchange(client, cfg, code, verifier, redirect_uri):
            return "access-tok"

        async def fake_userinfo(client, cfg, token):
            return OAuthUserInfo(username="octocat", email="octo@example.com", subject="42")

        monkeypatch.setattr("app.routers.oauth.exchange_code", fake_exchange)
        monkeypatch.setattr("app.routers.oauth.fetch_userinfo", fake_userinfo)

        state = "st-role-invalid"
        state_store.put(state, PendingAuth(provider="github", code_verifier="verifier"))

        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/callback?code=abc&state=" + state,
                headers={"X-API-Key": "test-key-oauth", "Accept": "application/json"},
            )
        assert resp.status_code == 200
        payload = verify_jwt(resp.json()["token"])
        assert payload is not None
        assert payload["role"] == "operator"

    def test_callback_uid_namespaced_by_provider_no_cross_provider_collision(self, monkeypatch):
        """Regression: the gateway uid used to be sha256(subject) alone, so
        two different providers handing out the same raw sub/id (common for
        small/sequential numeric ids) collided into the identical uid,
        letting one real user be treated as another.
        """
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        monkeypatch.setattr(settings, "oauth_allowed_emails", "same-subject@example.com")

        async def fake_exchange(client, cfg, code, verifier, redirect_uri):
            return "access-tok"

        async def fake_userinfo_github(client, cfg, token):
            return OAuthUserInfo(
                username="github-user", email="same-subject@example.com", subject="12345"
            )

        async def fake_userinfo_gitlab(client, cfg, token):
            return OAuthUserInfo(
                username="gitlab-user", email="same-subject@example.com", subject="12345"
            )

        monkeypatch.setattr("app.routers.oauth.exchange_code", fake_exchange)

        state1 = "st-collision-github"
        state_store.put(state1, PendingAuth(provider="github", code_verifier="verifier"))
        monkeypatch.setattr("app.routers.oauth.fetch_userinfo", fake_userinfo_github)
        with TestClient(app) as client:
            resp1 = client.get(
                "/api/auth/oauth/callback?code=abc&state=" + state1,
                headers={"X-API-Key": "test-key-oauth", "Accept": "application/json"},
            )
        assert resp1.status_code == 200
        uid1 = verify_jwt(resp1.json()["token"])["uid"]

        state2 = "st-collision-gitlab"
        state_store.put(state2, PendingAuth(provider="gitlab", code_verifier="verifier"))
        monkeypatch.setattr("app.routers.oauth.fetch_userinfo", fake_userinfo_gitlab)
        with TestClient(app) as client:
            resp2 = client.get(
                "/api/auth/oauth/callback?code=abc&state=" + state2,
                headers={"X-API-Key": "test-key-oauth", "Accept": "application/json"},
            )
        assert resp2.status_code == 200
        uid2 = verify_jwt(resp2.json()["token"])["uid"]

        assert uid1 != uid2

    def test_callback_browser_flow_returns_html(self, monkeypatch):
        monkeypatch.setattr(settings, "oauth_allowed_emails", "octo@example.com")

        async def fake_exchange(client, cfg, code, verifier, redirect_uri):
            return "access-tok"

        async def fake_userinfo(client, cfg, token):
            return OAuthUserInfo(username="octocat", email="octo@example.com", subject="42")

        monkeypatch.setattr("app.routers.oauth.exchange_code", fake_exchange)
        monkeypatch.setattr("app.routers.oauth.fetch_userinfo", fake_userinfo)

        state = "st-html"
        state_store.put(state, PendingAuth(provider="github", code_verifier="verifier"))

        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/callback?code=abc&state=" + state,
                headers={"X-API-Key": "test-key-oauth"},
            )
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "location.href = '/'" in resp.text
        # T79.18: the JWT is delivered via an httpOnly cookie, not localStorage.
        set_cookie = resp.headers.get("set-cookie", "")
        assert "auth_token=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=strict" in set_cookie

    def test_callback_email_not_allowed(self, monkeypatch):
        monkeypatch.setattr(settings, "oauth_allowed_emails", "team@example.com")

        async def fake_exchange(client, cfg, code, verifier, redirect_uri):
            return "access-tok"

        async def fake_userinfo(client, cfg, token):
            return OAuthUserInfo(username="octocat", email="attacker@evil.com", subject="42")

        monkeypatch.setattr("app.routers.oauth.exchange_code", fake_exchange)
        monkeypatch.setattr("app.routers.oauth.fetch_userinfo", fake_userinfo)

        state = "st-denied"
        state_store.put(state, PendingAuth(provider="github", code_verifier="verifier"))

        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/callback?code=abc&state=" + state,
                headers={"X-API-Key": "test-key-oauth"},
            )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "OAUTH_EMAIL_NOT_ALLOWED"

    def test_callback_email_denied_when_allowlist_empty(self, monkeypatch):
        """Fail-closed: SSO is unusable until OAUTH_ALLOWED_EMAILS is set."""
        monkeypatch.setattr(settings, "oauth_allowed_emails", "")

        async def fake_exchange(client, cfg, code, verifier, redirect_uri):
            return "access-tok"

        async def fake_userinfo(client, cfg, token):
            return OAuthUserInfo(username="octocat", email="octo@example.com", subject="42")

        monkeypatch.setattr("app.routers.oauth.exchange_code", fake_exchange)
        monkeypatch.setattr("app.routers.oauth.fetch_userinfo", fake_userinfo)

        state = "st-failclosed"
        state_store.put(state, PendingAuth(provider="github", code_verifier="verifier"))

        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/callback?code=abc&state=" + state,
                headers={"X-API-Key": "test-key-oauth", "Accept": "application/json"},
            )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "OAUTH_EMAIL_NOT_ALLOWED"

    def test_callback_oidc_provider_flow(self, monkeypatch):
        monkeypatch.setattr(settings, "oauth_provider", "oidc")
        monkeypatch.setattr(settings, "oauth_issuer_url", "https://idp.example.com")
        monkeypatch.setattr(settings, "oauth_allowed_emails", "alice@corp.example.com")

        async def fake_discover(client, issuer):
            return OAuthProviderConfig(
                name="oidc",
                authorize_url="https://idp.example.com/auth",
                token_url="https://idp.example.com/token",
                userinfo_url="https://idp.example.com/userinfo",
                scopes="openid email profile",
                username_field="preferred_username",
                email_field="email",
            )

        async def fake_exchange(client, cfg, code, verifier, redirect_uri):
            return "access-tok"

        async def fake_userinfo(client, cfg, token):
            return OAuthUserInfo(username="alice", email="alice@corp.example.com", subject="alice-1")

        monkeypatch.setattr("app.routers.oauth.discover_oidc", fake_discover)
        monkeypatch.setattr("app.routers.oauth.exchange_code", fake_exchange)
        monkeypatch.setattr("app.routers.oauth.fetch_userinfo", fake_userinfo)

        state = "st-oidc"
        state_store.put(state, PendingAuth(provider="oidc", code_verifier="verifier"))

        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/oauth/callback?code=abc&state=" + state,
                headers={"X-API-Key": "test-key-oauth", "Accept": "application/json"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "alice"
        payload = verify_jwt(data["token"])
        assert payload is not None
        assert payload["sub"] == "alice"

    def test_oauth_config_endpoint_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "oauth_provider", "")
        with TestClient(app) as client:
            resp = client.get("/api/auth/oauth/config", headers={"X-API-Key": "test-key-oauth"})
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "provider": ""}

    def test_oauth_config_endpoint_enabled(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/oauth/config", headers={"X-API-Key": "test-key-oauth"})
        assert resp.status_code == 200
        assert resp.json() == {"enabled": True, "provider": "github"}

    def test_oauth_endpoints_are_public_in_middleware(self):
        from app.auth_middleware import PUBLIC_AUTH_PATHS

        assert "/api/auth/oauth/authorize" in PUBLIC_AUTH_PATHS
        assert "/api/auth/oauth/callback" in PUBLIC_AUTH_PATHS
        assert "/api/auth/oauth/config" in PUBLIC_AUTH_PATHS

    def test_local_login_still_works_with_oauth_configured(self):
        """AC: local admin login works when OAuth is configured."""
        with TestClient(app) as client:
            reg = client.post(
                "/api/auth/register",
                json={
                    "username": "localadmin",
                    "password": "LocalPass123!",
                    "password_confirm": "LocalPass123!",
                    "setup_token": "test-setup-token-12345",
                },
            )
        assert reg.status_code == 201
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "localadmin", "password": "LocalPass123!"},
            )
        assert login.status_code == 200
        assert login.json()["username"] == "localadmin"
        assert login.cookies.get("auth_token")
