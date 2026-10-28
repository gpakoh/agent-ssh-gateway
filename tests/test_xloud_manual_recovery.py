from __future__ import annotations

import hashlib
import json
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "mcp_server"))
sys.path.insert(0, str(ROOT))
from examples.mcp_client_remote.fleet.docker_client import RunResult  # noqa: E402
from examples.mcp_server.mcp_infra.adapters import docker as adapter  # noqa: E402
from scripts import xloud_deploy_contract_runner as runner  # noqa: E402

HEAD = "a" * 40
BLOB = "b" * 40
SCRIPT_SHA = "c" * 64
SITE_ID = "sha256:" + "1" * 64
INTAKE_ID = "sha256:" + "2" * 64
OLD_SITE_ID = "sha256:" + "3" * 64
FP = "sha256:" + "e" * 64
NAMESPACE = "registry.example/gpakoh"
TARGETS = adapter._xloud_target_images(NAMESPACE, HEAD)
KWARGS = {
    "candidate_project": "candidate-xloud-release",
    "expected_head_sha": HEAD,
    "expected_script_blob_sha": BLOB,
    "expected_script_sha256": SCRIPT_SHA,
    "source_revision": HEAD,
    "image_namespace": NAMESPACE,
}


def pending_state() -> dict[str, str]:
    return {
        "STATE_VERSION": "1",
        "SOURCE_REVISION": HEAD,
        "SITE_IMAGE": TARGETS["site"]["image"],
        "INTAKE_IMAGE": TARGETS["intake"]["image"],
        "SITE_IMAGE_ID": SITE_ID,
        "INTAKE_IMAGE_ID": INTAKE_ID,
        "PREVIOUS_SITE_CID": "4" * 64,
        "PREVIOUS_SITE_REF": "xloud-site:local",
        "PREVIOUS_SITE_IMAGE_ID": OLD_SITE_ID,
        "PREVIOUS_INTAKE_CID": "absent",
        "PREVIOUS_INTAKE_REF": "absent",
        "PREVIOUS_INTAKE_IMAGE_ID": "absent",
    }


def base_plan() -> dict:
    return {
        "helper_image_id": "sha256:" + "d" * 64,
        "candidate_root": "/candidate/xloud",
        "operator_root": "/operator/site-audit",
        "script_path": "/candidate/xloud/deploy-xloud.sh",
        "env_file": "/operator/site-audit/.env",
        "state_volume": adapter._XLOUD_STATE_VOLUME,
        "state_volume_fingerprint": "sha256:" + "8" * 64,
        "network_fingerprints": {"proxmox_macvlan": "a", "site-audit-control": "b"},
        "audit_snapshot": {"container_id": "audit", "started_at": "yesterday", "image_id": "audit-img"},
        "secret_fingerprint": FP,
        "source_revision": HEAD,
        "target_images": TARGETS,
        "image_ids": {"site": SITE_ID, "intake": INTAKE_ID},
    }


def response(mode: str, **kwargs) -> RunResult:
    payload = {
        "version": 1,
        "mode": mode,
        "exit_code": 0,
        "timed_out": False,
        "pending_exists": mode == "inspect",
        "lkg_exists": False,
        "failed_exists": mode == "recover",
        "pending": pending_state() if mode == "inspect" else {},
        "failed": pending_state() if mode == "recover" else {},
        "lkg": {},
        "stdout_tail": [],
        "stderr_tail": [],
    }
    payload.update(kwargs)
    return RunResult(json.dumps(payload), "", 0)


class Client:
    def __init__(self, *, intake=True):
        self.intake = intake
        self.calls = []
        self.rows = {
            "xloud-site": {
                "Id": "old-site-live",
                "Image": OLD_SITE_ID,
                "RestartCount": 0,
                "Config": {"Image": "xloud-site:local"},
                "State": {
                    "Running": True,
                    "StartedAt": "2026-10-28T20:41:22Z",
                    "Health": {"Status": "healthy"},
                },
            },
            "xloud-intake": {
                "Id": "new-intake-live",
                "Image": INTAKE_ID,
                "RestartCount": 0,
                "Config": {"Image": TARGETS["intake"]["image"]},
                "State": {
                    "Running": True,
                    "StartedAt": "2026-10-28T20:37:52Z",
                    "Health": {"Status": "healthy"},
                },
            },
        }
        self.inspect_payload = response("inspect")
        self.recover_payload = response("recover")

    async def run_xloud_deploy_contract_helper(self, **kw):
        self.calls.append(kw)
        if kw["mode"] == "inspect":
            return self.inspect_payload
        if kw["mode"] == "recover":
            self.intake = False
            return self.recover_payload
        raise AssertionError("unexpected mode")

    async def inspect(self, name, max_lines=10):
        assert max_lines == 10
        return [self.rows[name]]

    async def container_network_snapshot(self, name):
        if name == "xloud-site":
            return {"proxmox_macvlan": "10.10.10.149", "xloud_default": "172.30.0.2"}
        if name == "xloud-intake":
            return {"site-audit-control": "172.31.0.8", "xloud_default": "172.30.0.3"}
        raise AssertionError(name)

    async def container_exists(self, name):
        assert name == "xloud-intake"
        return self.intake

    def _sanitize_string(self, value):
        return value


@pytest.fixture
def setup(monkeypatch):
    client = Client()
    monkeypatch.setattr(adapter, "_docker_client", lambda: client)
    monkeypatch.setattr(adapter, "_prepare_xloud_deploy", AsyncMock(return_value=base_plan()))
    monkeypatch.setattr(adapter, "_verify_xloud_guard_resources", AsyncMock())
    return client


@pytest.mark.asyncio
async def test_preflight_is_read_only_and_pins_exact_pending_and_live(setup):
    plan = await adapter._prepare_xloud_recovery(**KWARGS)
    assert [x["mode"] for x in setup.calls] == ["inspect"]
    assert plan["pending"] == pending_state()
    assert plan["pending_fingerprint"] == "sha256:" + hashlib.sha256(
        json.dumps(pending_state(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert plan["live"]["site_image_ref"] == "xloud-site:local"
    assert plan["live"]["intake_present"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["missing", "revision", "previous_intake", "foreign_sidecar", "wrong_old_image", "lkg"],
)
async def test_preflight_refuses_untrusted_or_completed_journal(setup, change):
    payload = json.loads(setup.inspect_payload.stdout)
    if change == "missing":
        payload["pending_exists"] = False
    elif change == "revision":
        payload["pending"]["SOURCE_REVISION"] = "f" * 40
    elif change == "previous_intake":
        payload["pending"]["PREVIOUS_INTAKE_CID"] = "5" * 64
    elif change == "foreign_sidecar":
        setup.rows["xloud-intake"]["Image"] = "sha256:" + "f" * 64
    elif change == "wrong_old_image":
        setup.rows["xloud-site"]["Image"] = "sha256:" + "f" * 64
    elif change == "lkg":
        payload["lkg_exists"] = True
    setup.inspect_payload = RunResult(json.dumps(payload), "", 0)
    with pytest.raises(RuntimeError):
        await adapter._prepare_xloud_recovery(**KWARGS)
    assert [c["mode"] for c in setup.calls] == ["inspect"]


@pytest.mark.asyncio
async def test_public_tool_requires_high_risk_confirmation(setup):
    import examples.mcp_server.server as srv

    result = await adapter.docker_recover_deployment(contract="xloud", parameters=KWARGS)
    assert result["ok"] is True
    action, _ = srv._confirm_store.peek_action_id(result["result"]["action_id"])
    assert action is not None and action.tool == "docker_recover_deployment"
    assert action.required_scope == "mcp:docker:admin"
    assert action.kwargs["pins"]["pending_fingerprint"].startswith("sha256:")
    assert [c["mode"] for c in setup.calls] == ["inspect"]


@pytest.mark.asyncio
async def test_confirmation_rechecks_identity_before_running_recover(setup):
    plan = await adapter._prepare_xloud_recovery(**KWARGS)
    changed = dict(plan)
    changed["pending_fingerprint"] = "sha256:" + "f" * 64
    result = await adapter._docker_recover_xloud_impl(
        **KWARGS,
        pending_fingerprint=changed["pending_fingerprint"],
        live=plan["live"],
        state_volume_fingerprint=plan["state_volume_fingerprint"],
        network_fingerprints=plan["network_fingerprints"],
        audit_snapshot=plan["audit_snapshot"],
        secret_fingerprint=plan["secret_fingerprint"],
        helper_image_id=plan["helper_image_id"],
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_PRECONDITION_FAILED", result
    assert "recover" not in [c["mode"] for c in setup.calls]


@pytest.mark.asyncio
async def test_recovery_accepts_only_exact_failed_state_and_absent_sidecar(setup, monkeypatch):
    plan = await adapter._prepare_xloud_recovery(**KWARGS)
    monkeypatch.setattr(adapter.asyncio, "sleep", AsyncMock())
    result = await adapter._docker_recover_xloud_impl(
        **KWARGS,
        pending_fingerprint=plan["pending_fingerprint"],
        live=plan["live"],
        state_volume_fingerprint=plan["state_volume_fingerprint"],
        network_fingerprints=plan["network_fingerprints"],
        audit_snapshot=plan["audit_snapshot"],
        secret_fingerprint=plan["secret_fingerprint"],
        helper_image_id=plan["helper_image_id"],
    )
    assert result["ok"] is True
    assert result["result"]["status"] == "recovered"
    assert result["result"]["intake_removed"] is True
    assert "recover" in [c["mode"] for c in setup.calls]


@pytest.mark.asyncio
async def test_failed_recovery_keeps_evidence_unproven(setup):
    plan = await adapter._prepare_xloud_recovery(**KWARGS)
    setup.recover_payload = response("recover", pending_exists=True)
    result = await adapter._docker_recover_xloud_impl(
        **KWARGS,
        pending_fingerprint=plan["pending_fingerprint"],
        live=plan["live"],
        state_volume_fingerprint=plan["state_volume_fingerprint"],
        network_fingerprints=plan["network_fingerprints"],
        audit_snapshot=plan["audit_snapshot"],
        secret_fingerprint=plan["secret_fingerprint"],
        helper_image_id=plan["helper_image_id"],
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS", result

def test_inspect_helper_is_read_only_and_never_executes_deploy_script(tmp_path):
    source = tmp_path / "source"
    operator = tmp_path / "operator"
    state = tmp_path / "state"
    source.mkdir()
    operator.mkdir()
    state.mkdir()
    marker = tmp_path / "script_was_executed"
    script = source / "deploy-xloud.sh"
    script.write_text("#!/bin/sh\ntouch " + str(marker) + "\n", encoding="utf-8")
    (operator / ".env").write_text("AUDIT_CONTROL_API_KEY=test-recovery-only\n", encoding="utf-8")
    journal = state / "pending.state"
    journal.write_text(
        "".join(f"{key}={value}\n" for key, value in pending_state().items()),
        encoding="utf-8",
    )
    args = Namespace(
        mode="inspect", source_root=str(source), operator_root=str(operator),
        script=str(script), env_file=str(operator / ".env"), state_dir=str(state),
        site_image=TARGETS["site"]["image"], intake_image=TARGETS["intake"]["image"],
        site_image_id=SITE_ID, intake_image_id=INTAKE_ID, source_revision=HEAD,
        secret_fingerprint="sha256:" + hashlib.sha256(b"test-recovery-only").hexdigest(),
        stability_seconds=90, timeout=120,
    )
    data = runner._run(args)
    assert data["mode"] == "inspect"
    assert data["pending_exists"] is True
    assert data["pending"] == pending_state()
    assert data["failed"] == {}
    assert not marker.exists()
    assert not (state / "last-failed.state").exists()
    assert "test-recovery-only" not in json.dumps(data)


@pytest.mark.asyncio
async def test_inspect_helper_uses_read_only_volume_and_exact_mode(monkeypatch):
    from examples.mcp_client_remote.fleet.docker_client import DockerClient

    client = DockerClient()
    intercepted = AsyncMock(return_value=response("inspect"))
    monkeypatch.setattr(client, "_run_with_result", intercepted)
    await client.run_xloud_deploy_contract_helper(
        helper_image_id="sha256:" + "d" * 64,
        candidate_root="/candidate/xloud", operator_root="/operator/site-audit",
        script_path="/candidate/xloud/deploy-xloud.sh",
        env_file="/operator/site-audit/.env", state_volume=adapter._XLOUD_STATE_VOLUME,
        site_image=TARGETS["site"]["image"], intake_image=TARGETS["intake"]["image"],
        site_image_id=SITE_ID, intake_image_id=INTAKE_ID,
        source_revision=HEAD, secret_fingerprint=FP,
        mode="inspect",
    )
    argv = intercepted.await_args.args[0]
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "inspect"
    assert f"type=volume,src={adapter._XLOUD_STATE_VOLUME},dst=/var/lib/xloud-deploy-state,readonly" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert "/var/run/docker.sock" not in " ".join(argv)

@pytest.mark.asyncio
async def test_generic_catalog_exposes_reviewed_contract_only():
    catalog = await adapter.docker_recovery_catalog()
    assert catalog["ok"]
    entries = catalog["result"]["contracts"]
    assert any(item["id"] == "xloud" for item in entries)
    assert all(item["scope"] == "mcp:docker:admin" for item in entries)
    assert all("parameters" in item and "confirmation" in item for item in entries)


@pytest.mark.asyncio
@pytest.mark.parametrize("contract,parameters", [
    ("arbitrary-shell", KWARGS),
    ("xloud", {**KWARGS, "command": "docker rm -f all"}),
    ("xloud", {key: value for key, value in KWARGS.items() if key != "source_revision"}),
])
async def test_generic_recovery_rejects_unregistered_or_extra_parameters(setup, contract, parameters):
    result = await adapter.docker_recover_deployment(contract=contract, parameters=parameters)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert setup.calls == []


@pytest.mark.asyncio
async def test_generic_confirm_dispatch_uses_only_registered_handler(setup, monkeypatch):
    import examples.mcp_server.server as srv
    prepared = await adapter.docker_recover_deployment(contract="xloud", parameters=KWARGS)
    assert prepared["ok"] is True
    action, _ = srv._confirm_store.peek_action_id(prepared["result"]["action_id"])
    assert action.tool == "docker_recover_deployment"
    assert action.kwargs["contract"] == "xloud"
    monkeypatch.setattr(adapter.asyncio, "sleep", AsyncMock())
    result = await adapter._docker_recover_deployment_impl(**action.kwargs)
    assert result["ok"] is True
    assert result["tool"] == "docker_recover_deployment"
    assert [call["mode"] for call in setup.calls] == ["inspect", "inspect", "recover"]
