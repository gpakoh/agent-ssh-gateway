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
