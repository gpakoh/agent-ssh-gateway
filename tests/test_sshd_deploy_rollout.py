import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker" / "docker-compose.yml"
DEPLOY = ROOT / "scripts" / "deploy-from-registry.sh"


def test_sshd_executor_is_a_versioned_deploy_artifact() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    sshd = compose["services"]["sshd"]
    assert sshd["image"] == "${SSH_GATEWAY_SSHD_IMAGE:-web-ssh-gateway-sshd:latest}"
    assert sshd["deploy"]["resources"]["limits"]["memory"] == "16G"

    text = DEPLOY.read_text(encoding="utf-8")
    assert 'docker pull "$SSHD_REPO:$DEPLOY_TAG"' in text
    assert 'NEW_EXECUTOR_IMAGE=$(repo_digest "$SSHD_REPO:$DEPLOY_TAG")' in text
    assert 'deploy_services "$NEW_GATEWAY_IMAGE" "$NEW_MCP_IMAGE" "$NEW_EXECUTOR_IMAGE"' in text
    assert 'write_state "$NEW_GATEWAY_IMAGE" "$NEW_MCP_IMAGE" "$NEW_EXECUTOR_IMAGE"' in text
    assert 'deploy_services "$PREVIOUS_GATEWAY_IMAGE" "$PREVIOUS_MCP_IMAGE" "$PREVIOUS_SSHD_IMAGE"' in text


def test_sshd_rollout_cannot_be_skipped_when_only_compose_config_changed() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert "Up to date — nothing to deploy." not in text
    assert 'wait_docker_health "ssh-gateway-sshd" ssh-gateway-sshd 120' in text
    assert "{{.HostConfig.Memory}}" in text
    assert "17179869184" in text


def test_first_rollout_raw_image_id_is_bound_to_the_actual_running_executor() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert 'local ref="$1" expected_running_id="$2"' in text
    assert '[ "$ref" = "$expected_running_id" ]' in text
    assert 'validate_sshd_image_ref "$PREVIOUS_SSHD_IMAGE" "$RUNNING_SSHD_ID"' in text
    assert "'sshd_image': '''$3'''" in text

def test_deploy_services_fail_closed_on_each_compose_up() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    deploy_fn = text.split("deploy_services() {", 1)[1].split("\n}", 1)[0]
    compose_lines = [
        line.strip()
        for line in deploy_fn.splitlines()
        if "run_compose_up $COMPOSE up -d --no-deps --no-build" in line
    ]
    assert len(compose_lines) == 4
    assert all(line.endswith("|| return $?") for line in compose_lines)


def test_compose_retry_is_bounded_to_exact_stop_exit_event_failure() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    helper = text.split("run_compose_up() {", 1)[1].split("\n}", 1)[0]
    classifier = text.split("is_transient_compose_failure() {", 1)[1].split("\n}", 1)[0]

    assert 'local max_attempts=2' in helper
    assert 'output=$("$@" 2>&1) || rc=$?' in helper
    assert 'is_transient_compose_failure "$output"' in helper
    assert 'return "$rc"' in helper
    assert '"cannot stop container"' in classifier
    assert '"tried to kill container, but did not receive an exit event"' in classifier


def test_initial_service_deploy_failure_reaches_rollback_path() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    initial = 'if ! deploy_services "$NEW_GATEWAY_IMAGE" "$NEW_MCP_IMAGE" "$NEW_EXECUTOR_IMAGE"; then'
    assert initial in text
    assert "DEPLOY_SERVICES_OK=false" in text
    assert "if $DEPLOY_SERVICES_OK; then" in text
    assert text.index(initial) < text.index("POST_MIGRATION_REVISION=")


def test_rollback_deploy_failure_is_captured_fail_closed() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    rollback = 'if ! deploy_services "$PREVIOUS_GATEWAY_IMAGE" "$PREVIOUS_MCP_IMAGE" "$PREVIOUS_SSHD_IMAGE"; then'
    assert rollback in text
    rollback_window = text[text.index(rollback) : text.index("SCHEMA_ADVANCED=false")]
    assert "Rollback deployment FAILED" in rollback_window
    assert "exit 1" in rollback_window

def _run_compose_retry_harness(tmp_path: Path, mode: str) -> tuple[int, int]:
    text = DEPLOY.read_text(encoding="utf-8")
    classifier = (
        "is_transient_compose_failure() {"
        + text.split("is_transient_compose_failure() {", 1)[1].split("\n}\n", 1)[0]
        + "\n}\n"
    )
    helper = (
        "run_compose_up() {"
        + text.split("run_compose_up() {", 1)[1].split("\n}\n", 1)[0]
        + "\n}\n"
    )
    counter = tmp_path / "count"
    fake = tmp_path / "fake-compose"
    fake.write_text(
        """#!/usr/bin/env bash
set -u
count=0
if [ -f "$COUNT_FILE" ]; then
  count=$(cat "$COUNT_FILE")
fi
count=$((count + 1))
printf '%s' "$count" > "$COUNT_FILE"
case "$MODE" in
  transient-once)
    if [ "$count" -eq 1 ]; then
      echo "cannot stop container abc: tried to kill container, but did not receive an exit event" >&2
      exit 1
    fi
    exit 0
    ;;
  transient-always)
    echo "cannot stop container abc: tried to kill container, but did not receive an exit event" >&2
    exit 1
    ;;
  name-conflict-once)
    if [ "$count" -eq 1 ]; then
      echo 'Error when allocating new name: Conflict. The container name "/ssh-gateway-agent-sshd" is already in use by container "old".' >&2
      exit 1
    fi
    exit 0
    ;;
  other-name-conflict)
    echo 'Error when allocating new name: Conflict. The container name "/mcp-server" is already in use by container "old".' >&2
    exit 1
    ;;
  unrelated)
    echo "permission denied" >&2
    exit 1
    ;;
esac
exit 99
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -euo pipefail\n"
        "log() { :; }\n"
        "sleep() { :; }\n"
        + classifier
        + helper
        + 'run_compose_up "$FAKE"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(harness)],
        env={"PATH": "/usr/bin:/bin", "FAKE": str(fake), "COUNT_FILE": str(counter), "MODE": mode},
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode, int(counter.read_text(encoding="utf-8"))


def test_compose_retry_retries_exact_transient_once(tmp_path: Path) -> None:
    returncode, attempts = _run_compose_retry_harness(tmp_path, "transient-once")
    assert returncode == 0
    assert attempts == 2


def test_compose_retry_persistent_transient_is_bounded(tmp_path: Path) -> None:
    returncode, attempts = _run_compose_retry_harness(tmp_path, "transient-always")
    assert returncode != 0
    assert attempts == 2


def test_compose_retry_retries_exact_agent_name_conflict_once(tmp_path: Path) -> None:
    returncode, attempts = _run_compose_retry_harness(tmp_path, "name-conflict-once")
    assert returncode == 0
    assert attempts == 2


def test_compose_retry_does_not_retry_other_container_name_conflict(tmp_path: Path) -> None:
    returncode, attempts = _run_compose_retry_harness(tmp_path, "other-name-conflict")
    assert returncode != 0
    assert attempts == 1


def test_compose_retry_does_not_retry_unrelated_failure(tmp_path: Path) -> None:
    returncode, attempts = _run_compose_retry_harness(tmp_path, "unrelated")
    assert returncode != 0
    assert attempts == 1

def test_compose_retry_preserves_service_image_environment(tmp_path: Path) -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    classifier = (
        "is_transient_compose_failure() {"
        + text.split("is_transient_compose_failure() {", 1)[1].split("\n}\n", 1)[0]
        + "\n}\n"
    )
    helper = (
        "run_compose_up() {"
        + text.split("run_compose_up() {", 1)[1].split("\n}\n", 1)[0]
        + "\n}\n"
    )
    observed = tmp_path / "observed"
    fake = tmp_path / "fake-compose"
    fake.write_text(
        """#!/usr/bin/env bash
printf '%s|%s|%s' \
  "${SSH_GATEWAY_SSHD_IMAGE:-}" \
  "${WEB_SSH_GATEWAY_IMAGE:-}" \
  "${MCP_SERVER_IMAGE:-}" > "$OBSERVED"
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    harness = tmp_path / "env-harness.sh"
    harness.write_text(
        "set -euo pipefail\n"
        "log() { :; }\n"
        "sleep() { :; }\n"
        + classifier
        + helper
        + 'SSH_GATEWAY_SSHD_IMAGE="sshd@sha256:test" '
        + 'WEB_SSH_GATEWAY_IMAGE="gateway@sha256:test" '
        + 'MCP_SERVER_IMAGE="mcp@sha256:test" '
        + 'run_compose_up "$FAKE"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(harness)],
        env={"PATH": "/usr/bin:/bin", "FAKE": str(fake), "OBSERVED": str(observed)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert observed.read_text(encoding="utf-8") == (
        "sshd@sha256:test|gateway@sha256:test|mcp@sha256:test"
    )

def test_deploy_services_short_circuits_inside_errexit_suppressed_if(tmp_path: Path) -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    classifier = (
        "is_transient_compose_failure() {"
        + text.split("is_transient_compose_failure() {", 1)[1].split("\n}\n", 1)[0]
        + "\n}\n"
    )
    helper = (
        "run_compose_up() {"
        + text.split("run_compose_up() {", 1)[1].split("\n}\n", 1)[0]
        + "\n}\n"
    )
    deploy = (
        "deploy_services() {"
        + text.split("deploy_services() {", 1)[1].split("\n}\n", 1)[0]
        + "\n}\n"
    )
    counter = tmp_path / "count"
    fake = tmp_path / "fake-compose"
    fake.write_text(
        """#!/usr/bin/env bash
count=0
if [ -f "$COUNT_FILE" ]; then
  count=$(cat "$COUNT_FILE")
fi
count=$((count + 1))
printf '%s' "$count" > "$COUNT_FILE"
echo "permission denied" >&2
exit 1
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    harness = tmp_path / "deploy-harness.sh"
    harness.write_text(
        "set -euo pipefail\n"
        "log() { :; }\n"
        "sleep() { :; }\n"
        + classifier
        + helper
        + deploy
        + 'COMPOSE="$FAKE"\n'
        + 'if ! deploy_services "gateway" "mcp" "sshd"; then\n'
        + '  :\n'
        + 'fi\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(harness)],
        env={"PATH": "/usr/bin:/bin", "FAKE": str(fake), "COUNT_FILE": str(counter)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert counter.read_text(encoding="utf-8") == "1"


def test_sshd_image_generates_host_keys_at_runtime_not_build_time() -> None:
    dockerfile = (ROOT / "docker" / "sshd" / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker" / "sshd" / "entrypoint.sh").read_text(encoding="utf-8")

    assert 'ENTRYPOINT ["/usr/local/bin/sshd-entrypoint"]' in dockerfile
    assert "COPY entrypoint.sh /usr/local/bin/sshd-entrypoint" in dockerfile
    assert "ssh-keygen -q -t rsa -b 4096 -f /etc/ssh/hostkeys/ssh_host_rsa_key" not in dockerfile
    assert "ssh-keygen -q -t ecdsa -f /etc/ssh/hostkeys/ssh_host_ecdsa_key" not in dockerfile
    assert "ssh-keygen -q -t ed25519 -f /etc/ssh/hostkeys/ssh_host_ed25519_key" not in dockerfile

    assert 'if [ ! -s "$key_path" ]; then' in entrypoint
    assert 'chmod 600 "$key_path"' in entrypoint
    assert 'exec "$@"' in entrypoint


def test_sshd_entrypoint_preserves_existing_key_and_creates_missing(tmp_path: Path) -> None:
    entrypoint = (ROOT / "docker" / "sshd" / "entrypoint.sh").read_text(encoding="utf-8")
    key_dir = tmp_path / "hostkeys"
    key_dir.mkdir()
    existing = key_dir / "ssh_host_rsa_key"
    existing.write_text("existing-key", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_keygen = fake_bin / "ssh-keygen"
    fake_keygen.write_text(
        """#!/usr/bin/env sh
set -eu
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-f" ]; then
    shift
    out="$1"
    break
  fi
  shift
done
printf 'generated-key' > "$out"
printf 'generated-pub' > "$out.pub"
""",
        encoding="utf-8",
    )
    fake_keygen.chmod(0o755)

    harness = tmp_path / "entrypoint.sh"
    harness.write_text(
        entrypoint.replace(
            "HOSTKEY_DIR=/etc/ssh/hostkeys",
            f"HOSTKEY_DIR={key_dir}",
        ),
        encoding="utf-8",
    )
    harness.chmod(0o755)

    result = subprocess.run(
        [str(harness), "/bin/true"],
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert existing.read_text(encoding="utf-8") == "existing-key"
    assert (key_dir / "ssh_host_ecdsa_key").read_text(encoding="utf-8") == "generated-key"
    assert (key_dir / "ssh_host_ed25519_key").read_text(encoding="utf-8") == "generated-key"
