"""P1 #702: Gitea Issue/PR separation and bounded pagination contracts."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "examples" / "mcp_server", ROOT / "examples" / "mcp_client_remote"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import fleet.gitea_server as fleet_server  # noqa: E402

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient  # noqa: E402
from examples.mcp_client_remote.fleet.shared import (  # noqa: E402
    classify_gitea_issue,
    list_confirmed_gitea_issues,
)
from examples.mcp_server import server as gateway_server  # noqa: E402


def issue(n: int, **fields: object) -> dict:
    return {
        "number": n, "title": f"Issue {n}", "state": "closed",
        "pull_request": None, **fields,
    }


def pull(n: int) -> dict:
    return {
        "number": n, "title": f"PR {n}", "state": "closed",
        "pull_request": {"merged": True},
    }


class PageClient:
    def __init__(self, pages: dict[int, list[dict]], error_page: int | None = None):
        self.pages = pages
        self.error_page = error_page
        self.calls: list[tuple[str, int, int]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def list_issues(self, owner, repo, *, state, limit, page=1):
        self.calls.append((state, limit, page))
        if self.error_page == page:
            raise TimeoutError("simulated timeout")
        return self.pages.get(page, [])

    async def get_issue(self, owner, repo, number):
        return self.pages[1][0]

    async def list_pull_requests(self, owner, repo, *, state, limit, page=1):
        return [pull(50)]


@pytest.mark.asyncio
async def test_underlying_client_sends_typed_filter_before_pagination():
    async with GiteaClient("dummy-token") as client:
        client._get = AsyncMock(return_value=[issue(12)])
        assert await client.list_issues("owner", "repo", state="closed", limit=15, page=3)
        assert client._get.await_args.kwargs["params"] == {
            "state": "closed", "type": "issues", "limit": 15, "page": 3,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["open", "closed", "all"])
async def test_both_mcp_surfaces_type_filter_and_deep_issues(monkeypatch, state):
    pages = {
        1: [pull(i) for i in range(1, 51)],
        2: [issue(51), issue(52)],
    }
    gateway_client = PageClient(pages)
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(gateway_server, "GiteaClient", lambda token: gateway_client)
    gateway = await gateway_server.gitea_list_issues("owner", "repo", state=state, limit=50)
    assert gateway["ok"] is True
    result = gateway["result"]
    assert [i["number"] for i in result["items"]] == [51, 52]
    assert result["count"] == 2
    assert result["filtered_pull_requests"] == 50
    assert result["pages_scanned"] == 2
    assert result["partial"] is False
    assert result["truncated"] is False
    assert gateway_client.calls == [(state, 50, 1), (state, 50, 2)]

    fleet_client = PageClient(pages)
    monkeypatch.setattr(fleet_server, "_get_client", lambda: fleet_client)
    fleet = await fleet_server.gitea_list_issues("owner", "repo", state=state, limit=50)
    assert fleet["ok"] is True
    assert fleet["result"] == result
    assert fleet_client.calls == gateway_client.calls


@pytest.mark.asyncio
async def test_pr_only_pages_are_not_misreported_as_zero_issues():
    pages = {page: [pull(page * 100 + i) for i in range(10)] for page in range(1, 9)}
    client = PageClient(pages)
    result = await list_confirmed_gitea_issues(
        client, "owner", "repo", state="closed", limit=10,
    )
    assert result["count"] == 0
    assert result["items"] == []
    assert result["partial"] is True
    assert result["truncated"] is True
    assert result["more_pages_possible"] is True
    assert result["pages_scanned"] == 8
    assert result["filtered_pull_requests"] == 80


@pytest.mark.asyncio
async def test_malformed_pr_marker_and_unknown_entry_never_become_issues():
    client = PageClient({
        1: [
            issue(1), {"number": 2, "pull_request": "malformed"}, None, pull(4),
            {"number": 5, "title": "", "state": "open"},
            {"number": 6, "title": "partial"},
        ],
    })
    result = await list_confirmed_gitea_issues(
        client, "owner", "repo", state="all", limit=10,
    )
    assert result["count"] == 1
    assert result["items"][0]["item_type"] == "issue"
    assert result["unknown_items"] == 4
    assert result["filtered_pull_requests"] == 1
    assert result["partial"] is True


@pytest.mark.asyncio
async def test_duplicate_page_boundary_is_not_counted_twice():
    client = PageClient({
        1: [issue(1), issue(2), pull(9)],
        2: [issue(2), issue(3)],
    })
    result = await list_confirmed_gitea_issues(
        client, "owner", "repo", state="open", limit=3,
    )
    assert [x["number"] for x in result["items"]] == [1, 2, 3]
    assert result["duplicate_items"] == 1
    assert result["partial"] is True
    assert result["more_pages_possible"] is False


@pytest.mark.asyncio
async def test_later_page_failure_preserves_qualified_items_but_marks_partial():
    client = PageClient({1: [issue(1), pull(2)]}, error_page=2)
    result = await list_confirmed_gitea_issues(
        client, "owner", "repo", state="open", limit=2,
    )
    assert result["count"] == 1
    assert result["partial"] is True
    assert result["partial_error_class"] == "TimeoutError"
    assert result["more_pages_possible"] is True


@pytest.mark.asyncio
async def test_first_page_failure_remains_error():
    client = PageClient({}, error_page=1)
    with pytest.raises(TimeoutError):
        await list_confirmed_gitea_issues(client, "owner", "repo", state="all", limit=4)


def test_untyped_get_distinguishes_pr_and_unknown():
    assert classify_gitea_issue(pull(17)) == "pull_request"
    assert classify_gitea_issue(issue(18)) == "issue"
    assert classify_gitea_issue({"number": 19}) == "unknown"
    assert classify_gitea_issue({"number": 19}, typed_list=True) == "issue"
    assert classify_gitea_issue({"number": 19, "head": {"sha": "a"}}) == "unknown"


@pytest.mark.asyncio
async def test_single_get_is_explicitly_typed_and_pr_list_unchanged(monkeypatch):
    client = PageClient({1: [pull(77)]})
    monkeypatch.setenv("GITEA_TOKEN", "fake")
    monkeypatch.setattr(gateway_server, "GiteaClient", lambda token: client)
    result = await gateway_server.gitea_get_issue("owner", "repo", 77)
    assert result["ok"] is True
    assert result["result"]["item_type"] == "pull_request"
    assert result["result"]["pull_request"] is True
    prs = await gateway_server.gitea_list_pull_requests("owner", "repo")
    assert prs["ok"] is True
    assert [x["number"] for x in prs["result"]["items"]] == [50]


@pytest.mark.asyncio
async def test_typed_issue_page_with_missing_optional_pr_field():
    client = PageClient({1: [{"number": 21, "title": "real", "state": "open"}]})
    result = await list_confirmed_gitea_issues(
        client, "owner", "repo", state="all", limit=5,
    )
    assert result["count"] == 1
    assert result["items"][0]["item_type"] == "issue"
    assert result["partial"] is False
