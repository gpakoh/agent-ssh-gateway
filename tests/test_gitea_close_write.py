"""Security and contract tests for the narrow Gitea PR close tool.

The close tool must never touch merge or branch deletion: the only
mutation it may issue is a single PATCH that sets state=closed (see
`_patched endpoint allowlist` in gitea_client.py); head-SHA equality is
checked against a freshly-fetched PR BEFORE any mutation, so any
mismatch fails closed with zero writes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_infra.adapters import remote

SHA = "a" * 40


@pytest.mark.asyncio
async def test_client_close_pr_uses_fixed_endpoint_and_state_closed(monkeypatch):
    client = GiteaClient("token")
    patch = AsyncMock(return_value={"number": 25, "state": "closed"})
    monkeypatch.setattr(client, "_patch", patch)
    try:
        result = await client.close_pull_request("owner", "repo", 25)
    finally:
        await client.aclose()

    assert result == {"number": 25, "state": "closed"}
    patch.assert_awaited_once_with(
        "/repos/{owner}/{repo}/pulls/{number}",
        {"state": "closed"},
        owner="owner",
        repo="repo",
        number=25,
    )


@pytest.mark.asyncio
async def test_client_close_pr_rejects_bad_pull_number():
    client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="pull_number"):
            await client.close_pull_request("owner", "repo", 0)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_close_patch_rejects_non_allowlisted_endpoints():
    client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="Write endpoint not allowed"):
            await client._patch(
                "/repos/{owner}/{repo}/issues/{number}",
                {"state": "closed"},
                owner="owner",
                repo="repo",
                number=25,
            )
    finally:
        await client.aclose()


class FakeCloseClient:
    """Stand-in Gitea client: the ONLY mutation surface is close_pull_request.

    There is deliberately no merge_pull_request or branch-deletion method
    here -- any adapter attempt to merge or delete would raise
    AttributeError and surface as an INTERNAL_ERROR instead of mutating.
    """

    def __init__(self, token: str, *, state: str = "open", head_sha: str = SHA):
        assert token == "token"
        self.state = state
        self.head_sha = head_sha
        self.pr_reads = 0
        self.close_calls: list[tuple[str, str, int]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_pull_request(self, owner: str, repo: str, pull_number: int):
        self.pr_reads += 1
        return {
            "number": pull_number,
            "state": self.state,
            "head": {"sha": self.head_sha, "ref": "feat/x"},
            "base": {"ref": "master"},
            "html_url": "https://git.example/pr/25",
        }

    async def close_pull_request(self, owner: str, repo: str, pull_number: int):
        self.close_calls.append((owner, repo, pull_number))
        return {"number": pull_number, "state": "closed"}


@pytest.mark.asyncio
async def test_adapter_closes_exact_open_pr_once(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeCloseClient("token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_close_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is True
    assert client.pr_reads == 1
    assert client.close_calls == [("owner", "repo", 25)]
    assert result["result"] == {
        "number": 25,
        "closed": True,
        "already_closed": False,
        "head_sha": SHA,
        "base": "master",
        "html_url": "https://git.example/pr/25",
    }


@pytest.mark.asyncio
async def test_adapter_head_mismatch_fails_closed_zero_mutation(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeCloseClient("token", head_sha="c" * 40)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_close_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert client.close_calls == []


@pytest.mark.asyncio
async def test_adapter_already_closed_same_sha_is_idempotent(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeCloseClient("token", state="closed")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_close_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is True
    assert result["result"]["number"] == 25
    assert result["result"]["closed"] is True
    assert result["result"]["already_closed"] is True
    assert result["result"]["head_sha"] == SHA
    assert result["result"]["base"] == "master"
    assert result["result"]["html_url"] == "https://git.example/pr/25"
    assert client.close_calls == []


@pytest.mark.asyncio
async def test_adapter_already_closed_wrong_sha_fails_closed(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeCloseClient("token", state="closed", head_sha="c" * 40)
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_close_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert client.close_calls == []


@pytest.mark.asyncio
async def test_adapter_surfaces_close_api_failure(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    client = FakeCloseClient("token")

    async def boom(owner: str, repo: str, pull_number: int):
        raise PermissionError("gitea api /repos/o/r/pulls/25: forbidden")

    client.close_pull_request = boom  # type: ignore[method-assign]
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: client)

    result = await remote.gitea_close_pull_request("owner", "repo", 25, SHA)

    assert result["ok"] is False
    assert result["error"]["code"] == "AUTH_ERROR"


@pytest.mark.asyncio
async def test_adapter_requires_gitea_token(monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    result = await remote.gitea_close_pull_request("owner", "repo", 25, SHA)
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
async def test_adapter_rejects_invalid_head_sha(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    result = await remote.gitea_close_pull_request("owner", "repo", 25, "abc")
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"