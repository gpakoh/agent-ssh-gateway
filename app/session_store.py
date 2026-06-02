"""Persistent session storage using PostgreSQL."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import settings as _settings
from app.security import SecretManager

logger = logging.getLogger(__name__)


def _get_secret_manager() -> SecretManager:
    if _settings.encryption_key:
        return SecretManager(_settings.encryption_key)
    raise RuntimeError(
        "PERSISTENT_SESSIONS_ENABLED requires ENCRYPTION_KEY. "
        "Generate one with: "
        "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )

Base = declarative_base()


class PersistentSession(Base):
    """Persistent SSH session storage."""
    __tablename__ = "ssh_sessions"
    
    session_id = Column(String(36), primary_key=True)
    host = Column(String(255), nullable=False)
    port = Column(Integer, default=22)
    username = Column(String(255), nullable=False)
    password_encrypted = Column(Text, nullable=True)
    private_key_encrypted = Column(Text, nullable=True)
    key_passphrase_encrypted = Column(Text, nullable=True)
    connected_at = Column(DateTime, default=lambda: datetime.now(UTC))
    last_activity = Column(DateTime, default=lambda: datetime.now(UTC))
    expires_at = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True)
    reconnect_count = Column(Integer, default=0)
    metadata_json = Column(JSON, default=dict)
    
    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_active": self.is_active,
            "reconnect_count": self.reconnect_count,
        }


class EventHook(Base):
    """Event hook registration for webhook notifications."""
    __tablename__ = "event_hooks"

    id = Column(String(36), primary_key=True)
    url = Column(String(2048), nullable=False)
    events = Column(JSON, nullable=False)
    session_id = Column(String(36), nullable=True)
    headers_encrypted = Column(Text, nullable=True)
    secret_encrypted = Column(Text, nullable=True)
    include_output = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "events": self.events,
            "session_id": self.session_id,
            "include_output": self.include_output,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class WebhookDelivery(Base):
    """Outbox delivery record for event hooks."""
    __tablename__ = "webhook_deliveries"

    delivery_id = Column(String(36), primary_key=True)
    event_id = Column(String(36), nullable=False, index=True)
    hook_id = Column(String(36), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    url = Column(String(2048), nullable=False)
    payload_json = Column(Text, nullable=False)
    status = Column(String(16), default="pending", index=True)
    attempts = Column(Integer, default=0)
    next_retry_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    http_status = Column(Integer, nullable=True)
    leased_by = Column(String(64), nullable=True)
    leased_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))


class SessionStore:
    """Async session store using PostgreSQL."""
    
    def __init__(self, database_url: str):
        self._database_url = database_url
        self._engine = None
        self._session_maker: async_sessionmaker[AsyncSession] | None = None
    
    async def connect(self):
        """Initialize database connection."""
        try:
            self._engine = create_async_engine(self._database_url, echo=False)
            self._session_maker = async_sessionmaker(
                self._engine, 
                class_=AsyncSession,
                expire_on_commit=False
            )
            
            # Create Tables (DEV Only — Use Alembic In Production)
            logger.warning("Auto-creating Tables Via Base.metadata.create_all — Use Alembic For Production Migrations")
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            
            logger.info("Connected To Postgresql Session Store")
        except Exception as exc:
            logger.error("Failed to connect to PostgreSQL: %s", exc)
            raise
    
    async def disconnect(self):
        """Close database connection."""
        if self._engine:
            await self._engine.dispose()
            logger.info("Disconnected From Postgresql")
    
    async def save_session(
        self,
        session_id: str,
        host: str,
        port: int,
        username: str,
        password: str | None = None,
        private_key: str | None = None,
        key_passphrase: str | None = None,
        ttl: int = 3600,
    ) -> None:
        """Save session to database."""
        sm = self._session_maker
        assert sm is not None
        async with sm() as session:
            secret_manager = _get_secret_manager()
            db_session = PersistentSession(
                session_id=session_id,
                host=host,
                port=port,
                username=username,
                password_encrypted=secret_manager.encrypt(password) if password else None,
                private_key_encrypted=secret_manager.encrypt(private_key) if private_key else None,
                key_passphrase_encrypted=secret_manager.encrypt(key_passphrase) if key_passphrase else None,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
                is_active=True,
            )
            
            await session.merge(db_session)
            await session.commit()
            
            logger.info("Session %s saved to database", session_id)
    
    async def get_session(self, session_id: str) -> dict | None:
        """Get session from database."""
        sm = self._session_maker
        assert sm is not None
        async with sm() as session:
            from sqlalchemy import select
            
            result = await session.execute(
                select(PersistentSession).where(
                    PersistentSession.session_id == session_id,
                    PersistentSession.is_active == True,  # noqa: E712
                    PersistentSession.expires_at > datetime.now(UTC)
                )
            )
            db_session = result.scalar_one_or_none()
            
            if db_session:
                # Update Last Activity
                db_session.last_activity = datetime.now(UTC)
                await session.commit()
                return db_session.to_dict()
            
            return None
    
    async def update_session_activity(self, session_id: str) -> None:
        """Update session last activity."""
        sm = self._session_maker
        assert sm is not None
        async with sm() as session:
            from sqlalchemy import select
            
            result = await session.execute(
                select(PersistentSession).where(
                    PersistentSession.session_id == session_id
                )
            )
            db_session = result.scalar_one_or_none()
            
            if db_session:
                db_session.last_activity = datetime.now(UTC)
                await session.commit()

    async def deactivate_session(self, session_id: str) -> None:
        """Deactivate session."""
        sm = self._session_maker
        assert sm is not None
        async with sm() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(PersistentSession).where(
                    PersistentSession.session_id == session_id
                )
            )
            db_session = result.scalar_one_or_none()

            if db_session:
                db_session.is_active = False
                await session.commit()
                logger.info("Session %s deactivated", session_id)

    async def list_active_sessions(self) -> list[dict]:
        """List all active sessions."""
        sm = self._session_maker
        assert sm is not None
        async with sm() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(PersistentSession).where(
                    PersistentSession.is_active == True,  # noqa: E712
                    PersistentSession.expires_at > datetime.now(UTC)
                )
            )
            sessions = result.scalars().all()
            return [s.to_dict() for s in sessions]

    async def cleanup_expired_sessions(self) -> int:
        """Remove expired sessions in batches. Returns count removed."""
        total = 0
        batch_size = 1000
        sm = self._session_maker
        assert sm is not None
        async with sm() as session:
            from sqlalchemy import delete, select

            while True:
                subq = select(PersistentSession.session_id).where(
                    PersistentSession.expires_at < datetime.now(UTC)
                ).limit(batch_size)
                result = await session.execute(
                    delete(PersistentSession).where(
                        PersistentSession.session_id.in_(subq)
                    )
                )
                await session.commit()
                if result.rowcount == 0:
                    break
                total += result.rowcount

        if total > 0:
            logger.info("Cleaned up %d expired sessions", total)
        return total
    
    async def get_session_credentials(self, session_id: str) -> dict | None:
        """Get decrypted credentials for session."""
        sm = self._session_maker
        assert sm is not None
        async with sm() as session:
            from sqlalchemy import select
            
            result = await session.execute(
                select(PersistentSession).where(
                    PersistentSession.session_id == session_id,
                    PersistentSession.is_active == True,  # noqa: E712
                )
            )
            db_session = result.scalar_one_or_none()
            
            if not db_session:
                return None
            
            secret_manager = _get_secret_manager()
            return {
                "host": db_session.host,
                "port": db_session.port,
                "username": db_session.username,
                "password": secret_manager.decrypt(db_session.password_encrypted) if db_session.password_encrypted else None,
                "private_key": secret_manager.decrypt(db_session.private_key_encrypted) if db_session.private_key_encrypted else None,
                "key_passphrase": secret_manager.decrypt(db_session.key_passphrase_encrypted) if db_session.key_passphrase_encrypted else None,
            }
