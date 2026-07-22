"""Gateway notifier polling service."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable

from app.notifier.config import NotifierSettings
from app.notifier.formatting import format_audit_event
from app.notifier.gateway import GatewayAuditClient
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

    async def close(self) -> None:
        await self._gateway.close()
        await self._telegram.close()

    async def poll_once(self) -> int:
        """Poll once and return number of notification messages attempted."""
        if not self._settings.enabled:
            return 0
        if not self._settings.can_poll_gateway:
            logger.warning("gateway_notifier_not_ready: gateway polling not configured")
            return 0

        events = await self._gateway.recent_events(limit=100)
        events.reverse()  # endpoint returns newest-first; notify oldest new event first
        return await self._notify_events(events)

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
