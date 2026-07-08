from examples.mcp_server.tool_results import (
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
            "CONFIRM_TOKEN_INVALID",
            "CONFIRM_TOKEN_EXPIRED",
            "CONFIRM_TOKEN_CONSUMED",
            "DOCKER_COMMAND_FAILED",
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
