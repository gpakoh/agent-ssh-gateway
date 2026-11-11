"""Gate gateway deploy on the real external proxy-parser network name.

All subprocess tests use a private fake Docker executable: they do not touch
host networks, Compose projects, images or any production secrets.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker" / "docker-compose.yml"
PRECHECK = ROOT / "scripts" / "deploy-proxy-network-preflight.sh"
DEPLOY = ROOT / "scripts" / "deploy-from-registry.sh"


def test_proxy_parser_worker_network_has_explicit_real_name_and_no_creation():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert compose["networks"]["proxy-parser_default"] == {
        "external": True,
        "name": "${GATEWAY_PROXY_PARSER_NETWORK:-proxy-parser_default}",
    }
    for service in ("sshd", "agent-sshd"):
        assert "proxy-parser_default" in compose["services"][service]["networks"]
    # The OAuth application is not attached to the proxy egress network.
    assert "proxy-parser_default" not in compose["services"]["mcp-oauth"]["networks"]


def _run_fake_network_preflight(
    tmp_path: Path, *, available: str, configured: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "docker-calls.txt"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$DOCKER_CALLS"\n'
        'if [ "$#" -eq 3 ] && [ "$1" = network ] && '
        '[ "$2" = inspect ] && [ "$3" = "$AVAILABLE_NETWORK" ]; then\n'
        '  exit 0\n'
        'fi\n'
        'exit 1\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{fake_bin}:{env.get('PATH', '')}",
        DOCKER_CALLS=str(calls),
        AVAILABLE_NETWORK=available,
    )
    env.pop("GATEWAY_PROXY_PARSER_NETWORK", None)
    if configured is not None:
        env["GATEWAY_PROXY_PARSER_NETWORK"] = configured

    result = subprocess.run(
        ["bash", str(PRECHECK)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return result, calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


@pytest.mark.parametrize(
    ("configured", "available", "expected"),
    [
        (None, "proxy-parser_default", "proxy-parser_default"),
        ("proxy_parser_default", "proxy_parser_default", "proxy_parser_default"),
    ],
)
def test_existing_network_is_inspected_read_only(tmp_path, configured, available, expected):
    result, calls = _run_fake_network_preflight(
        tmp_path, available=available, configured=configured
    )
    assert result.returncode == 0, result.stderr
    assert "preflight: OK" in result.stdout
    assert calls == [f"network inspect {expected}"]


def test_missing_network_fails_before_any_mutation(tmp_path):
    result, calls = _run_fake_network_preflight(
        tmp_path, available="missing-operator-network"
    )
    assert result.returncode != 0
    assert calls == ["network inspect proxy-parser_default"]
    assert "NO-GO" in result.stderr
    assert "no rollout attempted" in result.stderr
    assert "proxy-parser_default" not in result.stderr


@pytest.mark.parametrize("invalid", ["proxy-parser_default;echo hi", "a/b", " ", "a" * 129])
def test_malformed_network_name_fails_without_invoking_docker(tmp_path, invalid):
    result, calls = _run_fake_network_preflight(
        tmp_path, available="proxy-parser_default", configured=invalid
    )
    assert result.returncode != 0
    assert calls == []
    assert "NO-GO (invalid network name)" in result.stderr
    if invalid.strip():
        assert invalid not in result.stderr


def test_deploy_scripts_are_valid_bash():
    for path in (PRECHECK, DEPLOY):
        result = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr


def test_deploy_preflights_before_pulls_and_never_synthesizes_network():
    text = DEPLOY.read_text(encoding="utf-8")
    precheck = 'bash "$ROOT/scripts/deploy-proxy-network-preflight.sh"'
    first_pull = 'docker pull "$GATEWAY_REPO:$DEPLOY_TAG"'
    assert precheck in text
    assert text.index(precheck) < text.index(first_pull)
    assert 'if [[ "$COMPOSE" == "$BASE_COMPOSE"* ]]; then' in text
    source = PRECHECK.read_text(encoding="utf-8")
    assert 'docker network inspect "$proxy_network"' in source
    assert "docker network create" not in source
    assert "docker network connect" not in source
