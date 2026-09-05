"""Tests for Gitea tool list response normalization (same contract as GitHub tools)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"))

from fleet.gitea_client import (
    MAX_ACTION_JOB_LOG_BYTES,
    GiteaClient,
    _normalize_action_run_status_filter,
)
from fleet.shared import minimize_action_run_payload, normalize_list_response


def test_gitea_action_run_status_filter_normalizes_running_alias():
    assert _normalize_action_run_status_filter(None) is None
    assert _normalize_action_run_status_filter("completed") == "completed"
    assert _normalize_action_run_status_filter("waiting") == "waiting"
    assert _normalize_action_run_status_filter("in_progress") == "in_progress"
    assert _normalize_action_run_status_filter("running") == "in_progress"


def test_gitea_action_run_status_filter_rejects_unknown_status_before_http():
    with pytest.raises(ValueError, match="status must be one of"):
        _normalize_action_run_status_filter("queued")


def test_gitea_branches_normalized():
    result = normalize_list_response([{"name": "main"}, {"name": "dev"}])
    assert result == {"items": [{"name": "main"}, {"name": "dev"}], "count": 2}


def test_gitea_commits_normalized():
    data = [{"sha": "abc123", "message": "fix bug"}, {"sha": "def456", "message": "add feature"}]
    result = normalize_list_response(data)
    assert result["count"] == 2
    assert result["items"][0]["sha"] == "abc123"


def test_gitea_issues_normalized():
    data = [{"number": 1, "title": "Bug fix"}, {"number": 2, "title": "Feature request"}]
    result = normalize_list_response(data)
    assert result["count"] == 2
    assert result["items"][1]["number"] == 2


def test_gitea_pull_requests_normalized():
    data = [{"number": 42, "title": "Fix reconnect"}]
    result = normalize_list_response(data)
    assert result["count"] == 1
    assert result["items"][0]["number"] == 42


def test_gitea_action_runs_preserved():
    result = normalize_list_response({"total_count": 5, "workflow_runs": []})
    assert result["total_count"] == 5
    assert "workflow_runs" in result


def _raw_gitea_action_run_payload() -> dict[str, object]:
    return {
        "id": 123,
        "run_number": 45,
        "run_attempt": 1,
        "display_title": "CI",
        "event": "push",
        "status": "success",
        "conclusion": "success",
        "head_branch": "main",
        "head_sha": "abc123",
        "actor": {
            "id": 1,
            "login": "gpakoh",
            "email": "gpakoh@example.com",
            "is_admin": True,
            "last_login": "2026-01-01T00:00:00Z",
        },
        "trigger_actor": {
            "id": 1,
            "login": "gpakoh",
            "email": "gpakoh@example.com",
            "is_admin": True,
        },
        "repository": {
            "id": 9,
            "name": "web-ssh-gateway",
            "full_name": "gpakoh/web-ssh-gateway",
            "clone_url": "https://git.example/gpakoh/web-ssh-gateway.git",
            "topics": ["python"],
        },
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:01:00Z",
        "html_url": "https://git.example/gpakoh/web-ssh-gateway/actions/runs/123",
    }


def test_gitea_action_run_payload_minimized():
    """Regression: list_action_runs returned raw workflow runs embedding
    full user objects (email, is_admin, last_login) under actor and
    trigger_actor, plus a ~50-field repository object — a PII/context
    flood. minimize_action_run_payload keeps only triage fields."""
    out = minimize_action_run_payload(_raw_gitea_action_run_payload())
    assert out["id"] == 123
    assert out["actor"] == {"login": "gpakoh"}
    assert out["trigger_actor"] == {"login": "gpakoh"}
    assert out["repository"] == {
        "name": "web-ssh-gateway",
        "full_name": "gpakoh/web-ssh-gateway",
    }
    assert "email" not in str(out)
    assert "is_admin" not in str(out)
    assert "last_login" not in str(out)
    assert "clone_url" not in str(out)
    assert "topics" not in str(out)


def test_gitea_single_issue_preserved():
    result = normalize_list_response({"number": 1, "title": "Bug fix"})
    assert result["number"] == 1
    assert "items" not in result


def test_gitea_empty_list():
    result = normalize_list_response([])
    assert result == {"items": [], "count": 0}


@pytest.mark.asyncio
async def test_gitea_get_action_run_uses_minimized_payload(monkeypatch):
    from fleet.gitea_client import GiteaClient

    async def fake_get(self, endpoint, params=None, **path_params):
        assert endpoint == "/repos/{owner}/{repo}/actions/runs/{run_id}"
        assert path_params == {"owner": "owner", "repo": "repo", "run_id": 123}
        return _raw_gitea_action_run_payload()

    monkeypatch.setattr(GiteaClient, "_get", fake_get)

    client = GiteaClient("token")
    try:
        out = await client.get_action_run("owner", "repo", 123)
    finally:
        await client.aclose()

    assert out["id"] == 123
    assert out["actor"] == {"login": "gpakoh"}
    assert out["trigger_actor"] == {"login": "gpakoh"}
    assert out["repository"] == {
        "name": "web-ssh-gateway",
        "full_name": "gpakoh/web-ssh-gateway",
    }
    serialized = str(out)
    assert "email" not in serialized
    assert "is_admin" not in serialized
    assert "last_login" not in serialized
    assert "clone_url" not in serialized
    assert "topics" not in serialized


@pytest.mark.asyncio
async def test_gitea_get_action_job_logs_fetches_text_tail_and_redacts(monkeypatch):
    async def fake_get_text(self, endpoint, params=None, **path_params):
        assert endpoint == "/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
        assert params is None
        assert path_params == {"owner": "owner", "repo": "repo", "job_id": 456}
        return "first line\nSECRET_TOKEN=abc123\nAuthorization: Bearer raw-token\nfinal failure\n"

    monkeypatch.setattr(GiteaClient, "_get_text", fake_get_text)

    client = GiteaClient("token")
    try:
        out = await client.get_action_job_logs("owner", "repo", 456, max_bytes=200)
    finally:
        await client.aclose()

    assert out["job_id"] == 456
    assert out["truncated"] is False
    assert out["redacted"] is True
    assert "final failure" in out["logs"]
    assert "abc123" not in out["logs"]
    assert "raw-token" not in out["logs"]
    assert "SECRET_TOKEN=<redacted>" in out["logs"]
    assert "Authorization: <redacted>" in out["logs"]


@pytest.mark.asyncio
async def test_gitea_get_action_job_logs_returns_bounded_tail(monkeypatch):
    async def fake_get_text(self, endpoint, params=None, **path_params):
        return "prefix\n" + "x" * 100 + "TAIL"

    monkeypatch.setattr(GiteaClient, "_get_text", fake_get_text)

    client = GiteaClient("token")
    try:
        out = await client.get_action_job_logs("owner", "repo", 456, max_bytes=8)
    finally:
        await client.aclose()

    assert out["truncated"] is True
    assert out["truncation"] == "tail"
    assert out["logs"] == "xxxxTAIL"
    assert out["bytes_returned"] == 8
    assert out["bytes_total_after_redaction"] > out["bytes_returned"]


@pytest.mark.asyncio
async def test_gitea_get_action_job_logs_rejects_bad_bounds_before_http(monkeypatch):
    get_text = AsyncMock(return_value="never called")
    monkeypatch.setattr(GiteaClient, "_get_text", get_text)

    client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="job_id must be a positive integer"):
            await client.get_action_job_logs("owner", "repo", 0)
        with pytest.raises(ValueError, match="max_bytes must be <="):
            await client.get_action_job_logs(
                "owner",
                "repo",
                456,
                max_bytes=MAX_ACTION_JOB_LOG_BYTES + 1,
            )
    finally:
        await client.aclose()

    get_text.assert_not_awaited()
