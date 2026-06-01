"""Event hook storage — CRUD over SQLAlchemy."""

from __future__ import annotations

import uuid
import logging
from datetime import datetime, timezone
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.session_store import Base, EventHook

logger = logging.getLogger(__name__)


class EventHookStore:
    def __init__(self, database_url: str):
        self._engine = create_async_engine(database_url)
        self._session_factory = async_sessionmaker(self._engine, class_=AsyncSession)

    async def create_tables(self):
        logger.warning("Auto-creating Event Hook Tables Via Base.metadata.create_all — Use Alembic For Production Migrations")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self):
        await self._engine.dispose()

    async def create(
        self,
        url: str,
        events: list[str],
        session_id: str | None,
        headers_encrypted: str | None,
        secret_encrypted: str | None,
        include_output: bool,
    ) -> EventHook:
        hook = EventHook(
            id=uuid.uuid4().hex,
            url=url,
            events=events,
            session_id=session_id,
            headers_encrypted=headers_encrypted,
            secret_encrypted=secret_encrypted,
            include_output=include_output,
            is_active=True,
        )
        async with self._session_factory() as session:
            session.add(hook)
            await session.commit()
            await session.refresh(hook)
            return hook

    async def list_hooks(self) -> list[EventHook]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(EventHook).order_by(EventHook.created_at)
            )
            return list(result.scalars().all())

    async def get(self, hook_id: str) -> EventHook | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(EventHook).where(EventHook.id == hook_id)
            )
            return result.scalar_one_or_none()

    async def update(
        self,
        hook_id: str,
        url: str | None = None,
        events: List[str] | None = None,
        session_id: str | None = None,
        headers_encrypted: str | None = None,
        secret_encrypted: str | None = None,
        include_output: bool | None = None,
        is_active: bool | None = None,
    ) -> EventHook | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(EventHook).where(EventHook.id == hook_id)
            )
            hook = result.scalar_one_or_none()
            if not hook:
                return None
            if url is not None:
                hook.url = url
            if events is not None:
                hook.events = events
            if session_id is not None:
                hook.session_id = session_id
            if headers_encrypted is not None:
                hook.headers_encrypted = headers_encrypted
            if secret_encrypted is not None:
                hook.secret_encrypted = secret_encrypted
            if include_output is not None:
                hook.include_output = include_output
            if is_active is not None:
                hook.is_active = is_active
            hook.updated_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(hook)
            return hook

    async def delete(self, hook_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(EventHook).where(EventHook.id == hook_id)
            )
            hook = result.scalar_one_or_none()
            if not hook:
                return False
            await session.delete(hook)
            await session.commit()
            return True

    async def find_matching(
        self, event_type: str, session_id: str
    ) -> list[EventHook]:
        """Find active hooks matching event_type and optionally session_id."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(EventHook).where(EventHook.is_active.is_(True))
            )
            hooks: list[EventHook] = list(result.scalars().all())
            matched = []
            for h in hooks:
                events = h.events
                if events is None:
                    continue
                if event_type not in events:
                    continue
                if h.session_id and h.session_id != session_id:
                    continue
                matched.append(h)
            return matched
