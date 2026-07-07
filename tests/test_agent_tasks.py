"""Tests for Agent Handoff v2 — agent_tasks module."""

from __future__ import annotations

import json

import pytest

from examples.mcp_server.agent_tasks import (
    archive_agent_task,
    build_current_plan,
    build_initial_status,
    build_task_json,
    list_agent_tasks,
    read_agent_task_file,
    validate_task_id,
)


class TestValidateTaskId:
    def test_valid_ids(self):
        for tid in [
            "2026-06-24-stage-12-15a-rag-search-chunks-opencode",
            "a12345678901",
            "fix-test-flake-auth-mimo",
        ]:
            validate_task_id(tid)

    def test_invalid_ids(self):
        for tid in ["", "too-short", "UPPERCASE", "has spaces", "\xe4", None]:
            with pytest.raises((ValueError, TypeError)):
                validate_task_id(tid)  # type: ignore[arg-type]


class TestBuildTaskJson:
    def test_minimal(self):
        result = build_task_json(task_id="a12345678901", agent="opencode")
        data = json.loads(result)
        assert data["task_id"] == "a12345678901"
        assert data["agent"] == "opencode"
        assert data["allowed_files"] == []
        assert data["commit_allowed"] is False
        assert "created" in data

    def test_full(self):
        result = build_task_json(
            task_id="b23456789012",
            agent="mimo",
            allowed_files=["src/**", "tests/**"],
            forbidden_files=["migrations/**"],
            required_checks=["pytest -q", "ruff check"],
            worktree_path="../agent-worktrees/task-b",
            commit_allowed=False,
            push_allowed=False,
        )
        data = json.loads(result)
        assert data["agent"] == "mimo"
        assert "src/**" in data["allowed_files"]
        assert data["required_checks"] == ["pytest -q", "ruff check"]


class TestBuildInitialStatus:
    def test_created_status(self):
        result = build_initial_status(agent="opencode", task_id="a12345678901")
        assert "Status: created" in result
        assert "opencode" in result
        assert "a12345678901" in result

    def test_different_agent(self):
        result = build_initial_status(agent="mimo", task_id="b23456789012")
        assert "Status: created" in result
        assert "mimo" in result


class TestBuildCurrentPlan:
    def test_minimal(self):
        result = build_current_plan(task_id="c34567890123", task="Fix tests")
        assert "# Fix tests" in result
        assert "c34567890123" in result
        assert "implementation-diff.patch" in result
        assert "Do not commit or push" in result

    def test_full(self):
        result = build_current_plan(
            task_id="d45678901234",
            task="Add search chunks",
            scope="UI only",
            allowed_files=["father-ui/src/**"],
            forbidden_files=["app/**"],
            required_checks=["pytest -q"],
            acceptance_criteria=["Build passes", "Tests pass"],
            commit_message="polish: improve RAG search",
            constraints="No model changes",
        )
        assert "## Scope" in result
        assert "father-ui/src/**" in result
        assert "app/**" in result
        assert "polish: improve RAG search" in result
        assert "No model changes" in result


class TestReadAgentTaskFile:
    def test_returns_callable_result(self):
        """Verify read_agent_task_file passes args to run_cmd correctly."""
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "file content", "stderr": "", "exit_code": 0}

        result = read_agent_task_file(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            filename="agent-status.md",
        )
        assert result["stdout"] == "file content"
        assert len(calls) == 1
        assert calls[0][0] == "my-proj"
        assert "a12345678901/agent-status.md" in calls[0][1]


class TestListAgentTasks:
    def test_passes_project(self):
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "## Tasks\ntask-1\ntask-2", "stderr": "", "exit_code": 0}

        list_agent_tasks(fake_run_cmd, project="my-proj")
        assert calls[0][0] == "my-proj"
        assert ".ai-bridge/tasks/" in calls[0][1]


class TestArchiveAgentTask:
    def test_passes_project_and_task_id(self):
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "archived a12345678901", "stderr": "", "exit_code": 0}

        result = archive_agent_task(fake_run_cmd, project="my-proj", task_id="a12345678901")
        assert result["stdout"] == "archived a12345678901"
        assert ".ai-bridge/archive/" in calls[0][1]
        assert "mv" in calls[0][1]

    def test_invalid_task_id_raises(self):
        with pytest.raises(ValueError):
            archive_agent_task(lambda p, c: {}, project="p", task_id="bad")
