"""Core Gitea read tools reject unsafe identity at the adapter boundary (#884)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote

SHA = "8ea92bbf37390a4a6feb4b6ce9bd1457e981d988"
READERS = (
    (remote.gitea_list_commits, {"limit": 1}),
    (remote.gitea_list_commit_statuses, {"sha": SHA, "page": 1, "limit": 1}),
    (remote.gitea_get_issue, {"issue_number": 1}),
    (remote.gitea_list_pull_requests, {"state": "open", "limit": 1}),
    (remote.gitea_get_pull_request, {"pull_number": 1}),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader", "kwargs"),
    READERS,
    ids=lambda val: val.__name__ if callable(val) else None,
)
@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("owner", "foo/../../admin"),
        ("repo", "../admin"),
        ("owner", "bad\nheader"),
        ("repo", "bad\rheader"),
        ("owner", " bad"),
        ("repo", "bad "),
        ("owner", None),
        ("repo", {"name": "repo"}),
    ],
)
async def test_unsafe_identity_fails_without_credentials_or_echo(
    monkeypatch: pytest.MonkeyPatch,
    reader: Any,
    kwargs: dict[str, Any],
    field: str,
    invalid: object,
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    owner: object = "safe-owner"
    repo: object = "safe-repo"
    if field == "owner":
        owner = invalid
    else:
        repo = invalid

    with patch.object(
        remote,
        "_server_gitea_client",
        side_effect=AssertionError("must not create a client"),
    ):
        response = await reader(cast(Any, owner), cast(Any, repo), **kwargs)

    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_INPUT"
    assert response["error"]["retryable"] is False
    assert response["error"]["details"] == {"field": field}
    assert response["error"]["message"] == f"{field} must be a safe Gitea repository identifier"
    assert str(invalid) not in response["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader", "kwargs"), READERS,
    ids=lambda val: val.__name__ if callable(val) else None,
)
async def test_valid_identity_without_credentials_still_requires_token(
    monkeypatch: pytest.MonkeyPatch, reader: Any, kwargs: dict[str, Any]
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(remote, "_server_gitea_client", side_effect=AssertionError):
        response = await reader("safe-owner", "safe-repo", **kwargs)
    assert response["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader", "kwargs"), READERS,
    ids=lambda val: val.__name__ if callable(val) else None,
)
async def test_valid_identity_is_forwarded_verbatim(
    monkeypatch: pytest.MonkeyPatch, reader: Any, kwargs: dict[str, Any]
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.list_commits.return_value = []
    client.list_commit_statuses.return_value = []
    client.get_issue.return_value = {"number": 1, "state": "open"}
    client.list_pull_requests.return_value = []
    client.get_pull_request.return_value = {"number": 1, "merged": False}

    with (
        patch.object(remote, "_server_gitea_client") as factory,
        patch.object(remote, "_gitea_pr_branch_tracking", new_callable=AsyncMock),
    ):
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        response = await reader("safe-owner", "safe-repo", **kwargs)

    assert response["ok"] is True
    if reader is remote.gitea_list_commits:
        client.list_commits.assert_awaited_once_with(
            "safe-owner", "safe-repo", sha=None, limit=1
        )
    elif reader is remote.gitea_list_commit_statuses:
        client.list_commit_statuses.assert_awaited_once_with(
            "safe-owner", "safe-repo", SHA, page=1, limit=1
        )
    elif reader is remote.gitea_get_issue:
        client.get_issue.assert_awaited_once_with("safe-owner", "safe-repo", 1)
    elif reader is remote.gitea_list_pull_requests:
        client.list_pull_requests.assert_awaited_once_with(
            "safe-owner", "safe-repo", state="open", limit=1
        )
    else:
        client.get_pull_request.assert_awaited_once_with("safe-owner", "safe-repo", 1)
