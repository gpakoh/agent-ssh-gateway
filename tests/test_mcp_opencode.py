"""Tests for OpenCode runner MCP tool — opencode_tools module.

CRITICAL: project_run_opencode is hard-blocked.
--dangerously-skip-permissions is never allowed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MCP_DIR = str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)

from examples.mcp_server.opencode_tools import project_run_opencode  # noqa: E402


def _fake_run_cmd(project: str, command: str) -> dict:
    return {"stdout": "", "stderr": "", "exit_code": 0}


class TestProjectRunOpencodeBlocked:
    """project_run_opencode is hard-blocked — --dangerously-skip-permissions not allowed."""

    def test_raises_command_policy_error(self):
        """Must raise CommandPolicyError, never execute."""
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_opencode(
                _fake_run_cmd,
                project="test",
                task_id="2026-06-25-fix-auth-opencode",
            )

    def test_run_cmd_never_called(self):
        """The run_cmd callable must NEVER be invoked."""
        mock_run = MagicMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError):
            project_run_opencode(
                mock_run,
                project="test",
                task_id="2026-06-25-fix-auth-opencode",
            )
        mock_run.assert_not_called()

    def test_no_dangerously_skip_permissions_in_any_path(self):
        """Even if someone catches the error, no command with the flag was built."""
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError, match="--dangerously-skip-permissions"):
            project_run_opencode(
                _fake_run_cmd,
                project="test",
                task_id="2026-06-25-fix-auth-opencode",
            )

    def test_model_override_also_blocked(self):
        """Even with a model override, the tool is blocked."""
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError, match="blocked"):
            project_run_opencode(
                _fake_run_cmd,
                project="test",
                task_id="2026-06-25-fix-auth-opencode",
                model="gpt-4o",
            )

    def test_error_message_includes_safe_alternatives(self):
        """Error message suggests safe alternatives."""
        from command_policy import CommandPolicyError

        with pytest.raises(CommandPolicyError, match="project_run_pytest"):
            project_run_opencode(
                _fake_run_cmd,
                project="test",
                task_id="2026-06-25-fix-auth-opencode",
            )


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
            if "mcp_server" in name or "tool_modes" in name or "opencode_tools" in name:
                sys.modules.pop(name, None)
        server = importlib.import_module("server")
        tool_fn = getattr(server, "project_run_opencode", None)
        assert tool_fn is not None

        result = tool_fn(project="test", task_id="2026-06-25-fix-auth-opencode")
        # run_tool catches CommandPolicyError → MCP error response
        assert result.get("isError") is True
        err_msg = result.get("structuredContent", {}).get("error", "")
        assert "blocked" in err_msg.lower()
