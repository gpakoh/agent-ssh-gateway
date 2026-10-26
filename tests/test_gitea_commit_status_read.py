"""Read-only Gitea statuses must be scoped to one exact commit and never imply CI success."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"))

from fleet.gitea_client import (
    GiteaClient,
    GiteaCommitStatusResponseError,
)

from examples.mcp_server.mcp_infra.adapters import remote

SHA = "a" * 40


def test_commit_status_read_is_available_in_both_mcp_client_modes():
    from examples.mcp_server.tool_modes import tools_for_mode

    for mode in ("mcp_client", "mcp_client_write"):
        assert "gitea_list_commit_statuses" in tools_for_mode(mode)
    assert "gitea_list_commit_statuses" not in tools_for_mode("standard")


@pytest.mark.asyncio
async def test_statuses_exact_endpoint_bounded_and_minimized(monkeypatch):
    get = AsyncMock(return_value=[
        {
            "id": 23, "context": "ci/verify", "status": "success",
            "target_url": "https://git.example.com/org/repo/actions/runs/3",
            "creator": {"login": "ci-bot", "email": "secret@example.com", "is_admin": True},
            "created_at": "2026-10-25T00:00:00Z",
            "updated_at": "2026-10-25T00:02:00Z",
            "description": "private output",
        },
        {
            "id": 24, "context": "ci/docker", "status": "skipped",
            "target_url": "https://git.example.com/run/1?token=sensitive",
            "creator": None,
        },
    ])
    monkeypatch.setattr(GiteaClient, "_get", get)
    client = GiteaClient("test-token")
    try:
        data = await client.list_commit_statuses("owner", "repo", SHA, page=2, limit=3)
    finally:
        await client.aclose()
    get.assert_awaited_once_with(
        "/repos/{owner}/{repo}/statuses/{sha}",
        params={"page": 2, "limit": 3},
        owner="owner", repo="repo", sha=SHA,
    )
    assert len(data) == 2
    assert data[0] == {
        "id": 23, "context": "ci/verify", "state": "success",
        "conclusion": None,
        "target_url": "https://git.example.com/org/repo/actions/runs/3",
        "creator": {"login": "ci-bot"},
        "created_at": "2026-10-25T00:00:00Z",
        "updated_at": "2026-10-25T00:02:00Z",
    }
    assert data[1]["state"] == "skipped"
    assert data[1]["target_url"] is None
    assert "secret@example.com" not in str(data)
    assert "private output" not in str(data)
    assert "sensitive" not in str(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["a" * 39, "main", "A" * 40, "", None, 1])
async def test_statuses_reject_non_exact_sha_without_http(monkeypatch, invalid):
    get = AsyncMock()
    monkeypatch.setattr(GiteaClient, "_get", get)
    client = GiteaClient("test-token")
    try:
        with pytest.raises(ValueError, match="exact lowercase"):
            await client.list_commit_statuses("owner", "repo", invalid)
    finally:
        await client.aclose()
    get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("page,limit", [(0, 2), (True, 2), (1, 0), (1, 51), (1, True)])
async def test_statuses_reject_invalid_page_bounds(monkeypatch, page, limit):
    get = AsyncMock()
    monkeypatch.setattr(GiteaClient, "_get", get)
    client = GiteaClient("test-token")
    try:
        with pytest.raises(ValueError):
            await client.list_commit_statuses("owner", "repo", SHA, page=page, limit=limit)
    finally:
        await client.aclose()
    get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    {"statuses": []}, None, [None],
    [{"context": "ci", "status": "green"}],
    [{"context": "", "status": "success"}],
    [{"context": "ci", "status": "success", "sha": "b" * 40}],
    [{"context": "ci", "status": "success", "creator": "secret"}],
])
async def test_statuses_fail_closed_on_malformed_upstream(monkeypatch, raw):
    monkeypatch.setattr(GiteaClient, "_get", AsyncMock(return_value=raw))
    client = GiteaClient("test-token")
    try:
        with pytest.raises(GiteaCommitStatusResponseError):
            await client.list_commit_statuses("owner", "repo", SHA)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_statuses_reject_page_exceeding_limit(monkeypatch):
    monkeypatch.setattr(GiteaClient, "_get", AsyncMock(return_value=[
        {"context": "ci/verify", "status": "pending"},
        {"context": "ci/docker", "status": "success"},
    ]))
    client = GiteaClient("test-token")
    try:
        with pytest.raises(GiteaCommitStatusResponseError):
            await client.list_commit_statuses("owner", "repo", SHA, limit=1)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_statuses_tool_absence_and_pagination_are_explicit(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "test-token")

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def list_commit_statuses(self, owner, repo, sha, *, page, limit):
            assert (owner, repo, sha, limit) == ("owner", "repo", SHA, 2)
            return [] if page == 1 else [
                {"context": "ci/verify", "state": "pending"},
                {"context": "ci/docker", "state": "skipped"},
            ]

    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda _: FakeClient())
    empty = await remote.gitea_list_commit_statuses("owner", "repo", SHA, page=1, limit=2)
    assert empty["ok"] is True
    assert empty["result"]["absent"] is True
    assert empty["result"]["complete"] is True
    assert empty["result"]["sha"] == SHA
    next_page = await remote.gitea_list_commit_statuses("owner", "repo", SHA, page=2, limit=2)
    assert next_page["result"]["absent"] is False
    assert next_page["result"]["complete"] is False
    assert next_page["result"]["more_pages_possible"] is True
    assert next_page["result"]["statuses"][1]["state"] == "skipped"


@pytest.mark.asyncio
async def test_statuses_tool_rejects_bad_sha_before_any_remote_request(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "test-token")
    factory = AsyncMock()
    monkeypatch.setattr(remote, "_server_gitea_client", factory)
    invalid = await remote.gitea_list_commit_statuses("owner", "repo", "main")
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "INVALID_INPUT"
    factory.assert_not_awaited()
