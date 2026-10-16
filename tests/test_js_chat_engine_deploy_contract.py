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
    "js_chat_engine_deploy_contract_runner",
    Path("scripts/js_chat_engine_deploy_contract_runner.py"),
)
assert _RUNNER_SPEC is not None and _RUNNER_SPEC.loader is not None
runner = importlib.util.module_from_spec(_RUNNER_SPEC)
_RUNNER_SPEC.loader.exec_module(runner)

REPO = "192.168.1.103:3005/gpakoh/js-chat-engine"
DIGEST = "1" * 64
TARGET_IMAGE = f"{REPO}@sha256:{DIGEST}"
REVISION = "2" * 40
HEAD = "3" * 40
BLOB = "4" * 40
SCRIPT_SHA256 = "5" * 64
HELPER_IMAGE_ID = "sha256:" + "6" * 64
TARGET_IMAGE_ID = "sha256:" + "7" * 64
STATE_FINGERPRINT = "sha256:" + "8" * 64
RUNTIME_FINGERPRINT = "sha256:" + "9" * 64
DEPLOYED_AT = "2026-10-15T00:00:00+00:00"


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


def _plan() -> dict[str, object]:
    return {
        "candidate_root": "/workspace/infra-candidate",
        "operator_root": "/workspace/infra-quart",
        "script_path": "/workspace/infra-candidate/scripts/deploy-js-chat-engine.sh",
        "env_file": "/workspace/infra-quart/.env",
        "state_dir": "/workspace/infra-quart/.state",
        "core_databases_root": "/workspace/quart-core-databases",
        "target_image": TARGET_IMAGE,
        "target_image_id": TARGET_IMAGE_ID,
        "release_revision": REVISION,
        "state_fingerprint": STATE_FINGERPRINT,
        "runtime_fingerprint": RUNTIME_FINGERPRINT,
        "prior_runtime": {"mode": "present"},
        "helper_image_id": HELPER_IMAGE_ID,
    }


def _live() -> dict[str, object]:
    return {
        "container_id": "container-1",
        "image_id": TARGET_IMAGE_ID,
        "image": TARGET_IMAGE,
        "revision": REVISION,
        "health": "healthy",
        "restart_count": 0,
        "started_at": "2026-10-15T00:00:01Z",
        "memory": docker_adapter._JS_CHAT_ENGINE_MEMORY_BYTES,
        "memory_swap": docker_adapter._JS_CHAT_ENGINE_MEMORY_BYTES,
        "read_only": True,
        "user": "nobody",
    }


@pytest.mark.asyncio
async def test_preflight_confirmation_binds_state_runtime_and_release_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ConfirmationStore()
    monkeypatch.setattr(docker_adapter, "_confirm_store", lambda: store)
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_js_chat_engine_deploy",
        AsyncMock(return_value=_plan()),
    )

    signature = inspect.signature(docker_adapter.docker_deploy_contract)
    assert "state_dir" not in signature.parameters
    assert "core_databases_root" not in signature.parameters

    result = await docker_adapter.docker_deploy_contract(
        contract="js-chat-engine",
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
    confirmation = action_kwargs["js_chat_engine_confirmation"]
    assert confirmation == {
        "state_fingerprint": STATE_FINGERPRINT,
        "runtime_fingerprint": RUNTIME_FINGERPRINT,
        "release_revision": REVISION,
    }
    serialized = json.dumps(action_kwargs, sort_keys=True)
    assert ".env" not in serialized
    assert "POSTGRES_PASSWORD" not in serialized
    assert "JS_CHAT_ENGINE_POSTGRES_DSN" not in serialized


@pytest.mark.asyncio
async def test_generic_dispatch_preserves_js_chat_engine_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegated = AsyncMock(return_value={"ok": True, "result": {"status": "accepted"}})
    monkeypatch.setattr(
        docker_adapter,
        "_docker_deploy_js_chat_engine_contract_impl",
        delegated,
    )
    confirmation = {
        "state_fingerprint": STATE_FINGERPRINT,
        "runtime_fingerprint": RUNTIME_FINGERPRINT,
        "release_revision": REVISION,
    }

    result = await docker_adapter._docker_deploy_contract_impl(
        contract="js-chat-engine",
        candidate_project="candidate-infra",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA256,
        target_image=TARGET_IMAGE,
        timeout=480,
        js_chat_engine_confirmation=confirmation,
    )

    assert result["ok"] is True
    delegated.assert_awaited_once_with(
        candidate_project="candidate-infra",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA256,
        target_image=TARGET_IMAGE,
        state_fingerprint=STATE_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
        release_revision=REVISION,
        timeout=480,
    )


def test_release_pin_parser_binds_exact_image_and_revision(tmp_path: Path) -> None:
    release = tmp_path / "deploy" / "js-chat-engine.release.env"
    release.parent.mkdir()
    release.write_text(
        f"JS_CHAT_ENGINE_IMAGE_REPOSITORY={REPO}\n"
        f"JS_CHAT_ENGINE_IMAGE_DIGEST={DIGEST}\n"
        f"JS_CHAT_ENGINE_RELEASE_GIT_SHA={REVISION}\n"
        "JS_CHAT_ENGINE_APPS_JSON='{}'\n",
        encoding="utf-8",
    )

    parsed = docker_adapter._js_chat_engine_release_pin(tmp_path)
    assert parsed["image"] == TARGET_IMAGE
    assert parsed["revision"] == REVISION

    release.write_text(release.read_text(encoding="utf-8") + f"JS_CHAT_ENGINE_IMAGE_DIGEST={DIGEST}\n")
    with pytest.raises(ValueError, match="more than once"):
        docker_adapter._js_chat_engine_release_pin(tmp_path)


@pytest.mark.asyncio
async def test_confirmation_revalidates_state_and_runtime_before_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = _plan()
    changed["runtime_fingerprint"] = "sha256:changed"
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_js_chat_engine_deploy",
        AsyncMock(return_value=changed),
    )
    client = SimpleNamespace(run_js_chat_engine_deploy_contract_helper=AsyncMock())
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)

    result = await docker_adapter._docker_deploy_js_chat_engine_contract_impl(
        candidate_project="candidate-infra",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA256,
        target_image=TARGET_IMAGE,
        state_fingerprint=STATE_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
        release_revision=REVISION,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_PRECONDITION_FAILED"
    client.run_js_chat_engine_deploy_contract_helper.assert_not_called()


@pytest.mark.asyncio
async def test_success_requires_helper_state_durable_state_and_stable_live_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_js_chat_engine_deploy",
        AsyncMock(return_value=_plan()),
    )
    helper_payload = {
        "version": 1,
        "exit_code": 0,
        "output_tail": "Deploy OK: exact digest is healthy and real Core/Astra smoke passed.",
        "state": {"image": TARGET_IMAGE, "git_sha": REVISION, "deployed_at": DEPLOYED_AT},
    }
    client = SimpleNamespace(
        run_js_chat_engine_deploy_contract_helper=AsyncMock(
            return_value=RunResult(stdout=json.dumps(helper_payload), stderr="", exit_code=0)
        ),
        _sanitize_string=lambda value: value,
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    monkeypatch.setattr(
        docker_adapter,
        "_js_chat_engine_read_state",
        lambda _root: {
            "exists": True,
            "state": {"image": TARGET_IMAGE, "git_sha": REVISION, "deployed_at": DEPLOYED_AT},
        },
    )
    verify = AsyncMock(side_effect=[_live(), _live()])
    monkeypatch.setattr(docker_adapter, "_verify_js_chat_engine_live", verify)
    sleep = AsyncMock()
    monkeypatch.setattr(docker_adapter.asyncio, "sleep", sleep)

    result = await docker_adapter._docker_deploy_js_chat_engine_contract_impl(
        candidate_project="candidate-infra",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA256,
        target_image=TARGET_IMAGE,
        state_fingerprint=STATE_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
        release_revision=REVISION,
    )

    assert result["ok"] is True
    assert result["result"]["reconciled_after_unknown"] is False
    assert verify.await_count == 2
    sleep.assert_awaited_once_with(docker_adapter._JS_CHAT_ENGINE_VERIFY_STABILITY_SECONDS)
    helper_kwargs = client.run_js_chat_engine_deploy_contract_helper.await_args.kwargs
    assert helper_kwargs["target_image"] == TARGET_IMAGE
    assert helper_kwargs["release_revision"] == REVISION


@pytest.mark.asyncio
async def test_timeout_without_target_state_and_live_proof_is_ambiguous_and_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docker_adapter,
        "_prepare_js_chat_engine_deploy",
        AsyncMock(return_value=_plan()),
    )
    client = SimpleNamespace(
        run_js_chat_engine_deploy_contract_helper=AsyncMock(
            return_value=RunResult(stdout="", stderr="timed out", exit_code=-1)
        ),
        _sanitize_string=lambda value: value,
    )
    monkeypatch.setattr(docker_adapter, "_docker_client", lambda: client)
    monkeypatch.setattr(
        docker_adapter,
        "_js_chat_engine_read_state",
        lambda _root: {"exists": False, "state": {}},
    )

    result = await docker_adapter._docker_deploy_js_chat_engine_contract_impl(
        candidate_project="candidate-infra",
        expected_head_sha=HEAD,
        expected_script_blob_sha=BLOB,
        expected_script_sha256=SCRIPT_SHA256,
        target_image=TARGET_IMAGE,
        state_fingerprint=STATE_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
        release_revision=REVISION,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS"
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_docker_client_helper_has_fixed_bounded_mounts() -> None:
    client = DockerClient()
    captured = AsyncMock(return_value=RunResult(stdout="{}", stderr="", exit_code=0))
    client._run_with_result = captured  # type: ignore[method-assign]

    await client.run_js_chat_engine_deploy_contract_helper(
        helper_image_id=HELPER_IMAGE_ID,
        candidate_root="/workspace/infra-candidate",
        operator_root="/workspace/infra-quart",
        script_path="/workspace/infra-candidate/scripts/deploy-js-chat-engine.sh",
        env_file="/workspace/infra-quart/.env",
        state_dir="/workspace/infra-quart/.state",
        core_databases_root="/workspace/quart-core-databases",
        target_image=TARGET_IMAGE,
        release_revision=REVISION,
        timeout=480,
    )

    argv = captured.await_args.args[0]
    assert argv[0:3] == ["/usr/bin/docker", "run", "--rm"]
    assert argv[argv.index("--network") + 1] == "host"
    assert "/app/scripts/js_chat_engine_deploy_contract_runner.py" in argv
    mounts = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--mount"]
    assert "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock" in mounts
    assert "type=bind,src=/workspace/infra-candidate,dst=/workspace/infra-candidate,readonly" in mounts
    assert "type=bind,src=/workspace/infra-quart,dst=/workspace/infra-quart,readonly" in mounts
    assert "type=bind,src=/workspace/infra-quart/.state,dst=/workspace/infra-quart/.state" in mounts
    assert "type=bind,src=/workspace/quart-core-databases,dst=/workspace/quart-core-databases" in mounts
    joined = " ".join(argv)
    assert "POSTGRES_PASSWORD" not in joined
    assert "JS_CHAT_ENGINE_POSTGRES_DSN" not in joined


def test_runner_strips_inherited_release_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    (source_root / "deploy").mkdir(parents=True)
    script = source_root / "scripts" / "deploy-js-chat-engine.sh"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    release = source_root / "deploy" / "js-chat-engine.release.env"
    release.write_text("JS_CHAT_ENGINE_IMAGE_REPOSITORY=repo\n", encoding="utf-8")
    operator_root = tmp_path / "operator"
    state_dir = operator_root / ".state"
    state_dir.mkdir(parents=True)
    env_file = operator_root / ".env"
    env_file.write_text("POSTGRES_PASSWORD=secret\n", encoding="utf-8")
    core_root = tmp_path / "quart-core-databases"
    core_root.mkdir()
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> object:
        captured["env"] = kwargs["env"]
        return runner.subprocess.CompletedProcess(args[0], 1, stdout="failed", stderr=None)

    monkeypatch.setenv("JS_CHAT_ENGINE_IMAGE_DIGEST", "evil")
    monkeypatch.setenv("JS_CHAT_ENGINE_RELEASE_GIT_SHA", "evil")
    monkeypatch.setenv("PROD_ENV_FILE", "/evil")
    monkeypatch.setattr(runner.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "js_chat_engine_deploy_contract_runner.py",
            "--script",
            str(script),
            "--source-root",
            str(source_root),
            "--operator-root",
            str(operator_root),
            "--env-file",
            str(env_file),
            "--state-dir",
            str(state_dir),
            "--core-databases-root",
            str(core_root),
            "--target-image",
            TARGET_IMAGE,
            "--release-revision",
            REVISION,
            "--timeout",
            "60",
        ],
    )

    assert runner.main() == 1
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "JS_CHAT_ENGINE_IMAGE_DIGEST" not in child_env
    assert "JS_CHAT_ENGINE_RELEASE_GIT_SHA" not in child_env
    assert child_env["PROD_ENV_FILE"] == str(env_file)
    assert child_env["PROD_STATE_DIR"] == str(state_dir)
    assert child_env["QUART_CORE_DATABASES_ROOT"] == str(core_root)
