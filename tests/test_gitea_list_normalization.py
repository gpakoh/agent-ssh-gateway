"""Tests for Gitea tool list response normalization (same contract as GitHub tools)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"))

from fleet.shared import minimize_action_run_payload, normalize_list_response


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


def test_gitea_action_run_payload_minimized():
    """Regression: list_action_runs returned raw workflow runs embedding
    full user objects (email, is_admin, last_login) under actor and
    trigger_actor, plus a ~50-field repository object — a PII/context
    flood. minimize_action_run_payload keeps only triage fields."""
    run = {
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
    out = minimize_action_run_payload(run)
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
