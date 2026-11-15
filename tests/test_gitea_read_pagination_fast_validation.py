"""Gitea list/read pagination must reject schema errors before auth or HTTP (#884)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["gitea_list_branches", "gitea_list_commits", "gitea_list_issues"],
)
@pytest.mark.parametrize("bad_limit", [0, -1, True, False, "2", 2.5, None, 501])
async def test_gitea_list_limit_invalid_before_credentials(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, bad_limit: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("network client constructed")
    ):
        result = await getattr(remote, tool_name)("owner", "repo", limit=bad_limit)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == "limit"
    assert result["error"]["details"]["min_value"] == 1
    assert result["error"]["details"]["max_value"] == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("page", 0), ("page", -1), ("page", True), ("page", False),
        ("page", "2"), ("page", 1.5), ("page", None),
        ("limit", 0), ("limit", -1), ("limit", True), ("limit", False),
        ("limit", "2"), ("limit", 2.5), ("limit", None), ("limit", 51),
    ],
)
async def test_commit_status_pagination_invalid_before_credentials(
    monkeypatch: pytest.MonkeyPatch, field: str, bad_value: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("network client constructed")
    ):
        result = await remote.gitea_list_commit_statuses(
            "owner", "repo", "a" * 40, **{field: bad_value}
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == field
    assert result["error"]["details"]["min_value"] == 1
    if field == "limit":
        assert result["error"]["details"]["max_value"] == 50
    else:
        assert "max_value" not in result["error"]["details"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["gitea_list_branches", "gitea_list_commits", "gitea_list_issues"],
)
async def test_valid_list_limit_preserves_auth_failure(
    monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("network client constructed")
    ):
        result = await getattr(remote, tool_name)("owner", "repo", limit=30)
    assert result["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
async def test_valid_status_pagination_preserves_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("network client constructed")
    ):
        result = await remote.gitea_list_commit_statuses(
            "owner", "repo", "a" * 40, page=2, limit=30
        )
    assert result["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "client_method"),
    [
        ("gitea_list_branches", "list_branches"),
        ("gitea_list_commits", "list_commits"),
        ("gitea_list_commit_statuses", "list_commit_statuses"),
    ],
)
async def test_valid_limits_reach_existing_client(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, client_method: str
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    getattr(client, client_method).return_value = []
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        if tool_name == "gitea_list_commit_statuses":
            result = await remote.gitea_list_commit_statuses(
                "owner", "repo", "a" * 40, page=2, limit=30
            )
            client.list_commit_statuses.assert_awaited_once_with(
                "owner", "repo", "a" * 40, page=2, limit=30
            )
        else:
            result = await getattr(remote, tool_name)("owner", "repo", limit=30)
            if tool_name == "gitea_list_branches":
                client.list_branches.assert_awaited_once_with("owner", "repo", limit=30)
            else:
                client.list_commits.assert_awaited_once_with(
                    "owner", "repo", sha=None, limit=30
                )
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_commit_status_large_positive_page_keeps_existing_transport_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.list_commit_statuses.return_value = []
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        result = await remote.gitea_list_commit_statuses(
            "owner", "repo", "a" * 40, page=501, limit=30
        )
    assert result["ok"] is True
    client.list_commit_statuses.assert_awaited_once_with(
        "owner", "repo", "a" * 40, page=501, limit=30
    )
