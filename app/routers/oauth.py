"""OAuth2/SSO routes — authorize redirect + callback.

Both endpoints are intentionally public (browser flow): the authorize
endpoint redirects the user to the provider, and the callback receives
the provider's redirect with a code. After a successful exchange a
gateway JWT (same sub/type model as local login) is issued.
"""

import hashlib
import logging
import secrets

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import settings
from app.oauth_sso import (
    OAuthConfigError,
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
from app.rbac import VALID_ROLE_NAMES
from app.user_auth import create_jwt, set_auth_cookie

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


def _require_oauth_configured() -> None:
    if not is_oauth_enabled():
        raise HTTPException(
            status_code=503,
            detail={
                "message": "OAuth/SSO is not configured",
                "code": "OAUTH_NOT_CONFIGURED",
                "retryable": False,
                "hint": "Set OAUTH_PROVIDER, OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET",
                "http_status": 503,
            },
        )


def _redirect_uri(request: Request) -> str:
    if settings.oauth_redirect_uri.strip():
        return settings.oauth_redirect_uri.strip()
    return str(request.url_for("oauth_callback"))


@router.get("/api/auth/oauth/config")
async def oauth_config():
    """Public config for the Web UI: whether SSO is enabled and which provider."""
    if not is_oauth_enabled():
        return {"enabled": False, "provider": ""}
    return {"enabled": True, "provider": settings.oauth_provider.strip().lower()}


@router.get("/api/auth/oauth/authorize")
async def oauth_authorize(request: Request, provider: str = ""):
    """Step 1 — build the provider authorize URL and redirect the browser."""
    _require_oauth_configured()
    try:
        name = normalize_provider_name(provider or settings.oauth_provider)
        if name == "oidc":
            async with httpx.AsyncClient(timeout=10.0) as client:
                provider_cfg = await discover_oidc(client, settings.oauth_issuer_url)
        else:
            provider_cfg = get_provider_config(name)
    except OAuthConfigError as exc:
        logger.warning("OAuth authorize failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="OAuth provider configuration unavailable",
        ) from exc

    state = secrets.token_urlsafe(24)
    code_verifier = generate_code_verifier()
    state_store.put(state, PendingAuth(provider=name, code_verifier=code_verifier))

    redirect_uri = _redirect_uri(request)
    url = build_authorize_url(provider_cfg, state, generate_code_challenge(code_verifier), redirect_uri)
    logger.info("OAuth authorize: provider=%s state_len=%d", name, len(state))
    return RedirectResponse(url, status_code=302)


@router.get("/api/auth/oauth/callback")
async def oauth_callback(request: Request, code: str = "", state: str = ""):
    """Step 2 — exchange the code, fetch userinfo, issue a gateway JWT."""
    _require_oauth_configured()
    if not code:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "OAuth callback is missing the authorization code",
                "code": "OAUTH_MISSING_CODE",
                "retryable": False,
                "http_status": 400,
            },
        )
    pending = state_store.take(state)
    if pending is None:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "OAuth state is missing, expired, or already used",
                "code": "OAUTH_INVALID_STATE",
                "retryable": False,
                "hint": "Restart the login flow from the Web UI",
                "http_status": 400,
            },
        )

    try:
        if pending.provider == "oidc":
            async with httpx.AsyncClient(timeout=10.0) as client:
                provider_cfg = await discover_oidc(client, settings.oauth_issuer_url)
                access_token = await exchange_code(
                    client, provider_cfg, code, pending.code_verifier, _redirect_uri(request)
                )
                userinfo = await fetch_userinfo(client, provider_cfg, access_token)
        else:
            provider_cfg = get_provider_config(pending.provider)
            async with httpx.AsyncClient(timeout=10.0) as client:
                access_token = await exchange_code(
                    client, provider_cfg, code, pending.code_verifier, _redirect_uri(request)
                )
                userinfo = await fetch_userinfo(client, provider_cfg, access_token)
    except OAuthConfigError as exc:
        logger.warning("OAuth callback failed for provider %s: %s", pending.provider, exc)
        raise HTTPException(
            status_code=502,
            detail="OAuth token exchange failed",
        ) from exc

    if not is_email_allowed(userinfo.email):
        logger.warning("OAuth email not allowed: %s", userinfo.email)
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Your OAuth email is not allowed to sign in",
                "code": "OAUTH_EMAIL_NOT_ALLOWED",
                "retryable": False,
                "http_status": 403,
            },
        )

    # Namespace the hash with the provider name: two different providers can
    # both hand out the same raw `sub`/`id` value (small/sequential numeric
    # ids are common), and without the provider prefix those would collide
    # into the same gateway uid — letting one user impersonate another.
    identity_key = f"{pending.provider}:{userinfo.subject}"
    uid = int.from_bytes(
        hashlib.sha256(identity_key.encode("utf-8")).digest()[:4],
        "big",
    )
    # SSO is inherently multi-user (anyone matching OAUTH_ALLOWED_EMAILS) —
    # unlike local register(), which is fail-safe by being single-admin-only,
    # every SSO login must get an explicit, deployer-configured role rather
    # than falling back to create_jwt's admin default.
    oauth_role = settings.oauth_default_role if settings.oauth_default_role in VALID_ROLE_NAMES else "operator"
    if settings.oauth_default_role not in VALID_ROLE_NAMES:
        logger.warning(
            "Invalid OAUTH_DEFAULT_ROLE=%r, falling back to 'operator'",
            settings.oauth_default_role,
        )
    token = create_jwt(username=userinfo.username, user_id=uid, role=oauth_role)
    logger.info(
        "OAuth login ok: provider=%s username=%s",
        pending.provider,
        userinfo.username,
    )

    # Browser flow: httpOnly cookie carries the JWT; the page then redirects.
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        response = JSONResponse(
            {"token": token, "username": userinfo.username, "provider": pending.provider}
        )
        set_auth_cookie(response, token)
        return response

    response = HTMLResponse(
        content=(
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<title>Sign in complete</title></head><body>"
            "<p>Sign in complete. Redirecting…</p>"
            "<script>location.href = '/';</script></body></html>"
        ),
        status_code=200,
    )
    set_auth_cookie(response, token)
    return response
