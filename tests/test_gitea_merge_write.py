"""Security and contract tests for the narrow Gitea PR merge tool."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_infra.adapters import remote

SHA = "a" * 40
BASE_SHA = "1" * 40
NEW_BASE_SHA = "2" * 40


@pytest.mark.asyncio
async def test_client_merge_pr_uses_fixed_endpoint_and_optimistic_head_lock(monkeypatch):
    client = GiteaClient("token")
    post = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_post", post)
    try:
        result = await client.merge_pull_request(
            "owner",
            "repo",
            25,
            expected_head_sha=SHA.upper(),
        )
    finally:
        await client.aclose()

    assert result == {}
    post.assert_awaited_once_with(
        "/repos/{owner}/{repo}/pulls/{number}/merge",
        {"Do": "merge", "head_commit_id": SHA},
        owner="owner",
        repo="repo",
        number=25,
    )


@pytest.mark.asyncio
async def test_client_merge_pr_rejects_invalid_sha_and_non_merge_methods():
    client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="40-character SHA-1"):
            await client.merge_pull_request("owner", "repo", 1, expected_head_sha="abc")
        with pytest.raises(ValueError, match="only merge method"):
            await client.merge_pull_request(
                "owner", "repo", 1, expected_head_sha=SHA, method="squash"
            )
        with pytest.raises(ValueError, match="pull_number"):
            await client.merge_pull_request("owner", "repo", 0, expected_head_sha=SHA)
    finally:
        await client.aclose()


class FakeMergeClient:
    def __init__(
        self,
        token: str,
        *,
        ci_conclusion: str = "success",
        head_sha: str = SHA,
        base_sha: str = BASE_SHA,
        latest_head_sha: str | None = None,
        latest_base_sha: str | None = None,
        behind_by: int = 0,
    ):
        assert token == "token"
        self.ci_conclusion = ci_conclusion
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.latest_head_sha = latest_head_sha
        self.latest_base_sha = latest_base_sha
        self.behind_by = behind_by
        self.compare_calls: list[tuple[str, str]] = []
        self.merge_calls: list[dict] = []
        self.pr_reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_pull_request(self, owner: str, repo: str, pull_number: int):
        self.pr_reads += 1
        current_head_sha = (
            self.head_sha if self.pr_reads == 1 else self.latest_head_sha or self.head_sha
        )
        current_base_sha = (
            self.base_sha if self.pr_reads == 1 else self.latest_base_sha or self.base_sha
        )
        merged = bool(self.merge_calls)
        return {
            "number": pull_number,
            "state": "closed" if merged else "open",
            "merged": merged,
            "mergeable": True,
            "merge_commit_sha": "b" * 40 if merged else None,
            "head": {"sha": current_head_sha, "ref": "feat/x"},
            "base": {"sha": current_base_sha, "ref": "master"},
            "html_url": "https://git.example/pr/25",
        }

    async def list_action_runs(self, owner: str, repo: str, status: str | None, limit: int):
        assert status is None
        assert limit == 50
        return {
            "workflow_runs": [
                {
                    "id": 765,
                    "event": "pull_request",
                    "head_sha": self.head_sha,
                    "status": "completed",
                    "conclusion": self.ci_conclusion,
                }
            ]
        }

    async def compare_commits(self, owner: str, repo: str, *, base: str, head: str):
        self.compare_calls.append((base, head))
        current_head_sha = self.latest_head_sha or self.head_sha
        current_base_sha = self.latest_base_sha or self.base_sha
        if base == current_base_sha and head == current_head_sha:
            return {"total_commits": 1, "commits": [{"sha": current_head_sha}]}
        if base == current_head_sha and head == current_base_sha:
            return {"total_commits": self.behind_by, "commits": [{}] * self.behind_by}
        if base == self.base_sha and head == self.head_sha:
            return {"total_commits": 1, "commits": [{"sha": self.head_sha}]}
        if base == self.head_sha and head == self.base_sha:
            return {"total_commits": self.behind_by, "commits": [{}] * self.behind_by}
        raise AssertionError(f"unexpected compare: {base!r}...{head!r}")

    async def merge_pull_request(self, owner: str, repo: str, pull_number: int, **kwargs):
        self.merge_calls.append(
            {"owner": owner, "repo": repo, "pull_number": pull_number, **kwargs}
        )
        return {}


@pytest.mark.asyncio
async def test_adapter_merges_only_expected_green_head_and_confirms_result(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is True
    assert client.merge_calls == [
        {
            "owner": "owner",
            "repo": "repo",
            "pull_number": 25,
            "expected_head_sha": SHA,
            "method": "merge",
        }
    ]
    assert result["result"] == {
        "number": 25,
        "merged": True,
        "head_sha": SHA,
        "base": "master",
        "base_sha": BASE_SHA,
        "method": "merge",
        "branch_tracking": {
            "base_ref": "master",
            "base_sha": BASE_SHA,
            "head_ref": "feat/x",
            "head_sha": SHA,
            "compare_by": "sha",
            "branch_contains_base": True,
            "branch_is_current": True,
            "ahead_by": 1,
            "behind_by": 0,
            "warning": None,
            "operator_choices": [],
        },
        "outdated_base_accepted": False,
        "merge_commit_sha": "b" * 40,
        "html_url": "https://git.example/pr/25",
    }
    assert client.compare_calls == [
        (BASE_SHA, SHA),
        (SHA, BASE_SHA),
        (BASE_SHA, SHA),
        (SHA, BASE_SHA),
    ]
    assert client.pr_reads == 3
    assert "token" not in repr(result)


@pytest.mark.asyncio
async def test_adapter_rejects_changed_head_before_merge(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", head_sha="c" * 40)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_rejects_outdated_branch_before_using_ci(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", behind_by=2)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "PR_BRANCH_OUTDATED"
    tracking = result["error"]["details"]["branch_tracking"]
    assert tracking["branch_is_current"] is False
    assert tracking["behind_by"] == 2
    assert tracking["warning"] == "PR_BRANCH_OUTDATED"
    assert tracking["operator_choices"][0]["action"] == "update_branch_to_base_and_rerun_ci"
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_allows_outdated_branch_only_with_explicit_override(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", behind_by=1)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request(
        "owner",
        "repo",
        25,
        SHA,
        allow_outdated_base=True,
    )

    assert result["ok"] is True
    assert result["result"]["outdated_base_accepted"] is True
    assert result["result"]["branch_tracking"]["branch_is_current"] is False
    assert len(client.merge_calls) == 1


@pytest.mark.asyncio
async def test_adapter_rejects_non_green_ci_before_merge(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", ci_conclusion="failure")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "CI_NOT_GREEN"
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_uses_newest_matching_ci_run(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token")

    async def list_runs(owner: str, repo: str, status: str | None, limit: int):
        assert status is None
        return {
            "workflow_runs": [
                {
                    "id": 10,
                    "event": "pull_request",
                    "head_sha": SHA,
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "id": 11,
                    "event": "pull_request",
                    "head_sha": SHA,
                    "status": "completed",
                    "conclusion": "failure",
                },
            ]
        }

    client.list_action_runs = list_runs  # type: ignore[method-assign]
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "CI_NOT_GREEN"
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_rejects_expected_base_sha_mismatch(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request(
        "owner",
        "repo",
        25,
        SHA,
        expected_base_sha=NEW_BASE_SHA,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "BASE_MISMATCH"
    assert result["error"]["details"] == {
        "expected_base_sha": NEW_BASE_SHA,
        "observed_base_sha": BASE_SHA,
    }
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_rejects_base_advancing_after_green_ci(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeMergeClient("token", latest_base_sha=NEW_BASE_SHA)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "BASE_MISMATCH"
    assert result["error"]["details"] == {
        "expected_base_sha": BASE_SHA,
        "observed_base_sha": NEW_BASE_SHA,
    }
    assert client.merge_calls == []


@pytest.mark.asyncio
async def test_adapter_requires_gitea_token(monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPENDENCY_MISSING"
