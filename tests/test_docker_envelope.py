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


class TestDockerRmEnvelope:
    """docker_rm — confirmation_required envelope."""

    def test_confirmation_required(self):
        result = tool_success(
            tool="docker_rm",
            result={
                "status": "confirmation_required",
                "action_id": "rm-001",
                "confirm_token": "tok_rm_001",
                "expires_in_sec": 55,
                "summary": "Remove container foo",
                "risk": "high",
            },
            source="docker",
            dangerous=True,
        )
        assert_docker_envelope(result, ok=True, tool="docker_rm", dangerous=True)
        assert result["result"]["status"] == "confirmation_required"
        assert result["result"]["summary"] == "Remove container foo"


class TestDockerComposeDownEnvelope:
    """docker_compose_down — confirmation_required + scope error."""

    def test_confirmation_required(self):
        result = tool_success(
            tool="docker_compose_down",
            result={
                "status": "confirmation_required",
                "action_id": "cd-001",
                "confirm_token": "tok_cd_001",
                "expires_in_sec": 42,
                "summary": "Compose down project=myapp",
                "risk": "high",
            },
            source="docker",
            dangerous=True,
        )
        assert_docker_envelope(result, ok=True, tool="docker_compose_down", dangerous=True)
        assert result["result"]["status"] == "confirmation_required"

    def test_volumes_requires_admin_scope(self):
        result = tool_error(
            tool="docker_compose_down",
            code="DOCKER_ADMIN_SCOPE_REQUIRED",
            message="volumes=true requires mcp:docker:admin scope.",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_compose_down", has_error=True)
        assert result["error"]["code"] == "DOCKER_ADMIN_SCOPE_REQUIRED"


class TestDockerPruneEnvelope:
    """docker_prune — confirmation_required + scope error + invalid input."""

    def test_confirmation_required_container(self):
        result = tool_success(
            tool="docker_prune",
            result={
                "status": "confirmation_required",
                "action_id": "pr-001",
                "confirm_token": "tok_pr_001",
                "expires_in_sec": 30,
                "summary": "Prune containers",
                "risk": "high",
            },
            source="docker",
            dangerous=True,
        )
        assert_docker_envelope(result, ok=True, tool="docker_prune", dangerous=True)

    def test_confirmation_required_image(self):
        result = tool_success(
            tool="docker_prune",
            result={
                "status": "confirmation_required",
                "action_id": "pr-002",
                "confirm_token": "tok_pr_002",
                "expires_in_sec": 30,
                "summary": "Prune images",
                "risk": "high",
            },
            source="docker",
            dangerous=True,
        )
        assert_docker_envelope(result, ok=True, tool="docker_prune", dangerous=True)

    def test_volume_without_admin(self):
        result = tool_error(
            tool="docker_prune",
            code="DOCKER_ADMIN_SCOPE_REQUIRED",
            message="Prune type 'volume' requires mcp:docker:admin scope.",
            hint="Request admin scope or use one of: container, image, network.",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_prune", has_error=True)
        assert result["error"]["code"] == "DOCKER_ADMIN_SCOPE_REQUIRED"

    def test_system_without_admin(self):
        result = tool_error(
            tool="docker_prune",
            code="DOCKER_ADMIN_SCOPE_REQUIRED",
            message="Prune type 'system' requires mcp:docker:admin scope.",
            hint="Request admin scope or use one of: container, image, network.",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_prune", has_error=True)
        assert result["error"]["code"] == "DOCKER_ADMIN_SCOPE_REQUIRED"

    def test_invalid_type(self):
        result = tool_error(
            tool="docker_prune",
            code="INVALID_INPUT",
            message="Invalid prune type: invalid_type",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_prune", has_error=True)
        assert result["error"]["code"] == "INVALID_INPUT"


class TestDockerExecEnvelope:
    """docker_exec — confirmation_required + invalid input + blocked command."""

    def test_confirmation_required(self):
        result = tool_success(
            tool="docker_exec",
            result={
                "status": "confirmation_required",
                "action_id": "exec-001",
                "confirm_token": "tok_exec_001",
                "expires_in_sec": 55,
                "summary": "Exec in web: ls -la",
                "risk": "high",
            },
            source="docker",
            dangerous=True,
        )
        assert_docker_envelope(result, ok=True, tool="docker_exec", dangerous=True)
        assert result["result"]["summary"] == "Exec in web: ls -la"

    def test_invalid_container_name(self):
        result = tool_error(
            tool="docker_exec",
            code="INVALID_INPUT",
            message="Invalid container name: foo/bar",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_exec", has_error=True)
        assert result["error"]["code"] == "INVALID_INPUT"

    def test_blocked_command(self):
        result = tool_error(
            tool="docker_exec",
            code="DOCKER_EXEC_COMMAND_BLOCKED",
            message="Command contains denylisted pattern: env",
            hint="Use a narrower diagnostic command that does not dump environment variables, SSH keys, or shadow files.",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_exec", has_error=True)
        assert result["error"]["code"] == "DOCKER_EXEC_COMMAND_BLOCKED"
        assert "hint" in result["error"]


class TestDockerRunEnvelope:
    """docker_run — confirmation_required + allowlist/validation errors."""

    def test_confirmation_required(self):
        result = tool_success(
            tool="docker_run",
            result={
                "status": "confirmation_required",
                "action_id": "run-001",
                "confirm_token": "tok_run_001",
                "expires_in_sec": 55,
                "summary": "Run alpine:latest: echo hello (name=test)",
                "risk": "high",
            },
            source="docker",
            dangerous=True,
        )
        assert_docker_envelope(result, ok=True, tool="docker_run", dangerous=True)
        assert result["result"]["summary"] == "Run alpine:latest: echo hello (name=test)"

    def test_allowlist_not_configured(self):
        result = tool_error(
            tool="docker_run",
            code="DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED",
            message="docker_run requires MCP_DOCKER_RUN_ALLOWED_IMAGES environment variable.",
            hint="Set MCP_DOCKER_RUN_ALLOWED_IMAGES with comma-separated image:tag entries.",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_run", has_error=True)
        assert result["error"]["code"] == "DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED"

    def test_image_invalid(self):
        result = tool_error(
            tool="docker_run",
            code="DOCKER_RUN_IMAGE_INVALID",
            message="Invalid image tag: :invalid",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_run", has_error=True)
        assert result["error"]["code"] == "DOCKER_RUN_IMAGE_INVALID"

    def test_image_not_allowed(self):
        result = tool_error(
            tool="docker_run",
            code="DOCKER_RUN_IMAGE_NOT_ALLOWED",
            message="Image 'ubuntu:latest' is not in the configured allowlist.",
            hint="Only images listed in MCP_DOCKER_RUN_ALLOWED_IMAGES are permitted.",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_run", has_error=True)
        assert result["error"]["code"] == "DOCKER_RUN_IMAGE_NOT_ALLOWED"

    def test_invalid_container_name(self):
        result = tool_error(
            tool="docker_run",
            code="INVALID_INPUT",
            message="Invalid container name: foo/bar",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_run", has_error=True)
        assert result["error"]["code"] == "INVALID_INPUT"

    def test_blocked_command(self):
        result = tool_error(
            tool="docker_run",
            code="DOCKER_EXEC_COMMAND_BLOCKED",
            message="Command contains denylisted pattern: env",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_run", has_error=True)
        assert result["error"]["code"] == "DOCKER_EXEC_COMMAND_BLOCKED"


class TestDockerRmiEnvelope:
    """docker_rmi — confirmation_required + invalid reference errors."""

    def test_confirmation_required(self):
        result = tool_success(
            tool="docker_rmi",
            result={
                "status": "confirmation_required",
                "action_id": "rmi-001",
                "confirm_token": "tok_rmi_001",
                "expires_in_sec": 55,
                "summary": "Remove image(s): alpine:latest, busybox:latest",
                "risk": "high",
            },
            source="docker",
            dangerous=True,
        )
        assert_docker_envelope(result, ok=True, tool="docker_rmi", dangerous=True)
        assert result["result"]["summary"] == "Remove image(s): alpine:latest, busybox:latest"

    def test_empty_images_list(self):
        result = tool_error(
            tool="docker_rmi",
            code="DOCKER_RMI_INVALID_REFERENCE",
            message="docker_rmi accepts 1-5 images.",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_rmi", has_error=True)
        assert result["error"]["code"] == "DOCKER_RMI_INVALID_REFERENCE"

    def test_too_many_images(self):
        result = tool_error(
            tool="docker_rmi",
            code="DOCKER_RMI_INVALID_REFERENCE",
            message="docker_rmi accepts 1-5 images.",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_rmi", has_error=True)
        assert result["error"]["code"] == "DOCKER_RMI_INVALID_REFERENCE"

    def test_invalid_image_ref(self):
        result = tool_error(
            tool="docker_rmi",
            code="DOCKER_RMI_INVALID_REFERENCE",
            message="Invalid image reference: INVALID",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_rmi", has_error=True)
        assert result["error"]["code"] == "DOCKER_RMI_INVALID_REFERENCE"


class TestDockerVolumeRmEnvelope:
    """docker_volume_rm — confirmation_required + invalid name errors."""

    def test_confirmation_required(self):
        result = tool_success(
            tool="docker_volume_rm",
            result={
                "status": "confirmation_required",
                "action_id": "vol-001",
                "confirm_token": "tok_vol_001",
                "expires_in_sec": 55,
                "summary": "Remove volume(s): my_volume",
                "risk": "high",
            },
            source="docker",
            dangerous=True,
        )
        assert_docker_envelope(result, ok=True, tool="docker_volume_rm", dangerous=True)
        assert result["result"]["summary"] == "Remove volume(s): my_volume"

    def test_empty_volumes_list(self):
        result = tool_error(
            tool="docker_volume_rm",
            code="DOCKER_VOLUME_RM_INVALID_NAME",
            message="docker_volume_rm accepts 1-5 volumes.",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_volume_rm", has_error=True)
        assert result["error"]["code"] == "DOCKER_VOLUME_RM_INVALID_NAME"

    def test_too_many_volumes(self):
        result = tool_error(
            tool="docker_volume_rm",
            code="DOCKER_VOLUME_RM_INVALID_NAME",
            message="docker_volume_rm accepts 1-5 volumes.",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_volume_rm", has_error=True)

    def test_invalid_volume_name(self):
        result = tool_error(
            tool="docker_volume_rm",
            code="DOCKER_VOLUME_RM_INVALID_NAME",
            message="Invalid volume name: ..",
            source="docker",
        )
        assert_docker_envelope(result, ok=False, tool="docker_volume_rm", has_error=True)
        assert result["error"]["code"] == "DOCKER_VOLUME_RM_INVALID_NAME"
