from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker" / "docker-compose.notifier.yml"
MAIN_COMPOSE_PATH = ROOT / "docker" / "docker-compose.yml"
RUNBOOK_PATH = ROOT / "docs" / "operations" / "NOTIFIER.md"


def _load_compose():
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _load_main_compose():
    return yaml.safe_load(MAIN_COMPOSE_PATH.read_text(encoding="utf-8"))


def test_notifier_compose_exists():
    assert COMPOSE_PATH.exists()


def test_notifier_compose_is_internal_only():
    service = _load_compose()["services"]["gateway-notifier"]
    assert service["networks"] == {"internal_net": {}}
    assert "ports" not in service


def test_notifier_compose_defaults_disabled_and_dry_run():
    env = _load_compose()["services"]["gateway-notifier"]["environment"]
    assert "GATEWAY_NOTIFIER_ENABLED=${GATEWAY_NOTIFIER_ENABLED:-false}" in env
    assert "GATEWAY_NOTIFIER_DRY_RUN=${GATEWAY_NOTIFIER_DRY_RUN:-true}" in env


def test_notifier_compose_has_hardened_runtime():
    service = _load_compose()["services"]["gateway-notifier"]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert any(str(item).startswith("/tmp:") for item in service["tmpfs"])


def test_notifier_compose_uses_notifier_module_command():
    service = _load_compose()["services"]["gateway-notifier"]
    assert service["command"] == ["python", "-m", "app.notifier"]


# ---------------------------------------------------------------------------
# Main compose contract tests
# ---------------------------------------------------------------------------


def test_main_compose_has_no_notifier_service():
    """Main docker-compose.yml must NOT contain gateway-notifier."""
    main = _load_main_compose()
    assert "gateway-notifier" not in main.get("services", {})


def test_overlay_defaults_are_safe():
    """Overlay must have enabled=false and dry_run=true by default."""
    env = _load_compose()["services"]["gateway-notifier"]["environment"]
    env_map = {}
    for item in env:
        if "=" in item:
            key, _, value = item.partition("=")
            env_map[key] = value
    assert env_map.get("GATEWAY_NOTIFIER_ENABLED") == "${GATEWAY_NOTIFIER_ENABLED:-false}"
    assert env_map.get("GATEWAY_NOTIFIER_DRY_RUN") == "${GATEWAY_NOTIFIER_DRY_RUN:-true}"


def test_runbook_dry_run_smoke_requires_private_api_key():
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    assert "GATEWAY_NOTIFIER_API_KEY=<gateway-api-key>" in text
    assert "Telegram is forced to `dry_run`" in text


def test_runbook_real_send_uses_ignored_env_file():
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    assert ".env.notifier.real-send" in text
    assert "docker compose --env-file .env.notifier.real-send" in text
    assert "do not commit" in text.lower()


def test_runbook_has_no_invalid_compose_env_flag():
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    assert "-e GATEWAY_NOTIFIER_ENABLED" not in text
