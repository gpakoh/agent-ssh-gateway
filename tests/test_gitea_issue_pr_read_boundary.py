"""Gitea issue and PR read IDs are rejected before credentials or network."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "argument_name"),
    [
        ("gitea_get_issue", "issue_number"),
        ("gitea_get_pull_request", "pull_number"),
    ],
)
@pytest.mark.parametrize("bad_id", [0, -1, True, False, "1", 1.5, None])
async def test_invalid_issue_or_pr_number_rejected_before_auth(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    argument_name: str,
    bad_id: object,
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("client was constructed")
    ):
        result = await getattr(remote, tool_name)("owner", "repo", bad_id)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == argument_name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "client_method"),
    [
        ("gitea_get_issue", "get_issue"),
        ("gitea_get_pull_request", "get_pull_request"),
    ],
)
async def test_valid_issue_or_pr_number_still_reaches_client(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, client_method: str
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    raw = {"number": 42, "title": "test", "body": "", "state": "open"}
    getattr(client, client_method).return_value = raw
    with (
        patch.object(remote, "_server_gitea_client") as factory,
        patch.object(remote, "_gitea_pr_branch_tracking", new_callable=AsyncMock) as tracking,
    ):
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        tracking.return_value = {}
        result = await getattr(remote, tool_name)("owner", "repo", 42)

    assert result["ok"] is True
    getattr(client, client_method).assert_awaited_once_with("owner", "repo", 42)
