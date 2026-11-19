"""Keep Gitea governance, Actions artifact/job readers safe before auth (#884)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote

HEAD = "a" * 40
READERS = (
    (remote.gitea_get_branch_protection, {"name": "master"}),
    (remote.gitea_list_action_run_artifacts, {
        "pull_number": 54,
        "run_id": 200,
        "expected_pr_head_sha": HEAD,
        "expected_run_attempt": 2,
        "max_results": 1,
    }),
    (remote.gitea_list_action_run_jobs, {"run_id": 200}),
    (remote.gitea_get_action_job_logs, {
        "job_id": 200, "max_bytes": 1024, "tail_lines": 5,
    }),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader", "kwargs"), READERS,
    ids=lambda value: value.__name__ if callable(value) else None,
)
@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("owner", "foo/../../admin"), ("repo", "../admin"),
        ("owner", "bad\nheader"), ("repo", "bad\rheader"),
        ("owner", " bad"), ("repo", "bad "),
        ("owner", None), ("repo", {"repo": "bad"}),
    ],
)
async def test_malformed_identity_fails_before_auth_without_echo(
    monkeypatch: pytest.MonkeyPatch, reader: Any,
    kwargs: dict[str, Any], field: str, invalid: object,
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
        side_effect=AssertionError("must not construct HTTP client"),
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
    ids=lambda value: value.__name__ if callable(value) else None,
)
async def test_valid_identity_requires_credentials_without_http(
    monkeypatch: pytest.MonkeyPatch, reader: Any, kwargs: dict[str, Any]
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client",
        side_effect=AssertionError("must not construct HTTP client"),
    ):
        response = await reader("safe-owner", "safe-repo", **kwargs)
    assert response["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
async def test_valid_run_jobs_forwards_identity_and_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.list_action_run_jobs.return_value = {"jobs": []}
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        response = await remote.gitea_list_action_run_jobs("safe-owner", "safe-repo", 200)
    assert response["ok"] is True
    client.list_action_run_jobs.assert_awaited_once_with("safe-owner", "safe-repo", 200)


@pytest.mark.asyncio
async def test_valid_job_log_forwards_identity_and_bounded_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.get_action_job_logs.return_value = {"logs": "safe\nline"}
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        response = await remote.gitea_get_action_job_logs(
            "safe-owner", "safe-repo", 200, max_bytes=1024, tail_lines=5,
        )
    assert response["ok"] is True
    client.get_action_job_logs.assert_awaited_once_with(
        "safe-owner", "safe-repo", 200, max_bytes=1024,
    )
