"""Tests for session lifecycle digest in GatewayNotifierService."""

from __future__ import annotations

from typing import Any

from app.notifier.config import NotifierSettings
from app.notifier.formatting import format_digest_summary
from app.notifier.service import GatewayNotifierService


class FakeGateway:
    def __init__(self, events: list[dict[str, Any]] | None = None, health: str = "ok"):
        self.events = events or []
        self.health_sequence: list[str] = [health]
        self._health_idx = 0
        self.closed = False

    async def recent_events(self, *, limit: int = 100, event_type: str | None = None, decision: str | None = None, sort: str = "newest"):
        if event_type:
            return [e for e in self.events if e.get("event_type") == event_type]
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

    async def send_message(self, text: str, *, reply_markup=None):
        self.messages.append(text)
        return []

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Digest formatter tests
# ---------------------------------------------------------------------------


def test_format_digest_summary_basic():
    text = format_digest_summary({"session.connect": 3, "session.disconnect": 2})
    assert "[INFO]" in text
    assert "Session activity digest" in text
    assert "session.connect: <code>3</code>" in text
    assert "session.disconnect: <code>2</code>" in text


def test_format_digest_summary_empty():
    assert format_digest_summary({}) is None


def test_format_digest_summary_no_pii():
    text = format_digest_summary({"session.connect": 5})
    assert "192.168" not in text
    assert "10.0." not in text
    assert "session_" not in text
    assert "target_" not in text
    assert "root@" not in text


def test_format_digest_summary_zero_counts_omitted():
    text = format_digest_summary({"session.connect": 0, "session.disconnect": 0})
    assert text is None


def test_format_digest_summary_partial():
    text = format_digest_summary({"session.connect": 1})
    assert "session.connect: <code>1</code>" in text
    assert "session.disconnect" not in text


# ---------------------------------------------------------------------------
# Digest buffer behavior tests
# ---------------------------------------------------------------------------


async def test_connect_disconnect_events_do_not_send_immediate_individual_alerts():
    """Digest events are accumulated, not sent immediately."""
    gateway = FakeGateway(
        events=[
            {"event_id": "e1", "event_type": "session.connect"},
            {"event_id": "e2", "event_type": "session.connect"},
            {"event_id": "e3", "event_type": "session.disconnect"},
        ]
    )
    telegram = FakeTelegram()
    clock_val = [1000.0]
    service = GatewayNotifierService(
        settings=NotifierSettings(
            enabled=True,
            gateway_api_key="key",
            digest_interval_seconds=300,
            digest_types=("session.connect", "session.disconnect"),
            event_types=("session.connect", "session.disconnect"),
        ),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: clock_val[0],
    )

    await service.poll_once()

    # No Telegram messages — digest not yet flushed (interval not elapsed)
    assert len(telegram.messages) == 0
    # But counts are accumulated
    assert service._digest_counts["session.connect"] == 2
    assert service._digest_counts["session.disconnect"] == 1


async def test_digest_sends_one_summary_after_interval():
    """After interval elapses, one digest message is sent."""
    gateway = FakeGateway(
        events=[
            {"event_id": "e1", "event_type": "session.connect"},
            {"event_id": "e2", "event_type": "session.connect"},
            {"event_id": "e3", "event_type": "session.disconnect"},
        ]
    )
    telegram = FakeTelegram()
    clock_val = [1000.0]
    service = GatewayNotifierService(
        settings=NotifierSettings(
            enabled=True,
            gateway_api_key="key",
            digest_interval_seconds=300,
            digest_types=("session.connect", "session.disconnect"),
            event_types=("session.connect", "session.disconnect"),
        ),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: clock_val[0],
    )

    await service.poll_once()  # accumulate, no send
    assert len(telegram.messages) == 0

    clock_val[0] = 1400.0  # advance past 300s interval
    await service.poll_once()  # flush digest

    assert len(telegram.messages) == 1
    assert "Session activity digest" in telegram.messages[0]
    assert "session.connect: <code>2</code>" in telegram.messages[0]
    assert "session.disconnect: <code>1</code>" in telegram.messages[0]


async def test_digest_contains_counts_only_no_pii():
    """Digest message contains counts only — no host, IP, user, session_id."""
    gateway = FakeGateway(
        events=[
            {
                "event_id": "e1",
                "event_type": "session.connect",
                "target_id": "raw-session-123",
                "metadata": {"host": "10.0.0.1", "username": "root"},
            },
        ]
    )
    telegram = FakeTelegram()
    clock_val = [1000.0]
    service = GatewayNotifierService(
        settings=NotifierSettings(
            enabled=True,
            gateway_api_key="key",
            digest_interval_seconds=300,
            digest_types=("session.connect",),
            event_types=("session.connect",),
        ),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: clock_val[0],
    )

    await service.poll_once()
    clock_val[0] = 1400.0
    await service.poll_once()

    text = telegram.messages[0]
    assert "10.0.0.1" not in text
    assert "root" not in text
    assert "raw-session-123" not in text
    assert "session.connect: <code>1</code>" in text


async def test_command_deny_still_sends_realtime():
    """command.deny is realtime, not digested."""
    gateway = FakeGateway(
        events=[
            {"event_id": "e1", "event_type": "command.deny", "metadata": {"command_root": "rm"}},
        ]
    )
    telegram = FakeTelegram()
    clock_val = [1000.0]
    service = GatewayNotifierService(
        settings=NotifierSettings(
            enabled=True,
            gateway_api_key="key",
            digest_interval_seconds=300,
            digest_types=("session.connect", "session.disconnect"),
            event_types=("command.deny", "session.connect"),
        ),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: clock_val[0],
    )

    await service.poll_once()

    # command.deny sent immediately
    assert len(telegram.messages) == 1
    assert "Command blocked" in telegram.messages[0]


async def test_health_degraded_still_sends_realtime():
    """health.degraded is realtime, not digested."""
    gateway = FakeGateway()
    gateway.health_sequence = ["ok", "degraded"]
    telegram = FakeTelegram()
    clock_val = [1000.0]
    service = GatewayNotifierService(
        settings=NotifierSettings(
            enabled=True,
            gateway_api_key="key",
            digest_interval_seconds=300,
            digest_types=("session.connect",),
            event_types=("session.connect",),
        ),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: clock_val[0],
    )

    await service.poll_once()  # ok baseline
    await service.poll_once()  # → degraded

    assert len(telegram.messages) == 1
    assert "health.degraded" in telegram.messages[0]


async def test_digest_resets_after_flush():
    """After digest flush, counts reset and next interval starts fresh."""
    gateway = FakeGateway(
        events=[
            {"event_id": "e1", "event_type": "session.connect"},
            {"event_id": "e2", "event_type": "session.connect"},
        ]
    )
    telegram = FakeTelegram()
    clock_val = [1000.0]
    service = GatewayNotifierService(
        settings=NotifierSettings(
            enabled=True,
            gateway_api_key="key",
            digest_interval_seconds=300,
            digest_types=("session.connect",),
            event_types=("session.connect",),
        ),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: clock_val[0],
    )

    # First cycle
    await service.poll_once()
    clock_val[0] = 1400.0
    await service.poll_once()
    assert len(telegram.messages) == 1
    assert "session.connect: <code>2</code>" in telegram.messages[0]

    # Second cycle — fresh gateway events, new event_ids
    gateway.events = [
        {"event_id": "e3", "event_type": "session.connect"},
    ]
    await service.poll_once()
    clock_val[0] = 1800.0
    await service.poll_once()
    assert len(telegram.messages) == 2
    assert "session.connect: <code>1</code>" in telegram.messages[1]


async def test_digest_total_flushed_counter():
    """digest_total_flushed tracks total individual events flushed."""
    gateway = FakeGateway(
        events=[
            {"event_id": "e1", "event_type": "session.connect"},
            {"event_id": "e2", "event_type": "session.disconnect"},
            {"event_id": "e3", "event_type": "session.disconnect"},
        ]
    )
    telegram = FakeTelegram()
    clock_val = [1000.0]
    service = GatewayNotifierService(
        settings=NotifierSettings(
            enabled=True,
            gateway_api_key="key",
            digest_interval_seconds=300,
            digest_types=("session.connect", "session.disconnect"),
            event_types=("session.connect", "session.disconnect"),
        ),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: clock_val[0],
    )

    await service.poll_once()
    clock_val[0] = 1400.0
    await service.poll_once()

    snapshot = await service.status()
    assert snapshot["digest_total_flushed"] == 3  # 1 connect + 2 disconnect


async def test_digest_status_includes_counts():
    """status() includes digest_counts and digest_total_flushed."""
    gateway = FakeGateway(
        events=[
            {"event_id": "e1", "event_type": "session.connect"},
        ]
    )
    telegram = FakeTelegram()
    clock_val = [1000.0]
    service = GatewayNotifierService(
        settings=NotifierSettings(
            enabled=True,
            gateway_api_key="key",
            digest_interval_seconds=300,
            digest_types=("session.connect",),
            event_types=("session.connect",),
        ),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: clock_val[0],
    )

    await service.poll_once()  # accumulate
    snapshot = await service.status()

    assert snapshot["digest_counts"] == {"session.connect": 1}
    assert snapshot["digest_total_flushed"] == 0


async def test_no_events_no_digest():
    """If no digest events arrive, no digest is sent."""
    gateway = FakeGateway(events=[])
    telegram = FakeTelegram()
    clock_val = [1000.0]
    service = GatewayNotifierService(
        settings=NotifierSettings(
            enabled=True,
            gateway_api_key="key",
            digest_interval_seconds=300,
            digest_types=("session.connect",),
            event_types=("session.connect",),
        ),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: clock_val[0],
    )

    clock_val[0] = 2000.0  # far in the future
    await service.poll_once()

    assert len(telegram.messages) == 0
    assert service._digest_counts == {}
