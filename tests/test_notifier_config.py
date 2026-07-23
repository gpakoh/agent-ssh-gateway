from app.notifier.config import NotifierSettings, _parse_bool, _parse_csv, _parse_float


def test_parse_bool_defaults_and_truthy():
    assert _parse_bool(None, default=True) is True
    assert _parse_bool("", default=False) is False
    assert _parse_bool("true") is True
    assert _parse_bool("1") is True
    assert _parse_bool("no") is False


def test_parse_float_falls_back_for_invalid_values():
    assert _parse_float("2.5", default=1.0) == 2.5
    assert _parse_float("bad", default=1.0) == 1.0
    assert _parse_float("-3", default=1.0) == 1.0


def test_parse_csv_strips_empty_values():
    assert _parse_csv("1, 2,,3 ") == ("1", "2", "3")


def test_settings_defaults_are_safe():
    settings = NotifierSettings()
    assert settings.enabled is False
    assert settings.dry_run is True
    assert settings.can_send_telegram is False
    assert settings.can_poll_gateway is False


def test_settings_real_send_requires_all_switches():
    settings = NotifierSettings(
        enabled=True,
        dry_run=False,
        telegram_token="token",
        telegram_chat_ids=("chat",),
    )
    assert settings.can_send_telegram is True


def test_settings_loads_optional_proxy_from_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_NOTIFIER_PROXY", "http://proxy.example.invalid:3128")

    settings = NotifierSettings.from_env()

    assert settings.proxy == "http://proxy.example.invalid:3128"
