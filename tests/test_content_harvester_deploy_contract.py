from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

HEAD = "4" * 40
BLOB = "a" * 40
SCRIPT_SHA = "b" * 64
NAMESPACE = "registry.example/gpakoh"
TARGET_IMAGE = f"{NAMESPACE}/content-harvester-service:{HEAD}"
TARGET_IMAGE_ID = "sha256:" + "1" * 64
PREVIOUS_IMAGE_ID = "sha256:" + "9" * 64
PREVIOUS_REVISION = "3" * 40
CANDIDATE = "candidate-content-harvester-release"
HELPER_ID = "sha256:" + "2" * 64
ARTIFACT_VOLUME = "content-harvester-artifacts"
RUNTIME_NETWORK = "infra-quart_infra-net"
GENERATION = HEAD[:12] + "-20261011T010203Z"


@pytest.fixture(autouse=True)
def _clear_confirm_store():
    import examples.mcp_server.server as srv

    srv._confirm_store._actions.clear()
    yield
    srv._confirm_store._actions.clear()


def _volume(name: str) -> dict:
    return {"Name": name, "Driver": "local", "Scope": "local", "Options": {}, "Labels": {}}


def _network() -> dict:
    return {
        "Name": RUNTIME_NETWORK,
        "Id": "n" * 64,
        "Driver": "bridge",
        "Scope": "local",
        "Internal": False,
        "Attachable": False,
        "Ingress": False,
        "Options": {},
        "Labels": {},
    }


def _plan(*, prior_mode: str = "absent", topology: str = "sha256:" + "a" * 64) -> dict:
    state_volume = _volume(docker_adapter._CONTENT_HARVESTER_STATE_VOLUME)
    artifact = _volume(ARTIFACT_VOLUME)
    network = _network()
    return {
        "candidate_root": "/candidate/content-harvester",
        "operator_root": "/operator/content-harvester",
        "script_path": "/candidate/content-harvester/deploy-content-harvester.sh",
        "env_file": "/operator/content-harvester/.env",
        "state_volume": docker_adapter._CONTENT_HARVESTER_STATE_VOLUME,
        "state_volume_fingerprint": docker_adapter._content_harvester_resource_fingerprint(
            "volume", state_volume
        ),
        "artifact_volume": ARTIFACT_VOLUME,
        "artifact_volume_fingerprint": docker_adapter._content_harvester_resource_fingerprint(
            "volume", artifact
        ),
        "runtime_network": RUNTIME_NETWORK,
        "runtime_network_fingerprint": docker_adapter._content_harvester_resource_fingerprint(
            "network", network
        ),
        "helper_image_id": HELPER_ID,
        "source_revision": HEAD,
        "image_namespace": NAMESPACE,
        "target_image": TARGET_IMAGE,
        "prior_mode": prior_mode,
        "topology_fingerprint": topology,
    }


def _state() -> dict:
    return {
        "schema_version": 1,
        "source_revision": HEAD,
        "deploy_generation": GENERATION,
        "image": TARGET_IMAGE,
        "image_id": TARGET_IMAGE_ID,
        "artifact_volume": ARTIFACT_VOLUME,
        "runtime_network": RUNTIME_NETWORK,
        "stability_seconds": 60,
        "services": ["api", "telegram-worker", "telegram-scheduler", "backup-worker"],
    }


def _pending_absent() -> dict:
    return {
        "schema_version": 1,
        "status": "pending",
        "source_revision": HEAD,
        "generation": GENERATION,
        "previous_mode": "absent",
        "previous_image_id": None,
        "previous_config_image": None,
        "rollback_ref": None,
        "target_image": TARGET_IMAGE,
        "target_image_id": TARGET_IMAGE_ID,
        "artifact_volume": ARTIFACT_VOLUME,
        "runtime_network": RUNTIME_NETWORK,
    }


def _pending_present() -> dict:
    return {
        **_pending_absent(),
        "previous_mode": "present",
        "previous_image_id": PREVIOUS_IMAGE_ID,
        "previous_config_image": "registry.example/gpakoh/content-harvester-service:previous",
        "rollback_ref": "registry.example/gpakoh/content-harvester-service-rollback:rollback",
    }


def _target_inspect(container: str, index: int) -> dict:
    state: dict = {
        "Running": True,
        "StartedAt": f"2026-10-11T01:02:0{index}Z",
    }
    if container == "content-harvester-api":
        state["Health"] = {"Status": "healthy", "FailingStreak": 0}
    return {
        "Id": f"target-{index}",
        "Image": TARGET_IMAGE_ID,
        "RestartCount": 0,
        "Config": {
            "Image": TARGET_IMAGE,
            "Labels": {"org.opencontainers.image.revision": HEAD},
        },
        "State": state,
    }


def _payload(
    state: dict | None = None,
    *,
    mode: str = "deploy",
    pending_before: dict | None = None,
    pending_exists: bool = False,
) -> str:
    return json.dumps(
        {
            "version": 1,
            "mode": mode,
            "exit_code": 0,
            "output_tail": "content harvester deploy ok",
            "state": _state() if state is None else state,
            "pending_before": pending_before or {},
            "pending_exists": pending_exists,
        }
    )


class FakeHarvesterClient:
    def __init__(self, runs: list[RunResult]) -> None:
        self.runs = list(runs)
        self.helper_calls: list[dict] = []
        self.inspect_rows = {
            container: _target_inspect(container, index)
            for index, (_service, container) in enumerate(
                docker_adapter._CONTENT_HARVESTER_TARGETS, start=1
            )
        }
        self.volumes = {
            docker_adapter._CONTENT_HARVESTER_STATE_VOLUME: _volume(
                docker_adapter._CONTENT_HARVESTER_STATE_VOLUME
            ),
            ARTIFACT_VOLUME: _volume(ARTIFACT_VOLUME),
        }
        self.network = _network()
        self.exists = {container: True for _service, container in docker_adapter._CONTENT_HARVESTER_TARGETS}
        self.revisions = {
            TARGET_IMAGE_ID: HEAD,
            PREVIOUS_IMAGE_ID: PREVIOUS_REVISION,
        }

    def _sanitize_string(self, value: str) -> str:
        return value

    async def run_content_harvester_deploy_contract_helper(self, **kwargs):
        self.helper_calls.append(kwargs)
        return self.runs.pop(0)

    async def inspect(self, name: str, max_lines: int = 10):
        assert max_lines == 10
        return [self.inspect_rows[name]]

    async def volume_metadata(self, name: str) -> dict:
        return self.volumes[name]

    async def network_metadata(self, name: str) -> dict:
        assert name == RUNTIME_NETWORK
        return self.network

    async def container_exists(self, name: str) -> bool:
        return self.exists[name]

    async def image_id(self, image: str) -> str:
        assert image == TARGET_IMAGE
        return TARGET_IMAGE_ID

    async def image_revision(self, image: str) -> str:
        return self.revisions[image]


@pytest.mark.asyncio
async def test_content_harvester_confirmation_pins_binary_resources_and_topology(monkeypatch):
    plan = _plan()
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_deploy",
        AsyncMock(return_value=plan),
    )

    result = await docker_adapter.docker_deploy_content_harvester(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        artifact_volume=ARTIFACT_VOLUME,
        runtime_network=RUNTIME_NETWORK,
    )

    assert result["ok"] is True
    pending = result["result"]
    assert pending["status"] == "confirmation_required"
    assert pending["risk"] == "high"
    assert "four exact roles" in pending["summary"]
    import examples.mcp_server.server as srv

    action, _ = srv._confirm_store.peek_action_id(pending["action_id"])
    assert action is not None
    assert action.tool == "docker_deploy_content_harvester"
    assert action.required_scope == "mcp:docker:admin"
    assert action.kwargs["target_image"] == TARGET_IMAGE
    assert action.kwargs["artifact_volume_fingerprint"] == plan["artifact_volume_fingerprint"]
    assert action.kwargs["runtime_network_fingerprint"] == plan["runtime_network_fingerprint"]
    assert action.kwargs["prior_mode"] == "absent"


@pytest.mark.asyncio
async def test_content_harvester_happy_path_verifies_four_targets_and_resources(monkeypatch):
    client = FakeHarvesterClient([RunResult(_payload(), "", 0)])
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_deploy",
        AsyncMock(return_value=_plan()),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    sleep = AsyncMock()
    monkeypatch.setattr(docker_adapter.asyncio, "sleep", sleep)

    result = await docker_adapter._docker_deploy_content_harvester_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        artifact_volume=ARTIFACT_VOLUME,
        runtime_network=RUNTIME_NETWORK,
        target_image=TARGET_IMAGE,
        state_volume_fingerprint=_plan()["state_volume_fingerprint"],
        artifact_volume_fingerprint=_plan()["artifact_volume_fingerprint"],
        runtime_network_fingerprint=_plan()["runtime_network_fingerprint"],
        prior_mode="absent",
        topology_fingerprint=_plan()["topology_fingerprint"],
    )

    assert result["ok"] is True
    assert result["result"]["reconciled_after_unknown"] is False
    assert len(result["result"]["deployment"]["live"]) == 4
    assert "content-harvester-backup-worker" in result["result"]["deployment"]["live"]
    assert result["result"]["target_image_id"] == TARGET_IMAGE_ID
    assert "expected_image_id" not in client.helper_calls[0]
    assert client.helper_calls[0]["recover"] is False
    sleep.assert_awaited_once_with(docker_adapter._CONTENT_HARVESTER_VERIFY_STABILITY_SECONDS)


@pytest.mark.asyncio
async def test_content_harvester_partial_prior_topology_fails_closed():
    client = FakeHarvesterClient([])
    client.exists = {
        "content-harvester-api": True,
        "content-harvester-telegram-worker": False,
        "content-harvester-telegram-scheduler": False,
        "content-harvester-backup-worker": False,
    }
    with pytest.raises(RuntimeError, match="partial prior"):
        await docker_adapter._content_harvester_topology_fingerprint(client)


def test_content_harvester_state_rejects_legacy_three_role_service_set():
    state = _state()
    state["services"] = ["api", "telegram-worker", "telegram-scheduler"]

    with pytest.raises(RuntimeError, match="service set"):
        docker_adapter._content_harvester_state(
            state,
            source_revision=HEAD,
            target_image=TARGET_IMAGE,
            artifact_volume=ARTIFACT_VOLUME,
            runtime_network=RUNTIME_NETWORK,
        )


@pytest.mark.asyncio
async def test_content_harvester_live_rejects_wrong_oci_revision():
    client = FakeHarvesterClient([])
    client.inspect_rows["content-harvester-telegram-worker"]["Config"]["Labels"][
        "org.opencontainers.image.revision"
    ] = "5" * 40
    with pytest.raises(RuntimeError, match="OCI revision"):
        await docker_adapter._verify_content_harvester_live(
            client, state=_state(), plan=_plan()
        )


@pytest.mark.asyncio
async def test_content_harvester_resource_drift_is_rejected():
    client = FakeHarvesterClient([])
    client.network["Id"] = "changed-network-id"
    with pytest.raises(RuntimeError, match="runtime network changed"):
        await docker_adapter._verify_content_harvester_live(
            client, state=_state(), plan=_plan()
        )


@pytest.mark.asyncio
async def test_content_harvester_confirmation_revalidates_all_identities(monkeypatch):
    changed = _plan()
    changed["topology_fingerprint"] = "sha256:" + "f" * 64
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_deploy",
        AsyncMock(return_value=changed),
    )
    result = await docker_adapter._docker_deploy_content_harvester_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        artifact_volume=ARTIFACT_VOLUME,
        runtime_network=RUNTIME_NETWORK,
        target_image=TARGET_IMAGE,
        state_volume_fingerprint=_plan()["state_volume_fingerprint"],
        artifact_volume_fingerprint=_plan()["artifact_volume_fingerprint"],
        runtime_network_fingerprint=_plan()["runtime_network_fingerprint"],
        prior_mode="absent",
        topology_fingerprint=_plan()["topology_fingerprint"],
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_PRECONDITION_FAILED"
    assert "identities changed" in result["error"]["message"]


@pytest.mark.asyncio
async def test_content_harvester_success_with_pending_evidence_is_rejected(monkeypatch):
    client = FakeHarvesterClient([RunResult(_payload(pending_exists=True), "", 0)])
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_deploy",
        AsyncMock(return_value=_plan()),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    result = await docker_adapter._docker_deploy_content_harvester_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        artifact_volume=ARTIFACT_VOLUME,
        runtime_network=RUNTIME_NETWORK,
        target_image=TARGET_IMAGE,
        state_volume_fingerprint=_plan()["state_volume_fingerprint"],
        artifact_volume_fingerprint=_plan()["artifact_volume_fingerprint"],
        runtime_network_fingerprint=_plan()["runtime_network_fingerprint"],
        prior_mode="absent",
        topology_fingerprint=_plan()["topology_fingerprint"],
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_VERIFICATION_FAILED"
    assert "pending transaction" in result["error"]["message"]


@pytest.mark.asyncio
async def test_content_harvester_unknown_outcome_reconciles_target_success(monkeypatch):
    client = FakeHarvesterClient(
        [
            RunResult("", "outer timeout", -1),
            RunResult(_payload(mode="recover", pending_before=_pending_absent()), "", 0),
        ]
    )
    prepare = AsyncMock(return_value=_plan())
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_deploy",
        prepare,
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    monkeypatch.setattr(docker_adapter.asyncio, "sleep", AsyncMock())

    result = await docker_adapter._docker_deploy_content_harvester_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        artifact_volume=ARTIFACT_VOLUME,
        runtime_network=RUNTIME_NETWORK,
        target_image=TARGET_IMAGE,
        state_volume_fingerprint=_plan()["state_volume_fingerprint"],
        artifact_volume_fingerprint=_plan()["artifact_volume_fingerprint"],
        runtime_network_fingerprint=_plan()["runtime_network_fingerprint"],
        prior_mode="absent",
        topology_fingerprint=_plan()["topology_fingerprint"],
    )
    assert result["ok"] is True
    assert result["result"]["reconciled_after_unknown"] is True
    assert result["result"]["recovery_result"] == "target_accepted"
    assert [call["recover"] for call in client.helper_calls] == [False, True]
    assert prepare.await_count == 2
    assert "require_stable_topology" not in prepare.await_args_list[0].kwargs
    assert prepare.await_args_list[1].kwargs["require_stable_topology"] is False


@pytest.mark.asyncio
async def test_content_harvester_unknown_bootstrap_can_reconcile_absent(monkeypatch):
    client = FakeHarvesterClient(
        [
            RunResult("", "outer timeout", -1),
            RunResult(
                _payload(state={}, mode="recover", pending_before=_pending_absent()), "", 0
            ),
        ]
    )
    client.exists = {container: False for _service, container in docker_adapter._CONTENT_HARVESTER_TARGETS}
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_deploy",
        AsyncMock(return_value=_plan()),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter._docker_deploy_content_harvester_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        artifact_volume=ARTIFACT_VOLUME,
        runtime_network=RUNTIME_NETWORK,
        target_image=TARGET_IMAGE,
        state_volume_fingerprint=_plan()["state_volume_fingerprint"],
        artifact_volume_fingerprint=_plan()["artifact_volume_fingerprint"],
        runtime_network_fingerprint=_plan()["runtime_network_fingerprint"],
        prior_mode="absent",
        topology_fingerprint=_plan()["topology_fingerprint"],
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_RECOVERED_NOT_DEPLOYED"
    assert result["error"]["retryable"] is True
    assert result["result"]["recovery"]["outcome"] == "bootstrap_absent_restored"


@pytest.mark.asyncio
async def test_content_harvester_unknown_upgrade_proves_prior_image_id(monkeypatch):
    pending = _pending_present()
    client = FakeHarvesterClient(
        [
            RunResult("", "outer timeout", -1),
            RunResult(_payload(state={}, mode="recover", pending_before=pending), "", 0),
        ]
    )
    for index, (_service, container) in enumerate(
        docker_adapter._CONTENT_HARVESTER_TARGETS, start=1
    ):
        runtime: dict = {"Running": True, "StartedAt": f"2026-10-11T02:00:0{index}Z"}
        if container == "content-harvester-api":
            runtime["Health"] = {"Status": "healthy", "FailingStreak": 0}
        client.inspect_rows[container] = {
            "Id": f"rollback-{index}",
            "Image": PREVIOUS_IMAGE_ID,
            "RestartCount": 0,
            "Config": {"Image": pending["rollback_ref"], "Labels": {}},
            "State": runtime,
        }
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_deploy",
        AsyncMock(return_value=_plan(prior_mode="present")),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter._docker_deploy_content_harvester_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        artifact_volume=ARTIFACT_VOLUME,
        runtime_network=RUNTIME_NETWORK,
        target_image=TARGET_IMAGE,
        state_volume_fingerprint=_plan()["state_volume_fingerprint"],
        artifact_volume_fingerprint=_plan()["artifact_volume_fingerprint"],
        runtime_network_fingerprint=_plan()["runtime_network_fingerprint"],
        prior_mode="present",
        topology_fingerprint=_plan(prior_mode="present")["topology_fingerprint"],
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_RECOVERED_NOT_DEPLOYED"
    assert result["result"]["recovery"]["outcome"] == "rolled_back_to_previous_image_id"
    assert result["result"]["recovery"]["previous_revision"] == PREVIOUS_REVISION


@pytest.mark.asyncio
async def test_content_harvester_failed_recovery_stays_nonretryable_ambiguous(monkeypatch):
    failed = json.dumps(
        {
            "version": 1,
            "mode": "recover",
            "exit_code": 125,
            "output_tail": "recovery failed",
            "state": {},
            "pending_before": _pending_absent(),
            "pending_exists": True,
            "error": "cannot reconcile",
        }
    )
    client = FakeHarvesterClient(
        [RunResult("", "outer timeout", -1), RunResult(failed, "", 125)]
    )
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_content_harvester_deploy",
        AsyncMock(return_value=_plan()),
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    result = await docker_adapter._docker_deploy_content_harvester_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        artifact_volume=ARTIFACT_VOLUME,
        runtime_network=RUNTIME_NETWORK,
        target_image=TARGET_IMAGE,
        state_volume_fingerprint=_plan()["state_volume_fingerprint"],
        artifact_volume_fingerprint=_plan()["artifact_volume_fingerprint"],
        runtime_network_fingerprint=_plan()["runtime_network_fingerprint"],
        prior_mode="absent",
        topology_fingerprint=_plan()["topology_fingerprint"],
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS"
    assert result["error"]["retryable"] is False
    assert [call["recover"] for call in client.helper_calls] == [False, True]


class _PrepareClient:
    def __init__(self) -> None:
        self.exists = {container: False for _service, container in docker_adapter._CONTENT_HARVESTER_TARGETS}

    def _validate_project_dir(self, path: str) -> None:
        assert path

    async def volume_metadata(self, name: str) -> dict:
        return _volume(name)

    async def network_metadata(self, name: str) -> dict:
        assert name == RUNTIME_NETWORK
        return _network()

    async def container_exists(self, name: str) -> bool:
        return self.exists[name]

    async def container_image_id(self, container: str) -> str:
        assert container == "mcp-oauth"
        return HELPER_ID


@pytest.mark.asyncio
async def test_content_harvester_prepare_does_not_require_prebuilt_target_image(monkeypatch, tmp_path):
    candidate = tmp_path / "candidate"
    operator = tmp_path / "operator"
    candidate.mkdir()
    operator.mkdir()
    script = candidate / docker_adapter._CONTENT_HARVESTER_SCRIPT_PATH
    script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (operator / ".env").write_text("HARVESTER_DATABASE_URL=x\n", encoding="utf-8")
    script_sha = hashlib.sha256(script.read_bytes()).hexdigest()

    registry = MagicMock()
    registry.project_info.side_effect = lambda name: (
        {"root": str(candidate), "type": "candidate-clone"}
        if name == CANDIDATE
        else {"root": str(operator), "type": "canonical"}
    )
    monkeypatch.setattr(
        docker_adapter,
        "server_attr",
        lambda name: (lambda: registry) if name == "_get_workspace_registry" else None,
    )
    monkeypatch.setattr(
        docker_adapter,
        "_read_candidate_metadata",
        lambda _root: {
            "project_id": CANDIDATE,
            "source_project": docker_adapter._CONTENT_HARVESTER_SOURCE_PROJECT,
            "head": HEAD,
            "base_sha": HEAD,
        },
    )

    def fake_git(_root, *args):
        if args == ("rev-parse", "HEAD"):
            return HEAD
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return ""
        if args == ("rev-parse", f"{HEAD}:{docker_adapter._CONTENT_HARVESTER_SCRIPT_PATH}"):
            return BLOB
        raise AssertionError(args)

    monkeypatch.setattr(docker_adapter, "_git_capture", fake_git)
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: _PrepareClient())

    plan = await docker_adapter._prepare_content_harvester_deploy(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=script_sha,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        artifact_volume=ARTIFACT_VOLUME,
        runtime_network=RUNTIME_NETWORK,
    )
    assert plan["target_image"] == TARGET_IMAGE
    assert "target_image_id" not in plan
    assert plan["prior_mode"] == "absent"


def test_content_harvester_oauth_plane_has_durable_state_volume():
    compose = (
        Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    assert "content_harvester_deploy_state:/var/lib/content-harvester-deploy-state" in compose
    assert "name: ssh-gateway-content-harvester-deploy-state" in compose


@pytest.mark.asyncio
async def test_content_harvester_client_helper_uses_fixed_runner_and_expected_image(monkeypatch):
    client = DockerClient()
    run = AsyncMock(return_value=RunResult('{"exit_code":0}', "", 0))
    monkeypatch.setattr(client, "_run_with_result", run)
    await client.run_content_harvester_deploy_contract_helper(
        helper_image_id=HELPER_ID,
        candidate_root="/candidate/content-harvester",
        operator_root="/operator/content-harvester",
        script_path="/candidate/content-harvester/deploy-content-harvester.sh",
        env_file="/operator/content-harvester/.env",
        state_volume=docker_adapter._CONTENT_HARVESTER_STATE_VOLUME,
        image_namespace=NAMESPACE,
        source_revision=HEAD,
        artifact_volume=ARTIFACT_VOLUME,
        runtime_network=RUNTIME_NETWORK,
        timeout=720,
        recover=True,
    )
    argv = run.await_args.args[0]
    assert "/app/scripts/content_harvester_deploy_contract_runner.py" in argv
    assert "--recover" in argv
    assert "--expected-image-id" not in argv
    assert ARTIFACT_VOLUME in argv
    assert RUNTIME_NETWORK in argv
    assert "/usr/bin/docker" == argv[0]
    mounts = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "--mount"]
    assert any("/candidate/content-harvester" in mount and "readonly" in mount for mount in mounts)
    assert any("/operator/content-harvester" in mount and "readonly" in mount for mount in mounts)
    assert any(docker_adapter._CONTENT_HARVESTER_STATE_VOLUME in mount for mount in mounts)
