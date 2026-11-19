"""File paths must be locally validated before Gitea credentials (#884)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_path",
    [
        "/", "/README.md", "../README.md", "a/../b", "./README.md",
        "a/./b", "a//b", "a/", "//a", ".", "..", "a/..",
        None, True, False, 10, [], {"path": "README.md"},
    ],
)
async def test_invalid_file_path_fails_before_credentials(
    monkeypatch: pytest.MonkeyPatch, bad_path: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("network client constructed"),
    ):
        result = await remote.gitea_get_file(
            "owner", "repo", cast(Any, bad_path), include_content=False
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == "path"
    assert result["error"]["message"] == "path must be a safe repository-relative path"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "good_path",
    ["", "README.md", "docs/guide.md", ".github/workflows/ci.yml"],
)
async def test_valid_file_path_preserves_auth_boundary(
    monkeypatch: pytest.MonkeyPatch, good_path: str
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("network client constructed"),
    ):
        result = await remote.gitea_get_file(
            "owner", "repo", good_path, include_content=False
        )

    assert result["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("good_path", "branch"),
    [("", None), ("", "master"), ("README.md", ""), ("docs/guide.md", "master")],
)
async def test_valid_file_path_is_forwarded_unchanged(
    monkeypatch: pytest.MonkeyPatch, good_path: str, branch: str | None
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.get_file.return_value = {"path": good_path}
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        result = await remote.gitea_get_file(
            "owner", "repo", good_path, branch=branch, include_content=False
        )

    assert result["ok"] is True
    client.get_file.assert_awaited_once_with(
        "owner", "repo", good_path, branch=branch,
        include_content=False,
        max_content_bytes=remote.GITEA_GET_FILE_DEFAULT_MAX_CONTENT_BYTES,
    )
