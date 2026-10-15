from __future__ import annotations

import hashlib
import json
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))

from examples.mcp_client_remote.fleet.docker_client import DockerClient, RunResult  # noqa: E402
from examples.mcp_server.mcp_infra.adapters import docker as docker_adapter  # noqa: E402
from scripts import xloud_deploy_contract_runner as xloud_runner  # noqa: E402

HEAD = "a" * 40
TEST_SITE_IPV4 = ".".join(("192", "0", "2", "149"))
BLOB = "b" * 40
SCRIPT_SHA = "c" * 64
NAMESPACE = "registry.example/gpakoh"
CANDIDATE = "candidate-xloud-release"
HELPER_ID = "sha256:" + "d" * 64
SECRET = "operator-only-audit-key"
SECRET_FP = "sha256:" + hashlib.sha256(SECRET.encode()).hexdigest()
SITE_ID = "sha256:" + "1" * 64
INTAKE_ID = "sha256:" + "2" * 64
AUDIT_ID = "audit-api-container-id"


@pytest.fixture(autouse=True)
def _clear_confirm_store():
    import examples.mcp_server.server as srv

    srv._confirm_store._actions.clear()
    yield
    srv._confirm_store._actions.clear()


def _targets() -> dict[str, dict[str, str]]:
    return docker_adapter._xloud_target_images(NAMESPACE, HEAD)


def _volume() -> dict:
    return {
        "Name": docker_adapter._XLOUD_STATE_VOLUME,
        "Driver": "local",
        "Scope": "local",
        "Options": {},
        "Labels": {},
    }


def _network(name: str) -> dict:
    return {
        "Name": name,
        "Id": f"network-{name}",
        "Driver": "bridge" if name == "site-audit-control" else "macvlan",
        "Scope": "local",
        "Internal": False,
        "Attachable": False,
        "Ingress": False,
        "Options": {},
        "Labels": {},
    }


def _audit_inspect() -> dict:
    return {
        "Id": AUDIT_ID,
        "Image": "sha256:" + "9" * 64,
        "RestartCount": 0,
        "Config": {"Image": "site-audit-api:test", "Labels": {}},
        "State": {
            "StartedAt": "2026-10-14T12:00:00Z",
            "Health": {"Status": "healthy", "FailingStreak": 0},
        },
    }


def _lkg() -> dict[str, str]:
    targets = _targets()
    return {
        "STATE_VERSION": "1",
        "SOURCE_REVISION": HEAD,
        "SITE_IMAGE": targets["site"]["image"],
        "INTAKE_IMAGE": targets["intake"]["image"],
        "SITE_IMAGE_ID": SITE_ID,
        "INTAKE_IMAGE_ID": INTAKE_ID,
        "SITE_CONTAINER_ID": "1" * 64,
        "INTAKE_CONTAINER_ID": "2" * 64,
        "SITE_IPV4": TEST_SITE_IPV4,
        "STABILITY_SECONDS": "90",
    }


def _target_inspect(role: str) -> dict:
    targets = _targets()
    is_site = role == "site"
    container_id = "1" * 64 if is_site else "2" * 64
    image_id = SITE_ID if is_site else INTAKE_ID
    networks = {"xloud_default": {"IPAddress": "172.30.0.2"}}
    if is_site:
        networks["proxmox_macvlan"] = {"IPAddress": TEST_SITE_IPV4}
    else:
        networks["site-audit-control"] = {"IPAddress": "172.31.0.8"}
    return {
        "Id": container_id,
        "Image": image_id,
        "RestartCount": 0,
        "Config": {
            "Image": targets[role]["image"],
            "Labels": {
                "org.opencontainers.image.revision": HEAD,
                "io.xloud.role": role,
            },
        },
        "State": {
            "StartedAt": "2026-10-14T13:00:00Z",
            "Health": {"Status": "healthy", "FailingStreak": 0},
        },
        "NetworkSettings": {"Networks": networks},
    }


def _plan() -> dict:
    networks = {
        name: docker_adapter._xloud_resource_fingerprint(_network(name))
        for name in docker_adapter._XLOUD_REQUIRED_NETWORKS
    }
    return {
        "candidate_root": "/candidate/xloud",
        "operator_root": "/operator/site-audit",
        "script_path": "/candidate/xloud/deploy-xloud.sh",
        "env_file": "/operator/site-audit/.env",
        "state_volume": docker_adapter._XLOUD_STATE_VOLUME,
        "state_volume_fingerprint": docker_adapter._xloud_resource_fingerprint(_volume()),
        "network_fingerprints": networks,
        "audit_snapshot": {
            "container_id": AUDIT_ID,
            "started_at": "2026-10-14T12:00:00Z",
            "image_id": "sha256:" + "9" * 64,
        },
        "secret_fingerprint": SECRET_FP,
        "helper_image_id": HELPER_ID,
        "source_revision": HEAD,
        "image_namespace": NAMESPACE,
        "target_images": _targets(),
        "image_ids": {"site": SITE_ID, "intake": INTAKE_ID},
        "prior_mode": "absent",
        "topology_fingerprint": "sha256:" + hashlib.sha256(b"absent").hexdigest(),
    }


def _payload(*, timed_out: bool = False, failed: dict | None = None) -> str:
    return json.dumps(
        {
            "version": 1,
            "mode": "deploy",
            "exit_code": 124 if timed_out else 0,
            "timed_out": timed_out,
            "pending_exists": timed_out,
            "lkg_exists": not timed_out,
            "failed_exists": bool(failed),
            "lkg": {} if timed_out else _lkg(),
            "pending": {},
            "failed": failed or {},
            "stdout_tail": ["status=accepted"] if not timed_out else [],
            "stderr_tail": [],
        }
    )


class FakeClient:
    def __init__(self, runs: list[RunResult] | None = None) -> None:
        self.runs = list(runs or [])
        self.helper_calls: list[dict] = []
        self.inspect_rows = {
            docker_adapter._XLOUD_AUDIT_API_CONTAINER: _audit_inspect(),
            "xloud-site": _target_inspect("site"),
            "xloud-intake": _target_inspect("intake"),
        }

    def _sanitize_string(self, value: str) -> str:
        return value

    async def run_xloud_deploy_contract_helper(self, **kwargs):
        self.helper_calls.append(kwargs)
        return self.runs.pop(0)

    async def volume_metadata(self, _name: str) -> dict:
        return _volume()

    async def network_metadata(self, name: str) -> dict:
        return _network(name)

    async def inspect(self, name: str, max_lines: int = 10):
        assert max_lines == 10
        return [self.inspect_rows[name]]

    async def container_network_snapshot(self, name: str) -> dict[str, str]:
        if name == "xloud-site":
            return {
                "xloud_default": "172.30.0.2",
                "proxmox_macvlan": TEST_SITE_IPV4,
            }
        if name == "xloud-intake":
            return {
                "xloud_default": "172.30.0.3",
                "site-audit-control": "172.31.0.8",
            }
        raise AssertionError(name)

    async def container_exists(self, name: str) -> bool:
        return name in self.inspect_rows


@pytest.mark.asyncio
async def test_confirmation_pins_two_images_resources_and_secret_fingerprint(monkeypatch):
    monkeypatch.setattr(docker_adapter, "_prepare_xloud_deploy", AsyncMock(return_value=_plan()))

    result = await docker_adapter.docker_deploy_xloud(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
    )

    assert result["ok"] is True
    import examples.mcp_server.server as srv

    action, _ = srv._confirm_store.peek_action_id(result["result"]["action_id"])
    assert action is not None
    assert action.tool == "docker_deploy_xloud"
    assert action.required_scope == "mcp:docker:admin"
    assert action.kwargs["target_images"] == _targets()
    assert action.kwargs["image_ids"] == {"site": SITE_ID, "intake": INTAKE_ID}
    assert action.kwargs["secret_fingerprint"] == SECRET_FP
    assert SECRET not in json.dumps(action.kwargs)


@pytest.mark.asyncio
async def test_happy_path_verifies_exact_two_role_live_state(monkeypatch):
    client = FakeClient([RunResult(_payload(), "", 0)])
    monkeypatch.setattr(docker_adapter, "_prepare_xloud_deploy", AsyncMock(return_value=_plan()))
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    sleep = AsyncMock()
    monkeypatch.setattr(docker_adapter.asyncio, "sleep", sleep)

    result = await docker_adapter._docker_deploy_xloud_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=_targets(),
        image_ids={"site": SITE_ID, "intake": INTAKE_ID},
        state_volume_fingerprint=_plan()["state_volume_fingerprint"],
        network_fingerprints=_plan()["network_fingerprints"],
        audit_snapshot=_plan()["audit_snapshot"],
        secret_fingerprint=SECRET_FP,
        prior_mode="absent",
        topology_fingerprint=_plan()["topology_fingerprint"],
    )

    assert result["ok"] is True
    assert result["result"]["reconciled_after_unknown"] is False
    assert set(result["result"]["deployment"]["live"]) == {"xloud-site", "xloud-intake"}
    assert client.helper_calls[0]["mode"] == "deploy"
    assert SECRET not in json.dumps(client.helper_calls)
    sleep.assert_awaited_once_with(docker_adapter._XLOUD_VERIFY_STABILITY_SECONDS)


@pytest.mark.asyncio
async def test_confirm_fails_closed_if_secret_or_resource_identity_changes(monkeypatch):
    changed = _plan()
    changed["secret_fingerprint"] = "sha256:" + "f" * 64
    monkeypatch.setattr(docker_adapter, "_prepare_xloud_deploy", AsyncMock(return_value=changed))

    result = await docker_adapter._docker_deploy_xloud_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=_targets(),
        image_ids={"site": SITE_ID, "intake": INTAKE_ID},
        state_volume_fingerprint=_plan()["state_volume_fingerprint"],
        network_fingerprints=_plan()["network_fingerprints"],
        audit_snapshot=_plan()["audit_snapshot"],
        secret_fingerprint=SECRET_FP,
        prior_mode="absent",
        topology_fingerprint=_plan()["topology_fingerprint"],
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_PRECONDITION_FAILED"
    assert "confirmation identities changed" in result["error"]["message"]


def test_secret_fingerprint_is_strict_and_never_returns_plaintext(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"AUDIT_CONTROL_API_KEY={SECRET}\n", encoding="utf-8")
    fingerprint = docker_adapter._xloud_secret_fingerprint(env_file)
    assert fingerprint == SECRET_FP
    assert SECRET not in fingerprint

    env_file.write_text(
        f"AUDIT_CONTROL_API_KEY={SECRET}\nAUDIT_CONTROL_API_KEY=other\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exactly once"):
        docker_adapter._xloud_secret_fingerprint(env_file)


@pytest.mark.asyncio
async def test_live_verification_rejects_wrong_role_or_revision():
    client = FakeClient()
    client.inspect_rows["xloud-intake"]["Config"]["Labels"]["io.xloud.role"] = "site"
    with pytest.raises(RuntimeError, match="role label"):
        await docker_adapter._verify_xloud_live(
            client,
            state=_lkg(),
            source_revision=HEAD,
            target_images=_targets(),
            image_ids={"site": SITE_ID, "intake": INTAKE_ID},
        )


@pytest.mark.asyncio
async def test_unknown_outcome_recovers_bootstrap_absence(monkeypatch):
    failed = {
        "STATE_VERSION": "1",
        "SOURCE_REVISION": HEAD,
        "PREVIOUS_SITE_CID": "absent",
        "PREVIOUS_INTAKE_CID": "absent",
    }
    recovery_payload = json.dumps(
        {
            "version": 1,
            "mode": "recover",
            "exit_code": 0,
            "timed_out": False,
            "pending_exists": False,
            "lkg_exists": False,
            "failed_exists": True,
            "lkg": {},
            "pending": {},
            "failed": failed,
            "stdout_tail": ["status=recovered"],
            "stderr_tail": [],
        }
    )
    client = FakeClient(
        [RunResult("", "outer timeout", -1), RunResult(recovery_payload, "", 0)]
    )
    client.inspect_rows.pop("xloud-site")
    client.inspect_rows.pop("xloud-intake")
    prepare = AsyncMock(return_value=_plan())
    monkeypatch.setattr(docker_adapter, "_prepare_xloud_deploy", prepare)
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter._docker_deploy_xloud_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=_targets(),
        image_ids={"site": SITE_ID, "intake": INTAKE_ID},
        state_volume_fingerprint=_plan()["state_volume_fingerprint"],
        network_fingerprints=_plan()["network_fingerprints"],
        audit_snapshot=_plan()["audit_snapshot"],
        secret_fingerprint=SECRET_FP,
        prior_mode="absent",
        topology_fingerprint=_plan()["topology_fingerprint"],
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_RECOVERED_NOT_DEPLOYED"
    assert result["result"]["recovery"]["outcome"] == "bootstrap_absent_restored"
    assert [call["mode"] for call in client.helper_calls] == ["deploy", "recover"]


@pytest.mark.asyncio
async def test_confirmation_expires_fail_closed(monkeypatch):
    monkeypatch.setattr(docker_adapter, "_prepare_xloud_deploy", AsyncMock(return_value=_plan()))
    pending = await docker_adapter.docker_deploy_xloud(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
    )
    import examples.mcp_server.server as srv

    action, _ = srv._confirm_store.peek_action_id(pending["result"]["action_id"])
    assert action is not None
    action.created_at -= 61
    result = await docker_adapter.confirm_operation(token=action.confirm_token)
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIRM_TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_client_helper_uses_fixed_runner_and_never_places_secret_in_argv(monkeypatch):
    client = DockerClient()
    run = AsyncMock(return_value=RunResult('{"version":1,"exit_code":0}', "", 0))
    monkeypatch.setattr(client, "_run_with_result", run)

    await client.run_xloud_deploy_contract_helper(
        helper_image_id=HELPER_ID,
        candidate_root="/candidate/xloud",
        operator_root="/operator/site-audit",
        script_path="/candidate/xloud/deploy-xloud.sh",
        env_file="/operator/site-audit/.env",
        state_volume=docker_adapter._XLOUD_STATE_VOLUME,
        site_image=_targets()["site"]["image"],
        intake_image=_targets()["intake"]["image"],
        site_image_id=SITE_ID,
        intake_image_id=INTAKE_ID,
        source_revision=HEAD,
        secret_fingerprint=SECRET_FP,
        mode="deploy",
    )

    argv = run.await_args.args[0]
    joined = " ".join(argv)
    assert "/app/scripts/xloud_deploy_contract_runner.py" in argv
    assert "/var/run/docker.sock" in joined
    assert docker_adapter._XLOUD_STATE_VOLUME in joined
    assert SECRET not in joined
    assert "AUDIT_CONTROL_API_KEY=" not in joined
    assert "XLOUD_AUDIT_CONTROL_API_KEY=" not in joined
    assert "--secret-fingerprint" in argv


@pytest.mark.asyncio
async def test_internal_network_snapshot_preserves_public_inspect_privacy_boundary(monkeypatch):
    client = DockerClient()
    raw = json.dumps(
        {
            "xloud_default": {
                "IPAddress": "172.30.0.2",
                "NetworkID": "must-not-propagate",
                "EndpointID": "must-not-propagate",
            },
            "proxmox_macvlan": {
                "IPAddress": TEST_SITE_IPV4,
                "MacAddress": "must-not-propagate",
            },
        }
    )
    run = AsyncMock(return_value=raw)
    monkeypatch.setattr(client, "_run", run)

    snapshot = await client.container_network_snapshot("xloud-site")

    assert snapshot == {
        "xloud_default": "172.30.0.2",
        "proxmox_macvlan": TEST_SITE_IPV4,
    }
    argv = run.await_args.args[0]
    assert argv == [
        "/usr/bin/docker",
        "inspect",
        "--format",
        "{{json .NetworkSettings.Networks}}",
        "xloud-site",
    ]
    assert "NetworkID" not in json.dumps(snapshot)
    assert "EndpointID" not in json.dumps(snapshot)
    assert "MacAddress" not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_prepare_rejects_non_xloud_candidate_before_docker(monkeypatch, tmp_path):
    candidate = tmp_path / "candidate"
    operator = tmp_path / "operator"
    candidate.mkdir()
    operator.mkdir()
    (candidate / docker_adapter._XLOUD_SCRIPT_PATH).write_text("#!/bin/sh\n", encoding="utf-8")
    (operator / ".env").write_text(f"AUDIT_CONTROL_API_KEY={SECRET}\n", encoding="utf-8")

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
            "source_project": "not-xloud",
            "head": HEAD,
            "base_sha": HEAD,
        },
    )
    docker = MagicMock()
    monkeypatch.setattr(docker_adapter, "_docker_client", docker)

    with pytest.raises(ValueError, match="not authorized"):
        await docker_adapter._prepare_xloud_deploy(
            candidate_project=CANDIDATE,
            expected_head_sha=HEAD,
            expected_script_blob_sha=BLOB,
            expected_script_sha256=SCRIPT_SHA,
            source_revision=HEAD,
            image_namespace=NAMESPACE,
        )
    docker.assert_not_called()


def test_runner_redacts_operator_secret_even_if_child_prints_it(tmp_path):
    source = tmp_path / "source"
    operator = tmp_path / "operator"
    state = tmp_path / "state"
    source.mkdir()
    operator.mkdir()
    state.mkdir()
    script = source / "deploy-xloud.sh"
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$XLOUD_AUDIT_CONTROL_API_KEY"\nprintf "%s\\n" "$XLOUD_AUDIT_CONTROL_API_KEY" >&2\n',
        encoding="utf-8",
    )
    env_file = operator / ".env"
    env_file.write_text(f"AUDIT_CONTROL_API_KEY={SECRET}\n", encoding="utf-8")

    result = xloud_runner._run(
        Namespace(
            mode="preflight",
            script=str(script),
            source_root=str(source),
            operator_root=str(operator),
            env_file=str(env_file),
            state_dir=str(state),
            site_image=f"{NAMESPACE}/xloud-site:{HEAD}",
            intake_image=f"{NAMESPACE}/xloud-intake:{HEAD}",
            site_image_id=SITE_ID,
            intake_image_id=INTAKE_ID,
            source_revision=HEAD,
            secret_fingerprint=SECRET_FP,
            stability_seconds=90,
            timeout=60,
        )
    )

    rendered = json.dumps(result)
    assert SECRET not in rendered
    assert "<redacted>" in rendered


def test_xloud_tool_mode_scope_and_compose_state_volume_wiring():
    from examples.mcp_server.tool_modes import MCP_CLIENT_BLOCKED_TOOLS, TOOL_NAMES_BY_MODE
    from examples.mcp_server.tool_scopes import TOOL_SCOPES

    tool = "docker_deploy_xloud"
    assert tool in TOOL_NAMES_BY_MODE["mcp_client"]
    assert tool in MCP_CLIENT_BLOCKED_TOOLS
    assert TOOL_SCOPES[tool] == ["mcp:docker:admin"]

    compose = (Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "xloud_deploy_state:/var/lib/xloud-deploy-state" in compose
    assert "name: ssh-gateway-xloud-deploy-state" in compose
