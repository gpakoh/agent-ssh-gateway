"""File-read schema errors must fail before Gitea credentials or transport (#884)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_value",
    [-1, -50, True, False, "0", "100", 1.5, None, [], {}],
)
async def test_invalid_content_limit_fails_before_auth(
    monkeypatch: pytest.MonkeyPatch, bad_value: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("network constructed")
    ):
        result = await remote.gitea_get_file(
            "owner", "repo", "test.txt", max_content_bytes=bad_value
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == "max_content_bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_value",
    ["false", "true", 0, 1, None, [], {}],
)
async def test_invalid_include_content_fails_before_auth(
    monkeypatch: pytest.MonkeyPatch, bad_value: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("network constructed")
    ):
        result = await remote.gitea_get_file(
            "owner", "repo", "test.txt", include_content=bad_value
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == "include_content"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_limit", "include_content"),
    [(0, True), (1, False), (16384, True), (1048576, True)],
)
async def test_valid_schema_without_credentials_keeps_auth_boundary(
    monkeypatch: pytest.MonkeyPatch, content_limit: int, include_content: bool
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("network constructed")
    ):
        result = await remote.gitea_get_file(
            "owner", "repo", "test.txt",
            include_content=include_content, max_content_bytes=content_limit
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_limit", "include_content", "forwarded"),
    [(0, True, None), (1, False, 1), (16384, True, 16384), (1048576, True, 1048576)],
)
async def test_valid_schema_forwards_existing_client_contract(
    monkeypatch: pytest.MonkeyPatch, content_limit: int,
    include_content: bool, forwarded: int | None
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.get_file.return_value = {"path": "test.txt"}
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        result = await remote.gitea_get_file(
            "owner", "repo", "test.txt",
            include_content=include_content, max_content_bytes=content_limit
        )

    assert result["ok"] is True
    client.get_file.assert_awaited_once_with(
        "owner", "repo", "test.txt",
        branch=None, include_content=include_content, max_content_bytes=forwarded
    )
