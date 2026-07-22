from __future__ import annotations

from typing import Any

from app.notifier.config import NotifierSettings
from app.notifier.service import GatewayNotifierService


class FakeGateway:
    def __init__(self, events: list[dict[str, Any]] | None = None, health: str = "ok"):
        self.events = events or []
        self.health_sequence: list[str] = [health]
        self._health_idx = 0
        self.closed = False

    async def recent_events(self, *, limit: int = 100):
        return list(self.events)

    async def health(self) -> dict[str, Any]:
        status = self.health_sequence[
            min(self._health_idx, len(self.health_sequence) - 1)
        ]
        self._health_idx += 1
        return {"status": status, "ready": status == "ok", "version": "test"}

    async def close(self):
        self.closed = True


class FakeTelegram:
    def __init__(self):
        self.messages: list[str] = []
        self.closed = False

    async def send_message(self, text: str):
        self.messages.append(text)
        return []

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Existing event-notification tests
# ---------------------------------------------------------------------------


async def test_poll_once_notifies_matching_events_once():
    gateway = FakeGateway(
        events=[
            {"event_id": "2", "event_type": "file.read"},
            {"event_id": "1", "event_type": "command.deny", "metadata": {"command_root": "rm"}},
        ]
    )
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key"),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    assert await service.poll_once() == 1
    assert await service.poll_once() == 0
    assert len(telegram.messages) == 1
    assert "rm" in telegram.messages[0]


async def test_poll_once_disabled_is_noop():
    gateway = FakeGateway(events=[{"event_id": "1", "event_type": "command.deny"}])
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=False),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    assert await service.poll_once() == 0
    assert telegram.messages == []


async def test_close_closes_clients():
    gateway = FakeGateway()
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.close()
    assert gateway.closed is True
    assert telegram.closed is True


# ---------------------------------------------------------------------------
# Health transition tests
# ---------------------------------------------------------------------------


async def test_first_ok_sends_nothing():
    """First poll records baseline ok; no Telegram message."""
    gateway = FakeGateway(health="ok")
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key"),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.poll_once()
    assert service._prev_health == "ok"
    assert telegram.messages == []


async def test_ok_to_degraded_sends_one_alert():
    """ok → degraded sends exactly one health.degraded message."""
    gateway = FakeGateway()
    gateway.health_sequence = ["ok", "degraded"]
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key"),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.poll_once()  # first poll: ok baseline
    await service.poll_once()  # second poll: degraded

    assert service._prev_health == "degraded"
    assert len(telegram.messages) == 1
    assert "health.degraded" in telegram.messages[0]
    assert "ok" in telegram.messages[0]
    assert "degraded" in telegram.messages[0]
    assert "Gateway Status" in telegram.messages[0]
    assert "version" in telegram.messages[0]


async def test_degraded_to_degraded_no_duplicate():
    """degraded → degraded sends no duplicate message."""
    gateway = FakeGateway()
    gateway.health_sequence = ["ok", "degraded", "degraded"]
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key"),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.poll_once()  # ok baseline
    await service.poll_once()  # → degraded (sends)
    await service.poll_once()  # degraded → degraded (no send)

    assert len(telegram.messages) == 1
    assert "health.degraded" in telegram.messages[0]


async def test_degraded_to_ok_sends_recovered():
    """degraded → ok sends exactly one health.recovered message."""
    gateway = FakeGateway()
    gateway.health_sequence = ["ok", "degraded", "ok"]
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key"),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.poll_once()  # ok baseline
    await service.poll_once()  # → degraded (sends)
    await service.poll_once()  # → ok (sends recovered)

    assert service._prev_health == "ok"
    assert len(telegram.messages) == 2
    assert "health.degraded" in telegram.messages[0]
    assert "health.recovered" in telegram.messages[1]
    assert "Gateway Status" in telegram.messages[1]


async def test_unreachable_to_ok_sends_recovered():
    """non-ok → ok sends recovered."""
    gateway = FakeGateway()
    gateway.health_sequence = ["ok", "unreachable", "ok"]
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key"),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.poll_once()  # ok baseline
    await service.poll_once()  # → unreachable (sends degraded)
    await service.poll_once()  # → ok (sends recovered)

    assert len(telegram.messages) == 2
    assert "health.degraded" in telegram.messages[0]
    assert "health.recovered" in telegram.messages[1]
    assert "Gateway Status" in telegram.messages[1]


async def test_unreachable_to_degraded_no_notification():
    """non-ok → non-ok (unreachable → degraded) sends no notification."""
    gateway = FakeGateway()
    gateway.health_sequence = ["ok", "degraded", "unreachable", "degraded"]
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key"),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.poll_once()  # ok baseline
    await service.poll_once()  # → degraded (sends)
    await service.poll_once()  # → unreachable (non-ok → non-ok, no send)
    await service.poll_once()  # → degraded (non-ok → non-ok, no send)

    assert len(telegram.messages) == 1
    assert "health.degraded" in telegram.messages[0]



async def test_status_returns_snapshot():
    gateway = FakeGateway(health="ok")
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key"),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    snapshot = await service.status()

    assert snapshot["gateway_health"]["status"] == "ok"
    assert snapshot["last_poll_at"] is None
    assert snapshot["events_notified_total"] == 0
    assert snapshot["prev_health"] is None


async def test_status_reflects_poll_state_and_notification_count():
    gateway = FakeGateway()
    gateway.health_sequence = ["ok", "degraded", "degraded"]
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key"),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.poll_once()
    await service.poll_once()
    snapshot = await service.status()

    assert snapshot["last_poll_at"]
    assert snapshot["events_notified_total"] == 1
    assert snapshot["prev_health"] == "degraded"
