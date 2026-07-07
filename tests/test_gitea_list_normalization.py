"""Tests for Gitea tool list response normalization (same contract as GitHub tools)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "chatgpt_remote_mcp"))

from fleet.shared import normalize_list_response


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


def test_gitea_single_issue_preserved():
    result = normalize_list_response({"number": 1, "title": "Bug fix"})
    assert result["number"] == 1
    assert "items" not in result


def test_gitea_empty_list():
    result = normalize_list_response([])
    assert result == {"items": [], "count": 0}
