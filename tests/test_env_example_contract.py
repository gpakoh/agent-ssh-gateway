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


def test_public_hygiene_scan_has_no_public_repo_topology_hints() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_public_hygiene.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, (
        f"check_public_hygiene.py failed:\n{result.stdout}\n{result.stderr}"
    )


def _run_hygiene_scan() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_public_hygiene.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def _is_gitignored_by_scan(relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", relative_path],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return result.returncode == 0


def _write_probe(relative_path: str) -> None:
    probe = ROOT / relative_path
    probe.write_text('HOST = "192.168.1.103:3005/gpakoh/example"\n', encoding="utf-8")
    assert not _is_gitignored_by_scan(relative_path), (
        f"{relative_path} must be visible to the scan, not gitignored"
    )


def _probe_leak_is_detected(relative_path: str) -> None:
    """Scanner must reject the probe and name it in its report.

    The report intentionally redacts values ("values intentionally omitted"), so
    the assertion targets the file path and category rather than the address.
    """
    _write_probe(relative_path)
    try:
        result = _run_hygiene_scan()
        assert result.returncode != 0, (
            f"public hygiene scan accepted an internal RFC1918 address in "
            f"{relative_path}:\n{result.stdout}\n{result.stderr}"
        )
        assert relative_path in result.stdout, (
            f"scan output does not name {relative_path}:\n{result.stdout}"
        )
        assert "ip-literal" in result.stdout, (
            f"scan output does not categorise the finding:\n{result.stdout}"
        )
        assert "192.168.1.103" not in result.stdout, (
            f"scan output must not echo the leaked address:\n{result.stdout}"
        )
    finally:
        (ROOT / relative_path).unlink(missing_ok=True)


def test_public_hygiene_scan_rejects_internal_ip_in_examples_tree() -> None:
    # The probe must sit in a nested directory: git pathspec ``examples/**/*.py``
    # matches every tracked example file, but does not match a file created
    # directly in ``examples/``.
    _probe_leak_is_detected("examples/mcp_server/_hygiene_probe_tmp.py")


def test_public_hygiene_scan_out_of_scope_tree_is_not_a_finding() -> None:
    """Pin the scope boundary.

    ``tests/`` is deliberately out of scope: the suite legitimately uses IP
    literals as fixtures for access-control, IP-pinning and host-key logic
    (502 findings across ~60 files). Widening the scan there produces only
    false positives and would bury the real signals.
    """
    _write_probe("tests/_hygiene_probe_tmp.py")
    try:
        result = _run_hygiene_scan()
        assert "tests/_hygiene_probe_tmp.py" not in result.stdout, (
            f"tests/ must stay outside the public hygiene scope:\n{result.stdout}"
        )
    finally:
        (ROOT / "tests/_hygiene_probe_tmp.py").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Live overlay contract tests
# ---------------------------------------------------------------------------


def _is_gitignored(path: Path) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", str(path.relative_to(ROOT))],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return result.returncode == 0


def test_live_overlay_is_gitignored() -> None:
    live = ROOT / "docker" / "docker-compose.live.yml"
    assert _is_gitignored(live), (
        "docker-compose.live.yml must be gitignored (contains private network topology)"
    )


def test_docker_env_is_gitignored() -> None:
    env = ROOT / "docker" / ".env"
    assert _is_gitignored(env), (
        "docker/.env must be gitignored (contains secrets)"
    )


def test_live_example_is_tracked() -> None:
    example = ROOT / "docker" / "docker-compose.live.example.yml"
    assert not _is_gitignored(example), (
        "docker-compose.live.example.yml should be tracked (public template)"
    )


def test_main_compose_has_no_hardcoded_private_values() -> None:
    compose = ROOT / "docker" / "docker-compose.yml"
    content = compose.read_text(encoding="utf-8")
    forbidden = ["10.10.10.", "192.168.", "/media/1TB/", "docker_macvlan_example"]
    found = [p for p in forbidden if p in content]
    assert not found, (
        f"docker-compose.yml contains hardcoded private values: {found}"
    )
