"""Fail-closed deploy script outcome and output-provenance regressions (#878).

A repo-owned deploy script can have changed Docker state before the Gateway
times out or receives corrupt helper evidence. In neither case is blindly
replaying it safe. Script stdout is not a trusted diagnostic/secret surface.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from examples.mcp_client_remote.fleet.docker_client import RunResult
from examples.mcp_server.mcp_infra.adapters import docker as adapter
from scripts import deploy_contract_runner as helper

HEAD = "f" * 40
BLOB = "a" * 40
SCRIPT_SHA = "b" * 64
IMAGE = "registry.internal/audited@sha256:" + "c" * 64
SECRET = "DSN=postgres://operator:secret-value@host/db"


def _plan() -> dict:
    return {
        "spec": {"verify_container": "audited-container", "image_env": "GPT_BRIDGE_TARGET_IMAGE",
                 "profile_volume": "audited-volume"},
        "candidate_root": "/managed/source",
        "infra_root": "/managed/infra",
        "script_path": "/managed/source/deploy.sh",
        "state_file": "/managed/infra/deploy.json",
        "target_image_id": "sha256:" + "d" * 64,
        "helper_image_id": "sha256:" + "e" * 64,
        "profile_mountpoint": "/var/lib/docker/volumes/audited/_data",
    }


def _bind(monkeypatch, *, run: RunResult | None = None, exc: Exception | None = None):
    monkeypatch.setattr(adapter, "_prepare_deploy_contract", AsyncMock(return_value=_plan()))

    class Fake:
        async def run_deploy_contract_helper(self, **kwargs):
            if exc is not None:
                raise exc
            assert run is not None
            return run

        def _sanitize_string(self, value: str) -> str:
            # Unknown credentials cannot be guessed from a static sanitizer.
            return value

    client = Fake()
    monkeypatch.setattr(adapter, "_docker_client", lambda: client)
    return client


async def _execute():
    return await adapter._docker_deploy_contract_impl(
        contract="gpt-browser-bridge",
        candidate_project="candidate-audited",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA,
        target_image=IMAGE,
    )


@pytest.mark.asyncio
async def test_outer_timeout_is_unknown_and_never_retryable_or_secret_bearing(monkeypatch):
    _bind(monkeypatch, run=RunResult(SECRET, SECRET, -1))
    result = await _execute()
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS"
    assert result["error"]["retryable"] is False
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_invalid_json_after_possible_dispatch_is_unknown_not_safe_failure(monkeypatch):
    _bind(monkeypatch, run=RunResult(SECRET, "", 0))
    result = await _execute()
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS"
    assert result["error"]["retryable"] is False
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_helper_script_timeout_is_unknown_even_with_structured_receipt(monkeypatch):
    payload = {"version": 1, "exit_code": 124, "output_tail": SECRET, "error": SECRET,
               "state": {}}
    _bind(monkeypatch, run=RunResult(json.dumps(payload), "", 124))
    result = await _execute()
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS"
    assert result["error"]["retryable"] is False
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_helper_error_string_is_not_authoritative_diagnostic(monkeypatch):
    payload = {"version": 1, "exit_code": 2, "output_tail": SECRET, "error": SECRET,
               "state": {}}
    _bind(monkeypatch, run=RunResult(json.dumps(payload), "", 2))
    result = await _execute()
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_FAILED"
    assert result["error"]["retryable"] is False
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_helper_launch_exception_is_ambiguous_not_echoed(monkeypatch):
    _bind(monkeypatch, exc=RuntimeError(SECRET))
    result = await _execute()
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS"
    assert result["error"]["retryable"] is False
    assert SECRET not in repr(result)


def test_one_shot_helper_never_emits_untrusted_script_stdout(capsys):
    helper._emit(exit_code=0, output=SECRET + "\n" * 2, state={"deployed_at": "ok"})
    report = json.loads(capsys.readouterr().out)
    assert report["exit_code"] == 0
    assert SECRET not in repr(report)
    assert report["output_tail"] in {"", "[redacted: deploy-script output]"}


def test_one_shot_helper_never_emits_untrusted_timeout_stdout(capsys):
    helper._emit(exit_code=124, output=SECRET, error="deploy contract timed out")
    report = json.loads(capsys.readouterr().out)
    assert report["exit_code"] == 124
    assert SECRET not in repr(report)
    assert report["output_tail"] in {"", "[redacted: deploy-script output]"}


@pytest.mark.parametrize("deployed_at", [SECRET, "\n" + SECRET, "not-a-timestamp", 42])
def test_deploy_evidence_rejects_untrusted_deployed_at_before_return(deployed_at):
    inspected = {
        "Id": "e" * 64, "Image": "sha256:" + "d" * 64, "RestartCount": 0,
        "Config": {"Image": IMAGE, "Labels": {
            "deploy.generation": "f" * 64,
            "com.docker.compose.project": "infra-quart",
            "com.docker.compose.service": "gpt-browser-bridge",
        }},
        "State": {"Health": {"Status": "healthy"}, "StartedAt": "2026-10-05T00:00:00Z"},
    }
    state = {
        "gpt_browser_bridge_image": IMAGE,
        "gpt_browser_bridge_image_id": "sha256:" + "d" * 64,
        "gpt_browser_bridge_container_id": "e" * 64,
        "gpt_browser_bridge_deploy_generation": "f" * 64,
        "gpt_browser_bridge_compose_config_fingerprint": "1" * 64,
        "deployed_at": deployed_at,
    }
    with pytest.raises(RuntimeError, match="deployment state timestamp"):
        adapter._verify_deploy_evidence(
            spec={
                "generation_label": "deploy.generation",
                "compose_project": "infra-quart",
                "compose_service": "gpt-browser-bridge",
            },
            inspected=inspected,
            target_image=IMAGE,
            target_image_id="sha256:" + "d" * 64,
            state=state,
        )


def _valid_state() -> dict:
    return {
        "gpt_browser_bridge_image": IMAGE,
        "gpt_browser_bridge_image_id": "sha256:" + "d" * 64,
        "gpt_browser_bridge_compose_config_fingerprint": "1" * 64,
        "gpt_browser_bridge_container_id": "e" * 64,
        "gpt_browser_bridge_deploy_generation": "f" * 64,
        "deployed_at": "2026-11-18T00:00:00+00:00",
    }


def test_helper_reads_exact_state_format_without_emitting_stdout_secrets(tmp_path):
    file = tmp_path / "deploy.json"
    file.write_text(json.dumps(_valid_state()), encoding="utf-8")
    assert helper._read_state(file) == _valid_state()


@pytest.mark.parametrize("field", [
    "gpt_browser_bridge_image", "gpt_browser_bridge_image_id",
    "gpt_browser_bridge_compose_config_fingerprint",
    "gpt_browser_bridge_container_id",
    "gpt_browser_bridge_deploy_generation", "deployed_at",
])
def test_helper_rejects_untrusted_state_payload_before_serialization(tmp_path, field):
    state = _valid_state()
    state[field] = SECRET
    file = tmp_path / "deploy.json"
    file.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        helper._read_state(file)
    assert SECRET not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [
    {"version": True, "exit_code": 0, "state": {}},
    {"version": 2, "exit_code": 0, "state": {}},
    {"version": 1, "exit_code": True, "state": {}},
    {"version": 1, "exit_code": "0", "state": {}},
    {"version": 1, "exit_code": 5, "state": {}},
])
async def test_unsupported_helper_protocol_is_not_replay_authority(monkeypatch, wire):
    wire["output_tail"] = SECRET
    _bind(monkeypatch, run=RunResult(json.dumps(wire), "", 0))
    result = await _execute()
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS"
    assert result["error"]["retryable"] is False
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_accepted_deploy_never_echoes_untrusted_success_output(monkeypatch):
    payload = {
        "version": 1, "exit_code": 0,
        "output_tail": SECRET, "error": SECRET, "state": _valid_state(),
    }
    _bind(monkeypatch, run=RunResult(json.dumps(payload), "", 0))
    fake_evidence = {
        "container_id": "e" * 64, "restart_count": 0,
        "started_at": "2026-11-18T00:00:00Z", "deploy_generation": "f" * 64,
    }
    monkeypatch.setattr(adapter, "_verify_deploy_evidence", lambda **kwargs: dict(fake_evidence))
    monkeypatch.setattr(adapter.asyncio, "sleep", AsyncMock())

    # An inspect-only stub for the existing fake client, preserving the exact
    # zero-Docker-side-effect unit-test contract.
    client = adapter._docker_client()
    client.inspect = AsyncMock(return_value={"Id": "e" * 64})
    result = await _execute()
    assert result["ok"] is True
    assert result["result"]["deployment"]["container_id"] == "e" * 64
    assert SECRET not in repr(result)


def _audited_runtime_inspect() -> dict:
    return {
        "Id": "e" * 64,
        "Image": "sha256:" + "d" * 64,
        "RestartCount": 0,
        "Config": {
            "Image": IMAGE,
            "Labels": {
                "deploy.generation": "f" * 64,
                "com.docker.compose.project": "infra-quart",
                "com.docker.compose.service": "gpt-browser-bridge",
            },
        },
        "State": {
            "Health": {"Status": "healthy"},
            "StartedAt": "2026-11-18T00:00:01Z",
        },
    }


@pytest.mark.parametrize("key,wrong_value", [
    ("com.docker.compose.project", "other-project"),
    ("com.docker.compose.service", "other-service"),
    ("com.docker.compose.project", None),
    ("com.docker.compose.service", None),
])
def test_deploy_verifier_refuses_foreign_compose_runtime_even_if_digest_matches(key, wrong_value):
    inspected = _audited_runtime_inspect()
    inspected["Config"]["Labels"][key] = wrong_value
    with pytest.raises(RuntimeError, match="Compose"):
        adapter._verify_deploy_evidence(
            spec={
                "generation_label": "deploy.generation",
                "compose_project": "infra-quart",
                "compose_service": "gpt-browser-bridge",
            },
            inspected=inspected,
            target_image=IMAGE,
            target_image_id="sha256:" + "d" * 64,
            state=_valid_state(),
        )


def test_deploy_verifier_accepts_exact_compose_project_service_and_digest():
    result = adapter._verify_deploy_evidence(
        spec={
            "generation_label": "deploy.generation",
            "compose_project": "infra-quart",
            "compose_service": "gpt-browser-bridge",
        },
        inspected=_audited_runtime_inspect(),
        target_image=IMAGE,
        target_image_id="sha256:" + "d" * 64,
        state=_valid_state(),
    )
    assert result["image_id"] == "sha256:" + "d" * 64
    assert result["deploy_generation"] == "f" * 64
