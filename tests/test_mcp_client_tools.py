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
    # Keep the pre-existing module object around so monkeypatch can restore
    # it at teardown; a bare pop() would leave the reloaded copy in
    # sys.modules and break import identity for later tests.
    old = sys.modules.get(module_name)
    monkeypatch.setitem(sys.modules, module_name, old)
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


class TestWorkingDirectoryNoHostPaths:
    """T2.3: working_directory must not return absolute host paths."""

    def test_returns_project_relative_dot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = import_example_module(monkeypatch, "mcp_client_tools")
        result = mod.working_directory(_FakeClient(), "web-ssh-gateway")
        assert result["stdout"] == "."
        assert result["exit_code"] == 0
        assert result["outcome"] == "passed"

    def test_never_shells_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = import_example_module(monkeypatch, "mcp_client_tools")
        called: list[str] = []

        class _Probe:
            def execute_project_command(self, project: str, command: str) -> dict:
                called.append(command)
                return {"exit_code": 0, "stdout": "/media/1TB/Python/web-ssh-gateway\n"}

        result = mod.working_directory(_Probe(), "web-ssh-gateway")
        assert result["stdout"] == "."
        assert called == []
