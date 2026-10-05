from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))

from examples.mcp_client_remote.fleet.docker_client import (  # noqa: E402
    DockerClient,
    RunResult,
)
from examples.mcp_server.mcp_infra.adapters import docker as docker_adapter  # noqa: E402

HEAD = "f" * 40
BLOB = "a" * 40
SCRIPT_SHA = "b" * 64
IMAGE_REPO = "198.51.100.10/gpakoh/gpt-browser-bridge"
TARGET = IMAGE_REPO + "@sha256:" + "c" * 64
TARGET_ID = "sha256:" + "d" * 64
CONTAINER_ID = "e" * 64
GENERATION = "f" * 64
FINGERPRINT = "1" * 64


@pytest.fixture(autouse=True)
def _trusted_image_repo(monkeypatch):
    """Fail-closed env contract: the deploy adapter has no built-in registry."""
    monkeypatch.setenv("GPT_BRIDGE_IMAGE_REPO", IMAGE_REPO)


@pytest.fixture(autouse=True)
def _mcp_started():
    import examples.mcp_server.server as srv

    if not hasattr(srv, "_mcp_started_at"):
        srv._mcp_started_at = time.time()
    srv._confirm_store._actions.clear()
    yield
    srv._confirm_store._actions.clear()


def _plan() -> dict:
    return {
        "spec": dict(docker_adapter._DEPLOY_CONTRACTS["gpt-browser-bridge"]),
        "candidate_root": "/media/1TB/Python/.mcp-candidate-clones/bridge",
        "infra_root": "/media/1TB/Python/quart-platform/infra-quart",
        "script_path": (
            "/media/1TB/Python/.mcp-candidate-clones/bridge/"
            "deploy/deploy-gpt-browser-bridge.sh"
        ),
        "state_file": (
            "/media/1TB/Python/quart-platform/infra-quart/"
            ".gpt-browser-bridge-state/deploy.json"
        ),
        "target_image": TARGET,
        "target_image_id": TARGET_ID,
        "helper_image_id": "sha256:" + "2" * 64,
        "profile_mountpoint": (
            "/var/lib/docker/volumes/"
            "infra-quart_gpt-browser-bridge-profile/_data"
        ),
    }


def _state() -> dict:
    return {
        "gpt_browser_bridge_image": TARGET,
        "gpt_browser_bridge_image_id": TARGET_ID,
        "gpt_browser_bridge_compose_config_fingerprint": FINGERPRINT,
        "gpt_browser_bridge_container_id": CONTAINER_ID,
        "gpt_browser_bridge_deploy_generation": GENERATION,
        "deployed_at": "2026-09-30T00:00:00+00:00",
    }


def _inspect() -> list[dict]:
    return [
        {
            "Id": CONTAINER_ID,
            "Image": TARGET_ID,
            "RestartCount": 0,
            "Config": {
                "Image": TARGET,
                "Labels": {
                    "io.xloud.gpt-browser-bridge.deploy-generation": GENERATION
                },
            },
            "State": {
                "StartedAt": "2026-09-30T00:00:01Z",
                "Health": {"Status": "healthy", "FailingStreak": 0},
            },
        }
    ]


class FakeClient:
    def __init__(self, run: RunResult) -> None:
        self.run = run
        self.inspect_calls = 0

    def _sanitize_string(self, value: str) -> str:
        return value

    async def run_deploy_contract_helper(self, **kwargs):
        self.run_kwargs = kwargs
        return self.run

    async def inspect(self, name: str, max_lines: int = 10):
        assert name == "infra-quart-gpt-browser-bridge-1"
        assert max_lines == 10
        self.inspect_calls += 1
        return _inspect()


@pytest.mark.asyncio
async def test_deploy_contract_requires_high_risk_confirmation(monkeypatch):
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_deploy_contract",
        AsyncMock(return_value=_plan()),
    )

    result = await docker_adapter.docker_deploy_contract(
        contract="gpt-browser-bridge",
        candidate_project="candidate-gpt-browser-bridge-release",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        target_image=TARGET,
        timeout=480,
    )

    assert result["ok"] is True
    pending = result["result"]
    assert pending["status"] == "confirmation_required"
    assert pending["risk"] == "high"

    import examples.mcp_server.server as srv

    action, _status = srv._confirm_store.peek_action_id(pending["action_id"])
    assert action is not None
    assert action.tool == "docker_deploy_contract"
    assert action.required_scope == "mcp:docker:admin"
    assert action.kwargs["expected_head_sha"] == HEAD
    assert action.kwargs["target_image"] == TARGET


@pytest.mark.asyncio
async def test_deploy_contract_rejects_mutable_image_before_workspace_lookup():
    with pytest.raises(ValueError, match="immutable digest"):
        await docker_adapter._prepare_deploy_contract(
            contract="gpt-browser-bridge",
            candidate_project="unused",
            expected_head_sha=HEAD,
            expected_script_blob_sha=BLOB,
            expected_script_sha256=SCRIPT_SHA,
            target_image=IMAGE_REPO + ":latest",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("unset_value", [None, "", "   "])
async def test_deploy_contract_fails_closed_without_trusted_image_repo(
    monkeypatch, unset_value
):
    if unset_value is None:
        monkeypatch.delenv("GPT_BRIDGE_IMAGE_REPO", raising=False)
    else:
        monkeypatch.setenv("GPT_BRIDGE_IMAGE_REPO", unset_value)

    with pytest.raises(ValueError, match="GPT_BRIDGE_IMAGE_REPO"):
        await docker_adapter._prepare_deploy_contract(
            contract="gpt-browser-bridge",
            candidate_project="unused",
            expected_head_sha=HEAD,
            expected_script_blob_sha=BLOB,
            expected_script_sha256=SCRIPT_SHA,
            target_image=TARGET,
        )


@pytest.mark.asyncio
async def test_deploy_contract_rejects_image_outside_trusted_repo(monkeypatch):
    monkeypatch.setenv("GPT_BRIDGE_IMAGE_REPO", IMAGE_REPO)
    with pytest.raises(ValueError, match="immutable digest"):
        await docker_adapter._prepare_deploy_contract(
            contract="gpt-browser-bridge",
            candidate_project="unused",
            expected_head_sha=HEAD,
            expected_script_blob_sha=BLOB,
            expected_script_sha256=SCRIPT_SHA,
            target_image="203.0.113.9/other/gpt-browser-bridge@sha256:" + "c" * 64,
        )


@pytest.mark.asyncio
async def test_confirmed_deploy_revalidates_and_requires_stable_evidence(monkeypatch):
    payload = {
        "version": 1,
        "exit_code": 0,
        "output_tail": "deploy ok",
        "state": _state(),
    }
    client = FakeClient(RunResult(json.dumps(payload), "", 0))
    prepare = AsyncMock(return_value=_plan())
    sleep = AsyncMock()
    monkeypatch.setattr(docker_adapter, "_prepare_deploy_contract", prepare)
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    monkeypatch.setattr(docker_adapter.asyncio, "sleep", sleep)

    result = await docker_adapter._docker_deploy_contract_impl(
        contract="gpt-browser-bridge",
        candidate_project="candidate-gpt-browser-bridge-release",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        target_image=TARGET,
        timeout=480,
    )

    assert result["ok"] is True
    assert prepare.await_count == 1
    assert client.inspect_calls == 2
    sleep.assert_awaited_once_with(docker_adapter._DEPLOY_STABILITY_SECONDS)
    deployment = result["result"]["deployment"]
    assert deployment["image"] == TARGET
    assert deployment["image_id"] == TARGET_ID
    assert deployment["restart_count"] == 0
    assert deployment["deploy_generation"] == GENERATION


@pytest.mark.asyncio
async def test_outer_helper_timeout_is_ambiguous_not_failed_evidence(monkeypatch):
    client = FakeClient(
        RunResult("", "Command timed out after 480.0s", -1)
    )
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_deploy_contract",
        AsyncMock(return_value=_plan()),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter._docker_deploy_contract_impl(
        contract="gpt-browser-bridge",
        candidate_project="candidate-gpt-browser-bridge-release",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        target_image=TARGET,
        timeout=480,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS"
    assert result["error"]["retryable"] is True
    assert result["result"]["target_image"] == TARGET


@pytest.mark.asyncio
async def test_docker_client_helper_uses_fixed_operator_mounts(monkeypatch):
    client = DockerClient()
    run = AsyncMock(return_value=RunResult('{"exit_code":0}', "", 0))
    monkeypatch.setattr(client, "_run_with_result", run)

    await client.run_deploy_contract_helper(
        helper_image_id="sha256:" + "2" * 64,
        candidate_root="/media/1TB/Python/.mcp-candidate-clones/bridge",
        infra_root="/media/1TB/Python/quart-platform/infra-quart",
        script_path=(
            "/media/1TB/Python/.mcp-candidate-clones/bridge/"
            "deploy/deploy-gpt-browser-bridge.sh"
        ),
        state_file=(
            "/media/1TB/Python/quart-platform/infra-quart/"
            ".gpt-browser-bridge-state/deploy.json"
        ),
        image_env="GPT_BRIDGE_TARGET_IMAGE",
        image_ref=TARGET,
        profile_volume="infra-quart_gpt-browser-bridge-profile",
        profile_mountpoint=(
            "/var/lib/docker/volumes/"
            "infra-quart_gpt-browser-bridge-profile/_data"
        ),
        timeout=480,
    )

    argv = run.await_args.args[0]
    assert argv[:2] == ["/usr/bin/docker", "run"]
    assert ["--network", "host"] == argv[argv.index("--network") : argv.index("--network") + 2]
    assert ["--user", "0:0"] == argv[argv.index("--user") : argv.index("--user") + 2]
    assert "--read-only" in argv
    cap_add_index = argv.index("--cap-add")
    assert argv[cap_add_index + 1] == "DAC_OVERRIDE"
    mounts = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "--mount"]
    assert "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock" in mounts
    assert any("candidate-clones/bridge" in mount and "readonly" in mount for mount in mounts)
    assert any("infra-quart" in mount and "readonly" not in mount for mount in mounts)
    assert any(
        mount.startswith(
            "type=volume,src=infra-quart_gpt-browser-bridge-profile,"
        )
        for mount in mounts
    )
    assert "/app/scripts/deploy_contract_runner.py" in argv
