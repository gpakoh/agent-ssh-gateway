"""Host key storage backends and custom MissingHostKeyPolicy."""

import asyncio
import base64
import hashlib
import logging
from abc import ABC, abstractmethod
from typing import Optional

import paramiko

from app.config import settings

try:
    from datetime import datetime, timezone

    from sqlalchemy import (
        Column,
        DateTime,
        Integer,
        String,
        Text,
        UniqueConstraint,
    )
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.orm import declarative_base

    _sa_available = True
except ImportError:
    _sa_available = False

logger = logging.getLogger(__name__)


class HostKeyStore(ABC):
    """Abstract host key store. check() returns:
    None = unknown host, True = key matches, False = key changed.
    """

    @abstractmethod
    async def check(self, host: str, port: int, key: paramiko.PKey) -> Optional[bool]:
        ...

    @abstractmethod
    async def store(self, host: str, port: int, key: paramiko.PKey) -> None:
        ...

    async def list_keys(self) -> list[dict]:
        return []

    async def delete_host(self, host: str) -> int:
        return 0

    async def delete_all(self) -> int:
        return 0

    async def disconnect(self):
        pass


class NullHostKeyStore(HostKeyStore):
    """No-op store — every host is unknown, store() does nothing."""

    async def check(self, host: str, port: int, key: paramiko.PKey) -> Optional[bool]:
        return None

    async def store(self, host: str, port: int, key: paramiko.PKey) -> None:
        pass


class FileHostKeyStore(HostKeyStore):
    """OpenSSH-format known_hosts file via paramiko.hostkeys.HostKeys."""

    def __init__(self, path: str):
        self._path = path
        self._hk = paramiko.hostkeys.HostKeys()
        self._loaded = False
        self._lock = asyncio.Lock()

    async def _load(self):
        if self._loaded:
            return
        loop = asyncio.get_event_loop()
        try:
            self._hk = await loop.run_in_executor(
                None, lambda: paramiko.hostkeys.HostKeys(self._path)
            )
        except FileNotFoundError:
            self._hk = paramiko.hostkeys.HostKeys()
        self._loaded = True

    async def _save(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._hk.save, self._path)

    async def check(self, host: str, port: int, key: paramiko.PKey) -> Optional[bool]:
        async with self._lock:
            await self._load()
            host_key = self._hk.lookup(host)
            if host_key is None:
                return None
            for known_key in host_key.values():
                if known_key == key:
                    return True
            return False

    async def store(self, host: str, port: int, key: paramiko.PKey) -> None:
        async with self._lock:
            await self._load()
            self._hk.add(host, key.get_name(), key)
            try:
                await self._save()
            except Exception as exc:
                logger.warning("Failed to write known_hosts file %s: %s", self._path, exc)

    async def list_keys(self) -> list[dict]:
        async with self._lock:
            await self._load()
            results = []
            for host, entries in self._hk.items():
                for key_type, known_key in entries.items():
                    results.append({
                        "host": host,
                        "port": 22,
                        "key_type": key_type,
                        "fingerprint": hashlib.sha256(known_key.asbytes()).hexdigest(),
                    })
            return results

    async def delete_host(self, host: str) -> int:
        async with self._lock:
            await self._load()
            before = len(self._hk._entries)
            self._hk._entries = [
                e for e in self._hk._entries if host not in e.hostnames
            ]
            removed = before - len(self._hk._entries)
            if removed > 0:
                await self._save()
            return removed

    async def delete_all(self) -> int:
        async with self._lock:
            await self._load()
            count = len(self._hk._entries)
            self._hk._entries.clear()
            if count > 0:
                await self._save()
            return count


class PostgresHostKeyStore(HostKeyStore):
    """PostgreSQL-backed host key store using SQLAlchemy async."""

    def __init__(self, database_url: str):
        if not _sa_available:
            raise RuntimeError("SQLAlchemy is not installed")
        self._database_url = database_url
        self._engine = None
        self._session_maker = None
        self._lock = asyncio.Lock()

    async def _init_db(self):
        if self._engine is not None:
            return
        engine = create_async_engine(self._database_url, echo=False)
        session_maker = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False,
        )
        async with engine.begin() as conn:
            logger.warning("Auto-creating Host Key Tables Via Base.metadata.create_all — Use Alembic For Production Migrations")
            await conn.run_sync(Base.metadata.create_all)
        self._engine = engine
        self._session_maker = session_maker

    def _key_data(self, key: paramiko.PKey) -> str:
        return base64.b64encode(key.asbytes()).decode()

    def _key_type(self, key: paramiko.PKey) -> str:
        return key.get_name()

    def _fingerprint(self, key: paramiko.PKey) -> str:
        return hashlib.sha256(key.asbytes()).hexdigest()

    async def check(self, host: str, port: int, key: paramiko.PKey) -> Optional[bool]:
        if key is None:
            return None
        async with self._lock:
            await self._init_db()
        async with self._session_maker() as session:
            from sqlalchemy import select
            result = await session.execute(
                select(HostKeyRecord).where(
                    HostKeyRecord.host == host,
                    HostKeyRecord.port == port,
                    HostKeyRecord.key_type == self._key_type(key),
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                return None
            return record.key_data == self._key_data(key)

    async def store(self, host: str, port: int, key: paramiko.PKey) -> None:
        async with self._lock:
            await self._init_db()
        async with self._session_maker() as session:
            from sqlalchemy import select
            result = await session.execute(
                select(HostKeyRecord).where(
                    HostKeyRecord.host == host,
                    HostKeyRecord.port == port,
                    HostKeyRecord.key_type == self._key_type(key),
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                record = HostKeyRecord(
                    host=host, port=port,
                    key_type=self._key_type(key),
                    key_data=self._key_data(key),
                    fingerprint=self._fingerprint(key),
                )
                session.add(record)
            else:
                record.key_data = self._key_data(key)
                record.fingerprint = self._fingerprint(key)
            await session.commit()

    async def list_keys(self) -> list[dict]:
        async with self._lock:
            await self._init_db()
        async with self._session_maker() as session:
            from sqlalchemy import select
            result = await session.execute(select(HostKeyRecord))
            records = result.scalars().all()
            return [
                {"host": r.host, "port": r.port, "key_type": r.key_type,
                 "fingerprint": r.fingerprint,
                 "updated_at": r.updated_at.isoformat() if r.updated_at else None}
                for r in records
            ]

    async def delete_host(self, host: str) -> int:
        async with self._lock:
            await self._init_db()
        async with self._session_maker() as session:
            from sqlalchemy import delete as sa_delete
            result = await session.execute(
                sa_delete(HostKeyRecord).where(HostKeyRecord.host == host)
            )
            await session.commit()
            return result.rowcount

    async def delete_all(self) -> int:
        async with self._lock:
            await self._init_db()
        async with self._session_maker() as session:
            from sqlalchemy import delete as sa_delete
            result = await session.execute(sa_delete(HostKeyRecord))
            await session.commit()
            return result.rowcount

    async def disconnect(self):
        if self._engine:
            await self._engine.dispose()


if _sa_available:
    Base = declarative_base()

    class HostKeyRecord(Base):
        __tablename__ = "ssh_host_keys"

        host = Column(String(255), primary_key=True)
        port = Column(Integer, primary_key=True, default=22)
        key_type = Column(String(32), primary_key=True)
        key_data = Column(Text, nullable=False)
        fingerprint = Column(String(128), nullable=False)
        updated_at = Column(
            DateTime,
            default=lambda: datetime.now(timezone.utc),
            onupdate=lambda: datetime.now(timezone.utc),
        )

        __table_args__ = (
            UniqueConstraint("host", "port", "key_type", name="uq_host_key_type"),
        )


class KnownHostsPolicy(paramiko.MissingHostKeyPolicy):
    """Paramiko host key policy backed by an async HostKeyStore.

    Called by Paramiko when _host_keys has no entry for the host.
    Since we keep _host_keys empty, this is called on every connection.
    """

    def __init__(self, store: HostKeyStore, port: int = 22):
        self._store = store
        self._port = port

    def missing_host_key(self, client, hostname, key):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._check_or_store(hostname, key))
            return
        future = asyncio.run_coroutine_threadsafe(
            self._check_or_store(hostname, key), loop
        )
        future.result(timeout=5)

    async def _check_or_store(self, hostname, key):
        known = await self._store.check(hostname, self._port, key)
        if known is None:
            raise paramiko.SSHException(
                f"Unknown host {hostname}:{self._port}. "
                "Add the host key manually via SSHConnectResponse or the known-hosts API."
            )
        if not known:
            raise paramiko.SSHException(
                f"Host key for {hostname}:{self._port} changed — possible MITM attack. "
                "Remove the old key via the known-hosts API and re-add if the change was legitimate."
            )


def create_host_key_store(settings) -> HostKeyStore:
    kind = settings.known_hosts_store
    if kind == "file":
        from app.known_hosts import FileHostKeyStore
        return FileHostKeyStore(settings.known_hosts_file)
    if kind == "postgres":
        from app.known_hosts import PostgresHostKeyStore
        return PostgresHostKeyStore(settings.database_url)
    return NullHostKeyStore()
