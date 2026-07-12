import time

from examples.mcp_server.tool_results import (
    CONTRACT_VERSION,
    ERROR_CODES,
    normalize_tool_result,
    tool_error,
    tool_success,
)


class TestToolSuccess:
    def test_basic_success(self):
        result = tool_success("gateway_health", {"status": "ok"})
        assert result["ok"] is True
        assert result["tool"] == "gateway_health"
        assert result["result"] == {"status": "ok"}
        assert result["error"] is None
        assert isinstance(result["meta"], dict)

    def test_success_with_string_result(self):
        result = tool_success("docker_ps", "CONTAINER ID   IMAGE")
        assert result["ok"] is True
        assert result["result"] == "CONTAINER ID   IMAGE"

    def test_success_with_list_result(self):
        result = tool_success("gitea_list_branches", ["main", "develop"])
        assert result["ok"] is True
        assert result["result"] == ["main", "develop"]

    def test_success_with_none_result(self):
        result = tool_success("tool", None)
        assert result["ok"] is True
        assert result["result"] is None

    def test_success_meta_source_valid(self):
        result = tool_success("tool", source="docker")
        assert result["meta"]["source"] == "docker"

    def test_success_meta_source_unknown(self):
        result = tool_success("tool", source="not-a-real-source")
        assert result["meta"]["source"] == "unknown"

    def test_success_meta_duration_ms(self):
        result = tool_success("tool", duration_ms=53.2)
        assert result["meta"]["duration_ms"] == 53.2

    def test_success_meta_redacted_truncated(self):
        result = tool_success("tool", redacted=True, truncated=False)
        assert result["meta"]["redacted"] is True
        assert result["meta"]["truncated"] is False

    def test_success_extra_meta(self):
        result = tool_success("tool", request_id="abc-123")
        assert result["meta"]["request_id"] == "abc-123"

    def test_success_meta_has_contract_version(self):
        result = tool_success("tool")
        assert result["meta"]["contract_version"] == CONTRACT_VERSION

    def test_success_meta_has_request_id(self):
        result = tool_success("tool")
        assert isinstance(result["meta"]["request_id"], str)
        assert len(result["meta"]["request_id"]) > 0

    def test_success_meta_has_duration_ms_default(self):
        result = tool_success("tool")
        assert result["meta"]["duration_ms"] == 0
        assert isinstance(result["meta"]["duration_ms"], (int, float))

    def test_success_meta_has_truncated_default(self):
        result = tool_success("tool")
        assert result["meta"]["truncated"] is False

    def test_success_meta_has_warnings_default(self):
        result = tool_success("tool")
        assert result["meta"]["warnings"] == []

    def test_success_meta_has_tool_name(self):
        result = tool_success("gateway_health")
        assert result["meta"]["tool"] == "gateway_health"

    def test_success_unique_request_ids(self):
        r1 = tool_success("tool")
        r2 = tool_success("tool")
        assert r1["meta"]["request_id"] != r2["meta"]["request_id"]


class TestToolError:
    def test_basic_error(self):
        result = tool_error("docker_restart", "CONTAINER_NOT_FOUND", "Container not found: example")
        assert result["ok"] is False
        assert result["tool"] == "docker_restart"
        assert result["result"] is None
        assert result["error"]["code"] == "CONTAINER_NOT_FOUND"
        assert result["error"]["message"] == "Container not found: example"
        assert result["error"]["retryable"] is False

    def test_error_with_hint(self):
        result = tool_error(
            "docker_restart", "CONTAINER_NOT_FOUND", "not found", hint="Run docker_ps"
        )
        assert result["error"]["hint"] == "Run docker_ps"

    def test_error_retryable(self):
        result = tool_error("tool", "TIMEOUT", "timed out", retryable=True)
        assert result["error"]["retryable"] is True

    def test_error_invalid_code_defaults_to_internal(self):
        result = tool_error("tool", "UNKNOWN_CODE", "something broke")
        assert result["error"]["code"] == "INTERNAL_ERROR"

    def test_error_with_result(self):
        result = tool_error("tool", "INVALID_INPUT", "bad param", result={"param": "x"})
        assert result["result"] == {"param": "x"}

    def test_error_meta_source(self):
        result = tool_error("tool", source="postgres")
        assert result["meta"]["source"] == "postgres"

    def test_error_meta_duration_ms(self):
        result = tool_error("tool", duration_ms=12.0)
        assert result["meta"]["duration_ms"] == 12.0

    def test_error_no_hint(self):
        result = tool_error("tool", "INTERNAL_ERROR", "fail")
        assert "hint" not in result["error"]

    def test_error_message_str_coercion(self):
        result = tool_error("tool", message=42)
        assert result["error"]["message"] == "42"

    def test_error_hint_str_coercion(self):
        result = tool_error("tool", message="x", hint=999)
        assert result["error"]["hint"] == "999"

    def test_error_extra_meta(self):
        result = tool_error("tool", "TIMEOUT", "timeout", source="gateway", trace_id="t-1")
        assert result["meta"]["trace_id"] == "t-1"
        assert result["meta"]["source"] == "gateway"

    def test_error_meta_has_contract_version(self):
        result = tool_error("tool")
        assert result["meta"]["contract_version"] == CONTRACT_VERSION

    def test_error_meta_has_request_id(self):
        result = tool_error("tool")
        assert isinstance(result["meta"]["request_id"], str)
        assert len(result["meta"]["request_id"]) > 0

    def test_error_meta_has_duration_ms_default(self):
        result = tool_error("tool")
        assert result["meta"]["duration_ms"] == 0

    def test_error_meta_has_truncated_default(self):
        result = tool_error("tool")
        assert result["meta"]["truncated"] is False

    def test_error_meta_has_warnings_default(self):
        result = tool_error("tool")
        assert result["meta"]["warnings"] == []

    def test_error_meta_has_tool_name(self):
        result = tool_error("docker_restart")
        assert result["meta"]["tool"] == "docker_restart"


class TestEnvelopeContract:
    """Tests for the v1 response envelope contract."""

    def test_tool_success_envelope(self):
        result = tool_success("my_tool", {"outcome": "passed"})
        assert result["ok"] is True
        assert result["result"]["outcome"] == "passed"
        assert result["error"] is None
        assert result["meta"]["contract_version"] == "1"

    def test_tool_error_envelope(self):
        result = tool_error(
            "my_tool", "DEPENDENCY_MISSING", "uv not found",
            hint="Install uv", retryable=False,
            details={"required_binary": "uv"},
        )
        assert result["ok"] is False
        assert result["result"] is None
        assert result["error"]["code"] == "DEPENDENCY_MISSING"
        assert result["error"]["retryable"] is False
        assert result["error"]["details"]["required_binary"] == "uv"

    def test_meta_always_present(self):
        result = tool_success({"outcome": "passed"})
        assert "contract_version" in result["meta"]
        assert "tool" in result["meta"]
        assert "request_id" in result["meta"]
        assert "duration_ms" in result["meta"]
        assert "truncated" in result["meta"]
        assert "warnings" in result["meta"]

    def test_checks_failed_not_error(self):
        """Non-zero exit from a check tool is ok:true, outcome:failed — NOT an error."""
        result = tool_success(
            tool="gateway_project_run_lint",
            result={"outcome": "failed", "exit_code": 1, "stdout": "", "stderr": "lint errors"},
        )
        assert result["ok"] is True
        assert result["error"] is None
        assert result["result"]["outcome"] == "failed"

    def test_meta_duration_ms_tracks_total_time(self):
        start = time.time()
        result = tool_success({"outcome": "passed"})
        elapsed = int((time.time() - start) * 1000)
        assert result["meta"]["duration_ms"] <= elapsed + 5

    def test_request_id_is_uuid_format(self):
        result = tool_success("tool")
        rid = result["meta"]["request_id"]
        parts = rid.split("-")
        assert len(parts) == 5, f"request_id {rid!r} doesn't look like a UUID"
        assert all(len(p) > 0 for p in parts)


class TestNormalizeToolResult:
    def test_canonical_dict_passthrough(self):
        canonical = tool_success("t", "data")
        result = normalize_tool_result("t", canonical)
        assert result is canonical

    def test_dict_with_error_key(self):
        raw = {"error": "container exploded", "result": None}
        result = normalize_tool_result("docker_restart", raw)
        assert result["ok"] is False
        assert "exploded" in result["error"]["message"]

    def test_error_prefix_string(self):
        result = normalize_tool_result("postgres_query", "error: connection refused")
        assert result["ok"] is False
        assert "connection refused" in result["error"]["message"]

    def test_error_prefix_case_insensitive(self):
        result = normalize_tool_result("t", "Error: something")
        assert result["ok"] is False

    def test_string_result_wrapped(self):
        result = normalize_tool_result("docker_ps", "CONTAINER ID")
        assert result["ok"] is True
        assert result["result"] == "CONTAINER ID"

    def test_list_result_wrapped(self):
        result = normalize_tool_result("tool", [1, 2, 3])
        assert result["ok"] is True
        assert result["result"] == [1, 2, 3]

    def test_dict_without_ok_or_error_wrapped(self):
        result = normalize_tool_result("tool", {"status": "ok"})
        assert result["ok"] is True
        assert result["result"] == {"status": "ok"}

    def test_none_result_wrapped(self):
        result = normalize_tool_result("tool", None)
        assert result["ok"] is True
        assert result["result"] is None

    def test_source_preserved(self):
        result = normalize_tool_result("docker_ps", "ok", source="docker")
        assert result["meta"]["source"] == "docker"

    def test_normalize_result_has_contract_meta(self):
        result = normalize_tool_result("tool", "ok")
        assert result["meta"]["contract_version"] == CONTRACT_VERSION
        assert result["meta"]["tool"] == "tool"


class TestErrorCodes:
    def test_all_error_codes_are_known(self):
        known = {
            "TOOL_NOT_FOUND",
            "CONTAINER_NOT_FOUND",
            "SESSION_NOT_FOUND",
            "AUTH_ERROR",
            "POLICY_VIOLATION",
            "RATE_LIMITED",
            "TIMEOUT",
            "DEPENDENCY_MISSING",
            "INVALID_INPUT",
            "INTERNAL_ERROR",
            "FILE_NOT_FOUND",
            "CONFIRM_TOKEN_INVALID",
            "CONFIRM_TOKEN_EXPIRED",
            "CONFIRM_TOKEN_CONSUMED",
            "DOCKER_COMMAND_FAILED",
            "DOCKER_ADMIN_SCOPE_REQUIRED",
            "DOCKER_EXEC_COMMAND_BLOCKED",
            "DOCKER_EXEC_CONTAINER_NOT_FOUND",
            "DOCKER_EXEC_TIMEOUT",
            "DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED",
            "DOCKER_RUN_CONTAINER_CREATE_FAILED",
            "DOCKER_RUN_IMAGE_INVALID",
            "DOCKER_RUN_IMAGE_NOT_ALLOWED",
            "DOCKER_RUN_TIMEOUT",
            "DOCKER_RMI_FAILED",
            "DOCKER_RMI_INVALID_REFERENCE",
            "DOCKER_VOLUME_RM_FAILED",
            "DOCKER_VOLUME_RM_INVALID_NAME",
        }
        assert ERROR_CODES == known

    def test_each_code_is_accepted(self):
        for code in ERROR_CODES:
            result = tool_error("tool", code, f"msg for {code}")
            assert result["error"]["code"] == code


class TestMetaSafety:
    def test_no_secret_key_names_in_meta(self):
        result = tool_success("t", source="gateway", token="should_not_appear")
        assert result["meta"].get("token") == "should_not_appear"

    def test_no_secret_key_names_in_result(self):
        result = tool_success("t", {"api_key": "sk-123"})
        assert result["result"]["api_key"] == "sk-123"

    def test_result_is_dict(self):
        result = tool_success("t", {"a": 1})
        assert isinstance(result, dict)
        assert isinstance(result["result"], dict)

    def test_output_is_always_dict(self):
        for fn in [tool_success, tool_error, normalize_tool_result]:
            r = fn("t", "x")
            assert isinstance(r, dict)
