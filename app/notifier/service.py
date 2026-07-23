"""Gateway notifier polling service."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from app.notifier.actions import create_action
from app.notifier.config import NotifierSettings
from app.notifier.formatting import format_audit_event, format_digest_summary
from app.notifier.gateway import GatewayAuditClient, GatewayHealthError
from app.notifier.policy import build_dedup_key, classify_event_delivery
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
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        self._telegram = telegram
        self._seen: deque[str] = deque(maxlen=seen_limit)
        self._seen_set: set[str] = set()
        self._prev_health: str | None = None
        self._last_poll_at: str | None = None
        self._events_notified_total = 0
        self._events_suppressed_total = 0

        # Dedup: last_sent_by_key -> timestamp
        self._last_sent: dict[str, float] = {}
        self._clock = clock or _default_clock

        # Realtime dedup window
        self._dedup_window = settings.dedup_window_seconds
        self._max_alerts_per_poll = settings.max_alerts_per_poll

        # Digest state
        self._digest_counts: dict[str, int] = {}
        self._digest_started_at: float | None = None
        self._digest_interval = settings.digest_interval_seconds
        self._digest_total_flushed = 0

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

        # Poll per event_type using event_type filter
        event_notifications = 0
        for event_type in self._settings.event_types:
            if event_notifications >= self._max_alerts_per_poll:
                break
            events = await self._gateway.recent_events(
                limit=self._max_alerts_per_poll - event_notifications,
                event_type=event_type,
            )
            events.reverse()
            count = await self._notify_events(events)
            event_notifications += count

        total = health_notifications + event_notifications
        digest_notifications = await self._flush_digest_if_due()
        total += digest_notifications
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
            "events_suppressed_total": self._events_suppressed_total,
            "dedup_window_seconds": self._dedup_window,
            "dedup_keys_active": len(self._last_sent),
            "prev_health": self._prev_health,
            "digest_counts": dict(self._digest_counts),
            "digest_total_flushed": self._digest_total_flushed,
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
        """Process events with realtime dedup and max_alerts cap."""
        count = 0
        for event in events:
            if count >= self._max_alerts_per_poll:
                break

            event_id = str(event.get("event_id") or "")
            if not event_id or event_id in self._seen_set:
                continue
            self._mark_seen(event_id)

            event_type = str(event.get("event_type") or "")
            delivery = classify_event_delivery(
                event_type,
                self._settings.realtime_event_types,
                self._settings.digest_types,
            )
            if delivery == "skip":
                continue
            if delivery == "digest":
                self._accumulate_digest_event(event_type)
                continue

            # Realtime: check dedup window
            dedup_key = build_dedup_key(event)
            now = self._clock()
            if dedup_key:
                last_sent = self._last_sent.get(dedup_key, 0)
                if now - last_sent < self._dedup_window:
                    self._events_suppressed_total += 1
                    continue
                self._last_sent[dedup_key] = now

            text = format_audit_event(event)
            if not text:
                continue

            reply_markup = None
            event_type = str(event.get("event_type") or "")
            if (
                event_type in self._settings.action_event_types
                and event.get("actor_fingerprint")
                and event.get("source_ip")
            ):
                reply_markup = _build_action_keyboard(
                    event_type=event_type,
                    actor_fingerprint=str(event["actor_fingerprint"]),
                    source_ip=str(event["source_ip"]),
                    request_id=str(event.get("event_id", "")),
                )

            await self._telegram.send_message(text, reply_markup=reply_markup)
            count += 1
        return count

    def _accumulate_digest_event(self, event_type: str) -> None:
        """Accumulate a digest event count. Starts the buffer on first event."""
        now = self._clock()
        if self._digest_started_at is None:
            self._digest_started_at = now
        self._digest_counts[event_type] = self._digest_counts.get(event_type, 0) + 1

    async def _flush_digest_if_due(self) -> int:
        """Send digest if interval elapsed and counts > 0. Returns 1 if sent."""
        if not self._digest_counts or self._digest_started_at is None:
            return 0
        # Skip flush if all counts are zero
        if not any(self._digest_counts.values()):
            self._digest_counts.clear()
            self._digest_started_at = None
            return 0
        now = self._clock()
        if now - self._digest_started_at < self._digest_interval:
            return 0
        text = format_digest_summary(self._digest_counts)
        if not text:
            self._digest_counts.clear()
            self._digest_started_at = None
            return 0
        await self._telegram.send_message(text)
        flushed = sum(self._digest_counts.values())
        self._digest_total_flushed += flushed
        self._digest_counts.clear()
        self._digest_started_at = None
        return 1

    def _mark_seen(self, event_id: str) -> None:
        if len(self._seen) == self._seen.maxlen and self._seen:
            old = self._seen.popleft()
            self._seen_set.discard(old)
        self._seen.append(event_id)
        self._seen_set.add(event_id)


def _build_action_keyboard(
    *,
    event_type: str,
    actor_fingerprint: str,
    source_ip: str,
    request_id: str,
) -> dict[str, Any]:
    """Create inline keyboard with Allow + Deny buttons for operator action."""
    allow_token = create_action(
        action_type="allow_actor",
        actor_fingerprint=actor_fingerprint,
        source_ip=source_ip,
        event_type=event_type,
        request_id=request_id,
    )
    deny_token = create_action(
        action_type="deny_actor",
        actor_fingerprint=actor_fingerprint,
        source_ip=source_ip,
        event_type=event_type,
        request_id=request_id,
    )
    return {
        "inline_keyboard": [
            [
                {"text": "Allow", "callback_data": allow_token},
                {"text": "Deny", "callback_data": deny_token},
            ],
        ],
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _default_clock() -> float:
    """Default clock using time.monotonic."""
    import time
    return time.monotonic()
