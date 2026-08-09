"""Local auth for web UI — single admin registration + JWT login."""

import asyncio
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app import state as _state
from app.auth_middleware import verify_api_key
from app.config import settings
from app.security import rate_limit_mutation

# T79.17: precomputed bcrypt hash of a fixed dummy string, used to equalize
# login timing between existing and non-existing usernames.
_DUMMY_HASH: bytes = bcrypt.hashpw(b"dummy-timing-equalizer", bcrypt.gensalt())

# T79.18: web-ui JWT delivered via httpOnly cookie instead of localStorage.
AUTH_COOKIE_NAME = "auth_token"
AUTH_COOKIE_PATH = "/"

logger = logging.getLogger(__name__)

_register_lock = asyncio.Lock()

router = APIRouter(tags=["auth"])


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


_engine = None
_SessionLocal: async_sessionmaker[AsyncSession] | None = None


def get_auth_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _SessionLocal is None:
        raise RuntimeError("Auth database is not initialized")
    return _SessionLocal


async def init_auth_db():
    global _engine, _SessionLocal
    db_path = settings.auth_db_path
    logger.info("Initializing auth database at %s", db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _SessionLocal = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    logger.info("Auth database initialized")


async def get_db():
    SessionLocal = get_auth_sessionmaker()
    async with SessionLocal() as session:
        yield session


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(..., min_length=8, max_length=128)
    password_confirm: str = Field(..., min_length=8, max_length=128)
    setup_token: str = Field(default="", max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


def validate_password(password: str) -> tuple[bool, str]:
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r"[0-9]", password):
        return False, "Password must contain at least one digit"
    if not re.search(r"[!@#$%^&*(),.\"?{}|<>_\-+]", password):
        return False, "Password must contain at least one special character"
    return True, ""


def create_jwt(username: str, user_id: int, role: str = "admin") -> str:
    secret = settings.jwt_secret_required
    payload = {
        "sub": username,
        "uid": user_id,
        "role": role,
        "type": "web-ui",
        "exp": datetime.now(UTC) + timedelta(minutes=settings.jwt_expires_minutes),
        "iat": datetime.now(UTC),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_jwt(token: str) -> dict | None:
    secret = settings.jwt_secret_required
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        if payload.get("type") != "web-ui":
            return None
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def set_auth_cookie(response, token: str) -> None:
    """Attach the web-ui JWT as an httpOnly, SameSite=Strict cookie (T79.18).

    httpOnly keeps the token out of localStorage (XSS cannot read it);
    SameSite=Strict blocks cross-site sends (CSRF); the browser attaches the
    cookie to same-origin fetch/WebSocket automatically.
    """
    max_age = settings.jwt_expires_minutes * 60
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        path=AUTH_COOKIE_PATH,
        secure=settings.jwt_cookie_secure,
    )


def clear_auth_cookie(response) -> None:
    """Expire the web-ui auth cookie (logout)."""
    response.delete_cookie(key=AUTH_COOKIE_NAME, path=AUTH_COOKIE_PATH)


def auth_token_from_request(request: Request) -> str:
    """Read the web-ui JWT from the auth cookie, or "" when absent."""
    return request.cookies.get(AUTH_COOKIE_NAME, "")


@router.get("/api/auth/check")
async def auth_check_users(request: Request):
    token_store = getattr(_state, "agent_token_store", None)
    identity = await verify_api_key(
        request,
        settings.api_key,
        settings.agent_token,
        settings,
        token_store,
    )
    users_count = await _count_users()
    if identity is not None:
        return {
            "valid": True,
            "auth_mode": "api_key",
            "key_name": identity.name or "default",
            "users_count": users_count,
        }
    return JSONResponse(
        status_code=401,
        content={
            "message": "Invalid or missing API key",
            "code": "INVALID_API_KEY",
            "retryable": False,
            "hint": "Provide a valid X-API-Key header with your API key",
            "http_status": 401,
            "users_count": users_count,
        },
    )


async def _count_users() -> int:
    try:
        SessionLocal = get_auth_sessionmaker()
        async with SessionLocal() as session:
            result = await session.execute(select(func.count(User.id)))
            return result.scalar() or 0
    except Exception:
        return 0


@router.post("/api/auth/register", status_code=201)
@rate_limit_mutation(5, "minute")
async def register(request: Request, req: RegisterRequest):
    if req.password != req.password_confirm:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    valid, err = validate_password(req.password)
    if not valid:
        raise HTTPException(status_code=400, detail=err)

    async with _register_lock:
        SessionLocal = get_auth_sessionmaker()
        async with SessionLocal() as session:
            result = await session.execute(select(func.count(User.id)))
            count = result.scalar() or 0
            if count > 0:
                raise HTTPException(
                    status_code=403, detail="Registration disabled. An admin already exists."
                )

            if not settings.setup_token:
                raise HTTPException(
                    status_code=503,
                    detail="Registration disabled: SETUP_TOKEN is not configured on the server.",
                )
            if not secrets.compare_digest(req.setup_token, settings.setup_token):
                raise HTTPException(status_code=403, detail="Invalid or missing setup token")

            existing = await session.execute(select(User).where(User.username == req.username))
            if existing.scalar_one_or_none():
                raise HTTPException(status_code=409, detail="Username already taken")

            pw_hash = bcrypt.hashpw(req.password.encode("utf-8"), bcrypt.gensalt())
            user = User(username=req.username, password_hash=pw_hash.decode("utf-8"))
            session.add(user)
            await session.commit()
            await session.refresh(user)

            if user.id is None:
                raise RuntimeError("User ID was not assigned")
            if user.username is None:
                raise RuntimeError("Username was not assigned")

            token = create_jwt(username=user.username, user_id=user.id)
            # Audit finding: the token was returned here too, even though
            # the only real consumer (app/static/app.js) never reads it --
            # it relies entirely on the HttpOnly cookie set below. Echoing
            # the JWT into a JS-readable JSON response undermines the
            # whole point of HttpOnly (XSS can't read the cookie, but
            # could read this response body).
            response = JSONResponse(
                status_code=201,
                content={"username": user.username},
            )
            set_auth_cookie(response, token)
            return response


@router.post("/api/auth/login")
@rate_limit_mutation(10, "minute")
async def login(request: Request, req: LoginRequest):
    SessionLocal = get_auth_sessionmaker()
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.username == req.username))
        user = result.scalar_one_or_none()
        if user is None:
            # T79.17: burn the same bcrypt time as a real login so response
            # timing cannot be used to enumerate usernames.
            bcrypt.checkpw(req.password.encode("utf-8"), _DUMMY_HASH)
            raise HTTPException(status_code=401, detail="Invalid username or password")

        if not bcrypt.checkpw(req.password.encode("utf-8"), user.password_hash.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Invalid username or password")

        if user.id is None or user.username is None:
            raise RuntimeError("User record is incomplete")

        token = create_jwt(username=user.username, user_id=user.id)
        # See register()'s matching comment -- the JS client only reads
        # the HttpOnly cookie, never this body.
        response = JSONResponse(
            status_code=200,
            content={"username": user.username},
        )
        set_auth_cookie(response, token)
        return response


@router.get("/api/auth/verify")
async def verify(request: Request):
    token = auth_token_from_request(request)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    payload = verify_jwt(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"valid": True, "username": payload["sub"], "uid": payload["uid"]}


@router.post("/api/auth/logout")
async def logout():
    """Expire the web-ui auth cookie."""
    response = JSONResponse(status_code=200, content={"ok": True})
    clear_auth_cookie(response)
    return response
