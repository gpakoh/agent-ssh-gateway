"""Tests for OpenCode runner MCP tool — opencode_tools module."""
from __future__ import annotations

import pytest

from examples.mcp_server.opencode_tools import project_run_opencode


def _fake_run_cmd(project: str, command: str) -> dict:
    return {"stdout": "", "stderr": "", "exit_code": 0}


class TestProjectRunOpencode:
    def test_invalid_task_id_raises(self):
        with pytest.raises(ValueError, match="Invalid task_id"):
            project_run_opencode(
                _fake_run_cmd,
                project="test",
                task_id="bad",
            )

    def test_accepted_task_id(self):
        result = project_run_opencode(
            _fake_run_cmd,
            project="test",
            task_id="2026-06-25-fix-auth-opencode",
        )
        assert "task_id" in result
        assert result["task_id"] == "2026-06-25-fix-auth-opencode"

    def test_returns_structured_result(self):
        result = project_run_opencode(
            _fake_run_cmd,
            project="test",
            task_id="2026-06-25-fix-auth-opencode",
        )
        for key in ("task_id", "status", "exit_code", "stdout", "stderr", "started_at", "finished_at"):
            assert key in result, f"missing key: {key}"
        assert result["status"] == "needs-review"

    def test_failed_run_returns_failed_status(self):
        def _failing_run_cmd(project: str, command: str) -> dict:
            return {"stdout": "", "stderr": "error", "exit_code": 1}
        result = project_run_opencode(
            _failing_run_cmd,
            project="test",
            task_id="2026-06-25-fix-auth-opencode",
        )
        assert result["status"] == "failed"


class TestToolRegistration:
    def test_registered_in_chatgpt_mode(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        import importlib
        import sys
        from pathlib import Path
        example_dir = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
        monkeypatch.syspath_prepend(str(example_dir))
        sys.modules.pop("tool_modes", None)
        tm = importlib.import_module("tool_modes")
        assert tm.should_register_tool("gateway_project_run_opencode") is True


class TestServerTool:
    def test_tool_function_can_be_imported(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        import importlib, sys
        from pathlib import Path
        example_dir = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
        monkeypatch.syspath_prepend(str(example_dir))
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "handoff")
        monkeypatch.setenv("GITEA_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        for name in list(sys.modules):
            if "mcp_server" in name or "tool_modes" in name or "opencode_tools" in name:
                sys.modules.pop(name, None)
        server = importlib.import_module("server")
        tool = getattr(server, "gateway_project_run_opencode", None)
        assert tool is not None, "gateway_project_run_opencode not found in server module"
