"""Outbox delivery service — enqueue, claim, complete, fail, retry scheduler."""

from __future__ import annotations

import asyncio
import logging
import uuid
import random
from datetime import datetime, timedelta, timezone

import aiohttp
from sqlalchemy import select, and_, func, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.session_store import Base, WebhookDelivery
from app.metrics import metrics

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


def _compute_retry_at(attempts: int, base_sec: float, max_sec: float) -> datetime:
    delay = min(base_sec * (2 ** attempts), max_sec)
    jitter = delay * random.uniform(0.5, 1.5)
    return _now() + timedelta(seconds=jitter)


class DeliveryService:
    def __init__(self, database_url: str, instance_id: str):
        self._instance_id = instance_id
        self._engine = create_async_engine(database_url)
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        self._http_session: aiohttp.ClientSession | None = None
        self._worker_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._running = False

    async def create_tables(self):
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self):
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._http_session:
            await self._http_session.close()
        await self._engine.dispose()

    async def start(
        self,
        poll_interval: float,
        connect_timeout: float,
        read_timeout: float,
        max_attempts: int,
        retry_base_sec: float,
        retry_max_sec: float,
        lease_ttl: float,
        retention_sent_days: int,
        retention_dead_days: int,
    ):
        self._running = True
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=connect_timeout + read_timeout,
                connect=connect_timeout,
            ),
            allow_redirects=False,
        )
        self._worker_task = asyncio.create_task(
            self._worker_loop(
                poll_interval, max_attempts, retry_base_sec, retry_max_sec, lease_ttl
            )
        )
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(3600, retention_sent_days, retention_dead_days)
        )

    # ------------------------------------------------------------------
    # Outbox operations
    # ------------------------------------------------------------------

    async def enqueue(
        self, event_id: str, hook_id: str, event_type: str, url: str, payload_json: str
    ) -> str:
        delivery_id = uuid.uuid4().hex
        delivery = WebhookDelivery(
            delivery_id=delivery_id,
            event_id=event_id,
            hook_id=hook_id,
            event_type=event_type,
            url=url,
            payload_json=payload_json,
            status="pending",
            attempts=0,
        )
        async with self._session_factory() as session:
            session.add(delivery)
            await session.commit()
        return delivery_id

    async def claim_deliveries(
        self, limit: int, lease_ttl: float
    ) -> list[WebhookDelivery]:
        """Claim pending/failed deliveries with lease."""
        now = _now()
        stale = now - timedelta(seconds=lease_ttl)
        async with self._session_factory() as session:
            # Load candidate deliveries; filtering by state is done in Python
            # for SQLite compatibility (FOR UPDATE SKIP LOCKED in PG would be ideal)
            result = await session.execute(
                select(WebhookDelivery)
                .where(WebhookDelivery.status.in_(["pending", "failed"]))
                .limit(limit)
            )
            deliveries: list[WebhookDelivery] = list(result.scalars().all())
            claimed = []
            for d in deliveries:
                # Skip if leased by another instance and lease is still active
                if d.leased_by and d.leased_by != self._instance_id:
                    if d.leased_at and d.leased_at > stale:
                        continue
                # Skip pending deliveries that are too young (avoid races)
                if d.status == "pending":
                    age = (now - d.created_at).total_seconds()
                    if age < 2.0:
                        continue
                # Skip failed deliveries whose retry time hasn't come yet
                if d.status == "failed" and d.next_retry_at and d.next_retry_at > now:
                    continue

                d.leased_by = self._instance_id
                d.leased_at = now
                claimed.append(d)

            await session.commit()
            return claimed

    async def complete(self, delivery_id: str, http_status: int) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.delivery_id == delivery_id
                )
            )
            d = result.scalar_one_or_none()
            if not d:
                return False
            d.status = "sent"
            d.http_status = http_status
            d.leased_by = None
            d.leased_at = None
            d.updated_at = _now()
            await session.commit()
            return True

    async def fail(
        self,
        delivery_id: str,
        last_error: str,
        max_attempts: int,
        retry_base_sec: float,
        retry_max_sec: float,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.delivery_id == delivery_id
                )
            )
            d = result.scalar_one_or_none()
            if not d:
                return False
            d.attempts += 1
            d.last_error = last_error[:1024]
            d.leased_by = None
            d.leased_at = None
            d.updated_at = _now()

            if d.attempts >= max_attempts:
                d.status = "dead"
                d.next_retry_at = None
                dead_count = await session.scalar(
                    select(func.count()).select_from(WebhookDelivery).where(
                        WebhookDelivery.status == "dead"
                    ),
                )
                metrics.set_event_hook_dead_letter_count(dead_count or 0)
            else:
                d.status = "failed"
                d.next_retry_at = _compute_retry_at(
                    d.attempts, retry_base_sec, retry_max_sec
                )

            await session.commit()
            return True

    async def cleanup_old(self, sent_days: int, dead_days: int) -> int:
        now = _now()
        total = 0
        async with self._session_factory() as session:
            for status, days in [("sent", sent_days), ("dead", dead_days)]:
                cutoff = now - timedelta(days=days)
                result = await session.execute(
                    select(WebhookDelivery).where(
                        WebhookDelivery.status == status,
                        WebhookDelivery.updated_at < cutoff,
                    )
                )
                rows = list(result.scalars().all())
                for r in rows:
                    await session.delete(r)
                    total += 1
            await session.commit()
        return total

    # ------------------------------------------------------------------
    # Internal — background worker
    # ------------------------------------------------------------------

    async def _worker_loop(
        self,
        poll_interval: float,
        max_attempts: int,
        retry_base_sec: float,
        retry_max_sec: float,
        lease_ttl: float,
    ):
        while self._running:
            try:
                deliveries = await self.claim_deliveries(
                    limit=20, lease_ttl=lease_ttl
                )
                for d in deliveries:
                    asyncio.create_task(
                        self._send_delivery(
                            d, max_attempts, retry_base_sec, retry_max_sec
                        )
                    )
            except Exception:
                logger.exception("Delivery worker error")
            await asyncio.sleep(poll_interval)

    async def _send_delivery(
        self,
        delivery: WebhookDelivery,
        max_attempts: int,
        retry_base_sec: float,
        retry_max_sec: float,
    ):
        start = datetime.now(timezone.utc)
        metrics.record_event_hook_attempt()
        try:
            async with self._http_session.post(
                delivery.url,
                data=delivery.payload_json,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if 200 <= resp.status < 300:
                    await self.complete(delivery.delivery_id, resp.status)
                    metrics.record_event_hook_delivery(status="success", event=delivery.event_type)
                elif resp.status == 429 or resp.status >= 500:
                    await self.fail(
                        delivery.delivery_id,
                        f"HTTP {resp.status}",
                        max_attempts,
                        retry_base_sec,
                        retry_max_sec,
                    )
                    metrics.record_event_hook_delivery(status="retryable", event=delivery.event_type)
                else:
                    await self.fail(
                        delivery.delivery_id,
                        f"HTTP {resp.status} (non-retryable)",
                        max_attempts,
                        retry_base_sec,
                        retry_max_sec,
                    )
                    metrics.record_event_hook_delivery(status="failed", event=delivery.event_type)
        except Exception as exc:
            await self.fail(
                delivery.delivery_id,
                str(exc)[:1024],
                max_attempts,
                retry_base_sec,
                retry_max_sec,
            )
            metrics.record_event_hook_delivery(status="error", event=delivery.event_type)
        finally:
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            metrics.record_event_hook_latency(elapsed)

    async def _cleanup_loop(
        self, interval: float, sent_days: int, dead_days: int
    ):
        while self._running:
            try:
                count = await self.cleanup_old(sent_days, dead_days)
                if count:
                    logger.info("Cleaned up %d old delivery records", count)
            except Exception:
                logger.exception("Delivery cleanup error")
            await asyncio.sleep(interval)

    async def _get_record(self, delivery_id: str) -> WebhookDelivery | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.delivery_id == delivery_id
                )
            )
            return result.scalar_one_or_none()

    @property
    def max_output_bytes(self) -> int:
        from app.config import settings
        return settings.event_hooks_max_output_bytes
