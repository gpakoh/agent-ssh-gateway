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
        assert not should_register_tool(
            "gateway_execute_restricted", "chatgpt"
        )

    def test_includes_health(self):
        assert should_register_tool("gateway_health", "chatgpt")

    def test_includes_session_health(self):
        assert should_register_tool("gateway_session_health", "chatgpt")

    def test_includes_git_status(self):
        assert should_register_tool("gateway_git_status", "chatgpt")

    def test_includes_recent_commits(self):
        assert should_register_tool("gateway_recent_commits", "chatgpt")

    def test_includes_git_diff_stat(self):
        assert should_register_tool("gateway_git_diff_stat", "chatgpt")

    def test_includes_show_changes(self):
        assert should_register_tool("gateway_show_changes", "chatgpt")

    def test_includes_run_tests(self):
        assert should_register_tool("gateway_run_tests", "chatgpt")

    def test_includes_run_lint(self):
        assert should_register_tool("gateway_run_lint", "chatgpt")

    def test_includes_run_compileall(self):
        assert should_register_tool("gateway_run_compileall", "chatgpt")

    def test_includes_working_directory(self):
        assert should_register_tool("gateway_working_directory", "chatgpt")

    def test_includes_handoff_tools(self):
        assert should_register_tool("gateway_read_handoff", "chatgpt")
        assert should_register_tool("gateway_write_handoff_plan", "chatgpt")
        assert should_register_tool("gateway_show_handoff_status", "chatgpt")

    def test_includes_jobs(self):
        assert should_register_tool("gateway_job_status", "chatgpt")
        assert should_register_tool("gateway_job_result", "chatgpt")
        assert should_register_tool("gateway_wait_job", "chatgpt")

    def test_includes_read_file(self):
        assert should_register_tool("gateway_read_file", "chatgpt")

    def test_includes_repo_status(self):
        assert should_register_tool("gateway_repo_status", "chatgpt")

    def test_excludes_list_sessions(self):
        assert not should_register_tool("gateway_list_sessions", "chatgpt")

    def test_excludes_self_test(self):
        assert not should_register_tool("gateway_self_test", "chatgpt")

    def test_chatgpt_is_known_mode(self):
        assert "chatgpt" in TOOL_NAMES_BY_MODE


class TestChatgptToolsModule:
    def test_working_directory_uses_fixed_command(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        chatgpt_tools = import_example_module(monkeypatch, "chatgpt_tools")
        client = _FakeClient()
        result = chatgpt_tools.working_directory(client)
        assert result["exit_code"] == 0
        assert client.commands == ["pwd"]

    def test_git_status_uses_fixed_command(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        chatgpt_tools = import_example_module(monkeypatch, "chatgpt_tools")
        client = _FakeClient()
        result = chatgpt_tools.git_status(client)
        assert result["exit_code"] == 0
        assert client.commands == ["git status --short"]

    def test_run_tests_uses_fixed_command(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        chatgpt_tools = import_example_module(monkeypatch, "chatgpt_tools")
        client = _FakeClient()
        result = chatgpt_tools.run_tests(client)
        assert result["exit_code"] == 0
        assert client.commands == ["pytest -q"]

    def test_run_lint_uses_fixed_command(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        chatgpt_tools = import_example_module(monkeypatch, "chatgpt_tools")
        client = _FakeClient()
        result = chatgpt_tools.run_lint(client)
        assert result["exit_code"] == 0
        assert client.commands == ["ruff check app tests examples"]

    def test_run_compileall_uses_fixed_command(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        chatgpt_tools = import_example_module(monkeypatch, "chatgpt_tools")
        client = _FakeClient()
        result = chatgpt_tools.run_compileall(client)
        assert result["exit_code"] == 0
        assert client.commands == [
            "python -m compileall app tests examples"
        ]

    def test_recent_commits_uses_fixed_command(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        chatgpt_tools = import_example_module(monkeypatch, "chatgpt_tools")
        client = _FakeClient()
        result = chatgpt_tools.recent_commits(client)
        assert result["exit_code"] == 0
        assert client.commands == ["git log --oneline -10"]

    def test_git_diff_stat_uses_fixed_command(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        chatgpt_tools = import_example_module(monkeypatch, "chatgpt_tools")
        client = _FakeClient()
        result = chatgpt_tools.git_diff_stat(client)
        assert result["exit_code"] == 0
        assert client.commands == ["git diff --stat"]

    def test_show_changes_runs_two_commands(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        chatgpt_tools = import_example_module(monkeypatch, "chatgpt_tools")
        client = _FakeClient()
        result = chatgpt_tools.show_changes(client)
        assert "git_status" in result
        assert "git_diff_stat" in result
        assert client.commands == [
            "git status --short",
            "git diff --stat",
        ]


class _FakeClient:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def execute_restricted(
        self, command: str, session_id: str | None = None
    ) -> dict:
        self.commands.append(command)
        return {"job_id": f"job-{len(self.commands)}"}

    def wait_job(self, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
        }
