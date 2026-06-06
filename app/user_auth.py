"""Local auth for web UI — single admin registration + JWT login."""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import jwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings

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


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class AuthResponse(BaseModel):
    token: str
    username: str


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


def create_jwt(username: str, user_id: int) -> str:
    secret = settings.jwt_secret_required
    payload = {
        "sub": username,
        "uid": user_id,
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


@router.get("/api/auth/check")
async def auth_check_users():
    SessionLocal = get_auth_sessionmaker()
    async with SessionLocal() as session:
        result = await session.execute(select(func.count(User.id)))
        count = result.scalar() or 0
    return {"users_count": count}


@router.post("/api/auth/register", status_code=201)
async def register(req: RegisterRequest):
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
                raise HTTPException(status_code=403, detail="Registration disabled. An admin already exists.")

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
            return AuthResponse(token=token, username=user.username)


@router.post("/api/auth/login")
async def login(req: LoginRequest):
    SessionLocal = get_auth_sessionmaker()
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.username == req.username))
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid username or password")

        if not bcrypt.checkpw(req.password.encode("utf-8"), user.password_hash.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Invalid username or password")

        if user.id is None or user.username is None:
            raise RuntimeError("User record is incomplete")

        token = create_jwt(username=user.username, user_id=user.id)
        return AuthResponse(token=token, username=user.username)


@router.get("/api/auth/verify")
async def verify(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth[7:]
    payload = verify_jwt(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"valid": True, "username": payload["sub"], "uid": payload["uid"]}
