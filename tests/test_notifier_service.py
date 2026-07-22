from app.notifier.config import NotifierSettings
from app.notifier.service import GatewayNotifierService


class FakeGateway:
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def recent_events(self, *, limit=100):
        return list(self.events)

    async def close(self):
        self.closed = True


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.closed = False

    async def send_message(self, text):
        self.messages.append(text)
        return []

    async def close(self):
        self.closed = True


async def test_poll_once_notifies_matching_events_once():
    gateway = FakeGateway(
        [
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
    gateway = FakeGateway([{"event_id": "1", "event_type": "command.deny"}])
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(enabled=False),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    assert await service.poll_once() == 0
    assert telegram.messages == []


async def test_close_closes_clients():
    gateway = FakeGateway([])
    telegram = FakeTelegram()
    service = GatewayNotifierService(
        settings=NotifierSettings(),
        gateway=gateway,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
    )

    await service.close()
    assert gateway.closed is True
    assert telegram.closed is True
