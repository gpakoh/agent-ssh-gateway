"""Auth diagnostic routes — whoami identity endpoint."""

from fastapi import APIRouter, Depends

from app.auth_middleware import AuthIdentity, require_scope

router = APIRouter(tags=["auth"])


@router.get("/api/auth/whoami")
async def whoami(
    identity: AuthIdentity = Depends(require_scope("auth:read")),
) -> dict:
    """Return the caller's identity, scopes, auth method, and credential ID.

    Scope: auth:read (master key bypasses scope checks).
    """
    credential_id = "ak_" + identity.fingerprint[:8]
    scopes_list = list(identity.scopes) if "*" not in identity.scopes else ["*"]
    return {
        "identity": identity.name or identity.token_type,
        "scopes": scopes_list,
        "auth_method": (
            "api_key"
            if identity.token_type in ("master", "agent")
            else identity.token_type
        ),
        "credential_id": credential_id,
    }
