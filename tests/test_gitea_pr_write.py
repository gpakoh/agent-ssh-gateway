"""Security/contract tests for the narrow Gitea PR creation write surface."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_infra.adapters import remote


@pytest.mark.asyncio
async def test_client_create_pr_validates_and_posts_only_pr_payload(monkeypatch):
    client = GiteaClient("token")
    post = AsyncMock(
        return_value={
            "number": 17,
            "title": "Fleet hardening",
            "state": "open",
            "html_url": "https://git.example/pr/17",
            "head": {"ref": "ai/fleet-hardening"},
            "base": {"ref": "master"},
        }
    )
    monkeypatch.setattr(client, "_post", post)
    try:
        result = await client.create_pull_request(
            "owner",
            "repo",
            title=" Fleet hardening ",
            head="ai/fleet-hardening",
            base="master",
            body=" review me ",
        )
    finally:
        await client.aclose()

    assert result["number"] == 17
    post.assert_awaited_once_with(
        "/repos/{owner}/{repo}/pulls",
        {
            "title": "Fleet hardening",
            "head": "ai/fleet-hardening",
            "base": "master",
            "body": "review me",
        },
        owner="owner",
        repo="repo",
    )


@pytest.mark.asyncio
async def test_client_rejects_cross_repo_or_refspec_style_head():
    client = GiteaClient("token")
    try:
        for head in ("owner:feature", "HEAD:feature", "--force"):
            with pytest.raises(ValueError, match="branch name"):
                await client.create_pull_request(
                    "owner",
                    "repo",
                    title="PR",
                    head=head,
                    base="master",
                )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_rejects_same_head_and_base():
    client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="must differ"):
            await client.create_pull_request(
                "owner",
                "repo",
                title="PR",
                head="feature/x",
                base="feature/x",
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_raw_post_rejects_non_allowlisted_write_endpoint():
    client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="Write endpoint not allowed"):
            await client._post(
                "/repos/{owner}/{repo}/issues",
                {"title": "not allowed"},
                owner="owner",
                repo="repo",
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_adapter_minimizes_created_pr(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "token")
    calls: list[dict] = []

    class FakeClient:
        def __init__(self, token: str):
            assert token == "token"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def create_pull_request(self, owner, repo, **kwargs):
            calls.append({"owner": owner, "repo": repo, **kwargs})
            return {
                "number": 42,
                "title": kwargs["title"],
                "state": "open",
                "html_url": "https://git.example/pr/42",
                "head": {"ref": kwargs["head"], "repo": {"owner": {"email": "secret@example"}}},
                "base": {"ref": kwargs["base"]},
                "mergeable": True,
                "user": {"email": "secret@example"},
            }

    monkeypatch.setattr(remote, "_server_gitea_client", lambda: FakeClient)
    result = await remote.gitea_create_pull_request(
        "owner",
        "repo",
        "Fleet hardening",
        "ai/fleet-hardening",
        "master",
        "Review",
    )

    assert result["ok"] is True
    assert calls == [
        {
            "owner": "owner",
            "repo": "repo",
            "title": "Fleet hardening",
            "head": "ai/fleet-hardening",
            "base": "master",
            "body": "Review",
        }
    ]
    assert result["result"] == {
        "number": 42,
        "title": "Fleet hardening",
        "state": "open",
        "html_url": "https://git.example/pr/42",
        "head": "ai/fleet-hardening",
        "base": "master",
        "mergeable": True,
    }
    assert "secret@example" not in repr(result)


@pytest.mark.asyncio
async def test_adapter_requires_gitea_token(monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    result = await remote.gitea_create_pull_request(
        "owner", "repo", "PR", "feature/x", "master"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "DEPENDENCY_MISSING"
