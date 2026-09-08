from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci-docker-build-retry.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _fake_docker(tmp_path: Path, mode: str) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "count"
    fake = bin_dir / "docker"
    fake.write_text(
        """#!/usr/bin/env bash
set -u
count=0
if [ -f "$FAKE_DOCKER_COUNTER" ]; then count=$(cat "$FAKE_DOCKER_COUNTER"); fi
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_DOCKER_COUNTER"
printf '%s\n' "$*" >> "$FAKE_DOCKER_ARGS"
case "$FAKE_DOCKER_MODE" in
  transient_then_success)
    if [ "$count" -eq 1 ]; then
      echo 'failed to authorize: net/http: TLS handshake timeout' >&2
      exit 1
    fi
    exit 0
    ;;
  persistent_transient)
    echo 'failed to fetch anonymous token: TLS handshake timeout' >&2
    exit 1
    ;;
  deterministic_failure)
    echo 'Dockerfile:42: unknown instruction: BROKEN' >&2
    exit 1
    ;;
  *)
    echo "unknown fake mode: $FAKE_DOCKER_MODE" >&2
    exit 9
    ;;
esac
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    args_log = tmp_path / "args.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "FAKE_DOCKER_MODE": mode,
            "FAKE_DOCKER_COUNTER": str(counter),
            "FAKE_DOCKER_ARGS": str(args_log),
            "CI_DOCKER_BUILD_MAX_ATTEMPTS": "3",
            "CI_DOCKER_BUILD_RETRY_DELAY_SECONDS": "0",
        }
    )
    return env, counter


def _run_wrapper(tmp_path: Path, mode: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    env, counter = _fake_docker(tmp_path, mode)
    result = subprocess.run(
        ["bash", str(SCRIPT), "-f", "Dockerfile.test", "--build-arg", "X=1", "."],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return result, counter


def test_transient_registry_failure_retries_and_recovers(tmp_path: Path) -> None:
    result, counter = _run_wrapper(tmp_path, "transient_then_success")

    assert result.returncode == 0
    assert counter.read_text(encoding="utf-8").strip() == "2"
    assert "transient network/registry error; retrying" in result.stderr


def test_deterministic_build_failure_is_not_retried(tmp_path: Path) -> None:
    result, counter = _run_wrapper(tmp_path, "deterministic_failure")

    assert result.returncode == 1
    assert counter.read_text(encoding="utf-8").strip() == "1"
    assert "non-retryable error; not retrying" in result.stderr


def test_persistent_registry_failure_exhausts_bounded_retries(tmp_path: Path) -> None:
    result, counter = _run_wrapper(tmp_path, "persistent_transient")

    assert result.returncode == 1
    assert counter.read_text(encoding="utf-8").strip() == "3"
    assert "failed after 3/3 transient-network attempts" in result.stderr


def test_ci_routes_all_image_builds_through_bounded_retry_wrapper() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count("bash scripts/ci-docker-build-retry.sh -f") == 3
    assert "          docker build -f" not in workflow
