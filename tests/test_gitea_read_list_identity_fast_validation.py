"""Gitea list/run readers reject unsafe owner/repo without leaking identity (#884)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote

READERS = (
    (remote.gitea_list_branches, {"limit": 1}),
    (remote.gitea_list_issues, {"state": "open", "limit": 1}),
    (remote.gitea_list_action_runs, {"limit": 1}),
    (remote.gitea_get_action_run, {"run_id": 1}),
    (remote.gitea_list_action_jobs, {"page": 1, "limit": 1}),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"),
    READERS,
    ids=lambda item: item.__name__ if callable(item) else None,
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
async def test_invalid_identity_fails_before_auth_and_never_echoes(
    monkeypatch: pytest.MonkeyPatch,
    tool: Any, kwargs: dict[str, Any], field: str, invalid: object,
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    owner: object = "safe-owner"
    repo: object = "safe-repo"
    if field == "owner":
        owner = invalid
    else:
        repo = invalid
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("HTTP client must not be constructed"),
    ):
        result = await tool(cast(Any, owner), cast(Any, repo), **kwargs)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"] == {"field": field}
    assert result["error"]["message"] == f"{field} must be a safe Gitea repository identifier"
    assert str(invalid) not in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"), READERS,
    ids=lambda item: item.__name__ if callable(item) else None,
)
async def test_valid_identity_preserves_auth_boundary(
    monkeypatch: pytest.MonkeyPatch, tool: Any, kwargs: dict[str, Any]
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("HTTP client must not be constructed"),
    ):
        result = await tool("safe-owner", "safe-repo", **kwargs)
    assert result["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"), READERS,
    ids=lambda item: item.__name__ if callable(item) else None,
)
async def test_valid_identity_forwards_without_rewriting(
    monkeypatch: pytest.MonkeyPatch, tool: Any, kwargs: dict[str, Any]
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.list_branches.return_value = []
    client.list_issues.return_value = []
    client.list_action_runs.return_value = {"workflow_runs": []}
    client.get_action_run.return_value = {"id": 1}
    client.list_action_jobs.return_value = {"jobs": []}

    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        result = await tool("safe-owner", "safe-repo", **kwargs)

    assert result["ok"] is True
    if tool is remote.gitea_list_branches:
        client.list_branches.assert_awaited_once_with("safe-owner", "safe-repo", limit=1)
    elif tool is remote.gitea_list_issues:
        client.list_issues.assert_awaited()
    elif tool is remote.gitea_list_action_runs:
        client.list_action_runs.assert_awaited_once_with(
            "safe-owner", "safe-repo", status=None, limit=1
        )
    elif tool is remote.gitea_get_action_run:
        client.get_action_run.assert_awaited_once_with("safe-owner", "safe-repo", 1)
    else:
        client.list_action_jobs.assert_awaited_once_with(
            "safe-owner", "safe-repo", status=None, page=1, limit=1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_limit", [0, -1, 51, 500, 501, True, "50", None])
@pytest.mark.parametrize("token_present", [False, True])
async def test_issues_pagination_limit_rejected_before_token_or_http(
    monkeypatch: pytest.MonkeyPatch, invalid_limit: object, token_present: bool,
) -> None:
    """Confirmed Issues accept at most 50, independent of generic Gitea pagination."""
    if token_present:
        monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    else:
        monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("invalid limits must not open an HTTP client"),
    ):
        result = await remote.gitea_list_issues(
            "safe-owner", "safe-repo", limit=cast(Any, invalid_limit)
        )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"] == {
        "field": "limit", "min_value": 1, "max_value": 50,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_limit", [1, 30, 50])
async def test_issues_pagination_boundary_remains_accepted(
    monkeypatch: pytest.MonkeyPatch, valid_limit: int,
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.list_issues.return_value = []
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        result = await remote.gitea_list_issues(
            "safe-owner", "safe-repo", limit=valid_limit
        )
    assert result["ok"] is True
    assert result["result"]["count"] == 0
    client.list_issues.assert_awaited_once_with(
        "safe-owner", "safe-repo", state="open", limit=valid_limit, page=1
    )
