"""Outbox delivery service — enqueue, claim, complete, fail, retry scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import aiohttp
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings as _settings
from app.event_hook_security import validate_destination_ip
from app.metrics import metrics
from app.session_store import WebhookDelivery

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _compute_retry_at(attempts: int, base_sec: float, max_sec: float) -> datetime:
    delay = min(base_sec * (2**attempts), max_sec)
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
        self._inflight: set[asyncio.Task] = set()
        self._running = False

    async def create_tables(self):
        logger.warning(
            "Auto-creating WebhookDelivery table for feature bootstrap — use Alembic in production"
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(WebhookDelivery.__table__.create, checkfirst=True)

    async def close(self, drain_timeout: float = 10.0):
        """Stop the worker/cleanup loops and drain in-flight deliveries
        before touching the HTTP session or DB engine.

        `.cancel()` only *schedules* cancellation at the next checkpoint —
        it does not block until the task has actually stopped. Previously
        close() cancelled the loop tasks and immediately disposed the
        engine/closed the session, while _worker_loop's per-delivery
        `_send_delivery` tasks (fired via untracked `asyncio.create_task`)
        could still be mid-flight using that exact session/engine,
        producing unretrieved-exception warnings on shutdown.
        """
        self._running = False
        for task in (self._worker_task, self._cleanup_task):
            if task:
                task.cancel()
        for task in (self._worker_task, self._cleanup_task):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._inflight:
            _done, pending = await asyncio.wait(self._inflight, timeout=drain_timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending)
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
        )
        self._worker_task = asyncio.create_task(
            self._worker_loop(poll_interval, max_attempts, retry_base_sec, retry_max_sec, lease_ttl)
        )
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(3600, retention_sent_days, retention_dead_days)
        )

    # ------------------------------------------------------------------
    # Outbox Operations
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        event_id: str,
        hook_id: str,
        event_type: str,
        url: str,
        payload_json: str,
        headers_json: str | None = None,
    ) -> str:
        delivery_id = uuid.uuid4().hex
        delivery = WebhookDelivery(
            delivery_id=delivery_id,
            event_id=event_id,
            hook_id=hook_id,
            event_type=event_type,
            url=url,
            payload_json=payload_json,
            headers_json=headers_json,
            status="pending",
            attempts=0,
        )
        async with self._session_factory() as session:
            session.add(delivery)
            await session.commit()
        return delivery_id

    async def claim_deliveries(self, limit: int, lease_ttl: float) -> list[WebhookDelivery]:
        """Claim pending/failed deliveries with lease."""
        now = _now()
        stale = now - timedelta(seconds=lease_ttl)
        async with self._session_factory() as session:
            # Load Candidate Deliveries; Filtering By State Is Done In PYTHON
            # For Sqlite Compatibility (FOR UPDATE SKIP LOCKED In PG Would Be Ideal)
            result = await session.execute(
                select(WebhookDelivery)
                .where(WebhookDelivery.status.in_(["pending", "failed"]))
                .limit(limit)
            )
            deliveries: list[WebhookDelivery] = list(result.scalars().all())
            claimed = []
            for d in deliveries:
                # Skip If Leased By Another Instance And Lease Is Still Active
                if d.leased_by and d.leased_by != self._instance_id:
                    if d.leased_at and d.leased_at > stale:
                        continue
                # Skip Pending Deliveries That Are Too Young (avoid Races)
                if d.status == "pending":
                    created_at = d.created_at
                    assert created_at is not None
                    age = (now - created_at).total_seconds()
                    if age < 2.0:
                        continue
                # Skip Failed Deliveries Whose Retry Time Hasn't Come Yet
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
                select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
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
                select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
            )
            d = result.scalar_one_or_none()
            if not d:
                return False
            attempts = d.attempts
            d.attempts = (attempts if attempts is not None else 0) + 1
            d.last_error = last_error[:1024]
            d.leased_by = None
            d.leased_at = None
            d.updated_at = _now()

            if d.attempts >= max_attempts:
                d.status = "dead"
                d.next_retry_at = None
                dead_count = await session.scalar(
                    select(func.count())
                    .select_from(WebhookDelivery)
                    .where(WebhookDelivery.status == "dead"),
                )
                metrics.set_event_hook_dead_letter_count(dead_count or 0)
            else:
                d.status = "failed"
                d.next_retry_at = _compute_retry_at(d.attempts, retry_base_sec, retry_max_sec)

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
    # Internal — Background Worker
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
                deliveries = await self.claim_deliveries(limit=20, lease_ttl=lease_ttl)
                for d in deliveries:
                    task = asyncio.create_task(
                        self._send_delivery(d, max_attempts, retry_base_sec, retry_max_sec)
                    )
                    self._inflight.add(task)
                    task.add_done_callback(self._inflight.discard)
            except Exception:
                logger.exception("Delivery Worker Error")
            await asyncio.sleep(poll_interval)

    async def _send_delivery(
        self,
        delivery: WebhookDelivery,
        max_attempts: int,
        retry_base_sec: float,
        retry_max_sec: float,
    ):
        start = datetime.now(UTC)
        metrics.record_event_hook_attempt()
        if delivery.delivery_id is None:
            raise RuntimeError("Event hook delivery invariant violated: delivery_id is missing")
        if delivery.url is None:
            raise RuntimeError("Event hook delivery invariant violated: url is missing")
        if delivery.event_type is None:
            raise RuntimeError("Event hook delivery invariant violated: event_type is missing")
        d_id: str = delivery.delivery_id
        d_url: str = delivery.url
        d_event: str = delivery.event_type

        blocked_reason = await self._blocked_destination_reason(d_url)
        if blocked_reason:
            await self.fail(
                d_id,
                f"Blocked destination ({blocked_reason})",
                max_attempts,
                retry_base_sec,
                retry_max_sec,
            )
            metrics.record_event_hook_delivery(status="failed", event=d_event)
            elapsed = (datetime.now(UTC) - start).total_seconds()
            metrics.record_event_hook_latency(elapsed)
            return

        headers = {"Content-Type": "application/json"}
        if delivery.headers_json:
            try:
                headers = json.loads(delivery.headers_json)
            except (TypeError, ValueError):
                logger.warning("Delivery %s has invalid headers_json, using defaults", d_id)

        try:
            async with self._http_session.post(
                d_url,
                data=delivery.payload_json,
                headers=headers,
                allow_redirects=False,
            ) as resp:
                if 200 <= resp.status < 300:
                    await self.complete(d_id, resp.status)
                    metrics.record_event_hook_delivery(status="success", event=d_event)
                elif resp.status == 429 or resp.status >= 500:
                    await self.fail(
                        d_id,
                        f"HTTP {resp.status}",
                        max_attempts,
                        retry_base_sec,
                        retry_max_sec,
                    )
                    metrics.record_event_hook_delivery(status="retryable", event=d_event)
                else:
                    await self.fail(
                        d_id,
                        f"HTTP {resp.status} (non-retryable)",
                        max_attempts,
                        retry_base_sec,
                        retry_max_sec,
                    )
                    metrics.record_event_hook_delivery(status="failed", event=d_event)
        except Exception as exc:
            await self.fail(
                d_id,
                str(exc)[:1024],
                max_attempts,
                retry_base_sec,
                retry_max_sec,
            )
            metrics.record_event_hook_delivery(status="error", event=d_event)
        finally:
            elapsed = (datetime.now(UTC) - start).total_seconds()
            metrics.record_event_hook_latency(elapsed)

    async def _blocked_destination_reason(self, url: str) -> str | None:
        """Resolve the delivery URL's hostname and check every resolved
        address against the SSRF blocklist.

        validate_webhook_url() (checked only at hook-creation time) rejects a
        hostname only when it is itself a literal IP in a blocked range —
        ipaddress.ip_address() raises on any real hostname, so validation is
        silently skipped for it. A hook URL using a hostname that merely
        *resolves* to 127.0.0.1 / 169.254.169.254 / an RFC1918 address was
        never re-checked before this connected to it. Redirects are already
        disabled (allow_redirects=False) so this is the one remaining gap.
        """
        host = urlparse(url).hostname
        if not host:
            return "no hostname"
        try:
            addrs = await asyncio.get_running_loop().getaddrinfo(host, None)
        except OSError as exc:
            return f"DNS resolution failed: {exc}"
        for _family, _type, _proto, _canonname, sockaddr in addrs:
            ip = str(sockaddr[0])
            if not validate_destination_ip(ip).valid:
                return f"resolves to blocked address {ip}"
        return None

    async def _cleanup_loop(self, interval: float, sent_days: int, dead_days: int):
        while self._running:
            try:
                count = await self.cleanup_old(sent_days, dead_days)
                if count:
                    logger.info("Cleaned up %d old delivery records", count)
            except Exception:
                logger.exception("Delivery Cleanup Error")
            await asyncio.sleep(interval)

    async def _get_record(self, delivery_id: str) -> WebhookDelivery | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery_id)
            )
            return result.scalar_one_or_none()

    @property
    def max_output_bytes(self) -> int:
        return _settings.event_hooks_max_output_bytes
