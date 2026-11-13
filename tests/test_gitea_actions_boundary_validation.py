"""Gitea Actions input errors must fail before token, client or network work."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"status": "queued"}, "status"),
        ({"status": ""}, "status"),
        ({"status": True}, "status"),
        ({"status": 17}, "status"),
        ({"limit": 0}, "limit"),
        ({"limit": -4}, "limit"),
        ({"limit": True}, "limit"),
        ({"limit": 1.25}, "limit"),
        ({"limit": 501}, "limit"),
    ],
)
async def test_list_action_runs_invalid_input_before_token(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict, field: str
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(remote, "_server_gitea_client", side_effect=AssertionError("client looked up")):
        result = await remote.gitea_list_action_runs("owner", "repo", **kwargs)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == field
    if field == "status":
        assert result["error"]["details"]["allowed_values"] == [
            "completed", "in_progress", "running", "waiting"
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", [0, -1, True, False, "1", 1.5, None])
async def test_get_action_run_invalid_id_before_token(
    monkeypatch: pytest.MonkeyPatch, run_id: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(remote, "_server_gitea_client", side_effect=AssertionError("client looked up")):
        result = await remote.gitea_get_action_run("owner", "repo", run_id)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["details"]["field"] == "run_id"


@pytest.mark.asyncio
async def test_action_run_status_alias_preserved_for_valid_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.list_action_runs.return_value = {"workflow_runs": [], "total_count": 0}
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        result = await remote.gitea_list_action_runs(
            "owner", "repo", status="running", limit=10
        )

    assert result["ok"] is True
    client.list_action_runs.assert_awaited_once_with(
        "owner", "repo", status="in_progress", limit=10
    )


@pytest.mark.asyncio
async def test_get_action_run_valid_id_reaches_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.get_action_run.return_value = {"id": 42, "status": "completed"}
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        result = await remote.gitea_get_action_run("owner", "repo", 42)

    assert result["ok"] is True
    assert result["result"]["id"] == 42
    client.get_action_run.assert_awaited_once_with("owner", "repo", 42)


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", [0, -1, True, False, "1", 1.5, None])
async def test_list_action_run_jobs_invalid_run_id_before_token(
    monkeypatch: pytest.MonkeyPatch, run_id: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(remote, "_server_gitea_client", side_effect=AssertionError("client looked up")):
        result = await remote.gitea_list_action_run_jobs("owner", "repo", run_id)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == "run_id"


@pytest.mark.asyncio
async def test_list_action_run_jobs_valid_id_reaches_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "test-only-token")
    client = AsyncMock()
    client.list_action_run_jobs.return_value = {"jobs": [], "total_count": 0}
    with patch.object(remote, "_server_gitea_client") as factory:
        factory.return_value.return_value.__aenter__ = AsyncMock(return_value=client)
        result = await remote.gitea_list_action_run_jobs("owner", "repo", 42)

    assert result["ok"] is True
    client.list_action_run_jobs.assert_awaited_once_with("owner", "repo", 42)
