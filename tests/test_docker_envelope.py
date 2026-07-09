"""Tests for Docker tool canonical response envelope.

These test the envelope structure that Docker tools produce,
without importing server.py (which has module-level side effects).
"""

from __future__ import annotations

import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BASE / "tests"))
sys.path.insert(0, str(_BASE / "examples" / "mcp_server"))

from helpers import assert_docker_envelope  # noqa: E402
from tool_results import tool_error, tool_success  # noqa: E402


class TestConfirmationRequiredEnvelope:
    """`_confirmation_response()` produces this envelope pattern."""

    def test_success_envelope_structure(self):
        result = tool_success(
            tool="docker_rm",
            result={
                "status": "confirmation_required",
                "action_id": "abc123",
                "confirm_token": "xyz789",
                "expires_in_sec": 55,
                "summary": "Remove container foo",
                "risk": "high",
            },
            source="docker",
            dangerous=True,
        )
        assert_docker_envelope(result, ok=True, tool="docker_rm", dangerous=True)
        assert result["result"]["status"] == "confirmation_required"
        assert result["result"]["action_id"] == "abc123"
        assert result["result"]["expires_in_sec"] == 55
        assert "confirm_token" in result["result"]

    def test_different_tool_in_envelope(self):
        result = tool_success(
            tool="docker_exec",
            result={
                "status": "confirmation_required",
                "action_id": "def456",
                "confirm_token": "tok123",
                "expires_in_sec": 42,
                "summary": "Exec ls in web",
                "risk": "high",
            },
            source="docker",
            dangerous=True,
        )
        assert_docker_envelope(result, ok=True, tool="docker_exec", dangerous=True)


class TestConfirmSuccessEnvelope:
    """`docker_confirm` success (exit_code == 0) produces this envelope."""

    def test_flat_result_with_stdout(self):
        result = tool_success(
            tool="docker_exec",
            result={
                "stdout": "total 0\n",
                "stderr": "",
                "exit_code": 0,
            },
            source="docker",
        )
        assert_docker_envelope(result, ok=True, tool="docker_exec", dangerous=None)
        assert result["result"]["exit_code"] == 0
        assert "action" not in result["result"]
        assert "executed" not in result["result"]

    def test_flat_result_no_output(self):
        result = tool_success(
            tool="docker_rm",
            result={
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
            },
            source="docker",
        )
        assert_docker_envelope(result, ok=True, tool="docker_rm", dangerous=None)
        assert result["result"]["exit_code"] == 0


class TestConfirmFailureEnvelope:
    """`docker_confirm` failure (exit_code != 0) produces this envelope."""

    def test_command_failure_with_output(self):
        result = tool_error(
            tool="docker_exec",
            code="DOCKER_COMMAND_FAILED",
            message="Docker command failed",
            result={
                "stdout": "",
                "stderr": "Error: container not found\n",
                "exit_code": 1,
            },
            source="docker",
            retryable=False,
            hint="Check container name or Docker state.",
        )
        assert_docker_envelope(result, ok=False, tool="docker_exec", has_error=True)
        assert result["result"]["exit_code"] == 1
        assert result["error"]["code"] == "DOCKER_COMMAND_FAILED"
        assert "action" not in result["result"]

    def test_command_failure_nonzero_exit(self):
        result = tool_error(
            tool="docker_rm",
            code="DOCKER_COMMAND_FAILED",
            message="Docker command failed",
            result={
                "stdout": "",
                "stderr": "Error: ...",
                "exit_code": 2,
            },
            source="docker",
            retryable=False,
            hint="Check container name or Docker state.",
        )
        assert_docker_envelope(result, ok=False, tool="docker_rm", has_error=True)
        assert result["result"]["exit_code"] == 2
        assert result["error"]["code"] == "DOCKER_COMMAND_FAILED"


class TestPendingActionsEnvelope:
    """`docker_pending_actions` produces this envelope."""

    def test_empty_pending(self):
        result = tool_success(
            tool="docker_pending_actions",
            result={"count": 0, "items": []},
            source="docker",
        )
        assert_docker_envelope(result, ok=True, tool="docker_pending_actions")
        assert result["result"]["count"] == 0
        assert result["result"]["items"] == []

    def test_with_pending_items(self):
        result = tool_success(
            tool="docker_pending_actions",
            result={
                "count": 1,
                "items": [
                    {
                        "action_id": "abc",
                        "tool": "docker_rm",
                        "summary": "Remove foo",
                        "risk": "high",
                        "expires_in_sec": 45,
                        "confirm_token": "abc123...",
                    }
                ],
            },
            source="docker",
        )
        assert_docker_envelope(result, ok=True, tool="docker_pending_actions")
        assert result["result"]["count"] == 1
        assert result["result"]["items"][0]["confirm_token"].endswith("...")


class TestConfirmTokenErrorEnvelope:
    """`docker_confirm` token errors produce this envelope."""

    def test_invalid_token(self):
        result = tool_error(
            tool="docker_confirm",
            code="CONFIRM_TOKEN_INVALID",
            message="Invalid confirmation token",
            hint="Call the dangerous tool again to get a new token.",
            retryable=False,
            source="docker",
        )
        assert_docker_envelope(
            result, ok=False, tool="docker_confirm", has_error=True, has_result=True
        )

    def test_expired_token(self):
        result = tool_error(
            tool="docker_confirm",
            code="CONFIRM_TOKEN_EXPIRED",
            message="Confirmation token expired (TTL 60s)",
            hint="Call the dangerous tool again to get a new token.",
            retryable=False,
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_confirm", has_error=True)

    def test_consumed_token(self):
        result = tool_error(
            tool="docker_confirm",
            code="CONFIRM_TOKEN_CONSUMED",
            message="Confirmation token already used",
            hint="Call the dangerous tool again to get a new token.",
            retryable=False,
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_confirm", has_error=True)
