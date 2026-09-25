"""Tests for Gitea tool list response normalization (same contract as GitHub tools)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"))

from fleet.gitea_client import (
    MAX_ACTION_JOB_LOG_BYTES,
    MAX_LIMIT,
    GiteaClient,
    _normalize_action_job_status_filter,
    _normalize_action_run_status_filter,
    minimize_action_job_payload,
    normalize_action_jobs_response,
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


@pytest.mark.asyncio
async def test_gitea_list_action_runs_rejects_bad_page_limit_before_http(monkeypatch):
    get = AsyncMock(return_value=None)
    monkeypatch.setattr(GiteaClient, "_get", get)
    client = GiteaClient("token")
    try:
        for kwargs in (
            {"page": 0},
            {"page": -1},
            {"page": True},
            {"page": "2"},
            {"page": 1.5},
            {"limit": 0},
            {"limit": -3},
            {"limit": MAX_LIMIT + 1},
            {"limit": True},
            {"limit": 3.5},
            {"limit": "3"},
        ):
            with pytest.raises(ValueError):
                await client.list_action_runs("owner", "repo", **kwargs)
    finally:
        await client.aclose()
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_gitea_list_action_runs_forwards_optional_page_only_when_provided(monkeypatch):
    captured = []

    async def fake_get(self, endpoint, params=None, **path_params):
        captured.append((endpoint, params, path_params))
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(GiteaClient, "_get", fake_get)
    client = GiteaClient("token")
    try:
        await client.list_action_runs("owner", "repo")
        await client.list_action_runs(
            "owner",
            "repo",
            status="running",
            limit=25,
            page=3,
        )
    finally:
        await client.aclose()

    assert captured == [
        (
            "/repos/{owner}/{repo}/actions/runs",
            {"limit": 10},
            {"owner": "owner", "repo": "repo"},
        ),
        (
            "/repos/{owner}/{repo}/actions/runs",
            {"limit": 25, "page": 3, "status": "in_progress"},
            {"owner": "owner", "repo": "repo"},
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_response",
    [
        None,
        [],
        [{"email": "secret@example.com"}],
        {"total_count": 0},
        {"total_count": 0, "workflow_runs": None},
        {"total_count": 0, "workflow_runs": {}},
        {"total_count": 1, "workflow_runs": [None]},
    ],
)
async def test_gitea_list_action_runs_rejects_malformed_response_shape(monkeypatch, raw_response):
    get = AsyncMock(return_value=raw_response)
    monkeypatch.setattr(GiteaClient, "_get", get)
    client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="workflow_runs list") as exc_info:
            await client.list_action_runs("owner", "repo", page=1)
    finally:
        await client.aclose()

    assert "secret" not in str(exc_info.value).lower()
    get.assert_awaited_once()


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


# Repo-wide Actions job inventory (CI-002)

_JOB_OUTPUT_KEYS = (
    "id",
    "run_id",
    "run_attempt",
    "head_branch",
    "head_sha",
    "name",
    "status",
    "conclusion",
    "runner_id",
    "runner_name",
    "started_at",
    "completed_at",
    "url",
    "run_url",
)


def _raw_gitea_action_job_payload() -> dict[str, object]:
    return {
        "id": 10,
        "run_id": 5,
        "run_attempt": 1,
        "head_branch": "main",
        "head_sha": "0" * 40,
        "name": "lint",
        "status": "in_progress",
        "conclusion": None,
        "runner_id": 7,
        "runner_name": "runner-7",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": None,
        "url": "https://git.example/gpakoh/web-ssh-gateway/actions/jobs/10",
        "run_url": "https://git.example/gpakoh/web-ssh-gateway/actions/runs/5",
    }


def test_gitea_action_job_status_filter_accepts_exact_statuses():
    assert _normalize_action_job_status_filter(None) is None
    for status in ("pending", "queued", "in_progress", "failure", "success", "skipped"):
        assert _normalize_action_job_status_filter(status) == status
    assert _normalize_action_job_status_filter("running") == "in_progress"


def test_gitea_action_job_status_filter_rejects_unknown_before_http():
    for status in ("waiting", "completed", "", "in progress"):
        with pytest.raises(ValueError, match="status must be one of"):
            _normalize_action_job_status_filter(status)


def test_gitea_action_job_status_filter_rejects_non_string():
    for status in (123, True, ["pending"], {"status": "pending"}, 3.5):
        with pytest.raises(ValueError, match="status must be a string"):
            _normalize_action_job_status_filter(status)


@pytest.mark.asyncio
async def test_gitea_list_action_jobs_rejects_bad_status_before_http(monkeypatch):
    get = AsyncMock(return_value=None)
    monkeypatch.setattr(GiteaClient, "_get", get)
    client = GiteaClient("token")
    try:
        with pytest.raises(ValueError, match="status must be a string"):
            await client.list_action_jobs("owner", "repo", status=123)
        with pytest.raises(ValueError, match="status must be one of"):
            await client.list_action_jobs("owner", "repo", status="waiting")
    finally:
        await client.aclose()
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_gitea_list_action_jobs_rejects_bad_page_limit_before_http(monkeypatch):
    get = AsyncMock(return_value=None)
    monkeypatch.setattr(GiteaClient, "_get", get)
    client = GiteaClient("token")
    try:
        for kwargs in (
            {"page": 0},
            {"page": -1},
            {"page": True},
            {"page": "2"},
            {"page": 1.5},
            {"limit": 0},
            {"limit": -3},
            {"limit": 51},
            {"limit": True},
            {"limit": 3.5},
        ):
            with pytest.raises(ValueError):
                await client.list_action_jobs("owner", "repo", **kwargs)
    finally:
        await client.aclose()
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_gitea_list_action_jobs_hits_repo_wide_endpoint_and_normalizes(monkeypatch):
    captured = {}

    async def fake_get(self, endpoint, params=None, **path_params):
        captured["endpoint"] = endpoint
        captured["params"] = params
        captured["path_params"] = path_params
        return {"total_count": 2, "jobs": [_raw_gitea_action_job_payload()]}

    monkeypatch.setattr(GiteaClient, "_get", fake_get)

    client = GiteaClient("token")
    try:
        out = await client.list_action_jobs("owner", "repo")
    finally:
        await client.aclose()

    assert captured["endpoint"] == "/repos/{owner}/{repo}/actions/jobs"
    assert captured["params"] == {"page": 1, "limit": 50}
    assert captured["path_params"] == {"owner": "owner", "repo": "repo"}
    assert out == {
        "total_count": 2,
        "jobs": [_raw_gitea_action_job_payload()],
    }  # payload mirrors raw since it is already minimal-shaped
    assert list(out["jobs"][0].keys()) == list(_JOB_OUTPUT_KEYS)


@pytest.mark.asyncio
async def test_gitea_list_action_jobs_passes_status_page_limit(monkeypatch):
    captured = {}

    async def fake_get(self, endpoint, params=None, **path_params):
        captured["params"] = params
        return {"total_count": 0, "jobs": []}

    monkeypatch.setattr(GiteaClient, "_get", fake_get)

    client = GiteaClient("token")
    try:
        out = await client.list_action_jobs(
            "owner", "repo", status="running", page=2, limit=25
        )
    finally:
        await client.aclose()

    assert captured["params"] == {"page": 2, "limit": 25, "status": "in_progress"}
    assert out == {"total_count": 0, "jobs": []}


@pytest.mark.asyncio
async def test_gitea_list_action_jobs_minimizes_to_exact_fields(monkeypatch):
    async def fake_get(self, endpoint, params=None, **path_params):
        return {
            "total_count": 1,
            "jobs": [
                {
                    "id": 10,
                    "run_id": 5,
                    "run_attempt": 1,
                    "head_branch": "main",
                    "head_sha": "0" * 40,
                    "name": "lint",
                    "status": "in_progress",
                    "conclusion": None,
                    "runner_id": 7,
                    "runner_name": "runner-7",
                    "started_at": "2026-01-01T00:00:00Z",
                    "completed_at": None,
                    "url": "https://git.example/jobs/10",
                    "run_url": "https://git.example/runs/5",
                    "steps": [{"name": "checkout"}],
                    "repository": {"name": "web-ssh-gateway"},
                }
            ],
        }

    monkeypatch.setattr(GiteaClient, "_get", fake_get)

    client = GiteaClient("token")
    try:
        out = await client.list_action_jobs("owner", "repo")
    finally:
        await client.aclose()

    job = out["jobs"][0]
    assert list(job.keys()) == list(_JOB_OUTPUT_KEYS)
    assert job["id"] == 10
    assert job["run_id"] == 5
    assert job["run_attempt"] == 1
    assert job["runner_id"] == 7
    assert job["conclusion"] is None
    assert job["completed_at"] is None
    assert "steps" not in job
    assert "repository" not in job


@pytest.mark.asyncio
async def test_gitea_list_action_jobs_response_shape_fails_closed(monkeypatch):
    bad_responses = [
        None,
        [],
        {"jobs": []},
        {"total_count": None, "jobs": []},
        {"total_count": "2", "jobs": []},
        {"total_count": -1, "jobs": []},
        {"total_count": True, "jobs": []},
        {"total_count": 0, "jobs": {}},
        {"total_count": 0, "jobs": None},
        {"total_count": 0, "jobs": "not-a-list"},
    ]
    for bad in bad_responses:
        async def fake_get(self, endpoint, params=None, bad=bad, **path_params):
            return bad

        monkeypatch.setattr(GiteaClient, "_get", fake_get)
        client = GiteaClient("token")
        try:
            with pytest.raises(ValueError):
                await client.list_action_jobs("owner", "repo")
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_gitea_list_action_jobs_malformed_job_fields_fail_closed(monkeypatch):
    bad_mutations = [
        ("id", True),
        ("id", 1.5),
        ("id", "5"),
        ("id", None),
        ("run_id", 0),
        ("run_id", True),
        ("run_id", "5"),
        ("run_attempt", 0),
        ("run_attempt", -1),
        ("run_attempt", True),
        ("run_attempt", 1.0),
        ("head_branch", 123),
        ("head_branch", ["main"]),
        ("name", True),
        ("name", {"name": "x"}),
        ("status", ["in_progress"]),
        ("conclusion", {"x": 1}),
        ("runner_id", -1),
        ("runner_id", True),
        ("runner_id", "7"),
        ("runner_name", 7),
        ("started_at", 1),
        ("completed_at", []),
        ("url", {1: 2}),
        ("run_url", 0),
    ]
    for key, bad in bad_mutations:
        payload = dict(_raw_gitea_action_job_payload())
        payload[key] = bad
        with pytest.raises(ValueError):
            minimize_action_job_payload(payload)


def test_gitea_list_action_jobs_run_attempt_positive_succeeds():
    payload = dict(_raw_gitea_action_job_payload())
    payload["run_attempt"] = 1
    assert minimize_action_job_payload(payload)["run_attempt"] == 1


@pytest.mark.asyncio
async def test_gitea_list_action_jobs_head_sha_validated_without_coercion(monkeypatch):
    for bad in ("", "A" * 40, "0" * 39, "abc123", 123, True, ["0" * 40], b"0" * 40):
        payload = dict(_raw_gitea_action_job_payload())
        payload["head_sha"] = bad
        with pytest.raises(ValueError):
            minimize_action_job_payload(payload)

    for good in (None,):
        payload = dict(_raw_gitea_action_job_payload())
        payload["head_sha"] = good
        assert minimize_action_job_payload(payload)["head_sha"] is None

    payload = dict(_raw_gitea_action_job_payload())
    payload.pop("head_sha")
    assert minimize_action_job_payload(payload)["head_sha"] is None


def test_gitea_list_action_jobs_allows_absent_optional_scalars():
    payload = dict(_raw_gitea_action_job_payload())
    for key in (
        "head_branch",
        "name",
        "status",
        "conclusion",
        "runner_name",
        "started_at",
        "completed_at",
        "url",
        "run_url",
        "head_sha",
        "runner_id",
    ):
        payload.pop(key, None)
    job = minimize_action_job_payload(payload)
    assert list(job.keys()) == list(_JOB_OUTPUT_KEYS)
    for key in _JOB_OUTPUT_KEYS:
        if key in ("id", "run_id", "run_attempt"):
            continue
        assert job[key] is None, key
    assert job["id"] == 10 and job["run_id"] == 5 and job["run_attempt"] == 1


def test_gitea_normalize_action_jobs_response_requires_object_shape():
    assert normalize_action_jobs_response({"total_count": 0, "jobs": []}) == {
        "total_count": 0,
        "jobs": [],
    }
    with pytest.raises(ValueError):
        normalize_action_jobs_response([])
    with pytest.raises(ValueError):
        normalize_action_jobs_response({"total_count": "0", "jobs": []})
