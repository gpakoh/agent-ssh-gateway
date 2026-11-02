"""Executable fault-injection for the production Docker-health transition gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy-from-registry.sh"


def _exercise(
    *,
    docker_health_at: int,
    app_ready: bool = True,
    initial_status: str = "unhealthy",
) -> subprocess.CompletedProcess[str]:
    source = SCRIPT.read_text(encoding="utf-8")
    functions = "dump_health_failure_diagnostics() {" + source.split(
        "dump_health_failure_diagnostics() {", 1
    )[1].split("\nsmoke() {", 1)[0]
    # Bash-only stubs: fake seconds and health transitions, never touch Docker.
    harness = """
set -euo pipefail
T=0
HEALTH_AT="$1"
APP_READY="$2"
INITIAL_STATUS="$3"
date() { if [[ "${1:-}" == '+%s' ]]; then printf '%s\\n' "$T"; else command date "$@"; fi; }
sleep() { T=$((T + $1)); }
docker() {
  if [[ "$1" == 'inspect' ]]; then
    if (( T >= HEALTH_AT )); then echo healthy
    else echo "$INITIAL_STATUS"; fi
    return 0
  fi
  if [[ "$1" == 'exec' ]]; then
    if [[ "$APP_READY" == '1' ]]; then
      printf '%s\\n' '{"status":"ok","ready":true}'
      return 0
    fi
    printf '%s\\n' '{"status":"degraded","ready":false}'
    return 1
  fi
  return 1
}
"""
    cmd = harness + "\n" + functions + "\nwait_docker_health web-ssh-gateway web-ssh-gateway 120"
    return subprocess.run(
        ["bash", "-c", cmd, "test", str(docker_health_at), "1" if app_ready else "0", initial_status],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


@pytest.mark.parametrize(
    ("healthy_at", "app_ready", "expected_rc"),
    [(126, True, 0), (999, True, 1), (126, False, 1), (0, True, 0)],
)
def test_gateway_requires_docker_health_with_bounded_ready_grace(
    healthy_at: int, app_ready: bool, expected_rc: int
) -> None:
    result = _exercise(docker_health_at=healthy_at, app_ready=app_ready)
    assert result.returncode == expected_rc, (result.stdout, result.stderr)
    if expected_rc == 0:
        assert "OK" in result.stdout
    else:
        assert "FAIL" in result.stdout


def test_missing_container_is_never_accepted_from_api_health() -> None:
    result = _exercise(docker_health_at=999, initial_status="missing")
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "FAIL" in result.stdout
