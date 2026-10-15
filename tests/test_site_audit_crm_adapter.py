from __future__ import annotations

import json
import sys
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

HEAD = "b" * 40
HELPER_ID = "sha256:" + "2" * 64
ENV_SHA = "sha256:" + "3" * 64
NEW_ENV_SHA = "sha256:" + "4" * 64
KEY_FP = "sha256:" + "5" * 64


@pytest.fixture(autouse=True)
def _clear_confirm_store():
    import examples.mcp_server.server as srv

    srv._confirm_store._actions.clear()
    yield
    srv._confirm_store._actions.clear()


def _snapshot(prefix: str, revision: str | None) -> dict:
    return {
        "container_id": f"{prefix}-container",
        "image_id": "sha256:" + ("6" if prefix == "api" else "7") * 64,
        "started_at": "2026-10-15T10:00:00Z",
        "restart_count": 0,
        "revision": revision,
    }


def _plan(*, ready: bool = False, env_sha: str = ENV_SHA) -> dict:
    evidence = {
        "env_sha256": env_sha,
        "crm_env_state": "ready" if ready else "absent",
        "role_state": "matching" if ready else "absent",
        "api_user_state": "matching" if ready else "absent",
    }
    if ready:
        evidence |= {"role_id": "role-1", "api_user_id": "user-1"}
    return {
        "operator_root": "/operator/site-audit",
        "env_file": "/operator/site-audit/.env",
        "env_sha256": env_sha,
        "helper_image_id": HELPER_ID,
        "expected_api_revision": HEAD,
        "api_snapshot": _snapshot("api", HEAD),
        "crm_snapshot": _snapshot("crm", None),
        "inspect_evidence": evidence,
        "inspect_fingerprint": docker_adapter._site_audit_crm_evidence_fingerprint(evidence),
    }


@pytest.mark.asyncio
async def test_crm_provision_preflight_returns_admin_confirmation(monkeypatch):
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_site_audit_crm_provision",
        AsyncMock(return_value=_plan()),
    )

    result = await docker_adapter.docker_provision_site_audit_crm(HEAD)

    assert result["ok"] is True
    pending = result["result"]
    assert pending["status"] == "confirmation_required"
    assert pending["risk"] == "high"
    assert pending["crm_preflight"] == {
        "crm_env_state": "absent",
        "role_state": "absent",
        "api_user_state": "absent",
        "crm_projection_ready": False,
    }
    import examples.mcp_server.server as srv

    action, _ = srv._confirm_store.peek_action_id(pending["action_id"])
    assert action is not None
    assert action.tool == "docker_provision_site_audit_crm"
    assert action.required_scope == "mcp:docker:admin"
    assert action.kwargs["env_sha256"] == ENV_SHA
    serialized = json.dumps(action.kwargs)
    assert "CRM_ADMIN_PASSWORD" not in serialized
    assert "AUDIT_CRM_API_KEY" not in serialized


@pytest.mark.asyncio
async def test_crm_provision_preflight_is_idempotent_when_already_ready(monkeypatch):
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_site_audit_crm_provision",
        AsyncMock(return_value=_plan(ready=True, env_sha=NEW_ENV_SHA)),
    )

    result = await docker_adapter.docker_provision_site_audit_crm(HEAD)

    assert result["ok"] is True
    assert result["result"]["mutation_required"] is False
    assert result["result"]["crm_projection_ready"] is True
    assert result["result"]["api_redeploy_required"] is True


@pytest.mark.asyncio
async def test_crm_confirm_rejects_changed_env_lease(monkeypatch):
    changed = _plan()
    changed["env_sha256"] = NEW_ENV_SHA
    changed["inspect_evidence"]["env_sha256"] = NEW_ENV_SHA
    changed["inspect_fingerprint"] = docker_adapter._site_audit_crm_evidence_fingerprint(
        changed["inspect_evidence"]
    )
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_site_audit_crm_provision",
        AsyncMock(return_value=changed),
    )

    result = await docker_adapter._docker_provision_site_audit_crm_impl(
        expected_api_revision=HEAD,
        env_sha256=ENV_SHA,
        helper_image_id=HELPER_ID,
        api_snapshot=_plan()["api_snapshot"],
        crm_snapshot=_plan()["crm_snapshot"],
        inspect_fingerprint=_plan()["inspect_fingerprint"],
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "CRM_PROVISION_PRECONDITION_FAILED"
    assert "identities changed" in result["error"]["message"]


class _ProvisionClient:
    def __init__(self, run: RunResult) -> None:
        self.run = run
        self.calls: list[dict] = []

    async def run_site_audit_crm_provisioner(self, **kwargs) -> RunResult:
        self.calls.append(kwargs)
        return self.run


@pytest.mark.asyncio
async def test_crm_confirm_success_returns_only_non_secret_evidence(monkeypatch):
    payload = json.dumps(
        {
            "version": 1,
            "outcome": "ready",
            "role_id": "role-1",
            "api_user_id": "user-1",
            "role_created": True,
            "api_user_created": True,
            "env_updated": True,
            "env_sha256": NEW_ENV_SHA,
            "api_key_fingerprint": KEY_FP,
            "crm_projection_ready": True,
        }
    )
    client = _ProvisionClient(RunResult(payload, "", 0))
    prepare = AsyncMock(side_effect=[_plan(), _plan(ready=True, env_sha=NEW_ENV_SHA)])
    monkeypatch.setattr(docker_adapter, "_prepare_site_audit_crm_provision", prepare)
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    base = _plan()
    result = await docker_adapter._docker_provision_site_audit_crm_impl(
        expected_api_revision=HEAD,
        env_sha256=base["env_sha256"],
        helper_image_id=base["helper_image_id"],
        api_snapshot=base["api_snapshot"],
        crm_snapshot=base["crm_snapshot"],
        inspect_fingerprint=base["inspect_fingerprint"],
    )

    assert result["ok"] is True
    evidence = result["result"]
    assert evidence["crm_projection_ready"] is True
    assert evidence["api_redeploy_required"] is True
    assert evidence["api_key_fingerprint"] == KEY_FP
    assert "apiKey" not in json.dumps(evidence)
    assert client.calls[0]["expected_env_sha256"] == ENV_SHA
    assert prepare.await_count == 2


@pytest.mark.asyncio
async def test_crm_timeout_reconciles_only_when_readiness_is_proven(monkeypatch):
    client = _ProvisionClient(RunResult("", "timeout", -1))
    prepare = AsyncMock(side_effect=[_plan(), _plan(ready=True, env_sha=NEW_ENV_SHA)])
    monkeypatch.setattr(docker_adapter, "_prepare_site_audit_crm_provision", prepare)
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    base = _plan()

    result = await docker_adapter._docker_provision_site_audit_crm_impl(
        expected_api_revision=HEAD,
        env_sha256=base["env_sha256"],
        helper_image_id=base["helper_image_id"],
        api_snapshot=base["api_snapshot"],
        crm_snapshot=base["crm_snapshot"],
        inspect_fingerprint=base["inspect_fingerprint"],
    )

    assert result["ok"] is True
    assert result["result"]["reconciled_after_unknown"] is True
    assert result["result"]["crm_projection_ready"] is True
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_crm_client_runner_has_fixed_network_no_socket_and_secret_free_argv(monkeypatch):
    client = DockerClient()
    run = AsyncMock(return_value=RunResult('{"version":1}', "", 0))
    monkeypatch.setattr(client, "_run_with_result", run)

    await client.run_site_audit_crm_provisioner(
        helper_image_id=HELPER_ID,
        operator_root="/operator/site-audit",
        mode="inspect",
    )
    inspect_argv = run.await_args.args[0]
    assert inspect_argv[inspect_argv.index("--network") + 1] == "site-audit-control"
    assert "/var/run/docker.sock" not in " ".join(inspect_argv)
    inspect_mount = inspect_argv[inspect_argv.index("--mount") + 1]
    assert inspect_mount.endswith("dst=/operator,readonly")
    assert "CRM_ADMIN_PASSWORD" not in " ".join(inspect_argv)
    assert "AUDIT_CRM_API_KEY" not in " ".join(inspect_argv)

    await client.run_site_audit_crm_provisioner(
        helper_image_id=HELPER_ID,
        operator_root="/operator/site-audit",
        mode="provision",
        expected_env_sha256=ENV_SHA,
    )
    provision_argv = run.await_args.args[0]
    provision_mount = provision_argv[provision_argv.index("--mount") + 1]
    assert provision_mount == "type=bind,src=/operator/site-audit,dst=/operator"
    assert ENV_SHA in provision_argv
    assert "/var/run/docker.sock" not in " ".join(provision_argv)


def test_crm_provisioner_surface_is_admin_and_safe_mode_blocked():
    from examples.mcp_server.tool_modes import MCP_CLIENT_BLOCKED_TOOLS, TOOL_NAMES_BY_MODE
    from examples.mcp_server.tool_scopes import TOOL_SCOPES

    tool = "docker_provision_site_audit_crm"
    assert tool in TOOL_NAMES_BY_MODE["mcp_client"]
    assert tool in MCP_CLIENT_BLOCKED_TOOLS
    assert TOOL_SCOPES[tool] == ["mcp:docker:admin"]
    assert docker_adapter._CONFIRM_HANDLERS[tool] is docker_adapter._docker_provision_site_audit_crm_impl
