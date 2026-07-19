"""Tests for MCP block event audit wiring — verifies every security-relevant
block/deny path emits a structured audit event via McpAuditLogger.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure mcp_server is importable with bare names (same as server.py sees)
MCP_DIR = str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)

from command_policy import CommandPolicyError  # noqa: E402, I001
from write_modes import WriteModeError, WritePermissionError  # noqa: E402, I001

from examples.mcp_server.mcp_audit import McpAuditEvent, McpAuditLogger  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_logger() -> MagicMock:
    """Return a MagicMock that quacks like McpAuditLogger."""
    logger = MagicMock(spec=McpAuditLogger)
    logger.append = MagicMock()
    return logger


def _raising_fn(error: Exception):
    """Return a callable that raises the given error."""
    def _fn() -> dict[str, Any]:
        raise error
    return _fn


# ---------------------------------------------------------------------------
# Tests: run_tool — CommandPolicyError (READONLY_COMMAND)
# ---------------------------------------------------------------------------

class TestRunToolReadonlyCommand:
    def test_emits_audit_on_command_policy_error(self) -> None:
        from examples.mcp_server.server import run_tool

        mock_logger = _make_mock_logger()
        error = CommandPolicyError("Command denied by policy: rm")

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            result = run_tool(
                tool="execute_restricted",
                title="Restricted execute",
                fn=_raising_fn(error),
                success_text="ok",
            )

        assert result["isError"] is True
        mock_logger.append.assert_called_once()
        event = mock_logger.append.call_args[0][0]
        assert isinstance(event, McpAuditEvent)
        assert event.event_type == "mcp.tool_blocked"
        assert event.tool == "execute_restricted"
        assert event.decision == "block"
        assert event.error_code == "READONLY_COMMAND"

    def test_audit_failure_does_not_change_behavior(self) -> None:
        from examples.mcp_server.server import run_tool

        mock_logger = _make_mock_logger()
        mock_logger.append.side_effect = RuntimeError("audit down")

        error = CommandPolicyError("denied")
        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            result = run_tool(
                tool="t",
                title="T",
                fn=_raising_fn(error),
                success_text="ok",
            )
        assert result["isError"] is True


# ---------------------------------------------------------------------------
# Tests: run_tool — WritePermissionError
# ---------------------------------------------------------------------------

class TestRunToolWritePermission:
    def test_emits_audit_on_write_permission_error(self) -> None:
        from examples.mcp_server.server import run_tool

        mock_logger = _make_mock_logger()
        error = WritePermissionError("write not allowed")

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            result = run_tool(
                tool="workspace_file_write",
                title="Write file",
                fn=_raising_fn(error),
                success_text="ok",
            )

        assert result["isError"] is True
        mock_logger.append.assert_called_once()
        event = mock_logger.append.call_args[0][0]
        assert event.event_type == "mcp.tool_blocked"
        assert event.tool == "workspace_file_write"
        assert event.error_code == "WRITE_PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Tests: run_tool — WriteModeError
# ---------------------------------------------------------------------------

class TestRunToolWriteMode:
    def test_emits_audit_on_write_mode_error(self) -> None:
        from examples.mcp_server.server import run_tool

        mock_logger = _make_mock_logger()
        error = WriteModeError("write mode not enabled")

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            result = run_tool(
                tool="project_write_handoff_plan",
                title="Write handoff",
                fn=_raising_fn(error),
                success_text="ok",
            )

        assert result["isError"] is True
        mock_logger.append.assert_called_once()
        event = mock_logger.append.call_args[0][0]
        assert event.event_type == "mcp.tool_blocked"
        assert event.error_code == "WRITE_MODE_ERROR"


# ---------------------------------------------------------------------------
# Tests: run_tool — OPENCODE_BLOCKED
# ---------------------------------------------------------------------------

class TestRunToolOpenCodeBlocked:
    def test_emits_audit_on_opencode_blocked(self) -> None:
        from examples.mcp_server.server import run_tool

        mock_logger = _make_mock_logger()
        error = CommandPolicyError(
            "project_run_opencode is blocked: --dangerously-skip-permissions is not allowed."
        )

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            result = run_tool(
                tool="project_run_opencode",
                title="Run opencode task",
                fn=_raising_fn(error),
                success_text="ok",
            )

        assert result["isError"] is True
        event = mock_logger.append.call_args[0][0]
        assert event.error_code == "OPENCODE_BLOCKED"


# ---------------------------------------------------------------------------
# Tests: run_tool — MIMO_BLOCKED
# ---------------------------------------------------------------------------

class TestRunToolMimoBlocked:
    def test_emits_audit_on_mimo_blocked(self) -> None:
        from examples.mcp_server.server import run_tool

        mock_logger = _make_mock_logger()
        error = CommandPolicyError(
            "project_run_mimo is blocked: --dangerously-skip-permissions is not allowed."
        )

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            result = run_tool(
                tool="project_run_mimo",
                title="Run mimo task",
                fn=_raising_fn(error),
                success_text="ok",
            )

        assert result["isError"] is True
        event = mock_logger.append.call_args[0][0]
        assert event.error_code == "MIMO_BLOCKED"


# ---------------------------------------------------------------------------
# Tests: run_tool — AGENT_BACKEND_BLOCKED
# ---------------------------------------------------------------------------

class TestRunToolAgentBackendBlocked:
    def test_emits_audit_on_agent_backend_blocked(self) -> None:
        from examples.mcp_server.server import run_tool

        mock_logger = _make_mock_logger()
        error = CommandPolicyError(
            "project_run_agent is blocked: opencode agent backend is not allowed."
        )

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            result = run_tool(
                tool="project_run_agent",
                title="Run agent task",
                fn=_raising_fn(error),
                success_text="ok",
            )

        assert result["isError"] is True
        event = mock_logger.append.call_args[0][0]
        assert event.error_code == "AGENT_BACKEND_BLOCKED"


# ---------------------------------------------------------------------------
# Tests: _run_gateway — POLICY_VIOLATION (uses tool_error envelope)
# ---------------------------------------------------------------------------

class TestRunGatewayPolicyViolation:
    def test_emits_audit_on_command_policy_error(self) -> None:
        from examples.mcp_server.server import _run_gateway

        mock_logger = _make_mock_logger()
        error = CommandPolicyError("command denied")

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            result = _run_gateway(
                tool="project_list_files",
                fn=_raising_fn(error),
            )

        # tool_error envelope: {"ok": false, "error": {"code": "...", ...}}
        assert result["ok"] is False
        assert result["error"]["code"] == "POLICY_VIOLATION"
        mock_logger.append.assert_called_once()
        event = mock_logger.append.call_args[0][0]
        assert event.event_type == "mcp.command_denied"
        assert event.tool == "project_list_files"
        assert event.decision == "deny"
        assert event.error_code == "POLICY_VIOLATION"


# ---------------------------------------------------------------------------
# Tests: confirm_operation — token failures (uses tool_error envelope)
# ---------------------------------------------------------------------------

class TestConfirmOperation:
    @pytest.mark.asyncio
    async def test_emits_audit_on_invalid_token(self) -> None:
        from examples.mcp_server.server import confirm_operation

        mock_logger = _make_mock_logger()

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            result = await confirm_operation("bogus-token")

        assert result["ok"] is False
        assert result["error"]["code"] == "CONFIRM_TOKEN_INVALID"
        mock_logger.append.assert_called_once()
        event = mock_logger.append.call_args[0][0]
        assert event.event_type == "mcp.tool_blocked"
        assert event.tool == "confirm_operation"
        assert event.error_code == "CONFIRM_TOKEN_INVALID"

    @pytest.mark.asyncio
    async def test_emits_audit_on_expired_token(self) -> None:
        import time

        from examples.mcp_server.server import _confirm_store, confirm_operation

        mock_logger = _make_mock_logger()

        # Create an action and artificially expire it
        action = _confirm_store.create_action(
            "docker_start", {"container": "test"}, "Start test"
        )
        # Force expiry by backdating created_at
        action.created_at = time.monotonic() - 120

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            result = await confirm_operation(action.confirm_token)

        assert result["ok"] is False
        assert result["error"]["code"] == "CONFIRM_TOKEN_EXPIRED"
        event = mock_logger.append.call_args[0][0]
        assert event.error_code == "CONFIRM_TOKEN_EXPIRED"

    @pytest.mark.asyncio
    async def test_emits_audit_on_consumed_token(self) -> None:
        from examples.mcp_server.server import _confirm_store, confirm_operation

        mock_logger = _make_mock_logger()

        # Create and consume an action
        action = _confirm_store.create_action(
            "docker_stop", {"container": "test"}, "Stop test"
        )
        _confirm_store.confirm_action(action.confirm_token)

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            result = await confirm_operation(action.confirm_token)

        assert result["ok"] is False
        assert result["error"]["code"] == "CONFIRM_TOKEN_CONSUMED"
        event = mock_logger.append.call_args[0][0]
        assert event.error_code == "CONFIRM_TOKEN_CONSUMED"


# ---------------------------------------------------------------------------
# Tests: opencode_tools — hard block at raise site
# (local import means we patch mcp_audit.get_audit_logger directly)
# ---------------------------------------------------------------------------

class TestOpenCodeToolsAudit:
    def test_emits_audit_at_raise_site(self) -> None:
        from examples.mcp_server.opencode_tools import project_run_opencode

        mock_logger = _make_mock_logger()

        with patch("examples.mcp_server.mcp_audit.get_audit_logger", return_value=mock_logger):
            with pytest.raises(CommandPolicyError, match="blocked"):
                project_run_opencode(
                    run_cmd=lambda p, c: {},
                    project="test",
                    task_id="task-1",
                )

        mock_logger.append.assert_called_once()
        event = mock_logger.append.call_args[0][0]
        assert event.event_type == "mcp.tool_blocked"
        assert event.tool == "project_run_opencode"
        assert event.error_code == "OPENCODE_BLOCKED"


# ---------------------------------------------------------------------------
# Tests: mimo_tools — hard block at raise site
# ---------------------------------------------------------------------------

class TestMimoToolsAudit:
    def test_emits_audit_at_raise_site(self) -> None:
        from examples.mcp_server.mimo_tools import project_run_mimo

        mock_logger = _make_mock_logger()

        with patch("examples.mcp_server.mcp_audit.get_audit_logger", return_value=mock_logger):
            with pytest.raises(CommandPolicyError, match="blocked"):
                project_run_mimo(
                    run_cmd=lambda p, c: {},
                    project="test",
                    task_id="task-1",
                )

        mock_logger.append.assert_called_once()
        event = mock_logger.append.call_args[0][0]
        assert event.event_type == "mcp.tool_blocked"
        assert event.tool == "project_run_mimo"
        assert event.error_code == "MIMO_BLOCKED"


# ---------------------------------------------------------------------------
# Tests: agent_tools — opencode/mimo backend block at raise site
# ---------------------------------------------------------------------------

class TestAgentToolsAudit:
    def test_emits_audit_for_opencode_backend(self) -> None:
        import json

        from examples.mcp_server.agent_tools import project_run_agent

        mock_logger = _make_mock_logger()

        # task.json must contain agent=auto and allowed_backends with opencode/mimo
        task_json = json.dumps({
            "agent": "auto",
            "allowed_backends": ["opencode", "mimo"],
        })
        def _fake_run(project: str, command: str) -> dict[str, Any]:
            if "cat " in command and "task.json" in command:
                return {"stdout": task_json, "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 0}

        with patch("examples.mcp_server.mcp_audit.get_audit_logger", return_value=mock_logger):
            with pytest.raises(CommandPolicyError, match="blocked"):
                project_run_agent(
                    run_cmd=_fake_run,
                    project="test",
                    task_id="2026-07-19-test-task-01",
                )

        mock_logger.append.assert_called_once()
        event = mock_logger.append.call_args[0][0]
        assert event.event_type == "mcp.tool_blocked"
        assert event.tool == "project_run_agent"
        assert event.error_code == "AGENT_BACKEND_BLOCKED"


# ---------------------------------------------------------------------------
# Tests: No forbidden content in metadata
# ---------------------------------------------------------------------------

class TestNoForbiddenContent:
    def test_metadata_has_no_full_command(self) -> None:
        """Audit events must never include full command text in metadata."""
        from examples.mcp_server.server import run_tool

        mock_logger = _make_mock_logger()
        error = CommandPolicyError("denied: rm -rf /")

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            run_tool(
                tool="t",
                title="T",
                fn=_raising_fn(error),
                success_text="ok",
            )

        event = mock_logger.append.call_args[0][0]
        # metadata should not contain the full command
        assert "rm -rf /" not in str(event.metadata)

    def test_metadata_has_no_prompt_content(self) -> None:
        """Audit events must never include prompt or task content."""
        from examples.mcp_server.server import run_tool

        mock_logger = _make_mock_logger()
        error = CommandPolicyError("blocked")

        with patch("examples.mcp_server.server.get_audit_logger", return_value=mock_logger):
            run_tool(
                tool="t",
                title="T",
                fn=_raising_fn(error),
                success_text="ok",
            )

        event = mock_logger.append.call_args[0][0]
        assert "prompt" not in event.metadata
        assert "task" not in event.metadata


# ---------------------------------------------------------------------------
# Tests: Audit logger failure does not change tool behavior
# ---------------------------------------------------------------------------

class TestAuditFailureIsolation:
    def test_tool_error_returns_when_audit_raises(self) -> None:
        from examples.mcp_server.server import run_tool

        broken_logger = _make_mock_logger()
        broken_logger.append.side_effect = RuntimeError("disk full")

        error = CommandPolicyError("denied")

        with patch("examples.mcp_server.server.get_audit_logger", return_value=broken_logger):
            result = run_tool(
                tool="execute_restricted",
                title="Restricted execute",
                fn=_raising_fn(error),
                success_text="ok",
            )

        # Tool still returns the error result (error_result envelope)
        assert result["isError"] is True
        assert "denied" in str(result.get("content", ""))
