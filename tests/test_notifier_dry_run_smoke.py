from __future__ import annotations

from typing import Any

import scripts.notifier_dry_run_smoke as smoke


def test_build_settings_forces_dry_run():
    settings = smoke._build_settings(
        base_url="http://localhost:8085",
        api_key="api-key",
        timeout_seconds=2.0,
    )

    assert settings.enabled is True
    assert settings.dry_run is True
    assert settings.can_poll_gateway is True
    assert settings.can_send_telegram is False
    assert settings.telegram_chat_ids == ("dry-run",)


async def test_run_smoke_uses_dry_run_telegram(monkeypatch):
    created_telegram_kwargs: dict[str, Any] = {}

    class FakeGateway:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeTelegram:
        def __init__(self, **kwargs):
            created_telegram_kwargs.update(kwargs)

    class FakeService:
        def __init__(self, *, settings, gateway, telegram):
            self.settings = settings
            self.gateway = gateway
            self.telegram = telegram
            self.closed = False

        async def status(self):
            return {
                "gateway_health": {"status": "ok"},
                "events_notified_total": 1,
                "prev_health": "ok",
            }

        async def poll_once(self):
            return 1

        async def close(self):
            self.closed = True

    monkeypatch.setattr(smoke, "GatewayAuditClient", FakeGateway)
    monkeypatch.setattr(smoke, "TelegramClient", FakeTelegram)
    monkeypatch.setattr(smoke, "GatewayNotifierService", FakeService)

    result = await smoke.run_smoke(
        base_url="http://localhost:8085",
        api_key="api-key",
        timeout_seconds=2.0,
    )

    assert result["ok"] is True
    assert result["telegram_delivery"] == "dry_run"
    assert result["notifications_attempted"] == 1
    assert created_telegram_kwargs["dry_run"] is True
    assert created_telegram_kwargs["token"] == ""
