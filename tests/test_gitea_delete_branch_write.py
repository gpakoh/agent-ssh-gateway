"""Security and contract tests for lease-guarded remote branch deletion."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server import managed_git
from examples.mcp_server.mcp_infra.adapters import remote
from examples.mcp_server.tool_modes import tools_for_mode
from examples.mcp_server.tool_scopes import get_required_scopes

SHA = "a" * 40
BRANCH = "fix/obsolete-branch"


@pytest.mark.asyncio
async def test_client_get_branch_percent_encodes_slash():
    raw_paths: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        raw_paths.append(request.url.raw_path)
        return httpx.Response(
            200,
            json={"name": BRANCH, "commit": {"id": SHA}, "protected": False},
            request=request,
        )

    client = GiteaClient("token")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://git.example.test/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.get_branch("owner", "repo", BRANCH)
    finally:
        await client.aclose()

    assert result["commit"]["id"] == SHA
    assert raw_paths == [b"/api/v1/repos/owner/repo/branches/fix%2Fobsolete-branch"]


@pytest.mark.asyncio
async def test_client_get_branch_rejects_path_injection_before_network(monkeypatch):
    client = GiteaClient("token")
    get = AsyncMock()
    monkeypatch.setattr(client._client, "get", get)
    try:
        with pytest.raises(ValueError, match="Invalid branch branch name"):
            await client.get_branch("owner", "repo", "fix/../master")
    finally:
        await client.aclose()
    get.assert_not_awaited()


def test_managed_delete_uses_exact_force_with_lease_and_exact_ls_remote(monkeypatch):
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs.get("env") or {})))
        if list(argv)[:3] == ["git", "ls-remote", "--exit-code"]:
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(managed_git.subprocess, "run", fake_run)
    managed_git.delete_remote_branch_with_lease(
        owner="owner",
        repo="repo",
        branch=BRANCH,
        expected_sha=SHA,
        username="robot",
        token="super-secret-token",
        git_base="https://git.example.test",
    )

    assert len(calls) == 3
    push_argv, push_env = calls[1]
    assert push_argv == [
        "git",
        "push",
        "--porcelain",
        f"--force-with-lease=refs/heads/{BRANCH}:{SHA}",
        "https://git.example.test/owner/repo.git",
        f":refs/heads/{BRANCH}",
    ]
    ls_remote_argv, ls_remote_env = calls[2]
    assert ls_remote_argv == [
        "git",
        "ls-remote",
        "--exit-code",
        "https://git.example.test/owner/repo.git",
        f"refs/heads/{BRANCH}",
    ]
    assert "super-secret-token" not in repr(push_argv)
    assert "super-secret-token" not in repr(ls_remote_argv)
    assert push_env["GIT_TERMINAL_PROMPT"] == "0"
    assert ls_remote_env["GIT_TERMINAL_PROMPT"] == "0"
    assert push_env["GIT_CONFIG_KEY_1"] == "http.followRedirects"
    assert push_env["GIT_CONFIG_VALUE_1"] == "false"


def test_managed_delete_fails_if_exact_ref_still_exists_after_push(monkeypatch):
    calls = 0

    def fake_run(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=f"{SHA}\trefs/heads/{BRANCH}\n",
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(managed_git.subprocess, "run", fake_run)
    with pytest.raises(managed_git.ManagedGitError, match="still exists"):
        managed_git.delete_remote_branch_with_lease(
            owner="owner",
            repo="repo",
            branch=BRANCH,
            expected_sha=SHA,
            username="robot",
            token="super-secret-token",
            git_base="https://git.example.test",
        )


def test_managed_delete_accepts_only_ls_remote_rc2_as_confirmed_absent(monkeypatch):
    rc_by_attempt = {1: 1, 2: 3}

    for attempt, rc in rc_by_attempt.items():
        calls = 0

        def fake_run(argv, _expected_rc=rc, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                return subprocess.CompletedProcess(argv, _expected_rc, stdout="", stderr="secret")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(managed_git.subprocess, "run", fake_run)
        with pytest.raises(managed_git.ManagedGitError, match="existence check failed"):
            managed_git.delete_remote_branch_with_lease(
                owner="owner",
                repo="repo",
                branch=f"fix/obsolete-{attempt}",
                expected_sha=SHA,
                username="robot",
                token="super-secret-token",
                git_base="https://git.example.test",
            )


def test_managed_delete_rejects_protected_or_invalid_inputs_before_git(monkeypatch):
    run = Mock()
    monkeypatch.setattr(managed_git.subprocess, "run", run)

    with pytest.raises(ValueError, match="protected destination branch"):
        managed_git.delete_remote_branch_with_lease(
            owner="owner",
            repo="repo",
            branch="master",
            expected_sha=SHA,
            username="robot",
            token="token",
            git_base="https://git.example.test",
        )
    with pytest.raises(ValueError, match="expected_sha"):
        managed_git.delete_remote_branch_with_lease(
            owner="owner",
            repo="repo",
            branch=BRANCH,
            expected_sha="abc",
            username="robot",
            token="token",
            git_base="https://git.example.test",
        )

    run.assert_not_called()


def test_managed_delete_sanitizes_git_failure(monkeypatch):
    calls = 0

    def fake_run(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="remote https://internal.example/token",
            stderr="super-secret-token",
        )

    monkeypatch.setattr(managed_git.subprocess, "run", fake_run)
    with pytest.raises(managed_git.ManagedGitError) as exc_info:
        managed_git.delete_remote_branch_with_lease(
            owner="owner",
            repo="repo",
            branch=BRANCH,
            expected_sha=SHA,
            username="robot",
            token="super-secret-token",
            git_base="https://git.example.test",
        )
    message = str(exc_info.value)
    assert "exit code 1" in message
    assert "super-secret-token" not in message
    assert "internal.example" not in message


def test_managed_delete_lease_rejects_moved_remote_head(tmp_path: Path):
    remote_root = tmp_path / "remotes"
    remote = remote_root / "owner" / "repo.git"
    remote.parent.mkdir(parents=True)
    seed = tmp_path / "seed"

    def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if check:
            assert completed.returncode == 0, completed.stderr
        return completed

    git("init", "--bare", str(remote))
    git("init", str(seed))
    git("config", "user.name", "Lease Test", cwd=seed)
    git("config", "user.email", "lease-test@example.invalid", cwd=seed)
    (seed / "value.txt").write_text("first\n", encoding="utf-8")
    git("add", "value.txt", cwd=seed)
    git("commit", "-m", "first", cwd=seed)
    initial_sha = git("rev-parse", "HEAD", cwd=seed).stdout.strip()
    git("push", str(remote), f"HEAD:refs/heads/{BRANCH}", cwd=seed)

    (seed / "value.txt").write_text("second\n", encoding="utf-8")
    git("commit", "-am", "second", cwd=seed)
    moved_sha = git("rev-parse", "HEAD", cwd=seed).stdout.strip()
    git("push", str(remote), f"HEAD:refs/heads/{BRANCH}", cwd=seed)

    with pytest.raises(managed_git.ManagedGitError, match="deletion rejected"):
        managed_git.delete_remote_branch_with_lease(
            owner="owner",
            repo="repo",
            branch=BRANCH,
            expected_sha=initial_sha,
            username="robot",
            token="unused-for-file-transport",
            git_base=remote_root.as_uri(),
        )

    still_there = git("--git-dir", str(remote), "rev-parse", f"refs/heads/{BRANCH}")
    assert still_there.stdout.strip() == moved_sha

    managed_git.delete_remote_branch_with_lease(
        owner="owner",
        repo="repo",
        branch=BRANCH,
        expected_sha=moved_sha,
        username="robot",
        token="unused-for-file-transport",
        git_base=remote_root.as_uri(),
    )
    missing = git(
        "--git-dir",
        str(remote),
        "show-ref",
        "--verify",
        f"refs/heads/{BRANCH}",
        check=False,
    )
    assert missing.returncode != 0


class FakeDeleteClient:
    def __init__(
        self,
        token: str,
        *,
        head_sha: str = SHA,
        default_branch: str = "master",
        protected: bool = False,
        protection_name: str = "",
        open_prs: list[dict] | None = None,
        archived: bool = False,
    ) -> None:
        assert token == "token"
        self.head_sha = head_sha
        self.default_branch = default_branch
        self.protected = protected
        self.protection_name = protection_name
        self.open_prs = open_prs or []
        self.archived = archived
        self.branch_reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_repo(self, owner: str, repo: str):
        return {
            "default_branch": self.default_branch,
            "archived": self.archived,
            "permissions": {"push": True},
        }

    async def get_branch(self, owner: str, repo: str, branch: str):
        self.branch_reads += 1
        return {
            "name": branch,
            "commit": {"id": self.head_sha},
            "protected": self.protected,
            "effective_branch_protection_name": self.protection_name,
        }

    async def list_pull_requests(self, owner: str, repo: str, state: str, limit: int):
        assert state == "open"
        assert limit == 50
        return self.open_prs

    async def get_user(self):
        return {"login": "robot"}


async def _call_delete(monkeypatch, client: FakeDeleteClient, *, helper_error: Exception | None = None):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    monkeypatch.setattr(remote, "configured_gitea_git_base", lambda: "https://git.example.test")
    delete_calls: list[dict] = []

    def fake_delete(**kwargs):
        delete_calls.append(kwargs)
        if helper_error is not None:
            raise helper_error

    monkeypatch.setattr(remote, "delete_remote_branch_with_lease", fake_delete)
    result = await remote.gitea_delete_branch("owner", "repo", BRANCH, SHA)
    return result, delete_calls


@pytest.mark.asyncio
async def test_adapter_deletes_only_exact_unprotected_unused_branch(monkeypatch):
    result, delete_calls = await _call_delete(monkeypatch, FakeDeleteClient("token"))

    assert result["ok"] is True
    assert result["result"] == {
        "owner": "owner",
        "repo": "repo",
        "branch": BRANCH,
        "deleted": True,
        "head_sha": SHA,
        "lease_guarded": True,
        "verified_absent": True,
    }
    assert delete_calls == [
        {
            "owner": "owner",
            "repo": "repo",
            "branch": BRANCH,
            "expected_sha": SHA,
            "username": "robot",
            "token": "token",
            "git_base": "https://git.example.test",
        }
    ]
    assert "token" not in repr(result)


@pytest.mark.asyncio
async def test_adapter_head_mismatch_is_zero_delete(monkeypatch):
    result, delete_calls = await _call_delete(
        monkeypatch, FakeDeleteClient("token", head_sha="b" * 40)
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert delete_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "message"),
    [
        (FakeDeleteClient("token", default_branch=BRANCH), "default branch"),
        (FakeDeleteClient("token", protected=True), "protected branch"),
        (FakeDeleteClient("token", protection_name="release/*"), "protected branch"),
        (
            FakeDeleteClient(
                "token",
                open_prs=[
                    {
                        "number": 7,
                        "head": {"ref": BRANCH, "repo": {"full_name": "owner/repo"}},
                    }
                ],
            ),
            "open pull request",
        ),
        (FakeDeleteClient("token", archived=True), "archived repository"),
    ],
)
async def test_adapter_policy_guards_are_zero_delete(monkeypatch, client, message):
    result, delete_calls = await _call_delete(monkeypatch, client)
    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert message in result["error"]["message"]
    assert delete_calls == []


@pytest.mark.asyncio
async def test_adapter_does_not_block_known_fork_with_same_head_ref(monkeypatch):
    result, delete_calls = await _call_delete(
        monkeypatch,
        FakeDeleteClient(
            "token",
            open_prs=[
                {
                    "number": 7,
                    "head": {"ref": BRANCH, "repo": {"full_name": "fork-owner/repo"}},
                }
            ],
        ),
    )
    assert result["ok"] is True
    assert len(delete_calls) == 1


@pytest.mark.asyncio
async def test_adapter_fails_closed_for_ambiguous_pr_head_repo(monkeypatch):
    result, delete_calls = await _call_delete(
        monkeypatch,
        FakeDeleteClient("token", open_prs=[{"number": 7, "head": {"ref": BRANCH}}]),
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "could not be verified" in result["error"]["message"]
    assert delete_calls == []


@pytest.mark.asyncio
async def test_adapter_fails_closed_when_open_pr_scan_is_not_exhaustive(monkeypatch):
    prs = [{"number": i, "head": {"ref": f"other/{i}"}, "state": "open"} for i in range(50)]
    result, delete_calls = await _call_delete(
        monkeypatch, FakeDeleteClient("token", open_prs=prs)
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "too many open pull requests" in result["error"]["message"]
    assert delete_calls == []


@pytest.mark.asyncio
async def test_adapter_surfaces_managed_git_post_delete_check_failure(monkeypatch):
    result, delete_calls = await _call_delete(
        monkeypatch,
        FakeDeleteClient("token"),
        helper_error=managed_git.ManagedGitError("remote branch still exists after managed deletion"),
    )
    assert len(delete_calls) == 1
    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_PUSH_FAILED"
    assert "remote branch still exists" in result["error"]["message"]


@pytest.mark.asyncio
async def test_adapter_requires_token_and_valid_full_sha(monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    missing = await remote.gitea_delete_branch("owner", "repo", BRANCH, SHA)
    assert missing["ok"] is False
    assert missing["error"]["code"] == "DEPENDENCY_MISSING"

    monkeypatch.setenv("GITEA_TOKEN", "token")
    invalid = await remote.gitea_delete_branch("owner", "repo", BRANCH, "abc")
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "INVALID_INPUT"


def test_delete_branch_is_write_admin_only():
    assert "gitea_delete_branch" not in tools_for_mode("mcp_client")
    assert "gitea_delete_branch" in tools_for_mode("mcp_client_write")
    assert get_required_scopes("gitea_delete_branch") == ["mcp:repo", "mcp:admin"]
