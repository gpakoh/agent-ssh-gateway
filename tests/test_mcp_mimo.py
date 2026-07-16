"""Tests for Mimo runner MCP tool — mimo_tools module.

CRITICAL: project_run_mimo is hard-blocked.
--dangerously-skip-permissions is never allowed.
"""

from __future__ import annotations

import importlib.util
from unittest.mock import MagicMock

import pytest

from examples.mcp_server.mimo_tools import project_run_mimo

TASK_ID = "2026-06-25-mimo-task-opencode"


def _fake_run_cmd(project: str, command: str) -> dict:
    return {"stdout": command, "stderr": "", "exit_code": 0}


# ---------------------------------------------------------------------------
# Hard block tests — project_run_mimo cannot execute silently
# ---------------------------------------------------------------------------


class TestProjectRunMimoBlocked:
    """project_run_mimo is hard-blocked — --dangerously-skip-permissions not allowed."""

    def test_raises_command_policy_error(self):
        """Must raise CommandPolicyError, never execute."""
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_mimo(
                _fake_run_cmd,
                project="test",
                task_id=TASK_ID,
            )

    def test_run_cmd_never_called(self):
        """The run_cmd callable must NEVER be invoked."""
        mock_run = MagicMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError):
            project_run_mimo(
                mock_run,
                project="test",
                task_id=TASK_ID,
            )
        mock_run.assert_not_called()

    def test_no_dangerously_skip_permissions_in_any_path(self):
        """Even if someone catches the error, no command with the flag was built."""
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError, match="--dangerously-skip-permissions"):
            project_run_mimo(
                _fake_run_cmd,
                project="test",
                task_id=TASK_ID,
            )

    def test_model_override_also_blocked(self):
        """Even with a model override, the tool is blocked."""
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_mimo(
                _fake_run_cmd,
                project="test",
                task_id=TASK_ID,
                model="big-pickle",
            )

    def test_default_model_also_blocked(self):
        """Even with default model (None), the tool is blocked."""
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_mimo(
                _fake_run_cmd,
                project="test",
                task_id=TASK_ID,
                model=None,
            )

    def test_error_message_includes_safe_alternatives(self):
        """Error message suggests safe alternatives."""
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError, match="project_run_pytest"):
            project_run_mimo(
                _fake_run_cmd,
                project="test",
                task_id=TASK_ID,
            )

    def test_no_command_script_built(self):
        """No shell script with guards is constructed — block is before script build."""
        from command_policy import CommandPolicyError

        mock_run = MagicMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        with pytest.raises(CommandPolicyError):
            project_run_mimo(
                mock_run,
                project="test",
                task_id=TASK_ID,
            )
        # If run_cmd was called, the command would contain guard checks
        mock_run.assert_not_called()


class TestServerWrapperBlocked:
    """Server.py run_tool() wrapper catches CommandPolicyError → error response."""

    @pytest.mark.skipif(
        not importlib.util.find_spec("mcp"),
        reason="mcp package not installed",
    )
    def test_server_wrapper_returns_error_response(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        monkeypatch.setenv("MCP_GATEWAY_WRITE_MODE", "handoff")
        monkeypatch.setenv("GITEA_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        import importlib
        import sys
        from pathlib import Path

        example_dir = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
        monkeypatch.syspath_prepend(str(example_dir))
        for name in list(sys.modules):
            if "mimo_tools" in name or "mcp_server" in name or "tool_modes" in name:
                sys.modules.pop(name, None)
        server = importlib.import_module("server")
        tool_fn = getattr(server, "gateway_project_run_mimo", None)
        assert tool_fn is not None

        result = tool_fn(project="test", task_id=TASK_ID)
        # run_tool catches CommandPolicyError → MCP error response
        assert result.get("isError") is True
        err_msg = result.get("structuredContent", {}).get("error", "")
        assert "blocked" in err_msg.lower()


# ---------------------------------------------------------------------------
# Tool registration (unchanged — tool is still registered, just blocked)
# ---------------------------------------------------------------------------


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
        assert tm.should_register_tool("project_run_mimo") is True

    def test_visible_in_tools_for_chatgpt(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_TOOL_MODE", "chatgpt")
        import importlib
        import sys
        from pathlib import Path

        example_dir = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
        monkeypatch.syspath_prepend(str(example_dir))
        sys.modules.pop("tool_modes", None)
        tm = importlib.import_module("tool_modes")
        tools = tm.tools_for_mode()
        assert "project_run_mimo" in tools
