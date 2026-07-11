import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "app" / "config.py"
ENV_EXAMPLE_PATH = ROOT / ".env.example"


def _read_config_aliases() -> set[str]:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    return set(re.findall(r'alias="([^"]+)"', text))


def _read_env_example_keys() -> set[str]:
    keys: set[str] = set()

    for raw_line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key = line.split("=", 1)[0].strip()

        if key:
            keys.add(key)

    return keys


def _read_env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}

    for raw_line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    return values


def test_env_example_exists() -> None:
    assert ENV_EXAMPLE_PATH.exists(), ".env.example must exist in repository root"


def test_env_example_matches_config_aliases() -> None:
    aliases = _read_config_aliases()
    env_keys = _read_env_example_keys()

    missing = sorted(aliases - env_keys)
    extra = sorted(env_keys - aliases)

    assert not missing, f".env.example is missing config aliases: {missing}"
    assert not extra, f".env.example contains unknown keys: {extra}"


def test_env_example_does_not_contain_obvious_real_secrets() -> None:
    values = _read_env_example_values()

    sensitive_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS")

    allowed_placeholder_prefixes = (
        "",
        "change-me",
        "example",
        "dummy",
        "placeholder",
    )

    for key, value in values.items():
        if not any(marker in key.upper() for marker in sensitive_markers):
            continue

        assert value.startswith(allowed_placeholder_prefixes), (
            f"{key} in .env.example looks like a real secret"
        )


def test_event_hooks_are_disabled_by_default() -> None:
    config_text = CONFIG_PATH.read_text(encoding="utf-8")

    pattern = (
        r"event_hooks_enabled:\s*bool\s*=\s*Field"
        r"\(\s*default=False,\s*alias=\"EVENT_HOOKS_ENABLED\""
    )

    assert re.search(pattern, config_text), (
        "EVENT_HOOKS_ENABLED must default to False. "
        "Event hooks should be enabled explicitly because they require DATABASE_URL."
    )


def test_default_server_configs_are_empty_by_default() -> None:
    config_text = CONFIG_PATH.read_text(encoding="utf-8")

    pattern = (
        r"server_default_configs:\s*str\s*=\s*Field"
        r"\(\s*default=\"\{\}\",\s*alias=\"SERVER_DEFAULT_CONFIGS\""
    )

    assert re.search(pattern, config_text), (
        "SERVER_DEFAULT_CONFIGS must default to an empty JSON object. "
        "Do not hardcode infrastructure hosts in source code."
    )


def test_no_hardcoded_secrets_in_tracked_configs() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_no_hardcoded_secrets.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, (
        f"check_no_hardcoded_secrets.py failed:\n{result.stdout}\n{result.stderr}"
    )
