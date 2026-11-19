"""Gitea list-commits ref input is rejected locally before credentials or HTTP (#884)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_sha",
    [
        "", " ", " main", "main ", "main\nnext", "main\rnext",
        "main\tother", "main\x7f", "feature name", "refs/heads/../main",
        "main..other", "feature//other", "feature/", "@{", "main\\other",
        "main~1", "main^", "a" * 256, False, 123, ["main"],
    ],
)
async def test_invalid_commit_ref_fails_before_auth_and_network(
    monkeypatch: pytest.MonkeyPatch, bad_sha: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("client must never be constructed"),
    ):
        result = await remote.gitea_list_commits(
            "owner", "repo", sha=bad_sha, limit=1
        )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == "sha"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "good_sha",
    [None, "main", "feature/ref-validation", "refs/heads/main",
     "refs/tags/v1.0", "a" * 40],
)
async def test_valid_commit_ref_preserves_auth_boundary(
    monkeypatch: pytest.MonkeyPatch, good_sha: str | None
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("client must never be constructed"),
    ):
        result = await remote.gitea_list_commits(
            "owner", "repo", sha=good_sha, limit=1
        )
    assert result["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
@pytest.mark.parametrize("good_sha", [None, "main", "feature/x", "a" * 40])
async def test_valid_commit_ref_forwarded_unmodified(
    monkeypatch: pytest.MonkeyPatch, good_sha: str | None
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "unit-test-token")
    client = AsyncMock()
    client.list_commits.return_value = []
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        result = await remote.gitea_list_commits(
            "owner", "repo", sha=good_sha, limit=1
        )
    assert result["ok"] is True
    client.list_commits.assert_awaited_once_with(
        "owner", "repo", sha=good_sha, limit=1
    )
