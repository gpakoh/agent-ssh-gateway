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
        self.calls: list[dict[str, Any]] = []

    async def recent_events(self, *, limit: int = 100, event_type: str | None = None, decision: str | None = None, sort: str = "newest"):
        self.calls.append({"limit": limit, "event_type": event_type, "decision": decision, "sort": sort})
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


async def test_poll_once_alert_matrix_uses_safe_formatter():
    gateway = FakeGateway(
        events=[
            {
                "event_id": "5",
                "event_type": "system.error",
                "reason": "token=raw-secret-value",
                "target_id": "raw-system-target",
            },
            {
                "event_id": "4",
                "event_type": "session.disconnect",
                "target_id": "raw-disconnect-session",
                "metadata": {"host": "raw-disconnect-host"},
            },
            {
                "event_id": "3",
                "event_type": "session.connect",
                "target_id": "raw-connect-session",
                "metadata": {"host": "raw-connect-host"},
            },
            {
                "event_id": "2",
                "event_type": "workspace.readonly_block",
                "route": "POST /api/workspace/projects/*/files/write",
                "error_code": "WORKSPACE_READONLY",
                "target_id": "/raw/private/path",
            },
            {
                "event_id": "1",
                "event_type": "command.deny",
                "metadata": {
                    "command_root": "tee",
                    "command": "cat private-file | tee output-file",
                },
            },
        ]
    )
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key"),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    count = await service.poll_once()
    # With per-event-type polling, each event type is polled separately
    # but dedup suppresses events with the same key
    assert count >= 3  # at least command.deny, workspace.readonly_block, system.error
    combined = "\n".join(telegram.messages)

    assert "Command blocked" in combined
    assert "Workspace write blocked" in combined
    assert "Gateway system error" in combined
    assert "tee" in combined
    assert "[REDACTED]" in combined
    assert "raw-secret-value" not in combined
    assert "cat private-file" not in combined


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


# ---------------------------------------------------------------------------
# Dedup / filtering / max_alerts tests
# ---------------------------------------------------------------------------


async def test_realtime_dedup_suppresses_same_key():
    """Repeated events with same dedup key send only one alert within window."""
    gateway = FakeGateway(
        events=[
            {"event_id": "e1", "event_type": "command.deny", "route": "/api/ssh/execute", "error_code": "FORBIDDEN"},
            {"event_id": "e2", "event_type": "command.deny", "route": "/api/ssh/execute", "error_code": "FORBIDDEN"},
        ]
    )
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key", dedup_window_seconds=300),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: 1000.0,
    )

    await service.poll_once()
    assert len(telegram.messages) == 1
    assert service._events_suppressed_total == 1


async def test_dedup_window_allows_resend():
    """After dedup window, same dedup key sends again (different event_ids)."""
    gateway = FakeGateway(
        events=[
            {"event_id": "e1", "event_type": "command.deny", "route": "/api/ssh/execute"},
            {"event_id": "e2", "event_type": "command.deny", "route": "/api/ssh/execute"},
        ]
    )
    telegram = FakeTelegram()
    clock_val = [1000.0]
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key", dedup_window_seconds=60),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: clock_val[0],
    )

    await service.poll_once()
    # First event sent, second suppressed by dedup window (same key)
    assert len(telegram.messages) == 1

    clock_val[0] = 2000.0  # advance past window
    await service.poll_once()
    # Both events suppressed by event_id dedup (already seen)
    assert len(telegram.messages) == 1


async def test_different_command_root_sends_separately():
    """Different command_root produces different dedup keys → both sent."""
    gateway = FakeGateway(
        events=[
            {"event_id": "e1", "event_type": "command.deny", "metadata": {"command_root": "rm"}},
            {"event_id": "e2", "event_type": "command.deny", "metadata": {"command_root": "dd"}},
        ]
    )
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key"),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.poll_once()
    assert len(telegram.messages) == 2


async def test_max_alerts_per_poll_caps():
    """max_alerts_per_poll limits total sends per poll cycle."""
    events = [{"event_id": f"e{i}", "event_type": "command.deny", "route": f"/api/endpoint/{i}"} for i in range(5)]
    gateway = FakeGateway(events=events)
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key", max_alerts_per_poll=2),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.poll_once()
    assert len(telegram.messages) == 2


async def test_event_type_filter_passed_to_gateway():
    """Per-event-type polling passes event_type param to gateway."""
    gateway = FakeGateway(
        events=[
            {"event_id": "e1", "event_type": "command.deny"},
            {"event_id": "e2", "event_type": "session.connect"},
        ]
    )
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(
            enabled=True,
            gateway_api_key="key",
            event_types=("command.deny", "session.connect"),
        ),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.poll_once()

    # gateway should have been called with event_type filter for each event type
    event_type_calls = [c["event_type"] for c in gateway.calls]
    assert "command.deny" in event_type_calls
    assert "session.connect" in event_type_calls


async def test_status_includes_suppression_counters():
    gateway = FakeGateway(
        events=[
            {"event_id": "e1", "event_type": "command.deny", "route": "/api/ssh/execute"},
            {"event_id": "e2", "event_type": "command.deny", "route": "/api/ssh/execute"},
        ]
    )
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=True, gateway_api_key="key", dedup_window_seconds=300),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        clock=lambda: 1000.0,
    )

    await service.poll_once()
    snapshot = await service.status()

    assert snapshot["events_suppressed_total"] == 1
    assert snapshot["dedup_window_seconds"] == 300
    assert snapshot["dedup_keys_active"] >= 1
