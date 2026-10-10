from __future__ import annotations

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
CANDIDATE = "candidate-site-audit-release"
HELPER_ID = "sha256:" + "2" * 64
GENERATION = HEAD[:12] + "-20261011T010203Z"


@pytest.fixture(autouse=True)
def _clear_confirm_store():
    import examples.mcp_server.server as srv

    srv._confirm_store._actions.clear()
    yield
    srv._confirm_store._actions.clear()


def _targets() -> dict[str, dict[str, str]]:
    return docker_adapter._site_audit_target_images(NAMESPACE, HEAD)


def _state() -> dict:
    targets = _targets()
    deployed = []
    previous = []
    for index, (service, container, _image_name) in enumerate(
        docker_adapter._SITE_AUDIT_TARGETS, start=1
    ):
        deployed.append(
            {
                "service": service,
                "container": container,
                "image": targets[service]["image"],
                "image_id": "sha256:" + f"{index:x}" * 64,
                "container_id": f"target-{index}",
                "restart_count": 0,
                "started_at": f"2026-10-11T01:02:0{index}Z",
            }
        )
        previous.append(
            {
                "service": service,
                "container": container,
                "image_id": "sha256:" + "f" * 64,
                "rollback_ref": f"site-audit-rollback:{service}",
                "config_image": f"old/{service}:previous",
                "container_id": f"old-{index}",
                "restart_count": index - 1,
            }
        )
    protected = [
        {
            "container": container,
            "container_id": f"protected-{index}",
            "started_at": f"2026-10-10T23:00:{index:02d}Z",
        }
        for index, container in enumerate(
            docker_adapter._SITE_AUDIT_PROTECTED_CONTAINERS, start=1
        )
    ]
    return {
        "schema_version": 1,
        "source_revision": HEAD,
        "deploy_generation": GENERATION,
        "stability_seconds": 90,
        "crm_route_surface_verified": True,
        "protected_containers_unchanged": protected,
        "previous": previous,
        "deployed": deployed,
    }


def _plan() -> dict:
    return {
        "candidate_root": "/candidates/site-audit",
        "operator_root": "/operator/site-audit",
        "script_path": "/candidates/site-audit/deploy-site-audit.sh",
        "env_file": "/operator/site-audit/.env",
        "state_volume": docker_adapter._SITE_AUDIT_STATE_VOLUME,
        "helper_image_id": HELPER_ID,
        "source_revision": HEAD,
        "image_namespace": NAMESPACE,
        "target_images": _targets(),
    }


def _inspect_map(state: dict | None = None) -> dict[str, dict]:
    state = state or _state()
    entries: dict[str, dict] = {}
    for item in state["deployed"]:
        entries[item["container"]] = {
            "Id": item["container_id"],
            "Image": item["image_id"],
            "RestartCount": 0,
            "Config": {
                "Image": item["image"],
                "Labels": {"org.opencontainers.image.revision": HEAD},
            },
            "State": {
                "StartedAt": item["started_at"],
                "Health": {"Status": "healthy", "FailingStreak": 0},
            },
        }
    for item in state["protected_containers_unchanged"]:
        entries[item["container"]] = {
            "Id": item["container_id"],
            "Image": "sha256:" + "e" * 64,
            "RestartCount": 0,
            "Config": {"Image": "protected:stable", "Labels": {}},
            "State": {"StartedAt": item["started_at"]},
        }
    return entries


class FakeSiteAuditClient:
    def __init__(self, runs: list[RunResult], state: dict | None = None) -> None:
        self.runs = list(runs)
        self.inspect_rows = _inspect_map(state)
        self.helper_calls: list[dict] = []

    def _sanitize_string(self, value: str) -> str:
        return value

    async def run_site_audit_deploy_contract_helper(self, **kwargs):
        self.helper_calls.append(kwargs)
        return self.runs.pop(0)

    async def inspect(self, name: str, max_lines: int = 10):
        assert max_lines == 10
        return [self.inspect_rows[name]]


def _payload(state: dict | None = None, *, pending_exists: bool = False) -> str:
    return json.dumps(
        {
            "version": 1,
            "exit_code": 0,
            "output_tail": "site audit deploy ok",
            "state": state or _state(),
            "pending_exists": pending_exists,
        }
    )


@pytest.mark.asyncio
async def test_site_audit_confirmation_pins_complete_five_image_set(monkeypatch):
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_site_audit_deploy",
        AsyncMock(return_value=_plan()),
    )

    result = await docker_adapter.docker_deploy_site_audit(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
    )

    assert result["ok"] is True
    pending = result["result"]
    assert pending["status"] == "confirmation_required"
    assert pending["risk"] == "high"
    import examples.mcp_server.server as srv

    action, _ = srv._confirm_store.peek_action_id(pending["action_id"])
    assert action is not None
    assert action.tool == "docker_deploy_site_audit"
    assert action.required_scope == "mcp:docker:admin"
    assert action.kwargs["source_revision"] == HEAD
    assert action.kwargs["target_images"] == _targets()
    assert len(action.kwargs["target_images"]) == 5


@pytest.mark.asyncio
async def test_site_audit_happy_path_verifies_five_targets_and_protected(monkeypatch):
    client = FakeSiteAuditClient([RunResult(_payload(), "", 0)])
    prepare = AsyncMock(return_value=_plan())
    monkeypatch.setattr(docker_adapter, "_prepare_site_audit_deploy", prepare)
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    monkeypatch.setattr(docker_adapter.asyncio, "sleep", AsyncMock())

    result = await docker_adapter._docker_deploy_site_audit_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=_targets(),
    )

    assert result["ok"] is True
    assert result["result"]["reconciled_after_unknown"] is False
    assert len(result["result"]["deployment"]["live"]["targets"]) == 5
    assert len(result["result"]["deployment"]["live"]["protected"]) == 7
    assert client.helper_calls == [pytest.approx(client.helper_calls[0])]
    assert client.helper_calls[0]["recover"] is False
    assert prepare.await_count == 1


@pytest.mark.parametrize("mutation", ["missing", "wrong_image", "wrong_revision"])
def test_site_audit_lkg_rejects_missing_or_wrong_image_revision(mutation):
    state = _state()
    targets = _targets()
    if mutation == "missing":
        state["deployed"].pop()
        with pytest.raises(RuntimeError, match="exactly five"):
            docker_adapter._site_audit_state_maps(
                state, source_revision=HEAD, target_images=targets
            )
        return
    if mutation == "wrong_image":
        state["deployed"][0]["image"] = "registry.example/other:wrong"
        with pytest.raises(RuntimeError, match="image/container set changed"):
            docker_adapter._site_audit_state_maps(
                state, source_revision=HEAD, target_images=targets
            )
        return
    state["source_revision"] = "5" * 40
    with pytest.raises(RuntimeError, match="source revision mismatch"):
        docker_adapter._site_audit_state_maps(
            state, source_revision=HEAD, target_images=targets
        )


@pytest.mark.asyncio
async def test_site_audit_live_rejects_wrong_oci_revision(monkeypatch):
    client = FakeSiteAuditClient([RunResult(_payload(), "", 0)])
    first_container = docker_adapter._SITE_AUDIT_TARGETS[0][1]
    client.inspect_rows[first_container]["Config"]["Labels"][
        "org.opencontainers.image.revision"
    ] = "5" * 40
    with pytest.raises(RuntimeError, match="OCI revision"):
        await docker_adapter._verify_site_audit_live(
            client,
            state=_state(),
            source_revision=HEAD,
            target_images=_targets(),
        )


class _PrepareClient:
    def _validate_project_dir(self, path: str) -> None:
        assert path

    async def volume_metadata(self, volume: str) -> dict:
        return {"Name": volume, "Driver": "local", "Options": {}}

    async def container_image_id(self, container: str) -> str:
        assert container == "mcp-oauth"
        return HELPER_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("git_mode", "message"),
    [
        ("moved_head", "candidate HEAD changed"),
        ("changed_blob", "Git blob identity mismatch"),
    ],
)
async def test_site_audit_prepare_rejects_moved_head_or_changed_script_blob(
    monkeypatch, tmp_path, git_mode, message
):
    candidate = tmp_path / "candidate"
    operator = tmp_path / "operator"
    candidate.mkdir()
    operator.mkdir()
    script = candidate / docker_adapter._SITE_AUDIT_SCRIPT_PATH
    script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (operator / ".env").write_text("SITE_AUDIT_TEST=1\n", encoding="utf-8")
    script_sha = __import__("hashlib").sha256(script.read_bytes()).hexdigest()

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
            "source_project": docker_adapter._SITE_AUDIT_SOURCE_PROJECT,
            "head": HEAD,
            "base_sha": HEAD,
        },
    )

    def fake_git(_root, *args):
        if args == ("rev-parse", "HEAD"):
            return "5" * 40 if git_mode == "moved_head" else HEAD
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return ""
        if args == ("rev-parse", f"{HEAD}:{docker_adapter._SITE_AUDIT_SCRIPT_PATH}"):
            return "c" * 40 if git_mode == "changed_blob" else BLOB
        raise AssertionError(args)

    monkeypatch.setattr(docker_adapter, "_git_capture", fake_git)
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: _PrepareClient())

    with pytest.raises(ValueError, match=message):
        await docker_adapter._prepare_site_audit_deploy(
            candidate_project=CANDIDATE,
            expected_head_sha=HEAD,
            expected_script_blob_sha=BLOB,
            expected_script_sha256=script_sha,
            source_revision=HEAD,
            image_namespace=NAMESPACE,
        )


@pytest.mark.asyncio
async def test_site_audit_confirm_revalidates_moved_candidate_head(monkeypatch):
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_site_audit_deploy",
        AsyncMock(side_effect=ValueError("candidate HEAD changed")),
    )
    result = await docker_adapter._docker_deploy_site_audit_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=_targets(),
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_PRECONDITION_FAILED"
    assert "HEAD changed" in result["error"]["message"]


@pytest.mark.asyncio
async def test_site_audit_confirm_revalidates_changed_script(monkeypatch):
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_site_audit_deploy",
        AsyncMock(side_effect=ValueError("Site Audit deploy script Git blob identity mismatch")),
    )
    result = await docker_adapter._docker_deploy_site_audit_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=_targets(),
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_PRECONDITION_FAILED"
    assert "blob identity" in result["error"]["message"]


@pytest.mark.asyncio
async def test_site_audit_confirm_rejects_changed_five_image_set(monkeypatch):
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_site_audit_deploy",
        AsyncMock(return_value=_plan()),
    )
    changed = _targets()
    changed.pop("audit-security-worker")
    result = await docker_adapter._docker_deploy_site_audit_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=changed,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_PRECONDITION_FAILED"
    assert "five-image confirmation set changed" in result["error"]["message"]


@pytest.mark.asyncio
async def test_site_audit_rejects_malformed_or_pending_state(monkeypatch):
    malformed = _state()
    malformed["schema_version"] = 2
    client = FakeSiteAuditClient([RunResult(_payload(malformed), "", 0)], malformed)
    monkeypatch.setattr(docker_adapter, "_prepare_site_audit_deploy", AsyncMock(return_value=_plan()))
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    result = await docker_adapter._docker_deploy_site_audit_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=_targets(),
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_VERIFICATION_FAILED"
    assert "schema_version" in result["error"]["message"]

    pending_client = FakeSiteAuditClient(
        [RunResult(_payload(pending_exists=True), "", 0)]
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: pending_client)
    result = await docker_adapter._docker_deploy_site_audit_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=_targets(),
    )
    assert result["ok"] is False
    assert "pending transaction" in result["error"]["message"]


@pytest.mark.asyncio
async def test_site_audit_rejects_protected_dependency_identity_change(monkeypatch):
    client = FakeSiteAuditClient([RunResult(_payload(), "", 0)])
    changed = docker_adapter._SITE_AUDIT_PROTECTED_CONTAINERS[0]
    client.inspect_rows[changed]["Id"] = "replaced-protected-container"
    monkeypatch.setattr(docker_adapter, "_prepare_site_audit_deploy", AsyncMock(return_value=_plan()))
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    result = await docker_adapter._docker_deploy_site_audit_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=_targets(),
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_VERIFICATION_FAILED"
    assert "protected dependency/CRM identity changed" in result["error"]["message"]


@pytest.mark.asyncio
async def test_site_audit_helper_timeout_stays_ambiguous_after_failed_recovery(monkeypatch):
    recovery_payload = json.dumps(
        {
            "version": 1,
            "exit_code": 125,
            "output_tail": "recovery failed",
            "state": {},
            "pending_exists": True,
            "error": "pending transaction cannot be reconciled",
        }
    )
    client = FakeSiteAuditClient(
        [
            RunResult("", "outer timeout", -1),
            RunResult(recovery_payload, "", 125),
        ]
    )
    monkeypatch.setattr(docker_adapter, "_prepare_site_audit_deploy", AsyncMock(return_value=_plan()))
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter._docker_deploy_site_audit_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=_targets(),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS"
    assert result["error"]["retryable"] is False
    assert [call["recover"] for call in client.helper_calls] == [False, True]


@pytest.mark.asyncio
async def test_site_audit_unknown_outcome_can_reconcile_to_verified_success(monkeypatch):
    client = FakeSiteAuditClient(
        [RunResult("", "outer timeout", -1), RunResult(_payload(), "", 0)]
    )
    monkeypatch.setattr(docker_adapter, "_prepare_site_audit_deploy", AsyncMock(return_value=_plan()))
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    sleep = AsyncMock()
    monkeypatch.setattr(docker_adapter.asyncio, "sleep", sleep)

    result = await docker_adapter._docker_deploy_site_audit_impl(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
        target_images=_targets(),
    )

    assert result["ok"] is True
    assert result["result"]["reconciled_after_unknown"] is True
    assert [call["recover"] for call in client.helper_calls] == [False, True]
    sleep.assert_awaited_once_with(docker_adapter._SITE_AUDIT_VERIFY_STABILITY_SECONDS)


@pytest.mark.asyncio
async def test_site_audit_confirmation_expires_fail_closed(monkeypatch):
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_site_audit_deploy",
        AsyncMock(return_value=_plan()),
    )
    pending = await docker_adapter.docker_deploy_site_audit(
        candidate_project=CANDIDATE,
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        source_revision=HEAD,
        image_namespace=NAMESPACE,
    )
    import examples.mcp_server.server as srv

    action_id = pending["result"]["action_id"]
    action, _ = srv._confirm_store.peek_action_id(action_id)
    assert action is not None
    action.created_at -= 61
    result = await docker_adapter.confirm_operation(token=action.confirm_token)
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIRM_TOKEN_EXPIRED"


def test_site_audit_oauth_plane_wires_project_allowlist_and_durable_state_volume():
    compose = (
        Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    assert (
        "MCP_ALLOWED_PROJECT_ROOTS=${MCP_ALLOWED_PROJECT_ROOTS:-${MCP_OAUTH_PROJECT_ROOT:?set MCP_OAUTH_PROJECT_ROOT}}"
        in compose
    )
    assert "site_audit_deploy_state:/var/lib/site-audit-deploy-state" in compose
    assert "name: ssh-gateway-site-audit-deploy-state" in compose


@pytest.mark.asyncio
async def test_site_audit_client_helper_uses_fixed_runner_and_recover_flag(monkeypatch):
    client = DockerClient()
    run = AsyncMock(return_value=RunResult('{"exit_code":0}', "", 0))
    monkeypatch.setattr(client, "_run_with_result", run)
    await client.run_site_audit_deploy_contract_helper(
        helper_image_id=HELPER_ID,
        candidate_root="/candidate/site-audit",
        operator_root="/operator/site-audit",
        script_path="/candidate/site-audit/deploy-site-audit.sh",
        env_file="/operator/site-audit/.env",
        state_volume=docker_adapter._SITE_AUDIT_STATE_VOLUME,
        image_namespace=NAMESPACE,
        source_revision=HEAD,
        timeout=720,
        recover=True,
    )
    argv = run.await_args.args[0]
    assert "/app/scripts/site_audit_deploy_contract_runner.py" in argv
    assert "--recover" in argv
    assert "/usr/bin/docker" == argv[0]
    assert "compose" not in argv
    mounts = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "--mount"]
    assert any("/candidate/site-audit" in mount and "readonly" in mount for mount in mounts)
    assert any("/operator/site-audit" in mount and "readonly" in mount for mount in mounts)
    assert any(docker_adapter._SITE_AUDIT_STATE_VOLUME in mount for mount in mounts)
