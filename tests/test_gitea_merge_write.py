"""Security and contract tests for the narrow Gitea PR merge tool."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_infra.adapters import remote

SHA = "a" * 40


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
    def __init__(self, token: str, *, ci_conclusion: str = "success", head_sha: str = SHA):
        assert token == "token"
        self.ci_conclusion = ci_conclusion
        self.head_sha = head_sha
        self.merge_calls: list[dict] = []
        self.pr_reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_pull_request(self, owner: str, repo: str, pull_number: int):
        self.pr_reads += 1
        if self.pr_reads == 1:
            return {
                "number": pull_number,
                "state": "open",
                "merged": False,
                "mergeable": True,
                "head": {"sha": self.head_sha, "ref": "feat/x"},
                "base": {"ref": "master"},
                "html_url": "https://git.example/pr/25",
            }
        return {
            "number": pull_number,
            "state": "closed",
            "merged": True,
            "mergeable": True,
            "merge_commit_sha": "b" * 40,
            "head": {"sha": self.head_sha, "ref": "feat/x"},
            "base": {"ref": "master"},
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
        "method": "merge",
        "merge_commit_sha": "b" * 40,
        "html_url": "https://git.example/pr/25",
    }
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
async def test_adapter_requires_gitea_token(monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    result = await remote.gitea_merge_pull_request("owner", "repo", 25, SHA)
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPENDENCY_MISSING"
