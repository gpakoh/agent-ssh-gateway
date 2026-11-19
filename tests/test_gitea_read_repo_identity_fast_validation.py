"""Gitea read entrypoints reject unsafe repository identity before credentials (#884)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote

READ_TOOLS = (
    remote.gitea_get_repo,
    remote.gitea_get_file,
    remote.gitea_list_workflows,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", READ_TOOLS, ids=lambda tool: tool.__name__)
@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("owner", ""), ("repo", ""), ("owner", "foo/../../admin"),
        ("repo", "../admin"), ("owner", "owner\\\\bad"),
        ("owner", "valid-owner\n"), ("repo", "valid-repo\r"),
        ("repo", "bad\nheader"), ("owner", " bad"), ("repo", "bad "),
        ("owner", "a" * 101), ("repo", "r" * 101),
        ("owner", None), ("repo", True),
        ("owner", 42), ("repo", {"name": "repo"}),
    ],
)
async def test_invalid_identity_is_typed_and_not_echoed_before_auth(
    monkeypatch: pytest.MonkeyPatch, tool: Any, field: str, invalid: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    owner: object = "valid-owner"
    repo: object = "valid-repo"
    if field == "owner":
        owner = invalid
    else:
        repo = invalid
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("HTTP client must not be constructed"),
    ):
        result = await tool(cast(Any, owner), cast(Any, repo))

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"] == {"field": field}
    assert result["error"]["message"] == f"{field} must be a safe Gitea repository identifier"
    if len(str(invalid).strip()) >= 3:
        assert str(invalid) not in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", READ_TOOLS, ids=lambda tool: tool.__name__)
async def test_valid_identity_without_auth_keeps_auth_boundary(
    monkeypatch: pytest.MonkeyPatch, tool: Any
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("HTTP client must not be constructed"),
    ):
        result = await tool("valid-owner", "valid-repo")
    assert result["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", READ_TOOLS, ids=lambda tool: tool.__name__)
async def test_valid_identity_forwards_verbatim(
    monkeypatch: pytest.MonkeyPatch, tool: Any
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.get_repo.return_value = {
        "owner": {"login": "valid-owner"}, "name": "valid-repo",
    }
    client.get_file.return_value = {"path": ""}
    client.list_workflows.return_value = {"workflows": []}

    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        result = await tool("valid-owner", "valid-repo")

    assert result["ok"] is True
    if tool is remote.gitea_get_repo:
        client.get_repo.assert_awaited_once_with("valid-owner", "valid-repo")
    elif tool is remote.gitea_get_file:
        client.get_file.assert_awaited_once_with(
            "valid-owner", "valid-repo", "",
            branch=None, include_content=True,
            max_content_bytes=remote.GITEA_GET_FILE_DEFAULT_MAX_CONTENT_BYTES,
        )
    else:
        client.list_workflows.assert_awaited_once_with("valid-owner", "valid-repo")
