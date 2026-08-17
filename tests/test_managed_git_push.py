from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Any

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
        return {"permissions": {"push": True}}

    async def list_branches(self, owner: str, repo: str, limit: int = 30) -> list[dict[str, Any]]:
        assert limit == 50
        return [{"name": "hardening/runtime-deploy", "commit": {"id": SHA}}]


@pytest.mark.asyncio
async def test_adapter_verifies_remote_branch_after_managed_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def fake_push(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setenv("GITEA_GIT_BASE", "https://git.example.test")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: _FakeGiteaClient)
    monkeypatch.setattr(remote, "push_exact_sha", fake_push)

    result = await remote.gitea_push_local_ref(
        project="gpt-browser-bridge-hardening",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_sha=SHA,
    )

    assert result["ok"] is True
    assert result["result"]["verified"] is True
    assert result["result"]["sha"] == SHA
    assert captured["project_root"] == str(tmp_path)
    assert captured["token"] == "managed-token"


@pytest.mark.asyncio
async def test_adapter_rejects_non_supervisor_workspace_before_remote_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class NonSupervisorRegistry:
        def project_info(self, project: str) -> dict[str, Any]:
            assert project == "gpt-browser-bridge-hardening"
            return {"root": str(tmp_path), "type": "repository"}

    def remote_client_must_not_run() -> Any:
        raise AssertionError("Gitea client must not be created for a non-supervisor project")

    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: NonSupervisorRegistry())
    monkeypatch.setattr(remote, "_server_gitea_client", remote_client_must_not_run)

    result = await remote.gitea_push_local_ref(
        project="gpt-browser-bridge-hardening",
        owner="gpakoh",
        repo="gpt-browser-bridge",
        destination_branch="hardening/runtime-deploy",
        expected_sha=SHA,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "supervisor-workspace" in result["error"]["message"]
