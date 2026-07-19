"""Adversarial leak tests for MCP audit — verify NO sensitive data leakage.

These tests are designed to catch real-world secret leakage vectors that
basic functional tests might miss: secrets embedded in metadata strings,
forbidden keys that should never appear on disk, API key patterns, nested
structures, and resilience against broken filesystem states.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.mcp_server.mcp_audit import (
    FORBIDDEN_KEYS,
    McpAuditEvent,
    McpAuditLogger,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_logger(tmp_path: Path, limit: int = 500) -> McpAuditLogger:
    return McpAuditLogger(log_path=str(tmp_path / "audit.jsonl"), recent_limit=limit)


def _jsonl_text(tmp_path: Path) -> str:
    p = tmp_path / "audit.jsonl"
    return p.read_text() if p.exists() else ""


# ---------------------------------------------------------------------------
# Leak matrix: every check and pass/fail
# ---------------------------------------------------------------------------

class TestPasswordInMetadataRedacted:
    """Password embedded in metadata string value must be redacted."""

    def test_password_in_task_string_redacted(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.tool_blocked",
            tool="project_run_opencode",
            decision="block",
            metadata={"task": "deploy password=secret123 to prod"},
        )
        logger.append(event)
        records = logger.recent()
        full_dump = json.dumps(records)
        assert "secret123" not in full_dump
        text = _jsonl_text(tmp_path)
        assert "secret123" not in text


class TestBearerTokenRedacted:
    """Bearer token in metadata string must be redacted."""

    def test_bearer_token_redacted(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.command_denied",
            tool="execute_restricted",
            decision="deny",
            metadata={
                "command_root": "curl",
                "auth": "Authorization: Bearer abc123token",
            },
        )
        logger.append(event)
        records = logger.recent()
        full_dump = json.dumps(records)
        assert "abc123token" not in full_dump
        text = _jsonl_text(tmp_path)
        assert "abc123token" not in text


class TestForbiddenKeyLeakage:
    """Forbidden keys must never appear in JSONL output or in-memory buffer."""

    @pytest.mark.parametrize("key", list(FORBIDDEN_KEYS))
    def test_forbidden_key_never_in_jsonl(self, tmp_path: Path, key: str) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.test",
            tool="test_tool",
            metadata={key: "sensitive value here"},
        )
        logger.append(event)
        text = _jsonl_text(tmp_path)
        assert "sensitive value here" not in text
        assert f'"{key}"' not in text

    @pytest.mark.parametrize("key", list(FORBIDDEN_KEYS))
    def test_forbidden_key_never_in_buffer(self, tmp_path: Path, key: str) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.test",
            tool="test_tool",
            metadata={key: "leak_value_42"},
        )
        logger.append(event)
        records = logger.recent()
        full_dump = json.dumps(records)
        assert "leak_value_42" not in full_dump
        # The forbidden key itself must not appear as a metadata key
        if records:
            assert key not in records[-1].get("metadata", {})


class TestRequestIdPreserved:
    """Correlation IDs must survive redaction and forbidden-key stripping."""

    def test_request_id_preserved(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.test",
            tool="test_tool",
            request_id="req-123-abc",
        )
        logger.append(event)
        records = logger.recent()
        assert records[-1]["request_id"] == "req-123-abc"


class TestLoggerSurvivesInvalidPath:
    """Logger must never raise on write — only buffer is affected."""

    def test_logger_survives_invalid_path(self) -> None:
        logger = McpAuditLogger(log_path="/nonexistent/deeply/nested/file.jsonl")
        event = McpAuditEvent(event_type="mcp.test", tool="test_tool")
        logger.append(event)  # should not raise
        assert len(logger.recent()) == 1


class TestLoggerSurvivesWriteError:
    """Logger must handle OSError on write gracefully."""

    def test_logger_survives_write_error(self) -> None:
        logger = McpAuditLogger(log_path="/dev/null/fake.jsonl")
        event = McpAuditEvent(event_type="mcp.test", tool="test_tool")
        logger.append(event)  # should not raise
        assert len(logger.recent()) == 1


class TestFullCommandNotInEvent:
    """Command action text may appear but metadata must never contain output/stderr."""

    def test_full_command_not_in_event(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.command_denied",
            tool="execute_restricted",
            decision="deny",
            action="echo x > /tmp/file.txt",
            metadata={"command_root": "echo"},
        )
        logger.append(event)
        records = logger.recent()
        assert records[-1]["metadata"].get("command_root") == "echo"
        for forbidden in ["output", "stdout", "stderr"]:
            assert forbidden not in records[-1]["metadata"]


class TestApiKeyPatternRedacted:
    """API key patterns (sk-..., etc.) must be redacted in string values."""

    def test_api_key_pattern_redacted(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.test",
            tool="test_tool",
            metadata={"key_check": "api_key=sk-1234567890abcdef"},
        )
        logger.append(event)
        records = logger.recent()
        full_dump = json.dumps(records)
        assert "sk-1234567890abcdef" not in full_dump
        text = _jsonl_text(tmp_path)
        assert "sk-1234567890abcdef" not in text


class TestNestedSecretRedacted:
    """Secrets inside nested dicts must be redacted at every depth."""

    def test_nested_secret_redacted(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.test",
            tool="test_tool",
            metadata={"config": {"password": "hunter2", "host": "localhost"}},
        )
        logger.append(event)
        records = logger.recent()
        full_dump = json.dumps(records)
        assert "hunter2" not in full_dump
        assert records[-1]["metadata"]["config"]["host"] == "localhost"


class TestJsonlValidJson:
    """Every JSONL line must be valid JSON with required audit fields."""

    def test_jsonl_valid_json(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        for i in range(5):
            logger.append(McpAuditEvent(event_type=f"test.{i}", tool="test_tool"))
        lines = _jsonl_text(tmp_path).strip().split("\n")
        for line in lines:
            parsed = json.loads(line)
            assert "event_type" in parsed
            assert "timestamp" in parsed


# ---------------------------------------------------------------------------
# Additional adversarial vectors
# ---------------------------------------------------------------------------

class TestSecretKeyInDictValue:
    """A dict key named 'secret' should be replaced with [REDACTED]."""

    def test_secret_key_redacted(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.test",
            tool="test_tool",
            metadata={"credentials": {"secret": "my_secret_value", "user": "admin"}},
        )
        logger.append(event)
        records = logger.recent()
        full_dump = json.dumps(records)
        assert "my_secret_value" not in full_dump
        assert records[-1]["metadata"]["credentials"]["user"] == "admin"


class TestTokenInDictKey:
    """Any dict key containing 'token' should have value redacted."""

    def test_token_key_redacted(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.test",
            tool="test_tool",
            metadata={"access_token": "super_secret_token_value"},
        )
        logger.append(event)
        records = logger.recent()
        assert records[-1]["metadata"]["access_token"] == "[REDACTED]"


class TestPasswordInStringInsideList:
    """Secrets inside list items (strings) must be redacted."""

    def test_password_in_list_item(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.test",
            tool="test_tool",
            metadata={"args": ["--password=hunter2", "--verbose"]},
        )
        logger.append(event)
        records = logger.recent()
        full_dump = json.dumps(records)
        assert "hunter2" not in full_dump
        assert records[-1]["metadata"]["args"][1] == "--verbose"


class TestMultipleSecretPatternsInOneString:
    """A string with multiple secret patterns should have all redacted."""

    def test_multiple_patterns(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.test",
            tool="test_tool",
            metadata={"combined": "password=abc123 token=xyz789"},
        )
        logger.append(event)
        records = logger.recent()
        full_dump = json.dumps(records)
        assert "abc123" not in full_dump
        assert "xyz789" not in full_dump


class TestSshPassRedacted:
    """sshpass -p <password> pattern must be redacted."""

    def test_sshpass_redacted(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.test",
            tool="test_tool",
            metadata={"ssh_cmd": "sshpass -p MyP@ssw0rd! ssh root@host"},
        )
        logger.append(event)
        records = logger.recent()
        full_dump = json.dumps(records)
        assert "MyP@ssw0rd!" not in full_dump


class TestActionFieldPreserved:
    """The action field (top-level) should be preserved even if it contains secrets
    (it's the caller's responsibility to not put secrets there)."""

    def test_action_field_not_stripped(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.command_denied",
            tool="test_tool",
            action="echo hello",
            decision="deny",
        )
        logger.append(event)
        records = logger.recent()
        assert records[-1]["action"] == "echo hello"


class TestSafeMetadataPreserved:
    """Non-secret metadata keys must survive all redaction passes."""

    def test_safe_metadata_preserved(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.test",
            tool="test_tool",
            metadata={
                "host": "192.168.1.100",
                "port": 22,
                "username": "admin",
                "timeout": 30,
            },
        )
        logger.append(event)
        records = logger.recent()
        meta = records[-1]["metadata"]
        assert meta["host"] == "192.168.1.100"
        assert meta["port"] == 22
        assert meta["username"] == "admin"
        assert meta["timeout"] == 30
