"""Tests for ChatGPT-safe MCP tool profile."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from examples.mcp_server.tool_modes import TOOL_NAMES_BY_MODE, should_register_tool

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"


def import_example_module(monkeypatch: pytest.MonkeyPatch, module_name: str):
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


class TestChatgptModeVisibility:
    def test_excludes_generic_execute(self):
        assert not should_register_tool("execute_restricted", "mcp_client")

    def test_includes_health(self):
        assert should_register_tool("health", "mcp_client")

    def test_includes_session_health(self):
        assert should_register_tool("session_health", "mcp_client")

    def test_includes_git_status(self):
        assert should_register_tool("git_status", "mcp_client")

    def test_includes_recent_commits(self):
        assert should_register_tool("recent_commits", "mcp_client")

    def test_includes_git_diff_stat(self):
        assert should_register_tool("git_diff_stat", "mcp_client")

    def test_includes_show_changes(self):
        assert should_register_tool("show_changes", "mcp_client")

    def test_includes_run_tests(self):
        assert should_register_tool("run_tests", "mcp_client")

    def test_includes_run_lint(self):
        assert should_register_tool("run_lint", "mcp_client")

    def test_includes_run_compileall(self):
        assert should_register_tool("run_compileall", "mcp_client")

    def test_includes_working_directory(self):
        assert should_register_tool("working_directory", "mcp_client")

    def test_includes_handoff_tools(self):
        assert should_register_tool("read_handoff", "mcp_client")
        assert should_register_tool("write_handoff_plan", "mcp_client")
        assert should_register_tool("show_handoff_status", "mcp_client")

    def test_includes_jobs(self):
        assert should_register_tool("job_status", "mcp_client")
        assert should_register_tool("job_result", "mcp_client")
        assert should_register_tool("wait_job", "mcp_client")

    def test_includes_read_file(self):
        assert should_register_tool("read_file", "mcp_client")

    def test_includes_repo_status(self):
        assert should_register_tool("repo_status", "mcp_client")

    def test_excludes_list_sessions(self):
        assert not should_register_tool("list_sessions", "mcp_client")

    def test_excludes_self_test(self):
        assert not should_register_tool("self_test", "mcp_client")

    def test_mcp_client_is_known_mode(self):
        assert "mcp_client" in TOOL_NAMES_BY_MODE


class TestChatgptToolsModule:
    def test_working_directory_uses_fixed_command(self, monkeypatch: pytest.MonkeyPatch):
        mcp_client_tools = import_example_module(monkeypatch, "mcp_client_tools")
        client = _FakeClient()
        result = mcp_client_tools.working_directory(client)
        assert result["exit_code"] == 0
        assert client.commands == ["pwd"]

    def test_git_status_uses_fixed_command(self, monkeypatch: pytest.MonkeyPatch):
        mcp_client_tools = import_example_module(monkeypatch, "mcp_client_tools")
        client = _FakeClient()
        result = mcp_client_tools.git_status(client)
        assert result["exit_code"] == 0
        assert client.commands == ["git status --short"]

    def test_run_tests_uses_fixed_command(self, monkeypatch: pytest.MonkeyPatch):
        mcp_client_tools = import_example_module(monkeypatch, "mcp_client_tools")
        client = _FakeClient()
        result = mcp_client_tools.run_tests(client)
        assert result["exit_code"] == 0
        assert client.commands == ["pytest -q"]

    def test_run_lint_uses_fixed_command(self, monkeypatch: pytest.MonkeyPatch):
        mcp_client_tools = import_example_module(monkeypatch, "mcp_client_tools")
        client = _FakeClient()
        result = mcp_client_tools.run_lint(client)
        assert result["exit_code"] == 0
        assert client.commands == ["ruff check app tests examples"]

    def test_run_compileall_uses_fixed_command(self, monkeypatch: pytest.MonkeyPatch):
        mcp_client_tools = import_example_module(monkeypatch, "mcp_client_tools")
        client = _FakeClient()
        result = mcp_client_tools.run_compileall(client)
        assert result["exit_code"] == 0
        assert client.commands == ["python -m compileall app tests examples"]

    def test_recent_commits_uses_fixed_command(self, monkeypatch: pytest.MonkeyPatch):
        mcp_client_tools = import_example_module(monkeypatch, "mcp_client_tools")
        client = _FakeClient()
        result = mcp_client_tools.recent_commits(client)
        assert result["exit_code"] == 0
        assert client.commands == ["git log --oneline -10"]

    def test_git_diff_stat_uses_fixed_command(self, monkeypatch: pytest.MonkeyPatch):
        mcp_client_tools = import_example_module(monkeypatch, "mcp_client_tools")
        client = _FakeClient()
        result = mcp_client_tools.git_diff_stat(client)
        assert result["exit_code"] == 0
        assert client.commands == ["git diff --stat"]

    def test_show_changes_runs_two_commands(self, monkeypatch: pytest.MonkeyPatch):
        mcp_client_tools = import_example_module(monkeypatch, "mcp_client_tools")
        monkeypatch.setattr(mcp_client_tools, "_is_git_repo", lambda client, session_id=None: True)
        client = _FakeClient()
        result = mcp_client_tools.show_changes(client)
        assert "git_status" in result
        assert "git_diff_stat" in result
        assert client.commands == [
            "git status --short",
            "git diff --stat",
        ]

    def test_show_changes_not_git_repo_raises(self, monkeypatch: pytest.MonkeyPatch):
        mcp_client_tools = import_example_module(monkeypatch, "mcp_client_tools")
        monkeypatch.setattr(mcp_client_tools, "_is_git_repo", lambda client, session_id=None: False)
        client = _FakeClient()
        with pytest.raises(ValueError, match="not a git repository"):
            mcp_client_tools.show_changes(client)

    def test_show_changes_with_project(self, monkeypatch: pytest.MonkeyPatch):
        mcp_client_tools = import_example_module(monkeypatch, "mcp_client_tools")
        called = []
        monkeypatch.setattr(
            mcp_client_tools, "project_show_changes", lambda client, project: called.append(project) or {}
        )
        client = _FakeClient()
        result = mcp_client_tools.show_changes(client, project="my-project")
        assert called == ["my-project"]
        assert result == {}


class _FakeClient:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def execute_restricted(self, command: str, session_id: str | None = None) -> dict:
        self.commands.append(command)
        return {"job_id": f"job-{len(self.commands)}"}

    def wait_job(self, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
        }
