"""File ref validation must precede credentials and Gitea HTTP (#884)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_branch",
    [
        " ", " main", "main ", "main\nextra", "main\rextra",
        "main\tother", "main\x7f", "feature name", "feature//name",
        "feature/", "foo..bar", "../main", "a\\b", "main~1",
        "main^", "@{", "a" * 256, True, 17, ["main"], {"ref": "main"},
    ],
)
async def test_invalid_file_ref_is_typed_before_auth_or_http(
    monkeypatch: pytest.MonkeyPatch, bad_branch: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("must not create HTTP client"),
    ):
        result = await remote.gitea_get_file(
            "owner", "repo", "README.md", branch=cast(Any, bad_branch),
            include_content=False,
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"] == {"field": "branch", "max_length": 255}
    assert "Gitea" not in result["error"]["message"]
    if len(str(bad_branch).strip()) >= 3:
        assert str(bad_branch) not in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "good_branch",
    [None, "", "master", "feature/ref-check", "refs/heads/master",
     "refs/tags/v1.2", "a" * 40],
)
async def test_valid_file_refs_keep_missing_auth_contract(
    monkeypatch: pytest.MonkeyPatch, good_branch: str | None
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("must not create HTTP client"),
    ):
        result = await remote.gitea_get_file(
            "owner", "repo", "README.md", branch=good_branch,
        )

    assert result["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("good_branch", "path"),
    [(None, ""), ("", ""), ("master", "README.md"),
     ("refs/tags/v1.2", "docs/guide.md"), ("a" * 40, "README.md")],
)
async def test_valid_file_refs_forwarded_without_rewriting(
    monkeypatch: pytest.MonkeyPatch, good_branch: str | None, path: str
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.get_file.return_value = {"path": path}
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        result = await remote.gitea_get_file(
            "owner", "repo", path, branch=good_branch, include_content=False,
        )

    assert result["ok"] is True
    client.get_file.assert_awaited_once_with(
        "owner", "repo", path, branch=good_branch,
        include_content=False, max_content_bytes=remote.GITEA_GET_FILE_DEFAULT_MAX_CONTENT_BYTES,
    )
