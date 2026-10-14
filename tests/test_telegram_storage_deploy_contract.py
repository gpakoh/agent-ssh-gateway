from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from examples.mcp_client_remote.fleet.docker_client import DockerClient, RunResult
from examples.mcp_server.mcp_infra.adapters import docker as docker_adapter

_RUNNER_SPEC = importlib.util.spec_from_file_location(
    "telegram_storage_deploy_contract_runner",
    Path("scripts/telegram_storage_deploy_contract_runner.py"),
)
assert _RUNNER_SPEC is not None and _RUNNER_SPEC.loader is not None
runner = importlib.util.module_from_spec(_RUNNER_SPEC)
_RUNNER_SPEC.loader.exec_module(runner)


REPO = "192.168.1.103:3005/gpakoh/telegram-storage-service"
TARGET_IMAGE = f"{REPO}@sha256:{'1' * 64}"
PRIOR_IMAGE = f"{REPO}@sha256:{'2' * 64}"
HELPER_IMAGE_ID = f"sha256:{'3' * 64}"
STATE_VOLUME = "ssh-gateway-telegram-storage-deploy-state"
HEAD = "a" * 40
BLOB = "b" * 40
SCRIPT_SHA256 = "c" * 64
PRIOR_STARTED_AT = "2026-10-13T15:23:28.015688487Z"
DEPLOYED_AT = "2026-10-14T10:00:00+00:00"


class _ConfirmationStore:
    def __init__(self) -> None:
        self.created: dict[str, object] | None = None

    def create_action(
        self,
        tool: str,
        kwargs: dict[str, object],
        summary: str,
        *,
        risk: str = "medium",
        required_scope: str = "mcp:docker",
    ) -> SimpleNamespace:
        self.created = {
            "tool": tool,
            "kwargs": kwargs,
            "summary": summary,
            "risk": risk,
            "required_scope": required_scope,
        }
        return SimpleNamespace(
            tool=tool,
            kwargs=kwargs,
            summary=summary,
            risk=risk,
            required_scope=required_scope,
            action_id="action-1",
            confirm_token="confirm-1",
            owner_fingerprint="owner-1",
            created_at=time.monotonic(),
        )


def _prior_runtime(*, image: str = PRIOR_IMAGE, health: str = "healthy") -> dict[str, object]:
    return {
        "mode": "present",
        "container_id": "container-1",
        "image_id": f"sha256:{'4' * 64}",
        "image": image,
        "restart_count": 0,
        "started_at": PRIOR_STARTED_AT,
        "health": health,
        "compose_config_hash": "config-hash-1",
    }


def _plan(*, state_volume_fingerprint: str = "sha256:volume") -> dict[str, object]:
    return {
        "candidate_root": "/workspace/infra-candidate",
        "operator_root": "/workspace/infra-quart",
        "script_path": "/workspace/infra-candidate/scripts/deploy-telegram-storage-service.sh",
        "env_file": "/workspace/infra-quart/.env",
        "state_volume": STATE_VOLUME,
        "state_volume_fingerprint": state_volume_fingerprint,
        "state_fingerprint": "sha256:state-before",
        "state": {"state_exists": False, "state": {}},
        "helper_image_id": HELPER_IMAGE_ID,
        "target_image": TARGET_IMAGE,
        "prior_mode": "present",
        "prior_runtime": _prior_runtime(),
        "runtime_fingerprint": "sha256:runtime-before",
        "bootstrap_state": {
            "telegram_storage_service_image": PRIOR_IMAGE,
            "deployed_at": PRIOR_STARTED_AT,
        },
    }


def test_operator_env_validation_ignores_unrelated_shared_env_syntax(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "UNRELATED='unterminated\n"
        "export TELEGRAM_BOT_TOKEN='bot-token'\n"
        "TELEGRAM_STORAGE_API_TOKEN=storage-token\n"
        "TELEGRAM_STORAGE_CHAT_ID=-1001234567890\n",
        encoding="utf-8",
    )

    docker_adapter._telegram_storage_validate_operator_env(env_file)


def test_operator_env_validation_rejects_duplicate_required_assignment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=first\n"
        "TELEGRAM_BOT_TOKEN=second\n"
        "TELEGRAM_STORAGE_API_TOKEN=storage-token\n"
        "TELEGRAM_STORAGE_CHAT_ID=-1001234567890\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN exactly once"):
        docker_adapter._telegram_storage_validate_operator_env(env_file)


@pytest.mark.parametrize(
    ("assignment", "match"),
    [
        ("TELEGRAM_STORAGE_API_TOKEN=   ", "invalid TELEGRAM_STORAGE_API_TOKEN"),
        ("TELEGRAM_STORAGE_CHAT_ID=0", "invalid TELEGRAM_STORAGE_CHAT_ID"),
    ],
)
def test_operator_env_validation_rejects_invalid_required_value(
    tmp_path: Path,
    assignment: str,
    match: str,
) -> None:
    env_file = tmp_path / ".env"
    values = {
        "TELEGRAM_BOT_TOKEN": "bot-token",
        "TELEGRAM_STORAGE_API_TOKEN": "storage-token",
        "TELEGRAM_STORAGE_CHAT_ID": "-1001234567890",
    }
    key, value = assignment.split("=", 1)
    values[key] = value
    env_file.write_text(
        "\n".join(f"{name}={item}" for name, item in values.items()) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=match):
        docker_adapter._telegram_storage_validate_operator_env(env_file)


@pytest.mark.asyncio
async def test_preflight_confirmation_uses_fixed_state_volume_and_exposes_no_operator_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ConfirmationStore()
    monkeypatch.setattr(docker_adapter, "_confirm_store", lambda: store)
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_telegram_storage_deploy",
        AsyncMock(return_value=_plan()),
    )

    signature = inspect.signature(docker_adapter.docker_deploy_contract)
    assert "state_volume" not in signature.parameters

    result = await docker_adapter.docker_deploy_contract(
        contract="telegram-storage",
        candidate_project="candidate-infra",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA256,
        target_image=TARGET_IMAGE,
    )

    assert result["ok"] is True
    assert result["result"]["status"] == "confirmation_required"
    assert store.created is not None
    assert store.created["tool"] == "docker_deploy_contract"
    assert store.created["risk"] == "high"
    assert store.created["required_scope"] == "mcp:docker:admin"
    action_kwargs = store.created["kwargs"]
    assert isinstance(action_kwargs, dict)
    assert action_kwargs["contract"] == "telegram-storage"
    confirmation = action_kwargs["telegram_storage_confirmation"]
    assert isinstance(confirmation, dict)
    assert confirmation["state_volume"] == STATE_VOLUME
    assert action_kwargs["target_image"] == TARGET_IMAGE
    serialized = json.dumps(action_kwargs, sort_keys=True)
    assert ".env" not in serialized
    assert "TELEGRAM_BOT_TOKEN" not in serialized
    assert "TELEGRAM_STORAGE_API_TOKEN" not in serialized


@pytest.mark.asyncio
async def test_generic_contract_dispatch_preserves_telegram_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegated = AsyncMock(return_value={"ok": True, "result": {"status": "accepted"}})
    monkeypatch.setattr(
        docker_adapter,
        "_docker_deploy_telegram_storage_contract_impl",
        delegated,
    )
    confirmation = {
        "state_volume": STATE_VOLUME,
        "state_volume_fingerprint": "sha256:volume",
        "state_fingerprint": "sha256:state-before",
        "prior_mode": "present",
        "runtime_fingerprint": "sha256:runtime-before",
    }

    result = await docker_adapter._docker_deploy_contract_impl(
        contract="telegram-storage",
        candidate_project="candidate-infra",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA256,
        target_image=TARGET_IMAGE,
        timeout=300,
        telegram_storage_confirmation=confirmation,
    )

    assert result["ok"] is True
    delegated.assert_awaited_once_with(
        candidate_project="candidate-infra",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA256,
        target_image=TARGET_IMAGE,
        state_volume=STATE_VOLUME,
        state_volume_fingerprint="sha256:volume",
        state_fingerprint="sha256:state-before",
        prior_mode="present",
        runtime_fingerprint="sha256:runtime-before",
        timeout=300,
    )


def test_rollback_bootstrap_requires_immutable_healthy_live_generation() -> None:
    bootstrap = docker_adapter._telegram_storage_bootstrap_state(
        repo=REPO,
        state={"state_exists": False, "state": {}},
        prior_mode="present",
        prior_runtime=_prior_runtime(),
    )
    assert bootstrap == {
        "telegram_storage_service_image": PRIOR_IMAGE,
        "deployed_at": PRIOR_STARTED_AT,
    }

    with pytest.raises(RuntimeError, match="immutable repository digest"):
        docker_adapter._telegram_storage_bootstrap_state(
            repo=REPO,
            state={"state_exists": False, "state": {}},
            prior_mode="present",
            prior_runtime=_prior_runtime(image=f"{REPO}:latest"),
        )

    with pytest.raises(RuntimeError, match="not Docker-healthy"):
        docker_adapter._telegram_storage_bootstrap_state(
            repo=REPO,
            state={"state_exists": False, "state": {}},
            prior_mode="present",
            prior_runtime=_prior_runtime(health="unhealthy"),
        )


def test_existing_lkg_must_match_live_rollback_generation() -> None:
    assert (
        docker_adapter._telegram_storage_bootstrap_state(
            repo=REPO,
            state={
                "state_exists": True,
                "state": {
                    "telegram_storage_service_image": PRIOR_IMAGE,
                    "deployed_at": "older",
                },
            },
            prior_mode="present",
            prior_runtime=_prior_runtime(),
        )
        is None
    )

    with pytest.raises(RuntimeError, match="durable LKG differs"):
        docker_adapter._telegram_storage_bootstrap_state(
            repo=REPO,
            state={
                "state_exists": True,
                "state": {
                    "telegram_storage_service_image": TARGET_IMAGE,
                    "deployed_at": "older",
                },
            },
            prior_mode="present",
            prior_runtime=_prior_runtime(),
        )


@pytest.mark.asyncio
async def test_confirmation_revalidates_runtime_and_state_before_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = _plan()
    changed["runtime_fingerprint"] = "sha256:runtime-changed"
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_telegram_storage_deploy",
        AsyncMock(return_value=changed),
    )
    docker_client = AsyncMock()
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: docker_client)

    result = await docker_adapter._docker_deploy_telegram_storage_contract_impl(
        candidate_project="candidate-infra",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA256,
        target_image=TARGET_IMAGE,
        state_volume=STATE_VOLUME,
        state_volume_fingerprint="sha256:volume",
        state_fingerprint="sha256:state-before",
        prior_mode="present",
        runtime_fingerprint="sha256:runtime-before",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_PRECONDITION_FAILED"
    docker_client.run_telegram_storage_deploy_contract_helper.assert_not_called()


@pytest.mark.asyncio
async def test_first_rollout_bootstraps_lkg_then_verifies_durable_and_live_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume_metadata = {
        "Name": STATE_VOLUME,
        "Driver": "local",
        "Scope": "local",
        "Options": {},
        "Labels": {},
    }
    volume_fingerprint = docker_adapter._telegram_storage_resource_fingerprint(volume_metadata)
    plan = _plan(state_volume_fingerprint=volume_fingerprint)
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_telegram_storage_deploy",
        AsyncMock(return_value=plan),
    )

    helper_payload = {
        "version": 1,
        "exit_code": 0,
        "output_tail": "deploy ok",
        "state": {
            "telegram_storage_service_image": TARGET_IMAGE,
            "deployed_at": DEPLOYED_AT,
        },
    }
    client = SimpleNamespace(
        run_telegram_storage_deploy_contract_helper=AsyncMock(
            return_value=RunResult(stdout=json.dumps(helper_payload), stderr="", exit_code=0)
        ),
        volume_metadata=AsyncMock(return_value=volume_metadata),
        _sanitize_string=lambda value: value,
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    monkeypatch.setattr(
        docker_adapter,
        "_telegram_storage_read_state",
        AsyncMock(
            return_value=(
                {
                    "state_exists": True,
                    "state": {
                        "telegram_storage_service_image": TARGET_IMAGE,
                        "deployed_at": DEPLOYED_AT,
                    },
                },
                "sha256:state-after",
            )
        ),
    )
    live = {
        "container_id": "container-2",
        "image_id": f"sha256:{'5' * 64}",
        "image": TARGET_IMAGE,
        "restart_count": 0,
        "started_at": "2026-10-14T10:00:01Z",
        "health": "healthy",
        "compose_config_hash": "config-hash-2",
    }
    verify_live = AsyncMock(side_effect=[live, live])
    monkeypatch.setattr(docker_adapter, "_verify_telegram_storage_live", verify_live)
    sleep = AsyncMock()
    monkeypatch.setattr(docker_adapter.asyncio, "sleep", sleep)

    result = await docker_adapter._docker_deploy_telegram_storage_contract_impl(
        candidate_project="candidate-infra",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA256,
        target_image=TARGET_IMAGE,
        state_volume=STATE_VOLUME,
        state_volume_fingerprint=volume_fingerprint,
        state_fingerprint="sha256:state-before",
        prior_mode="present",
        runtime_fingerprint="sha256:runtime-before",
    )

    assert result["ok"] is True
    assert result["result"]["output_tail"] == "deploy ok"
    helper_kwargs = client.run_telegram_storage_deploy_contract_helper.await_args.kwargs
    assert helper_kwargs["state_volume"] == STATE_VOLUME
    assert helper_kwargs["bootstrap_image"] == PRIOR_IMAGE
    assert helper_kwargs["bootstrap_deployed_at"] == PRIOR_STARTED_AT
    assert helper_kwargs["target_image"] == TARGET_IMAGE
    assert sleep.await_args.args == (docker_adapter._TELEGRAM_STORAGE_VERIFY_STABILITY_SECONDS,)
    assert verify_live.await_count == 2


@pytest.mark.asyncio
async def test_timeout_is_ambiguous_and_never_marked_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_telegram_storage_deploy",
        AsyncMock(return_value=plan),
    )
    client = SimpleNamespace(
        run_telegram_storage_deploy_contract_helper=AsyncMock(
            return_value=RunResult(stdout="", stderr="timed out", exit_code=-1)
        ),
        _sanitize_string=lambda value: value,
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter._docker_deploy_telegram_storage_contract_impl(
        candidate_project="candidate-infra",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA256,
        target_image=TARGET_IMAGE,
        state_volume=STATE_VOLUME,
        state_volume_fingerprint="sha256:volume",
        state_fingerprint="sha256:state-before",
        prior_mode="present",
        runtime_fingerprint="sha256:runtime-before",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS"
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_docker_client_helper_uses_fixed_runner_and_bounded_mounts() -> None:
    client = DockerClient()
    captured = AsyncMock(return_value=RunResult(stdout="{}", stderr="", exit_code=0))
    client._run_with_result = captured  # type: ignore[method-assign]

    await client.run_telegram_storage_deploy_contract_helper(
        helper_image_id=HELPER_IMAGE_ID,
        candidate_root="/workspace/infra-candidate",
        operator_root="/workspace/infra-quart",
        script_path="/workspace/infra-candidate/scripts/deploy-telegram-storage-service.sh",
        env_file="/workspace/infra-quart/.env",
        state_volume=STATE_VOLUME,
        target_image=TARGET_IMAGE,
        bootstrap_image=PRIOR_IMAGE,
        bootstrap_deployed_at=PRIOR_STARTED_AT,
        timeout=300,
    )

    argv = captured.await_args.args[0]
    assert argv[0:3] == ["/usr/bin/docker", "run", "--rm"]
    assert argv[argv.index("--network") + 1] == "none"
    assert "/app/scripts/telegram_storage_deploy_contract_runner.py" in argv
    assert "type=bind,src=/workspace/infra-candidate,dst=/workspace/infra-candidate,readonly" in argv
    assert "type=bind,src=/workspace/infra-quart,dst=/workspace/infra-quart,readonly" in argv
    assert f"type=volume,src={STATE_VOLUME},dst=/var/lib/telegram-storage-deploy-state" in argv
    assert "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock" in argv
    assert argv[argv.index("--bootstrap-image") + 1] == PRIOR_IMAGE
    assert argv[argv.index("--bootstrap-deployed-at") + 1] == PRIOR_STARTED_AT
    assert "TELEGRAM_STORAGE_API_TOKEN" not in " ".join(argv)


def test_runner_strips_inherited_telegram_contract_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    script = source_root / "deploy.sh"
    script.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    env_file = tmp_path / "operator.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=operator\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> object:
        captured["env"] = kwargs["env"]
        return runner.subprocess.CompletedProcess(args[0], 1, stdout="failed", stderr=None)

    monkeypatch.setenv("TELEGRAM_STORAGE_API_TOKEN", "inherited-storage-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "inherited-bot-token")
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setattr(runner.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "telegram_storage_deploy_contract_runner.py",
            "--script",
            str(script),
            "--source-root",
            str(source_root),
            "--env-file",
            str(env_file),
            "--state-dir",
            str(state_dir),
            "--target-image",
            TARGET_IMAGE,
            "--timeout",
            "30",
        ],
    )

    assert runner.main() == 1
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "TELEGRAM_STORAGE_API_TOKEN" not in child_env
    assert "TELEGRAM_BOT_TOKEN" not in child_env
    assert "TELEGRAM_BOT_ENABLED" not in child_env
    assert child_env["TELEGRAM_STORAGE_SERVICE_TARGET_IMAGE"] == TARGET_IMAGE
    assert child_env["TELEGRAM_STORAGE_OPERATOR_ENV_FILE"] == str(env_file)
    assert child_env["TELEGRAM_STORAGE_DEPLOY_STATE_DIR"] == str(state_dir)


def test_runner_bootstrap_is_exclusive_and_records_only_lkg_identity(tmp_path: Path) -> None:
    state_file = tmp_path / "telegram-storage-service-deploy.json"
    runner._bootstrap_state(state_file, image=PRIOR_IMAGE, deployed_at=PRIOR_STARTED_AT)
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "telegram_storage_service_image": PRIOR_IMAGE,
        "deployed_at": PRIOR_STARTED_AT,
    }
    with pytest.raises(RuntimeError, match="appeared before bootstrap"):
        runner._bootstrap_state(state_file, image=TARGET_IMAGE, deployed_at=DEPLOYED_AT)


def test_oauth_compose_owns_fixed_telegram_storage_state_volume() -> None:
    compose = Path("docker/docker-compose.yml").read_text(encoding="utf-8")
    assert "telegram_storage_deploy_state:/var/lib/telegram-storage-deploy-state" in compose
    assert "name: ssh-gateway-telegram-storage-deploy-state" in compose
