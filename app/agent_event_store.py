"""Async PostgreSQL store for the append-only agent event log."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.session_store import AgentEventRecord


class AgentEventStore:
    """Append-only persistence for ``agent_events``.

    Events are keyed by a monotonically increasing ``sequence`` served by a
    dedicated PostgreSQL sequence. Readers use sequence ranges as replay and
    resume cursors scoped to a single job.
    """

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = session_maker

    async def insert(
        self,
        *,
        job_id: str,
        attempt_id: str | None = None,
        owner_id: str,
        agent_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentEventRecord:
        """Insert one event, return the committed record with its sequence."""
        async with self._sm() as session:
            record = AgentEventRecord(
                job_id=job_id,
                attempt_id=attempt_id,
                owner_id=owner_id,
                agent_id=agent_id,
                event_type=event_type,
                payload=payload if payload is not None else {},
            )
            session.add(record)
            # The sequence is applied server-side (nextval inline) and is not
            # included in INSERT..RETURNING by the asyncpg dialect, so the row
            # must be re-selected while still bound to load it eagerly.
            await session.flush()
            await session.refresh(record)
            await session.commit()
            return record

    async def get_events(
        self,
        job_id: str,
        *,
        after_sequence: int | None = None,
        up_to_sequence: int | None = None,
        limit: int = 500,
    ) -> list[AgentEventRecord]:
        """Query events for a job ordered by ascending sequence."""
        stmt = select(AgentEventRecord).where(AgentEventRecord.job_id == job_id)
        if after_sequence is not None:
            stmt = stmt.where(AgentEventRecord.sequence > after_sequence)
        if up_to_sequence is not None:
            stmt = stmt.where(AgentEventRecord.sequence <= up_to_sequence)
        stmt = stmt.order_by(AgentEventRecord.sequence.asc()).limit(limit)
        async with self._sm() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_latest_sequence(self, job_id: str) -> int | None:
        """Max committed sequence for a job (watermark)."""
        stmt = select(func.max(AgentEventRecord.sequence)).where(AgentEventRecord.job_id == job_id)
        async with self._sm() as session:
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_min_sequence(self, job_id: str) -> int | None:
        """Min sequence for a job (retention expiry check)."""
        stmt = select(func.min(AgentEventRecord.sequence)).where(AgentEventRecord.job_id == job_id)
        async with self._sm() as session:
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_owner_ids(self, job_id: str) -> list[str]:
        """Distinct owner_ids for a job — used for fail-closed authorization.

        A job whose history contains rows from more than one owner is treated
        as corrupted; callers must deny access regardless of role.
        """
        stmt = select(AgentEventRecord.owner_id).where(AgentEventRecord.job_id == job_id).distinct()
        async with self._sm() as session:
            result = await session.execute(stmt)
            return list(cast("list[str]", result.scalars().all()))

    async def get_owner_id(self, job_id: str) -> str | None:
        """Single-owner convenience wrapper over :meth:`get_owner_ids`.

        Returns the owner when exactly one distinct owner exists, else None
        (unknown job OR ambiguous ownership — ambiguous must not authorize).
        """
        owners = await self.get_owner_ids(job_id)
        if len(owners) == 1:
            return owners[0]
        return None
