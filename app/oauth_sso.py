"""OAuth2/OIDC SSO client for the Web UI.

Supports built-in providers (GitHub, GitLab, Google) plus generic OIDC
providers via discovery of ``/.well-known/openid-configuration``.

Flow: authorize → (user grants at provider) → callback with code →
token exchange → userinfo → gateway JWT (same sub/type model as local login).
"""

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

PKCE_CODE_VERIFIER_BYTES = 32
STATE_TTL_SECONDS = 600
DEFAULT_OIDC_SCOPES = "openid email profile"


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Static endpoints and claim mapping for one OAuth provider."""

    name: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scopes: str
    username_field: str
    email_field: str


BUILTIN_PROVIDERS: dict[str, OAuthProviderConfig] = {
    "github": OAuthProviderConfig(
        name="github",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        userinfo_url="https://api.github.com/user",
        scopes="read:user user:email",
        username_field="login",
        email_field="email",
    ),
    "gitlab": OAuthProviderConfig(
        name="gitlab",
        authorize_url="https://gitlab.com/oauth/authorize",
        token_url="https://gitlab.com/oauth/token",
        userinfo_url="https://gitlab.com/api/v4/user",
        scopes="read_user",
        username_field="username",
        email_field="email",
    ),
    "google": OAuthProviderConfig(
        name="google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
        scopes="openid email profile",
        username_field="email",
        email_field="email",
    ),
}


class OAuthConfigError(RuntimeError):
    """OAuth is not configured or is misconfigured."""


@dataclass
class PendingAuth:
    """Server-side state kept between authorize and callback."""

    provider: str
    code_verifier: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class OAuthUserInfo:
    """Normalised identity returned by a provider's userinfo endpoint."""

    username: str
    email: str
    subject: str


class OAuthStateStore:
    """In-memory store of pending OAuth authorizations (state → PendingAuth)."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingAuth] = {}

    def put(self, state: str, pending: PendingAuth) -> None:
        self._pending[state] = pending

    def take(self, state: str) -> PendingAuth | None:
        pending = self._pending.pop(state, None)
        if pending is None:
            return None
        if datetime.now(UTC) - pending.created_at > timedelta(seconds=STATE_TTL_SECONDS):
            logger.warning("OAuth state expired for provider %s", pending.provider)
            return None
        return pending

    def clear(self) -> None:
        self._pending.clear()

    def __len__(self) -> int:
        return len(self._pending)


state_store = OAuthStateStore()


def generate_code_verifier() -> str:
    """RFC 7636 S256 PKCE code verifier (43–128 ASCII chars)."""
    token = secrets.token_urlsafe(PKCE_CODE_VERIFIER_BYTES)
    return token[:128]


def generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def normalize_provider_name(name: str) -> str:
    """Resolve the requested provider name against config + builtins.

    provider=oidc requires OAUTH_ISSUER_URL; any other built-in name is
    taken from the built-in registry. Returns the canonical provider name.
    """
    provider = (name or "").strip().lower()
    if not provider:
        raise OAuthConfigError("OAuth is not configured: OAUTH_PROVIDER is empty")
    if provider not in BUILTIN_PROVIDERS and provider != "oidc":
        raise OAuthConfigError(f"Unsupported OAuth provider: {provider!r}")
    if provider == "oidc" and not settings.oauth_issuer_url:
        raise OAuthConfigError("OIDC provider requires OAUTH_ISSUER_URL")
    return provider


def is_oauth_enabled() -> bool:
    if not settings.oauth_provider.strip():
        return False
    if not settings.oauth_client_id:
        return False
    if not settings.oauth_client_secret:
        return False
    return True


async def discover_oidc(client: httpx.AsyncClient, issuer_url: str) -> OAuthProviderConfig:
    """Fetch and parse a provider's OIDC discovery document."""
    url = issuer_url.rstrip("/") + "/.well-known/openid-configuration"
    logger.info("OIDC discovery from %s", url)
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise OAuthConfigError(f"OIDC discovery failed for {issuer_url}: {exc}") from exc
    doc = resp.json()
    required = ("authorization_endpoint", "token_endpoint", "userinfo_endpoint")
    missing = [k for k in required if not doc.get(k)]
    if missing:
        raise OAuthConfigError(
            f"OIDC discovery document for {issuer_url} is missing: {', '.join(missing)}"
        )
    scopes = doc.get("scopes_supported") or [s for s in DEFAULT_OIDC_SCOPES.split() if s]
    return OAuthProviderConfig(
        name="oidc",
        authorize_url=doc["authorization_endpoint"],
        token_url=doc["token_endpoint"],
        userinfo_url=doc["userinfo_endpoint"],
        scopes=" ".join(scopes),
        username_field="preferred_username",
        email_field="email",
    )


def get_provider_config(provider: str) -> OAuthProviderConfig:
    """Static config for a built-in provider (authorize/token/userinfo URLs)."""
    if provider in BUILTIN_PROVIDERS:
        return BUILTIN_PROVIDERS[provider]
    raise OAuthConfigError(f"Unsupported OAuth provider: {provider!r}")


def build_authorize_url(
    provider_cfg: OAuthProviderConfig,
    state: str,
    code_challenge: str,
    redirect_uri: str,
) -> str:
    """Build the provider authorization URL (authorization code + PKCE)."""
    params = {
        "client_id": settings.oauth_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": provider_cfg.scopes,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    from urllib.parse import urlencode

    sep = "&" if "?" in provider_cfg.authorize_url else "?"
    return provider_cfg.authorize_url + sep + urlencode(params)


async def exchange_code(
    client: httpx.AsyncClient,
    provider_cfg: OAuthProviderConfig,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> str:
    """Exchange the authorization code for an access token."""
    data = {
        "client_id": settings.oauth_client_id,
        "client_secret": settings.oauth_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    headers = {"Accept": "application/json"}
    try:
        resp = await client.post(provider_cfg.token_url, data=data, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise OAuthConfigError(f"Token exchange failed for {provider_cfg.name}: {exc}") from exc
    body = resp.json()
    token = body.get("access_token")
    if not token:
        error = body.get("error", "unknown_error")
        description = body.get("error_description", "no description")
        raise OAuthConfigError(
            f"Token exchange returned no access_token "
            f"(error={error!r}, {description})"
        )
    return token


async def fetch_userinfo(
    client: httpx.AsyncClient,
    provider_cfg: OAuthProviderConfig,
    access_token: str,
) -> OAuthUserInfo:
    """Fetch the userinfo document and normalise it into OAuthUserInfo."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    try:
        resp = await client.get(provider_cfg.userinfo_url, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise OAuthConfigError(f"userinfo failed for {provider_cfg.name}: {exc}") from exc
    data: dict[str, Any] = resp.json()
    username = data.get(provider_cfg.username_field) or data.get("email") or data.get("sub")
    email = data.get(provider_cfg.email_field) or ""
    subject = str(data.get("sub") or data.get("id") or username or "")
    if not username:
        raise OAuthConfigError(f"userinfo for {provider_cfg.name} has no username field")
    return OAuthUserInfo(username=str(username), email=str(email), subject=subject)


def _split_allowlist(raw: str) -> set[str]:
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def is_email_allowed(email: str) -> bool:
    """Enforce OAUTH_ALLOWED_EMAILS allowlist (empty = allow all)."""
    allowlist = _split_allowlist(settings.oauth_allowed_emails)
    if not allowlist:
        return True
    return (email or "").strip().lower() in allowlist
