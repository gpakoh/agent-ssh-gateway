"""Gateway notifier polling service."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from app.notifier.config import NotifierSettings
from app.notifier.formatting import format_audit_event
from app.notifier.gateway import GatewayAuditClient, GatewayHealthError
from app.notifier.status import render_health_status
from app.notifier.telegram import TelegramClient

logger = logging.getLogger(__name__)


class GatewayNotifierService:
    """Poll gateway audit events and send operator notifications."""

    def __init__(
        self,
        *,
        settings: NotifierSettings,
        gateway: GatewayAuditClient,
        telegram: TelegramClient,
        seen_limit: int = 1000,
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        self._telegram = telegram
        self._seen: deque[str] = deque(maxlen=seen_limit)
        self._seen_set: set[str] = set()
        self._prev_health: str | None = None
        self._last_poll_at: str | None = None
        self._events_notified_total = 0

    async def close(self) -> None:
        await self._gateway.close()
        await self._telegram.close()

    async def poll_once(self) -> int:
        """Poll once and return number of notification messages attempted."""
        if not self._settings.enabled:
            return 0
        self._last_poll_at = _now_iso()
        if not self._settings.can_poll_gateway:
            logger.warning("gateway_notifier_not_ready: gateway polling not configured")
            return 0

        health_notifications = await self._check_health_transition()

        events = await self._gateway.recent_events(limit=100)
        events.reverse()  # endpoint returns newest-first; notify oldest new event first
        event_notifications = await self._notify_events(events)
        total = health_notifications + event_notifications
        self._events_notified_total += total
        return total

    async def run_forever(self) -> None:
        """Run the polling loop until cancelled."""
        try:
            while True:
                try:
                    await self.poll_once()
                except Exception:
                    logger.warning("gateway_notifier_poll_failed", exc_info=True)
                await asyncio.sleep(self._settings.poll_interval_seconds)
        finally:
            await self.close()

    async def status(self) -> dict[str, Any]:
        """Return a read-only snapshot of notifier state."""
        return {
            "gateway_health": await self._safe_health(),
            "last_poll_at": self._last_poll_at,
            "events_notified_total": self._events_notified_total,
            "prev_health": self._prev_health,
        }

    async def _safe_health(self) -> dict[str, Any]:
        try:
            return await self._gateway.health()
        except GatewayHealthError as exc:
            return {"status": "unreachable", "ready": False, "error_code": str(exc.status)}
        except Exception:
            return {"status": "unreachable", "ready": False}

    async def _check_health_transition(self) -> int:
        """Poll health and notify on state transitions."""
        health = await self._safe_health()
        current = str(health.get("status", "unreachable"))
        previous = self._prev_health

        if previous is None:
            # First poll: record baseline, no notification.
            self._prev_health = current
            return 0

        if previous == current:
            return 0

        self._prev_health = current

        if previous == "ok" and current != "ok":
            event_name = "health.degraded"
            detail = f"Gateway health degraded: {previous} -> {current}"
        elif previous != "ok" and current == "ok":
            event_name = "health.recovered"
            detail = f"Gateway health recovered: {previous} -> {current}"
        else:
            # Both non-ok but different (e.g. unreachable -> degraded): no notification.
            return 0

        text = f"<b>{event_name}</b>\n{detail}\n\n{render_health_status(health)}"
        await self._telegram.send_message(text)
        return 1

    async def _notify_events(self, events: Iterable[dict]) -> int:
        count = 0
        for event in events:
            event_id = str(event.get("event_id") or "")
            if not event_id or event_id in self._seen_set:
                continue
            self._mark_seen(event_id)

            event_type = str(event.get("event_type") or "")
            if event_type not in self._settings.event_types:
                continue

            text = format_audit_event(event)
            if not text:
                continue
            await self._telegram.send_message(text)
            count += 1
        return count

    def _mark_seen(self, event_id: str) -> None:
        if len(self._seen) == self._seen.maxlen and self._seen:
            old = self._seen.popleft()
            self._seen_set.discard(old)
        self._seen.append(event_id)
        self._seen_set.add(event_id)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
