"""Tests for gateway read-only tool canonical response envelope.

These test the envelope structure that gateway tools produce,
without importing server.py (which has module-level side effects).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BASE / "tests"))
sys.path.insert(0, str(_BASE / "examples" / "mcp_server"))

from helpers import assert_tool_envelope  # noqa: E402
from tool_results import CONTRACT_VERSION, tool_error, tool_success  # noqa: E402

GATEWAY_READ_TOOLS = [
    "gateway_health",
    "gateway_project_working_directory",
    "gateway_project_info",
    "gateway_project_list_files",
    "gateway_project_tree",
    "gateway_project_list_tree",
    "gateway_tools_manifest",
]


class TestGatewaySuccessEnvelope:
    """Gateway success responses must carry the canonical envelope."""

    def test_each_tool_has_correct_envelope(self):
        for tool_name in GATEWAY_READ_TOOLS:
            result = tool_success(
                tool=tool_name,
                result={"data": "sample"},
                source="gateway",
                read_only=True,
            )
            assert_tool_envelope(result, ok=True, tool=tool_name, source="gateway")
            assert result["result"] == {"data": "sample"}
            assert result["meta"].get("source") == "gateway"
            assert result["meta"].get("read_only") is True

    def test_each_tool_has_contract_meta(self):
        for tool_name in GATEWAY_READ_TOOLS:
            result = tool_success(
                tool=tool_name,
                result={"data": "sample"},
                source="gateway",
                read_only=True,
            )
            assert result["meta"]["contract_version"] == CONTRACT_VERSION
            assert result["meta"]["tool"] == tool_name
            assert isinstance(result["meta"]["request_id"], str)
            assert len(result["meta"]["request_id"]) > 0
            assert isinstance(result["meta"]["duration_ms"], (int, float))
            assert isinstance(result["meta"]["truncated"], bool)
            assert isinstance(result["meta"]["warnings"], list)

    def test_preserves_complex_payload(self):
        payload = {
            "project": "my-project",
            "root": "/data/projects/my-project",
            "resolved_path": "/data/projects/my-project",
            "exists": True,
            "is_dir": True,
            "is_git_repo": True,
        }
        result = tool_success(
            tool="gateway_project_info",
            result=payload,
            source="gateway",
            read_only=True,
        )
        assert_tool_envelope(result, ok=True, tool="gateway_project_info", source="gateway")
        assert result["result"] == payload

    def test_preserves_files_list_payload(self):
        payload = {
            "project": "web-ssh-gateway",
            "pattern": "*.py",
            "root": "/data/projects/web-ssh-gateway",
            "files": ["app/main.py", "app/config.py"],
            "count": 2,
        }
        result = tool_success(
            tool="gateway_project_list_files",
            result=payload,
            source="gateway",
            read_only=True,
        )
        assert_tool_envelope(result, ok=True, tool="gateway_project_list_files", source="gateway")
        assert result["result"] == payload

    def test_preserves_manifest_payload(self):
        payload = {
            "active_mode": "chatgpt",
            "tool_count": 106,
            "tools": [],
            "scopes": {},
            "profiles": {},
        }
        result = tool_success(
            tool="gateway_tools_manifest",
            result=payload,
            source="gateway",
            read_only=True,
        )
        assert_tool_envelope(result, ok=True, tool="gateway_tools_manifest", source="gateway")
        assert result["result"] == payload

    def test_preserves_health_payload(self):
        payload = {"status": "ok", "version": "0.1.28"}
        result = tool_success(
            tool="gateway_health",
            result=payload,
            source="gateway",
            read_only=True,
        )
        assert_tool_envelope(result, ok=True, tool="gateway_health", source="gateway")
        assert result["result"] == payload

    def test_preserves_working_directory_payload(self):
        payload = {"stdout": "/data/projects/my-project\n", "stderr": "", "exit_code": 0}
        result = tool_success(
            tool="gateway_project_working_directory",
            result=payload,
            source="gateway",
            read_only=True,
        )
        assert_tool_envelope(
            result, ok=True, tool="gateway_project_working_directory", source="gateway"
        )
        assert result["result"] == payload
        assert result["meta"].get("read_only") is True


class TestGatewayErrorEnvelope:
    """Gateway error responses must carry the canonical error envelope."""

    def _assert_gateway_error(
        self,
        tool: str,
        code: str,
        retryable: bool,
    ) -> None:
        result = tool_error(
            tool=tool,
            code=code,
            message="Something went wrong",
            source="gateway",
            read_only=True,
            retryable=retryable,
        )
        assert_tool_envelope(result, ok=False, tool=tool, source="gateway", has_error=True)
        assert result["error"]["code"] == code
        assert result["error"]["message"] == "Something went wrong"
        assert result["error"]["retryable"] is retryable
        assert result["result"] is None
        assert result["meta"].get("read_only") is True

    def test_internal_error(self):
        self._assert_gateway_error("gateway_health", "INTERNAL_ERROR", retryable=False)

    def test_policy_violation(self):
        self._assert_gateway_error(
            "gateway_project_working_directory", "POLICY_VIOLATION", retryable=False
        )

    def test_error_for_each_tool(self):
        for tool_name in GATEWAY_READ_TOOLS:
            result = tool_error(
                tool=tool_name,
                code="INTERNAL_ERROR",
                message="Connection refused",
                source="gateway",
                read_only=True,
            )
            assert_tool_envelope(result, ok=False, tool=tool_name, source="gateway", has_error=True)
            assert result["meta"].get("read_only") is True

    def test_error_with_hint(self):
        result = tool_error(
            tool="gateway_tools_manifest",
            code="INTERNAL_ERROR",
            message="Manifest build failed",
            retryable=True,
            hint="Check gateway availability and try again",
            source="gateway",
            read_only=True,
        )
        assert_tool_envelope(
            result, ok=False, tool="gateway_tools_manifest", source="gateway", has_error=True
        )
        assert result["error"]["hint"] == "Check gateway availability and try again"
        assert result["error"]["retryable"] is True

    def test_error_has_contract_meta(self):
        for tool_name in GATEWAY_READ_TOOLS:
            result = tool_error(
                tool=tool_name,
                code="INTERNAL_ERROR",
                message="fail",
                source="gateway",
            )
            assert result["meta"]["contract_version"] == CONTRACT_VERSION
            assert result["meta"]["tool"] == tool_name
            assert isinstance(result["meta"]["request_id"], str)
            assert isinstance(result["meta"]["duration_ms"], (int, float))
            assert isinstance(result["meta"]["truncated"], bool)
            assert isinstance(result["meta"]["warnings"], list)


class TestGatewayResultFieldErrorHandling:
    """Gateway helper maps exceptions to the correct error codes."""

    def _run_gateway(self, fn: Any) -> dict[str, Any]:
        """Replica of _run_gateway from server.py for test isolation."""
        from command_policy import CommandPolicyError
        from gateway_client import GatewayClientError
        from write_modes import WriteModeError, WritePermissionError

        try:
            data = fn()
        except (
            GatewayClientError,
            CommandPolicyError,
            WritePermissionError,
            WriteModeError,
        ) as exc:
            if isinstance(exc, (CommandPolicyError, WritePermissionError, WriteModeError)):
                code = "POLICY_VIOLATION"
            else:
                code = "INTERNAL_ERROR"
            return tool_error(
                tool="gateway_health",
                code=code,
                message=str(exc),
                source="gateway",
                read_only=True,
            )
        return tool_success(
            tool="gateway_health",
            result=data,
            source="gateway",
            read_only=True,
        )

    def test_gateway_client_error_maps_to_internal_error(self):
        result = self._run_gateway(fn=_raise_gateway_client_error)
        assert_tool_envelope(
            result, ok=False, tool="gateway_health", source="gateway", has_error=True
        )
        assert result["error"]["code"] == "INTERNAL_ERROR"
        assert result["meta"].get("read_only") is True

    def test_command_policy_error_maps_to_policy_violation(self):
        result = self._run_gateway(fn=_raise_command_policy_error)
        assert_tool_envelope(
            result, ok=False, tool="gateway_health", source="gateway", has_error=True
        )
        assert result["error"]["code"] == "POLICY_VIOLATION"

    def test_write_permission_error_maps_to_policy_violation(self):
        result = self._run_gateway(fn=_raise_write_permission_error)
        assert_tool_envelope(
            result, ok=False, tool="gateway_health", source="gateway", has_error=True
        )
        assert result["error"]["code"] == "POLICY_VIOLATION"

    def test_write_mode_error_maps_to_policy_violation(self):
        result = self._run_gateway(fn=_raise_write_mode_error)
        assert_tool_envelope(
            result, ok=False, tool="gateway_health", source="gateway", has_error=True
        )
        assert result["error"]["code"] == "POLICY_VIOLATION"

    def test_success_returns_data(self):
        result = self._run_gateway(fn=_return_sample_data)
        assert_tool_envelope(result, ok=True, tool="gateway_health", source="gateway")
        assert result["result"] == {"status": "ok"}

    def test_success_with_none_data(self):
        result = self._run_gateway(fn=lambda: None)
        assert_tool_envelope(result, ok=True, tool="gateway_health", source="gateway")
        assert result["result"] is None

    def test_success_has_contract_meta(self):
        result = self._run_gateway(fn=_return_sample_data)
        assert result["meta"]["contract_version"] == CONTRACT_VERSION
        assert result["meta"]["tool"] == "gateway_health"


# ── Exception-raising helpers ──────────────────────────────────────


def _raise_gateway_client_error() -> Any:
    from gateway_client import GatewayClientError

    raise GatewayClientError("Connection refused by gateway")


def _raise_command_policy_error() -> Any:
    from command_policy import CommandPolicyError

    raise CommandPolicyError("Command not in allowlist: rm -rf")


def _raise_write_permission_error() -> Any:
    from write_modes import WritePermissionError

    raise WritePermissionError("Write operations not allowed in current mode")


def _raise_write_mode_error() -> Any:
    from write_modes import WriteModeError

    raise WriteModeError("Unsupported write mode")


def _return_sample_data() -> dict[str, str]:
    return {"status": "ok"}
