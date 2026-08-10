"""Persistent audit log storage — PostgreSQL-backed, mirrors AuditEvent.

Design:
    - AuditLogStore: async SQLAlchemy store over the ``audit_log`` table.
      Mirrors metadata-only AuditEvent fields plus command execution
      details (command, exit_code, duration_ms). No command output, no
      file content, no secrets.
    - Insert failures are logged but never break the caller flow
      (same tolerance as the JSONL AuditEventLogger).
    - Retention cleanup deletes entries older than a configurable number
      of days, in batches.

Config:
    AUDIT_LOG_PERSIST_ENABLED       — master switch (default: false)
    AUDIT_LOG_RETENTION_DAYS        — keep entries younger than N days
                                      (default: 90; 0 disables cleanup)
    AUDIT_LOG_CLEANUP_INTERVAL_SECONDS — cleanup task cadence
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.audit import AuditEvent
from app.security import redact_secrets
from app.session_store import AuditLogEntry, Base

logger = logging.getLogger(__name__)


class AuditLogStore:
    """Async PostgreSQL audit log store."""

    def __init__(self, database_url: str):
        self._engine = create_async_engine(database_url)
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def create_tables(self):
        """Auto-create tables (dev convenience). Use Alembic in production."""
        logger.warning(
            "Auto-creating Audit Log Tables Via Base.metadata.create_all — "
            "Use Alembic For Production Migrations"
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self):
        await self._engine.dispose()

    # -- insert -----------------------------------------------------------

    async def insert_event(
        self,
        event: AuditEvent,
        *,
        command: str | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
    ) -> str | None:
        """Persist an AuditEvent (plus optional execution details).

        Returns the new row id, or None if the insert failed (logged).
        """
        entry = AuditLogEntry(
            id=uuid.uuid4().hex,
            event_id=event.event_id,
            event_type=event.event_type,
            session_id=event.target_id if event.target_type == "session" else None,
            actor_type=event.actor_type or None,
            actor_name=event.actor_name or None,
            actor_fingerprint=event.actor_fingerprint or None,
            request_id=event.request_id or None,
            source_ip=event.source_ip or None,
            route=event.route or None,
            tool=event.tool or None,
            action=event.action or None,
            target_type=event.target_type or None,
            target_id=event.target_id or None,
            policy=event.policy or None,
            profile=event.profile or None,
            decision=event.decision or None,
            reason=event.reason or None,
            error_code=event.error_code or None,
            metadata_json=redact_secrets(event.metadata) if event.metadata else None,
            command=redact_secrets(command) if command else None,
            exit_code=exit_code,
            duration_ms=duration_ms,
            created_at=datetime.fromisoformat(event.timestamp)
            if event.timestamp
            else datetime.now(UTC),
        )
        try:
            async with self._session_factory() as session:
                session.add(entry)
                await session.commit()
            return entry.id
        except Exception:
            logger.warning(
                "audit: failed to persist event %s to audit_log", event.event_id, exc_info=True
            )
            return None

    async def insert_command_execution(
        self,
        *,
        session_id: str,
        command: str,
        exit_code: int | None,
        duration_ms: int,
        actor_type: str = "",
        actor_name: str = "",
        actor_fingerprint: str = "",
        source_ip: str = "",
        route: str = "",
        request_id: str = "",
        decision: str = "allowed",
        reason: str = "",
        event_type: str = "command.execute",
    ) -> str | None:
        """Persist a command execution record directly.

        This is the primary write path for POST /api/ssh/execute so the
        audit table gets command/exit_code/duration_ms regardless of the
        JSONL event logger.
        """
        # The full command string can carry secrets typed straight into
        # the command line (curl -H 'Authorization: Bearer ...', a psql
        # DSN with an embedded password, sshpass -p ..., ...) -- unlike
        # AuditLogger.log_command() (JSONL path), which only ever stored
        # command_root for exactly this reason, this persistent DB path
        # stored the raw command verbatim in both `command` and
        # `metadata_json`. Redact the same way logs/tool output already do.
        redacted_command = redact_secrets(command) if command else None
        entry = AuditLogEntry(
            id=uuid.uuid4().hex,
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            session_id=session_id,
            actor_type=actor_type or None,
            actor_name=actor_name or None,
            actor_fingerprint=actor_fingerprint or None,
            request_id=request_id or None,
            source_ip=source_ip or None,
            route=route or None,
            action="command executed",
            target_type="session",
            target_id=session_id,
            decision=decision,
            reason=reason or None,
            metadata_json={"command": redacted_command} if redacted_command else None,
            command=redacted_command,
            exit_code=exit_code,
            duration_ms=duration_ms,
            created_at=datetime.now(UTC),
        )
        try:
            async with self._session_factory() as session:
                session.add(entry)
                await session.commit()
            return entry.id
        except Exception:
            logger.warning("audit: failed to persist command execution to audit_log", exc_info=True)
            return None

    # -- query ------------------------------------------------------------

    async def query(
        self,
        *,
        session_id: str | None = None,
        event_type: str | None = None,
        decision: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLogEntry]:
        """Query audit entries with filters, newest-first, paginated."""
        stmt = select(AuditLogEntry).order_by(AuditLogEntry.created_at.desc())
        stmt = self._apply_filters(stmt, session_id, event_type, decision, since, until)
        stmt = stmt.limit(limit).offset(offset)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def count(
        self,
        *,
        session_id: str | None = None,
        event_type: str | None = None,
        decision: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        """Count entries matching the same filters (for pagination)."""
        stmt = select(func.count(AuditLogEntry.id))
        stmt = self._apply_filters(stmt, session_id, event_type, decision, since, until)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return int(result.scalar_one())

    @staticmethod
    def _apply_filters(stmt, session_id, event_type, decision, since, until):
        if session_id:
            stmt = stmt.where(AuditLogEntry.session_id == session_id)
        if event_type:
            stmt = stmt.where(AuditLogEntry.event_type == event_type)
        if decision:
            stmt = stmt.where(AuditLogEntry.decision == decision)
        if since is not None:
            stmt = stmt.where(AuditLogEntry.created_at >= since)
        if until is not None:
            stmt = stmt.where(AuditLogEntry.created_at <= until)
        return stmt

    # -- retention --------------------------------------------------------

    async def cleanup_retention(self, retention_days: int, batch_size: int = 1000) -> int:
        """Delete entries older than retention_days. Returns count removed.

        retention_days <= 0 disables cleanup (returns 0).
        """
        if retention_days <= 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        total = 0
        async with self._session_factory() as session:
            while True:
                subq = (
                    select(AuditLogEntry.id)
                    .where(AuditLogEntry.created_at < cutoff)
                    .limit(batch_size)
                )
                result = await session.execute(
                    delete(AuditLogEntry).where(AuditLogEntry.id.in_(subq))
                )
                await session.commit()
                if cast(CursorResult, result).rowcount == 0:
                    break
                total += cast(CursorResult, result).rowcount
        if total:
            logger.info("Audit log retention: removed %d entries older than %d days", total, retention_days)
        return total
