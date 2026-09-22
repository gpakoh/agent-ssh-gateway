"""Security and contract tests for lease-guarded remote branch deletion."""

from __future__ import annotations

import re
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
FINGERPRINT = "ab" * 32


class RecordingAuditLogger:
    """Records append_required (strict) vs append (best-effort) audit events."""

    def __init__(self, order_log: list[str] | None = None):
        self.order_log = order_log
        self.required_events: list[object] = []
        self.append_events: list[object] = []

    def append_required(self, event) -> None:
        self.required_events.append(event)
        if self.order_log is not None:
            self.order_log.append("audit:intent")

    def append(self, event) -> None:
        self.append_events.append(event)
        if self.order_log is not None:
            self.order_log.append("audit:success")


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
        pr_pages: list[list[dict]] | None = None,
    ) -> None:
        assert token == "token"
        self.head_sha = head_sha
        self.default_branch = default_branch
        self.protected = protected
        self.protection_name = protection_name
        self.open_prs = open_prs or []
        self.pr_pages = pr_pages
        self.archived = archived
        self.branch_reads = 0
        self.pull_request_pages: list[int] = []

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

    async def list_pull_requests(
        self, owner: str, repo: str, state: str, limit: int, page: int = 1
    ):
        assert state == "all"
        assert limit == 50
        assert isinstance(page, int) and page >= 1
        self.pull_request_pages.append(page)
        if self.pr_pages is not None:
            assert page <= len(self.pr_pages), "pagination must terminate"
            return self.pr_pages[page - 1]
        assert page == 1
        return self.open_prs

    async def get_user(self):
        return {"login": "robot"}


async def _call_delete(
    monkeypatch,
    client: FakeDeleteClient,
    *,
    helper_error: Exception | None = None,
    audit_logger: RecordingAuditLogger | None = None,
    fingerprint: str | None = FINGERPRINT,
    order_log: list[str] | None = None,
):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)
    monkeypatch.setattr(remote, "configured_gitea_git_base", lambda: "https://git.example.test")
    if audit_logger is None:
        audit_logger = RecordingAuditLogger(order_log=order_log)
    elif order_log is not None:
        audit_logger.order_log = order_log

    def fake_server_attr(name: str):
        if name == "_current_auth_reuse_key":
            return lambda: fingerprint
        if name == "get_audit_logger":
            return lambda: audit_logger
        raise AssertionError(f"unexpected server_attr({name!r})")

    monkeypatch.setattr(remote, "server_attr", fake_server_attr)

    delete_calls: list[dict] = []

    def fake_delete(**kwargs):
        delete_calls.append(kwargs)
        if audit_logger.order_log is not None:
            audit_logger.order_log.append("delete")
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
            "unmerged pull request",
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
async def test_adapter_page_2_closed_unmerged_blocks_delete(monkeypatch):
    """The scan must examine every page: a matching closed-unmerged PR on
    page 2 must block deletion even when page 1 is full of irrelevant PRs."""
    first_page = [
        {"number": i, "head": {"ref": f"other/{i}"}, "state": "closed", "merged": True}
        for i in range(50)
    ]
    matching = {
        "number": 51,
        "head": {"ref": BRANCH, "repo": {"full_name": "owner/repo"}},
        "state": "closed",
        "merged": False,
    }
    client = FakeDeleteClient("token", pr_pages=[first_page, [matching]])
    result, delete_calls = await _call_delete(monkeypatch, client)
    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "unmerged pull request" in result["error"]["message"]
    assert client.pull_request_pages == [1, 2]
    assert delete_calls == []


@pytest.mark.asyncio
async def test_adapter_pagination_terminates_and_allows_delete_when_only_merged(monkeypatch):
    """A full first page followed by a short second page must terminate the
    scan and, with only merged/no-match PRs, allow deletion."""
    first_page = [
        {"number": i, "head": {"ref": f"other/{i}"}, "state": "closed", "merged": True}
        for i in range(50)
    ]
    second_page = [
        {"number": 50, "head": {"ref": "other/50"}, "state": "closed", "merged": True}
    ]
    client = FakeDeleteClient("token", pr_pages=[first_page, second_page])
    result, delete_calls = await _call_delete(monkeypatch, client)
    assert result["ok"] is True
    assert client.pull_request_pages == [1, 2]
    assert len(delete_calls) == 1


def _full_pr_pages(pages: int) -> list[list[dict]]:
    return [
        [
            {"number": i, "head": {"ref": f"other/{i}"}, "state": "closed", "merged": True}
            for i in range(50)
        ]
        for _ in range(pages)
    ]


@pytest.mark.asyncio
async def test_adapter_page_20_short_page_permits_continuation(monkeypatch):
    """19 full pages ending in a short page-20 prove exhaustive coverage:
    the scan may continue to page 20 and deletion proceeds normally."""
    pages = _full_pr_pages(19) + [[{"number": 0, "head": {"ref": "tail"}}]]
    client = FakeDeleteClient("token", pr_pages=pages)
    result, delete_calls = await _call_delete(monkeypatch, client)
    assert result["ok"] is True
    assert client.pull_request_pages == list(range(1, 21))
    assert len(delete_calls) == 1


@pytest.mark.asyncio
async def test_adapter_20_full_pages_fails_closed_never_page_21(monkeypatch):
    """20 full pages (1000 records) cannot prove exhaustion: POLICY_DENIED
    with zero delete, after requesting exactly pages 1..20 and never 21."""
    client = FakeDeleteClient("token", pr_pages=_full_pr_pages(20))
    result, delete_calls = await _call_delete(monkeypatch, client)
    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "1000" in result["error"]["message"]
    assert client.pull_request_pages == list(range(1, 21))
    assert delete_calls == []


@pytest.mark.asyncio
async def test_adapter_blocker_on_page_20_blocks_for_blocker_reason(monkeypatch):
    """A same-repo unmerged PR whose page-20 slot is the last of 50 must
    still block for the unmerged-PR reason (not the coverage-limit reason),
    after requesting exactly pages 1..20 with zero delete."""
    blocked = {
        "number": 500,
        "head": {"ref": BRANCH, "repo": {"full_name": "owner/repo"}},
        "state": "closed",
        "merged": False,
    }
    page_20 = [
        {"number": i, "head": {"ref": f"other/{i}"}, "state": "closed", "merged": True}
        for i in range(49)
    ] + [blocked]
    client = FakeDeleteClient("token", pr_pages=_full_pr_pages(19) + [page_20])
    result, delete_calls = await _call_delete(monkeypatch, client)
    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "unmerged pull request" in result["error"]["message"]
    assert "1000" not in result["error"]["message"]
    assert client.pull_request_pages == list(range(1, 21))
    assert delete_calls == []


@pytest.mark.asyncio
async def test_adapter_allows_delete_when_only_matching_pr_is_merged(monkeypatch):
    """A same-repo PR whose head is the target branch but merged=true does
    not block cleanup (r3 merged!=true semantics, open or closed)."""
    client = FakeDeleteClient(
        "token",
        open_prs=[
            {
                "number": 9,
                "head": {"ref": BRANCH, "repo": {"full_name": "owner/repo"}},
                "state": "closed",
                "merged": True,
            }
        ],
    )
    result, delete_calls = await _call_delete(monkeypatch, client)
    assert result["ok"] is True
    assert len(delete_calls) == 1


@pytest.mark.asyncio
async def test_adapter_delete_intent_audit_is_attributed_before_mutation(monkeypatch):
    """The destructive-intent audit fires before the git delete with the
    Gitea username, an opaque 64-hex caller fingerprint, correlation id and
    exact target/SHA; success audit trails it with the same correlation id."""
    order: list[str] = []
    logger = RecordingAuditLogger(order_log=order)
    client = FakeDeleteClient("token")
    result, delete_calls = await _call_delete(
        monkeypatch, client, audit_logger=logger, order_log=order
    )
    assert result["ok"] is True
    assert len(delete_calls) == 1
    assert order == ["audit:intent", "delete", "audit:success"]
    assert len(logger.required_events) == 1
    assert len(logger.append_events) == 1
    intent = logger.required_events[0]
    success = logger.append_events[0]
    assert intent.event_type == "mcp.gitea_destructive_intent"
    assert success.event_type == "mcp.gitea_destructive_success"
    target = intent.metadata
    assert target["owner"] == "owner"
    assert target["repo"] == "repo"
    assert target["branch"] == BRANCH
    assert target["expected_head_sha"] == SHA
    assert target["gitea_username"] == "robot"
    assert re.fullmatch(r"[0-9a-f]{64}", target["caller_fingerprint"]), target["caller_fingerprint"]
    assert re.fullmatch(r"[0-9a-f]{32}", target["correlation_id"]), target["correlation_id"]
    assert success.metadata["correlation_id"] == target["correlation_id"]
    for event in (intent, success):
        dump = repr(event.metadata)
        assert "GITEA_TOKEN" not in dump
        assert "Bearer" not in dump
        assert "token" not in str(event.metadata.values())


@pytest.mark.asyncio
async def test_adapter_delete_fsync_failure_is_audit_unavailable_zero_mutation(
    monkeypatch, tmp_path
):
    from examples.mcp_server.mcp_audit import McpAuditLogger

    logger = McpAuditLogger(log_path=str(tmp_path / "audit.jsonl"))

    def boom(fd):
        raise OSError("fsync failed")

    monkeypatch.setattr("examples.mcp_server.mcp_audit.os.fsync", boom)
    client = FakeDeleteClient("token")
    result, delete_calls = await _call_delete(monkeypatch, client, audit_logger=logger)
    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert delete_calls == []
    assert logger._buffer == []


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
