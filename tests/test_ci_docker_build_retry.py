from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci-docker-build-retry.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
E2E_COLLECTION_TIMEOUT_SECONDS = 60


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
  uv_connect_timeout_then_success)
    if [ "$count" -eq 1 ]; then
      echo 'Failed to download `redis==8.1.0`' >&2
      echo 'client error (Connect)' >&2
      echo 'operation timed out' >&2
      exit 1
    fi
    exit 0
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


def test_uv_connect_timeout_is_classified_as_transient(tmp_path: Path) -> None:
    result, counter = _run_wrapper(tmp_path, "uv_connect_timeout_then_success")

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


def test_ci_e2e_uses_digest_pinned_selenium_sidecar_and_requires_execution_proof() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    image = (
        "ghcr.io/seleniumhq/standalone-chromium:"
        "150.0.7871.114-chromedriver-150.0.7871.114-grid-4.46.0-20260707"
        "@sha256:3400b92f1cddb2dfaaf358654e8f7d83d7be45192fb73c5f28c25faa28d36504"
    )

    assert image in workflow
    assert "Start pinned Selenium Chromium sidecar" in workflow
    assert 'docker inspect "$job_container"' in workflow
    assert '--network "container:${job_container}"' in workflow
    assert 'SELENIUM_REMOTE_URL=http://127.0.0.1:4444/wd/hub' in workflow
    assert "Put preinstalled ChromeDriver on PATH" in workflow
    assert "github.server_url == 'https://github.com'" in workflow
    assert "Stop Selenium Chromium sidecar" in workflow
    assert 'docker rm -f "$E2E_SELENIUM_CONTAINER"' in workflow
    assert "services:\n      selenium:" not in workflow
    assert "steps.browser_check.outputs.available" not in workflow
    assert "uv run pytest tests/test_webui_e2e.py -m e2e -q --junitxml=e2e-results.xml" in workflow
    assert "uv run pytest -m e2e -q --junitxml=e2e-results.xml" not in workflow
    assert "if tests <= 0 or skipped != 0:" in workflow


def test_webui_e2e_remote_mode_collects_all_browser_tests_without_local_toolchain() -> None:
    env = os.environ.copy()
    env["SELENIUM_REMOTE_URL"] = "http://selenium.invalid:4444/wd/hub"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_webui_e2e.py"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=E2E_COLLECTION_TIMEOUT_SECONDS,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert output.count("tests/test_webui_e2e.py::TestWebUiE2E::") == 4, output
    assert "skipped" not in output.lower(), output
