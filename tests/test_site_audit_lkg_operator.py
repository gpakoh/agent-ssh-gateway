"""Site Audit historical LKG archive admission is ADMIN-gated and fail-closed."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES / "mcp_server"))
sys.path.insert(0, str(EXAMPLES.parent))

from examples.mcp_client_remote.fleet.docker_client import DockerClient, RunResult  # noqa: E402
from examples.mcp_server.mcp_infra.adapters import docker as adapter  # noqa: E402
from examples.mcp_server.tool_modes import should_register_tool  # noqa: E402
from examples.mcp_server.tool_scopes import get_required_scopes  # noqa: E402

NEW_SHA = "a" * 40
OLD_SHA = "b" * 40
SCRIPT_BLOB = "c" * 40
SCRIPT_SHA = "d" * 64
CANDIDATE = "candidate-site-audit-recovery-clean-main"
STATE_HASH = "e" * 64


def receipt():
    return {
        "schema_version": 1,
        "kind": "site_audit_legacy_lkg_recovery_evidence",
        "status": "forensic_preflight_only",
        "accepted_lkg": False,
        "authorizes_deployment": False,
        "historical_state_sha256": STATE_HASH,
        "historical_generation": OLD_SHA[:12] + "-20261018T010203Z",
        "source_revision": OLD_SHA,
        "historically_missing_container": "site-audit-crm-ingress",
        "historical_target_identity_drift": [],
        "historical_protected_identity_drift": [],
        "current_target_observations": {
            name: {
                "container_id": f"target-{idx}",
                "started_at": f"start-{idx}",
                "image": f"registry.example/{name}:{OLD_SHA}",
                "image_id": "sha256:" + f"{idx + 1:x}" * 64,
                "revision": OLD_SHA,
                "restart_count": 0,
                "differs_from_historical": False,
            }
            for idx, (_service, name, _image) in enumerate(adapter._SITE_AUDIT_TARGETS)
        },
        "current_protected_observations_not_backdated": {
            name: {
                "container_id": f"protected-{idx}",
                "started_at": f"protected-start-{idx}",
                **({"differs_from_historical": False}
                   if name != "site-audit-crm-ingress" else {}),
            }
            for idx, name in enumerate(adapter._SITE_AUDIT_PROTECTED_CONTAINERS)
        },
        "independent_stability_seconds": 10,
    }


def test_forensic_target_drift_is_recorded_without_lkg_acceptance():
    value = receipt()
    value["historical_target_identity_drift"] = ["site-audit-api"]
    value["current_target_observations"]["site-audit-api"]["differs_from_historical"] = True
    observed = adapter._verified_forensic_receipt(run(value), OLD_SHA)
    assert observed["historical_target_identity_drift"] == ["site-audit-api"]
    assert observed["accepted_lkg"] is False
    assert observed["authorizes_deployment"] is False


def call_args():
    return {
        "candidate_project": CANDIDATE,
        "expected_head_sha": NEW_SHA,
        "expected_script_blob_sha": SCRIPT_BLOB,
        "expected_script_sha256": SCRIPT_SHA,
        "historical_revision": OLD_SHA,
    }


def plan():
    return {
        "candidate_root": "/verified/candidate",
        "script_path": "/verified/candidate/scripts/prepare_site_audit_lkg_recovery.py",
        "state_volume": adapter._SITE_AUDIT_STATE_VOLUME,
        "helper_image_id": "sha256:" + "8" * 64,
        "historical_revision": OLD_SHA,
        "expected_head_sha": NEW_SHA,
        "expected_script_blob_sha": SCRIPT_BLOB,
        "expected_script_sha256": SCRIPT_SHA,
    }


def run(value=None):
    return RunResult(json.dumps(receipt() if value is None else value), "", 0)


class FakeClient:
    def __init__(self, responses=None):
        self.responses = list(responses if responses is not None else [run()])
        self.calls = []

    async def run_site_audit_lkg_forensics(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(accepted_lkg=True),
    lambda r: r.update(authorizes_deployment=True),
    lambda r: r.update(status="accepted_lkg"),
    lambda r: r.update(historical_state_sha256="wrong"),
    lambda r: r["current_protected_observations_not_backdated"].pop("site-audit-crm-ingress"),
    lambda r: r["current_target_observations"].pop("site-audit-api"),
    lambda r: r.update(source_revision=NEW_SHA),
    lambda r: r.update(historical_protected_identity_drift=["unknown"]),
    lambda r: r.update(historical_target_identity_drift=["site-audit-api"]),
    lambda r: r["current_target_observations"]["site-audit-api"].pop("differs_from_historical"),
    lambda r: r["current_target_observations"]["site-audit-api"].update(differs_from_historical=True),
])
def test_forensic_receipt_must_not_promote_historical_lkg(mutate):
    value = receipt()
    mutate(value)
    with pytest.raises(RuntimeError):
        adapter._verified_forensic_receipt(run(value), OLD_SHA)


@pytest.mark.asyncio
async def test_operator_preflight_requests_high_risk_confirmation_only(monkeypatch):
    from examples.mcp_server import server as srv

    srv._confirm_store._actions.clear()
    client = FakeClient([run()])
    monkeypatch.setattr(adapter, "_docker_client", lambda: client)
    monkeypatch.setattr(adapter, "_prepare_site_audit_lkg_forensics", AsyncMock(return_value=plan()))
    result = await adapter.docker_prepare_site_audit_lkg_recovery(**call_args(), timeout=240)
    assert result["ok"] is True
    pending = result["result"]
    assert pending["status"] == "confirmation_required"
    assert pending["risk"] == "high"
    action, _ = srv._confirm_store.peek_action_id(pending["action_id"])
    assert action.tool == "docker_prepare_site_audit_lkg_recovery"
    assert action.required_scope == "mcp:docker:admin"
    assert action.kwargs["expected_historical_state_sha256"] == STATE_HASH
    assert action.kwargs["expected_receipt_fingerprint"] == adapter._site_audit_forensic_fingerprint(receipt())
    assert len(client.calls) == 1 and client.calls[0]["archive"] is False
    srv._confirm_store._actions.clear()


@pytest.mark.asyncio
async def test_confirm_requalifies_before_append_only_archive(monkeypatch):
    client = FakeClient([run(), run()])
    monkeypatch.setattr(adapter, "_docker_client", lambda: client)
    monkeypatch.setattr(adapter, "_prepare_site_audit_lkg_forensics", AsyncMock(return_value=plan()))
    result = await adapter._docker_prepare_site_audit_lkg_recovery_impl(
        **call_args(),
        expected_historical_state_sha256=STATE_HASH,
        expected_receipt_fingerprint=adapter._site_audit_forensic_fingerprint(receipt()),
        timeout=240,
    )
    assert result["ok"] is True
    assert result["result"]["status"] == "forensic_archive_created"
    assert result["result"]["accepted_lkg"] is False
    assert [c["archive"] for c in client.calls] == [False, True]
    assert all(c["state_volume"] == adapter._SITE_AUDIT_STATE_VOLUME for c in client.calls)


@pytest.mark.asyncio
async def test_confirm_refuses_stale_lease_without_archiving(monkeypatch):
    changed = receipt()
    changed["current_target_observations"]["site-audit-api"]["container_id"] = "changed"
    client = FakeClient([run(changed)])
    monkeypatch.setattr(adapter, "_docker_client", lambda: client)
    monkeypatch.setattr(adapter, "_prepare_site_audit_lkg_forensics", AsyncMock(return_value=plan()))
    with pytest.raises(RuntimeError, match="changed"):
        await adapter._docker_prepare_site_audit_lkg_recovery_impl(
            **call_args(), expected_historical_state_sha256=STATE_HASH,
            expected_receipt_fingerprint=adapter._site_audit_forensic_fingerprint(receipt()),
        )
    assert [c["archive"] for c in client.calls] == [False]


@pytest.mark.asyncio
async def test_confirm_refuses_different_archive_observation(monkeypatch):
    changed = receipt()
    changed["current_protected_observations_not_backdated"]["site-audit-crm-web"]["container_id"] = "changed"
    client = FakeClient([run(), run(changed)])
    monkeypatch.setattr(adapter, "_docker_client", lambda: client)
    monkeypatch.setattr(adapter, "_prepare_site_audit_lkg_forensics", AsyncMock(return_value=plan()))
    with pytest.raises(RuntimeError, match="changed"):
        await adapter._docker_prepare_site_audit_lkg_recovery_impl(
            **call_args(), expected_historical_state_sha256=STATE_HASH,
            expected_receipt_fingerprint=adapter._site_audit_forensic_fingerprint(receipt()),
        )
    assert [c["archive"] for c in client.calls] == [False, True]


@pytest.mark.asyncio
async def test_preflight_failure_never_creates_confirmation(monkeypatch):
    from examples.mcp_server import server as srv

    srv._confirm_store._actions.clear()
    monkeypatch.setattr(adapter, "_prepare_site_audit_lkg_forensics",
                        AsyncMock(side_effect=ValueError("source SHA mismatch")))
    result = await adapter.docker_prepare_site_audit_lkg_recovery(**call_args())
    assert result["ok"] is False
    assert not srv._confirm_store._actions


@pytest.mark.asyncio
async def test_docker_client_forensic_helper_has_fixed_scope(monkeypatch):
    fake = object.__new__(DockerClient)
    fake._validate_deploy_mount_path = lambda v, name: v
    fake._validate_volume_name = lambda x: x
    captured = []

    async def capture(argv, *, timeout):
        captured.append((argv, timeout))
        return run()

    fake._run_with_result = capture
    base = {
        "helper_image_id": "sha256:" + "8" * 64,
        "candidate_root": "/verified/candidate",
        "script_path": "/verified/candidate/scripts/prepare_site_audit_lkg_recovery.py",
        "state_volume": adapter._SITE_AUDIT_STATE_VOLUME,
        "historical_revision": OLD_SHA,
        "expected_script_sha256": "d" * 64,
    }
    await fake.run_site_audit_lkg_forensics(**base, archive=False)
    args = captured[-1][0]
    assert "--archive" not in args
    assert "type=volume,src=ssh-gateway-site-audit-deploy-state,dst=/var/lib/site-audit-deploy-state,readonly" in args
    assert "--network" in args and args[args.index("--network") + 1] == "none"
    assert "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock,readonly" in args
    assert "compose" not in args
    await fake.run_site_audit_lkg_forensics(**base, archive=True)
    args = captured[-1][0]
    assert args[-1] == "--archive"
    assert "type=volume,src=ssh-gateway-site-audit-deploy-state,dst=/var/lib/site-audit-deploy-state" in args
    assert all(a != "/operator" for a in args)


def test_new_tool_has_admin_scope_and_mode_parity():
    assert get_required_scopes("docker_prepare_site_audit_lkg_recovery") == ["mcp:docker:admin"]
    assert should_register_tool("docker_prepare_site_audit_lkg_recovery", "mcp_client_write")
    assert should_register_tool("docker_prepare_site_audit_lkg_recovery", "mcp_client")
    # Runtime ADMIN scope is mandatory even when the tool is discoverable.
    assert adapter._CONFIRM_HANDLERS["docker_prepare_site_audit_lkg_recovery"] is adapter._docker_prepare_site_audit_lkg_recovery_impl


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ("moved_head", "HEAD moved"),
        ("dirty", "dirty"),
        ("wrong_blob", "Git blob mismatch"),
        ("wrong_sha256", "SHA-256 mismatch"),
        ("wrong_volume", "volume identity mismatch"),
    ],
)
async def test_forensic_source_and_volume_preflight_is_content_bound(
    tmp_path, monkeypatch, condition, message
):
    """Never issue even a read-only container probe from an untrusted source."""
    import hashlib

    root = tmp_path / "candidate"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "prepare_site_audit_lkg_recovery.py"
    script.write_text("# trusted script fixture\n", encoding="utf-8")
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    registry = MagicMock()
    registry.project_info.return_value = {"root": str(root), "type": "candidate-clone"}
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda key: (lambda: registry) if key == "_get_workspace_registry" else None,
    )
    monkeypatch.setattr(
        adapter, "_read_candidate_metadata",
        lambda _: {
            "project_id": CANDIDATE,
            "source_project": adapter._SITE_AUDIT_SOURCE_PROJECT,
            "head": NEW_SHA,
            "base_sha": NEW_SHA,
        },
    )

    def fake_git(_root, *args):
        if args == ("rev-parse", "HEAD"):
            return "f" * 40 if condition == "moved_head" else NEW_SHA
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return " M fixture" if condition == "dirty" else ""
        if args == ("rev-parse", f"{NEW_SHA}:{adapter._SITE_AUDIT_FORENSIC_SCRIPT}"):
            return "f" * 40 if condition == "wrong_blob" else SCRIPT_BLOB
        raise AssertionError(args)

    monkeypatch.setattr(adapter, "_git_capture", fake_git)

    class FakePrepareClient:
        async def volume_metadata(self, name):
            if condition == "wrong_volume":
                return {"Name": "other", "Driver": "local", "Options": {}}
            return {"Name": name, "Driver": "local", "Options": {}}

        async def container_image_id(self, _name):
            return "sha256:" + "8" * 64

    monkeypatch.setattr(adapter, "_docker_client", lambda: FakePrepareClient())
    kwargs = {
        **call_args(),
        "expected_script_sha256": "f" * 64 if condition == "wrong_sha256" else digest,
    }
    with pytest.raises((ValueError, RuntimeError), match=message):
        await adapter._prepare_site_audit_lkg_forensics(**kwargs)


@pytest.mark.asyncio
async def test_forensic_preflight_binds_source_and_volume_on_valid_fixture(tmp_path, monkeypatch):
    import hashlib

    root = tmp_path / "candidate"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts" / "prepare_site_audit_lkg_recovery.py"
    script.write_text("# pinned fixture\n", encoding="utf-8")
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    reg = MagicMock()
    reg.project_info.return_value = {"root": str(root), "type": "candidate-clone"}
    monkeypatch.setattr(
        adapter, "server_attr",
        lambda key: (lambda: reg) if key == "_get_workspace_registry" else None,
    )
    monkeypatch.setattr(
        adapter, "_read_candidate_metadata",
        lambda _: {
            "project_id": CANDIDATE,
            "source_project": adapter._SITE_AUDIT_SOURCE_PROJECT,
            "head": NEW_SHA,
            "base_sha": NEW_SHA,
        },
    )

    def fake_git(_root, *args):
        if args == ("rev-parse", "HEAD"):
            return NEW_SHA
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return ""
        if args == ("rev-parse", f"{NEW_SHA}:{adapter._SITE_AUDIT_FORENSIC_SCRIPT}"):
            return SCRIPT_BLOB
        raise AssertionError(args)

    monkeypatch.setattr(adapter, "_git_capture", fake_git)

    class FakePrepareClient:
        async def volume_metadata(self, name):
            return {"Name": name, "Driver": "local", "Options": {}}

        async def container_image_id(self, _name):
            return "sha256:" + "8" * 64

    monkeypatch.setattr(adapter, "_docker_client", lambda: FakePrepareClient())
    result = await adapter._prepare_site_audit_lkg_forensics(
        **{**call_args(), "expected_script_sha256": digest}
    )
    assert result["candidate_root"] == str(root)
    assert result["script_path"] == str(script)
    assert result["state_volume"] == adapter._SITE_AUDIT_STATE_VOLUME
    assert result["historical_revision"] == OLD_SHA

@pytest.mark.asyncio
async def test_forensic_bootstrap_executes_only_digest_verified_bytes(tmp_path):
    """A changed candidate file must not execute with the Docker socket."""
    import hashlib
    import subprocess

    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    script = script_dir / "prepare_site_audit_lkg_recovery.py"
    initial = b"print('PINNED_FORENSIC_SCRIPT_OK')\n"
    script.write_bytes(initial)
    digest = hashlib.sha256(initial).hexdigest()
    client = object.__new__(DockerClient)
    client._validate_deploy_mount_path = lambda value, _field: value
    client._validate_volume_name = lambda value: value
    commands = []

    async def capture(argv, *, timeout):
        commands.append(argv)
        return run()

    client._run_with_result = capture
    await client.run_site_audit_lkg_forensics(
        helper_image_id="sha256:" + "8" * 64,
        candidate_root=str(tmp_path),
        script_path=str(script),
        state_volume=adapter._SITE_AUDIT_STATE_VOLUME,
        historical_revision=OLD_SHA,
        expected_script_sha256=digest,
        archive=False,
    )
    argv = commands[0]
    index = argv.index("python3")
    assert argv[index + 1:index + 3] == ["-I", "-c"]
    bootstrap = argv[index + 3]
    assert argv[index + 4] == str(script)
    assert argv[index + 5] == digest

    def check():
        return subprocess.run(
            [sys.executable, "-I", "-c", bootstrap, str(script), digest],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    good = check()
    assert good.returncode == 0, good.stderr
    assert good.stdout.strip() == "PINNED_FORENSIC_SCRIPT_OK"

    script.write_bytes(b"print('UNTRUSTED_SCRIPT_EXECUTED')\n")
    bad = check()
    assert bad.returncode != 0
    assert "UNTRUSTED_SCRIPT_EXECUTED" not in bad.stdout
    script.unlink()
    script.symlink_to(script_dir / "nonexistent")
    symlinked = check()
    assert symlinked.returncode != 0
