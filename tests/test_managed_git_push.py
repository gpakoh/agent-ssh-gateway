from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from examples.mcp_server import managed_git
from examples.mcp_server.mcp_infra.adapters import remote

SHA = "4e846348f293539593a194236b42414336d22576"


def test_configured_git_base_requires_credential_free_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITEA_GIT_BASE", "http://gitea:3000")
    with pytest.raises(managed_git.ManagedGitError, match="requires an HTTPS"):
        managed_git.configured_gitea_git_base()

    monkeypatch.setenv("GITEA_GIT_BASE", "https://user:secret@git.example.test")
    with pytest.raises(managed_git.ManagedGitError, match="credential-free"):
        managed_git.configured_gitea_git_base()

    monkeypatch.setenv("GITEA_GIT_BASE", "https://git.example.test/prefix")
    with pytest.raises(managed_git.ManagedGitError, match="path prefix"):
        managed_git.configured_gitea_git_base()


def test_configured_git_base_can_use_forwarded_https_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITEA_GIT_BASE", raising=False)
    monkeypatch.setenv("GITEA_FORWARDED_HOST", "git.example.test")
    monkeypatch.setenv("GITEA_FORWARDED_PROTO", "https")
    assert managed_git.configured_gitea_git_base() == "https://git.example.test"


def test_push_exact_sha_keeps_token_out_of_argv_and_persistent_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "top-secret-token"
    calls: list[tuple[list[str], dict[str, str], Path | None]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env") or {}
        cwd = kwargs.get("cwd")
        calls.append((list(argv), dict(env), Path(cwd) if cwd else None))
        if argv[1] == "clone":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[1:3] == ["rev-parse", "--verify"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{SHA}\n", stderr="")
        assert argv[1:3] == ["push", "--porcelain"]
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(managed_git.subprocess, "run", fake_run)

    managed_git.push_exact_sha(
        project_root=tmp_path,
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_sha=SHA,
        username="gpakoh",
        token=token,
        git_base="https://git.example.test",
    )

    assert len(calls) == 3
    clone_argv, clone_env, _clone_cwd = calls[0]
    assert clone_argv[1:4] == ["clone", "--local", "--no-hardlinks"]
    assert token not in " ".join(clone_argv)
    assert "Authorization" not in " ".join(clone_env.values())
    assert clone_env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    push_argv, push_env, push_cwd = calls[2]
    assert push_cwd is not None and push_cwd != tmp_path
    assert push_cwd.name == "repo"
    assert token not in " ".join(push_argv)
    assert "@" not in push_argv[3]
    assert push_argv[-1] == f"{SHA}:refs/heads/hardening/runtime-deploy"
    expected_auth = base64.b64encode(f"gpakoh:{token}".encode()).decode("ascii")
    assert push_env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected_auth}"
    assert push_env["GIT_CONFIG_VALUE_1"] == "false"
    assert push_env["GIT_CONFIG_VALUE_2"] == ""
    assert "GITEA_TOKEN" not in push_env


def test_push_exact_sha_rejects_protected_branch_before_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = False

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("git must not run")

    monkeypatch.setattr(managed_git.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="protected destination"):
        managed_git.push_exact_sha(
            project_root=tmp_path,
            owner="gpakoh",
            repo="gpt-browser-bridge",
            destination_branch="main",
            expected_sha=SHA,
            username="gpakoh",
            token="secret",
            git_base="https://git.example.test",
        )
    assert not called


def test_push_failure_does_not_surface_remote_or_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "never-leak-me"

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1] == "clone":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[1:3] == ["rev-parse", "--verify"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{SHA}\n", stderr="")
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr=f"fatal https://user:{token}@git.example.test/private.git",
        )

    monkeypatch.setattr(managed_git.subprocess, "run", fake_run)
    with pytest.raises(managed_git.ManagedGitError) as exc_info:
        managed_git.push_exact_sha(
            project_root=tmp_path,
            owner="gpakoh",
            repo="gpt-browser-bridge",
            destination_branch="hardening/runtime-deploy",
            expected_sha=SHA,
            username="gpakoh",
            token=token,
            git_base="https://git.example.test",
        )
    message = str(exc_info.value)
    assert token not in message
    assert "git.example.test" not in message
    assert message == "managed Git push failed with exit code 1"


class _Registry:
    def __init__(self, root: Path) -> None:
        self.root = root

    def project_info(self, project: str) -> dict[str, Any]:
        assert project == "gpt-browser-bridge-hardening"
        return {"root": str(self.root), "type": "supervisor-workspace"}


class _FakeGiteaClient:
    def __init__(self, token: str) -> None:
        assert token == "managed-token"

    async def __aenter__(self) -> _FakeGiteaClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get_user(self) -> dict[str, Any]:
        return {"login": "gpakoh"}

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        assert (owner, repo) == ("gpakoh", "gpt-browser-bridge")
        return {"permissions": {"push": True}, "default_branch": "master"}

    async def get_branch(self, owner: str, repo: str, branch: str) -> dict[str, Any]:
        assert (owner, repo, branch) == (
            "gpakoh",
            "gpt-browser-bridge",
            "hardening/runtime-deploy",
        )
        return {
            "name": "hardening/runtime-deploy",
            "protected": False,
            "commit": {"id": SHA},
        }

    async def list_branches(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("trusted push must verify the exact branch directly, not via pagination")


def _install_push_adapter_mocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client_type: type[_FakeGiteaClient],
    push_impl: Any,
) -> None:
    staging = tmp_path / "trusted-candidate-staging"
    staging.mkdir(exist_ok=True)
    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setenv("GITEA_GIT_BASE", "https://git.example.test")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: client_type)
    monkeypatch.setattr(
        remote,
        "validate_task_candidate_for_push",
        lambda **_kwargs: ({"candidate_head_sha": SHA}, staging),
    )
    monkeypatch.setattr(remote, "push_trusted_staging_sha", push_impl)


@pytest.mark.asyncio
async def test_adapter_denies_actual_nonstandard_default_branch_before_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class DefaultDevelopClient(_FakeGiteaClient):
        async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
            assert (owner, repo) == ("gpakoh", "gpt-browser-bridge")
            return {"permissions": {"push": True}, "default_branch": "develop"}

    def must_not_push(**_kwargs: Any) -> None:
        raise AssertionError("default branch must be denied before mutation")

    _install_push_adapter_mocks(monkeypatch, tmp_path, DefaultDevelopClient, must_not_push)
    result = await remote.gitea_push_local_ref(
        project="gpt-browser-bridge-hardening",
        task_id="candidate-task-123",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="develop",
        expected_sha=SHA,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "default branch" in result["error"]["message"]


@pytest.mark.asyncio
async def test_adapter_denies_protected_branch_before_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class ProtectedClient(_FakeGiteaClient):
        async def get_branch(self, owner: str, repo: str, branch: str) -> dict[str, Any]:
            return {"name": branch, "protected": True, "commit": {"id": SHA}}

    def must_not_push(**_kwargs: Any) -> None:
        raise AssertionError("protected branch must be denied before mutation")

    _install_push_adapter_mocks(monkeypatch, tmp_path, ProtectedClient, must_not_push)
    result = await remote.gitea_push_local_ref(
        project="gpt-browser-bridge-hardening",
        task_id="candidate-task-123",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_sha=SHA,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "protected" in result["error"]["message"]


@pytest.mark.asyncio
async def test_adapter_allows_new_branch_then_verifies_exact_branch_directly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pushed = {"done": False}

    class NewBranchClient(_FakeGiteaClient):
        async def get_branch(self, owner: str, repo: str, branch: str) -> dict[str, Any]:
            if not pushed["done"]:
                request = httpx.Request("GET", "https://git.example.test/branch")
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError("not found", request=request, response=response)
            return {"name": branch, "protected": False, "commit": {"id": SHA}}

    def push(**_kwargs: Any) -> None:
        pushed["done"] = True

    _install_push_adapter_mocks(monkeypatch, tmp_path, NewBranchClient, push)
    result = await remote.gitea_push_local_ref(
        project="gpt-browser-bridge-hardening",
        task_id="candidate-task-123",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_sha=SHA,
    )
    assert pushed["done"] is True
    assert result["ok"] is True
    assert result["result"]["verified"] is True
    assert result["result"]["remote_ref"] == "refs/heads/hardening/runtime-deploy"
    assert result["result"]["remote_observed_sha"] == SHA


@pytest.mark.asyncio
async def test_adapter_post_push_direct_verification_rejects_wrong_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pushed = {"done": False}

    class WrongHeadClient(_FakeGiteaClient):
        async def get_branch(self, owner: str, repo: str, branch: str) -> dict[str, Any]:
            return {
                "name": branch,
                "protected": False,
                "commit": {"id": "0" * 40 if pushed["done"] else "1" * 40},
            }

    def push(**_kwargs: Any) -> None:
        pushed["done"] = True

    _install_push_adapter_mocks(monkeypatch, tmp_path, WrongHeadClient, push)
    result = await remote.gitea_push_local_ref(
        project="gpt-browser-bridge-hardening",
        task_id="candidate-task-123",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_sha=SHA,
    )
    assert pushed["done"] is True
    assert result["ok"] is False
    assert result["error"]["code"] == "CHECK_FAILED"
    assert result["error"]["retryable"] is True
    assert result["error"]["hint"]
    assert result["error"]["details"] == {
        "remote_ref": "refs/heads/hardening/runtime-deploy",
        "expected_sha": SHA,
        "remote_observed_sha": "0" * 40,
        "owner": "gpakoh",
        "repo": "gpt-browser-bridge",
        "branch": "hardening/runtime-deploy",
    }


@pytest.mark.asyncio
async def test_materialize_adapter_uses_authoritative_job_and_isolated_verifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class AgentClient:
        def job_result(self, job_id: str, redact_output: bool = True) -> dict[str, Any]:
            captured["job_id"] = job_id
            captured["redact_output"] = redact_output
            return {"status": "completed", "exit_code": 0}

    def fake_verify(*, staging_root, expected_sha, required_checks):
        captured["staging_root"] = staging_root
        captured["expected_sha"] = expected_sha
        captured["required_checks"] = required_checks

    def fake_materialize(**kwargs: Any) -> dict[str, Any]:
        captured["materialize"] = kwargs
        assert kwargs["expected_diff_sha256"] == "a" * 64
        assert kwargs["job_result"]("job-trusted")["status"] == "completed"
        kwargs["verify_candidate"](tmp_path / "candidate", SHA, ["pytest -q"])
        return {
            "base_head": "0" * 40,
            "implementation_diff_sha256": "a" * 64,
            "candidate_head_sha": SHA,
            "created_at": "2026-08-31T00:00:00+00:00",
        }

    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_agent_client", lambda: AgentClient())
    monkeypatch.setattr(remote, "verify_candidate_via_docker", fake_verify)
    monkeypatch.setattr(remote, "materialize_task_candidate", fake_materialize)

    result = await remote.gitea_materialize_task_candidate(
        project="gpt-browser-bridge-hardening",
        task_id="candidate-task-123",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_diff_sha256="a" * 64,
    )

    assert result["ok"] is True
    assert captured["job_id"] == "job-trusted"
    assert captured["redact_output"] is True
    assert captured["required_checks"] == ["pytest -q"]
    assert captured["materialize"]["project_root"] == str(tmp_path)


@pytest.mark.asyncio
async def test_materialize_adapter_can_seed_missing_delivery_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class AgentClient:
        def job_result(self, job_id: str, redact_output: bool = True) -> dict[str, Any]:
            return {"status": "completed", "exit_code": 0}

    def fake_record_contract(**kwargs: Any) -> dict[str, Any]:
        captured["contract"] = kwargs
        return {"ok": True}

    def fake_materialize(**kwargs: Any) -> dict[str, Any]:
        captured["materialize"] = kwargs
        return {
            "base_head": "0" * 40,
            "implementation_diff_sha256": "a" * 64,
            "candidate_head_sha": SHA,
            "created_at": "2026-08-31T00:00:00+00:00",
        }

    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_agent_client", lambda: AgentClient())
    monkeypatch.setattr(remote, "record_task_delivery_contract", fake_record_contract)
    monkeypatch.setattr(remote, "materialize_task_candidate", fake_materialize)

    result = await remote.gitea_materialize_task_candidate(
        project="gpt-browser-bridge-hardening",
        task_id="candidate-task-123",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_diff_sha256="a" * 64,
        base_ref="0" * 40,
        allowed_files=["src/**"],
        forbidden_files=["secrets/**"],
        required_checks=["pytest -q"],
    )

    assert result["ok"] is True
    assert result["result"]["delivery_contract_recorded"] is True
    assert captured["contract"] == {
        "project": "gpt-browser-bridge-hardening",
        "task_id": "candidate-task-123",
        "base_ref": "0" * 40,
        "allowed_files": ["src/**"],
        "forbidden_files": ["secrets/**"],
        "required_checks": ["pytest -q"],
    }
    assert captured["materialize"]["expected_diff_sha256"] == "a" * 64


@pytest.mark.asyncio
async def test_materialize_adapter_rejects_partial_delivery_contract_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_agent_client", lambda: object())

    result = await remote.gitea_materialize_task_candidate(
        project="gpt-browser-bridge-hardening",
        task_id="candidate-task-123",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_diff_sha256="a" * 64,
        allowed_files=["src/**"],
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "base_ref is required" in result["error"]["message"]


@pytest.mark.asyncio
async def test_adapter_pushes_only_validator_selected_trusted_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}
    staging = tmp_path / "trusted-candidate-staging"
    staging.mkdir()

    def fake_validate(**kwargs: Any):
        assert kwargs["project"] == "gpt-browser-bridge-hardening"
        assert kwargs["task_id"] == "candidate-task-123"
        assert kwargs["expected_sha"] == SHA
        return {"candidate_head_sha": SHA}, staging

    def fake_push(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setenv("GITEA_GIT_BASE", "https://git.example.test")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: _FakeGiteaClient)
    monkeypatch.setattr(remote, "validate_task_candidate_for_push", fake_validate)
    monkeypatch.setattr(remote, "push_trusted_staging_sha", fake_push)

    result = await remote.gitea_push_local_ref(
        project="gpt-browser-bridge-hardening",
        task_id="candidate-task-123",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_sha=SHA,
    )

    assert result["ok"] is True
    assert result["result"]["verified"] is True
    assert result["result"]["sha"] == SHA
    assert captured["staging_root"] == staging
    assert captured["expected_sha"] == SHA
    assert captured["token"] == "managed-token"


@pytest.mark.asyncio
async def test_adapter_candidate_error_preserves_machine_code_and_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def deny_candidate(**_kwargs: Any):
        raise remote.CandidateError(
            "agent job is not terminal-successful",
            code="CANDIDATE_JOB_NOT_SUCCESSFUL",
            retryable=True,
            hint="Wait for completion.",
            details={"job_status": "running", "exit_code": None},
        )

    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "validate_task_candidate_for_push", deny_candidate)

    async def inline_to_thread(func, *args: Any, **kwargs: Any):
        return func(*args, **kwargs)

    monkeypatch.setattr(remote.asyncio, "to_thread", inline_to_thread)
    result = await remote.gitea_push_local_ref(
        project="gpt-browser-bridge-hardening",
        task_id="candidate-task-123",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_sha=SHA,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "CANDIDATE_JOB_NOT_SUCCESSFUL"
    assert result["error"]["retryable"] is True
    assert result["error"]["hint"] == "Wait for completion."
    assert result["error"]["details"] == {"job_status": "running", "exit_code": None}


@pytest.mark.asyncio
async def test_adapter_candidate_denial_happens_before_remote_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class OrdinaryRegistry:
        def project_info(self, project: str) -> dict[str, Any]:
            assert project == "gpt-browser-bridge-hardening"
            return {"root": str(tmp_path), "type": "repository"}

    candidate_calls: list[dict[str, Any]] = []

    def deny_candidate(**kwargs: Any):
        candidate_calls.append(kwargs)
        raise remote.CandidateError("candidate receipt project/task binding mismatch")

    def remote_client_must_not_run() -> Any:
        raise AssertionError("Gitea client must not run before candidate authorization")

    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: OrdinaryRegistry())
    monkeypatch.setattr(remote, "validate_task_candidate_for_push", deny_candidate)
    monkeypatch.setattr(remote, "_server_gitea_client", remote_client_must_not_run)

    async def inline_to_thread(func, *args: Any, **kwargs: Any):
        return func(*args, **kwargs)

    monkeypatch.setattr(remote.asyncio, "to_thread", inline_to_thread)
    assert remote.gitea_push_local_ref.__globals__["validate_task_candidate_for_push"] is deny_candidate
    assert remote.gitea_push_local_ref.__globals__["CandidateError"] is remote.CandidateError

    result = await remote.gitea_push_local_ref(
        project="gpt-browser-bridge-hardening",
        task_id="candidate-task-123",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_sha=SHA,
    )

    assert len(candidate_calls) == 1
    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "project/task" in result["error"]["message"]


@pytest.mark.asyncio
async def test_verified_workspace_adapter_rechecks_then_pushes_exact_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {"proof_calls": 0}
    proof = {
        "base_sha": "0" * 40,
        "head_sha": SHA,
        "clean": True,
        "status_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "changed_files": ["app/a.py", "tests/test_a.py"],
        "allowed_files": ["app/**", "tests/**"],
        "scope_verified": True,
    }

    def verify_workspace(**kwargs: Any) -> dict[str, Any]:
        captured["proof_calls"] += 1
        captured["verify_kwargs"] = kwargs
        return dict(proof)

    def verify_checks(**kwargs: Any) -> None:
        captured["checks"] = kwargs

    def push(**kwargs: Any) -> None:
        captured["push"] = kwargs

    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setenv("GITEA_GIT_BASE", "https://git.example.test")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: _FakeGiteaClient)
    monkeypatch.setattr(remote, "verify_registered_delivery_workspace", verify_workspace)
    monkeypatch.setattr(remote, "verify_workspace_via_docker", verify_checks)
    monkeypatch.setattr(remote, "push_exact_sha", push)

    result = await remote.gitea_push_verified_commit(
        project="gpt-browser-bridge-hardening",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_base_sha="0" * 40,
        expected_head_sha=SHA,
        allowed_files="app/**\ntests/**",
        required_checks="pytest -q\nruff check .",
    )

    assert result["ok"] is True
    assert captured["proof_calls"] == 2
    assert captured["checks"] == {
        "workspace_root": tmp_path,
        "expected_sha": SHA,
        "required_checks": ["pytest -q", "ruff check ."],
    }
    assert captured["push"]["project_root"] == str(tmp_path)
    assert captured["push"]["expected_sha"] == SHA
    assert captured["push"]["token"] == "managed-token"
    assert result["result"]["changed_files"] == ["app/a.py", "tests/test_a.py"]
    assert result["result"]["checks_verified"] is True
    assert result["result"]["remote_observed_sha"] == SHA


@pytest.mark.asyncio
async def test_verified_workspace_adapter_requires_checks_before_workspace_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "managed-token")

    def registry_must_not_run() -> Any:
        raise AssertionError("workspace registry must not run before required_checks validation")

    monkeypatch.setattr(remote, "_server_workspace_registry", registry_must_not_run)

    result = await remote.gitea_push_verified_commit(
        project="gpt-browser-bridge-hardening",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_base_sha="0" * 40,
        expected_head_sha=SHA,
        allowed_files="app/**",
        required_checks=" \n ",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "required_checks" in result["error"]["message"]


@pytest.mark.asyncio
async def test_verified_workspace_adapter_denial_happens_before_gitea(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def deny(**_kwargs: Any) -> dict[str, Any]:
        raise remote.VerifiedWorkspaceError("WORKSPACE_DIRTY", "delivery workspace must be clean")

    def client_must_not_run() -> Any:
        raise AssertionError("Gitea access must happen only after workspace verification")

    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "verify_registered_delivery_workspace", deny)
    monkeypatch.setattr(remote, "_server_gitea_client", client_must_not_run)

    result = await remote.gitea_push_verified_commit(
        project="gpt-browser-bridge-hardening",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_base_sha="0" * 40,
        expected_head_sha=SHA,
        allowed_files="app/**",
        required_checks="pytest -q",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "WORKSPACE_DIRTY"


def _delivery_proof() -> dict[str, Any]:
    return {
        "base_sha": "0" * 40,
        "head_sha": SHA,
        "clean": True,
        "status_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "changed_files": ["app/a.py", "tests/test_a.py"],
        "allowed_files": ["app/**"],
        "scope_verified": True,
    }


@pytest.mark.asyncio
async def test_verified_commit_verifier_error_is_structured_no_push_no_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def verify_workspace(**kwargs: Any) -> dict[str, Any]:
        return _delivery_proof()

    def deny_checks(**kwargs: Any) -> None:
        raise remote.CandidateVerificationError(
            "a required verification check failed with exit code 83",
            code="CANDIDATE_CHECK_FAILED",
            phase="candidate_check",
            retryable=False,
            details={
                "phase": "candidate_check",
                "exit_code": 83,
                "check_index": 1,
                "failed_check": "ruff check .",
                "mutation_occurred": False,
                "output_tail": (
                    "fatal: /media/1TB/gpt-browser-bridge/app/main.py\n"
                    "Token: ghp_zzz\n"
                    "Authorization: Basic dXNlcjpzZWNyZXQ=\n"
                ),
            },
        )

    def must_not_push(**kwargs: Any) -> None:
        raise AssertionError("trusted push must not run after verification failure")

    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setenv("GITEA_GIT_BASE", "https://git.example.test")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: _FakeGiteaClient)
    monkeypatch.setattr(remote, "verify_registered_delivery_workspace", verify_workspace)
    monkeypatch.setattr(remote, "verify_workspace_via_docker", deny_checks)
    monkeypatch.setattr(remote, "push_exact_sha", must_not_push)

    result = await remote.gitea_push_verified_commit(
        project="gpt-browser-bridge-hardening",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_base_sha="0" * 40,
        expected_head_sha=SHA,
        allowed_files="app/**",
        required_checks="pytest -q\nruff check .",
    )

    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == "CANDIDATE_CHECK_FAILED"
    assert error["retryable"] is False
    assert error["message"] == "a required verification check failed with exit code 83"
    details = error["details"]
    assert details["phase"] == "candidate_check"
    assert details["failed_check"] == "ruff check ."
    assert details["check_index"] == 1
    assert details["mutation_occurred"] is False
    assert details["expected_base_sha"] == "0" * 40
    assert details["expected_head_sha"] == SHA
    assert details["branch"] == "hardening/runtime-deploy"
    assert details["allowed_files"] == ["app/**"]
    assert details["required_checks"] == ["pytest -q", "ruff check ."]

    serialized = json.dumps(result)
    assert "Traceback" not in serialized
    assert "CandidateVerificationError" not in serialized
    assert "/media/1TB" not in serialized
    assert "ghp_zzz" not in serialized
    assert "dXNlcjpzZWNyZXQ=" not in serialized


@pytest.mark.asyncio
async def test_verified_commit_clean_tree_failure_is_structured_no_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def deny(**_kwargs: Any) -> dict[str, Any]:
        raise remote.VerifiedWorkspaceError(
            "WORKSPACE_DIRTY",
            "delivery workspace must be clean before trusted push",
            retryable=False,
            details={"status_sha256": "a" * 64, "status_bytes": 12},
        )

    def client_must_not_run() -> Any:
        raise AssertionError("Gitea access must happen only after verification")

    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "verify_registered_delivery_workspace", deny)
    monkeypatch.setattr(remote, "_server_gitea_client", client_must_not_run)

    result = await remote.gitea_push_verified_commit(
        project="gpt-browser-bridge-hardening",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_base_sha="0" * 40,
        expected_head_sha=SHA,
        allowed_files="app/**",
        required_checks="pytest -q",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "WORKSPACE_DIRTY"
    details = result["error"]["details"]
    assert details["phase"] == "clean_tree_check"
    assert details["mutation_occurred"] is False
    assert details["status_sha256"] == "a" * 64
    assert details["status_bytes"] == 12
    assert details["expected_head_sha"] == SHA


@pytest.mark.asyncio
async def test_verified_commit_post_push_mismatch_marks_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pushed = {"done": False}

    class WrongHeadClient(_FakeGiteaClient):
        async def get_branch(self, owner: str, repo: str, branch: str) -> dict[str, Any]:
            return {
                "name": branch,
                "protected": False,
                "commit": {"id": "0" * 40 if pushed["done"] else SHA},
            }

    def verify_workspace(**kwargs: Any) -> dict[str, Any]:
        return _delivery_proof()

    def verify_checks(**kwargs: Any) -> None:
        return None

    def push(**kwargs: Any) -> None:
        pushed["done"] = True

    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setenv("GITEA_GIT_BASE", "https://git.example.test")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: WrongHeadClient)
    monkeypatch.setattr(remote, "verify_registered_delivery_workspace", verify_workspace)
    monkeypatch.setattr(remote, "verify_workspace_via_docker", verify_checks)
    monkeypatch.setattr(remote, "push_exact_sha", push)

    result = await remote.gitea_push_verified_commit(
        project="gpt-browser-bridge-hardening",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_base_sha="0" * 40,
        expected_head_sha=SHA,
        allowed_files="app/**",
        required_checks="pytest -q",
    )

    assert pushed["done"] is True
    assert result["ok"] is False
    assert result["error"]["code"] == "CHECK_FAILED"
    assert result["error"]["retryable"] is True
    assert result["error"]["details"]["mutation_occurred"] is True
    assert result["error"]["details"]["phase"] == "push_preflight"
    assert result["error"]["details"]["observed_head_sha"] == "0" * 40


def test_trusted_staging_push_never_clones_or_uses_another_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    staging = tmp_path / "trusted-staging"
    staging.mkdir()
    calls: list[tuple[list[str], Path]] = []
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kwargs["cwd"])
        calls.append((list(argv), cwd))
        assert cwd == staging
        assert argv[1] != "clone"
        if argv[1:3] == ["rev-parse", "--verify"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{SHA}\n", stderr="")
        if argv[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{SHA}\n", stderr="")
        assert argv[1:3] == ["push", "--porcelain"]
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
    monkeypatch.setattr(managed_git.subprocess, "run", fake_run)
    managed_git.push_trusted_staging_sha(staging_root=staging, owner="gpakoh", repo="gpt-browser-bridge", destination_branch="hardening/runtime-deploy", expected_sha=SHA, username="gpakoh", token="x", git_base="https://git.example.test")
    assert len(calls) == 3
