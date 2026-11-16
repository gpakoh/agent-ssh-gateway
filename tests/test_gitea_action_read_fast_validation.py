"""Action read-tool schema errors must fail before credentials or networking (#884)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from examples.mcp_server.mcp_infra.adapters import remote


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [0, -1, True, False, "1", 1.5, None])
async def test_job_log_invalid_id_is_typed_before_token_or_client(
    monkeypatch: pytest.MonkeyPatch, bad_id: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("client constructed")
    ):
        result = await remote.gitea_get_action_job_logs("owner", "repo", bad_id)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == "job_id"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_bytes", [0, -1, True, False, "10", 3.5, None, 262145])
async def test_job_log_invalid_byte_bound_is_typed_before_token_or_client(
    monkeypatch: pytest.MonkeyPatch, bad_bytes: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("client constructed")
    ):
        result = await remote.gitea_get_action_job_logs("owner", "repo", 12, max_bytes=bad_bytes)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == "max_bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_tail", [0, -1, True, False, "10", 3.5, None, 1000000])
async def test_job_log_invalid_tail_is_typed_before_token_or_client(
    monkeypatch: pytest.MonkeyPatch, bad_tail: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("client constructed")
    ):
        result = await remote.gitea_get_action_job_logs("owner", "repo", 12, tail_lines=bad_tail)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == "tail_lines"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_status", ["waiting", "completed", "", True, 4, ["pending"], {}])
async def test_job_list_invalid_status_is_typed_before_token_or_client(
    monkeypatch: pytest.MonkeyPatch, bad_status: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("client constructed")
    ):
        result = await remote.gitea_list_action_jobs("owner", "repo", status=bad_status)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == "status"
    assert "in_progress" in result["error"]["details"]["allowed_values"]
    assert "running" in result["error"]["details"]["allowed_values"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "bad_value"),
    [("page", 0), ("page", True), ("page", "3"), ("page", 1.5),
     ("limit", 0), ("limit", True), ("limit", "3"), ("limit", 1.5),
     ("limit", 51)],
)
async def test_job_list_invalid_pagination_is_typed_before_token_or_client(
    monkeypatch: pytest.MonkeyPatch, field: str, bad_value: object
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("client constructed")
    ):
        result = await remote.gitea_list_action_jobs("owner", "repo", **{field: bad_value})
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"]["field"] == field


@pytest.mark.asyncio
async def test_job_log_valid_input_without_credentials_is_dependency_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("client constructed")
    ):
        result = await remote.gitea_get_action_job_logs(
            "owner", "repo", 12, max_bytes=64, tail_lines=10
        )
    assert result["error"]["code"] == "DEPENDENCY_MISSING"


@pytest.mark.asyncio
async def test_job_list_valid_alias_without_credentials_is_dependency_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with patch.object(
        remote, "_server_gitea_client", side_effect=AssertionError("client constructed")
    ):
        result = await remote.gitea_list_action_jobs(
            "owner", "repo", status="running", page=2, limit=25
        )
    assert result["error"]["code"] == "DEPENDENCY_MISSING"
