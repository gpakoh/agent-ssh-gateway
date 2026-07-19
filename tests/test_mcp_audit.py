"""Tests for mcp_audit — MCP-local structured audit logger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.mcp_server.mcp_audit import (
    FORBIDDEN_KEYS,
    McpAuditEvent,
    McpAuditLogger,
    redact_secrets,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_logger(tmp_path: Path) -> McpAuditLogger:
    log_file = tmp_path / "audit.jsonl"
    return McpAuditLogger(log_path=str(log_file), recent_limit=50)


# ---------------------------------------------------------------------------
# Tests: McpAuditEvent
# ---------------------------------------------------------------------------

class TestMcpAuditEvent:
    def test_event_id_is_hex(self) -> None:
        event = McpAuditEvent()
        assert len(event.event_id) == 32
        int(event.event_id, 16)  # must be valid hex

    def test_timestamp_is_iso(self) -> None:
        event = McpAuditEvent()
        assert "T" in event.timestamp
        assert event.timestamp.endswith("Z") or "+" in event.timestamp

    def test_frozen(self) -> None:
        event = McpAuditEvent(event_type="test")
        with pytest.raises(AttributeError):
            event.event_type = "changed"  # type: ignore[misc]

    def test_defaults(self) -> None:
        event = McpAuditEvent()
        assert event.event_type == ""
        assert event.tool == ""
        assert event.decision == ""
        assert event.reason == ""
        assert event.error_code == ""
        assert event.request_id == ""
        assert event.metadata == {}


# ---------------------------------------------------------------------------
# Tests: redact_secrets
# ---------------------------------------------------------------------------

class TestRedactSecrets:
    def test_none_passthrough(self) -> None:
        assert redact_secrets(None) is None

    def test_int_passthrough(self) -> None:
        assert redact_secrets(42) == 42

    def test_string_password_redacted(self) -> None:
        result = redact_secrets("password=secret123")
        assert "secret123" not in result
        assert "[REDACTED]" in result

    def test_string_token_redacted(self) -> None:
        result = redact_secrets("token: abc123def")
        assert "abc123def" not in result
        assert "[REDACTED]" in result

    def test_string_api_key_redacted(self) -> None:
        result = redact_secrets("api-key=xyz789")
        assert "xyz789" not in result
        assert "[REDACTED]" in result

    def test_string_bearer_redacted(self) -> None:
        result = redact_secrets("Authorization: Bearer tok123")
        assert "tok123" not in result
        assert "[REDACTED]" in result

    def test_string_sshpass_redacted(self) -> None:
        result = redact_secrets("sshpass -p mypassword")
        assert "mypassword" not in result
        assert "[REDACTED]" in result

    def test_dict_secret_keys_redacted(self) -> None:
        data = {"token": "abc", "api_key": "xyz", "safe": "ok"}
        result = redact_secrets(data)
        assert result["token"] == "[REDACTED]"
        assert result["api_key"] == "[REDACTED]"
        assert result["safe"] == "ok"

    def test_dict_nested_redacted(self) -> None:
        data = {"a": {"password": "secret", "nested": "ok"}, "b": "normal"}
        result = redact_secrets(data)
        assert result["a"]["password"] == "[REDACTED]"
        assert result["a"]["nested"] == "ok"
        assert result["b"] == "normal"

    def test_list_redacted(self) -> None:
        data = ["password=abc", "normal", 42]
        result = redact_secrets(data)
        assert "abc" not in result[0]
        assert result[1] == "normal"
        assert result[2] == 42

    def test_tuple_redacted(self) -> None:
        data = ("token=abc", "normal")
        result = redact_secrets(data)
        assert isinstance(result, tuple)
        assert "abc" not in result[0]


# ---------------------------------------------------------------------------
# Tests: McpAuditLogger
# ---------------------------------------------------------------------------

class TestMcpAuditLogger:
    def test_append_creates_file(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(event_type="test.event")
        logger.append(event)
        log_file = tmp_path / "audit.jsonl"
        assert log_file.exists()

    def test_jsonl_valid(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        for i in range(5):
            logger.append(McpAuditEvent(event_type=f"event.{i}"))
        log_file = tmp_path / "audit.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 5
        for line in lines:
            record = json.loads(line)
            assert "event_id" in record
            assert "timestamp" in record
            assert "event_type" in record

    def test_recent_buffer_capped(self, tmp_path: Path) -> None:
        logger = McpAuditLogger(log_path=str(tmp_path / "audit.jsonl"), recent_limit=10)
        for i in range(25):
            logger.append(McpAuditEvent(event_type=f"event.{i}"))
        assert len(logger._buffer) == 10
        assert logger._buffer[0]["event_type"] == "event.15"
        assert logger._buffer[-1]["event_type"] == "event.24"

    def test_secrets_redacted_in_file(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="test.secrets",
            metadata={"password": "hunter2", "token": "secret123", "safe": "ok"},
        )
        logger.append(event)
        log_file = tmp_path / "audit.jsonl"
        record = json.loads(log_file.read_text().strip())
        assert record["metadata"]["password"] == "[REDACTED]"
        assert record["metadata"]["token"] == "[REDACTED]"
        assert record["metadata"]["safe"] == "ok"

    def test_secrets_redacted_in_buffer(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="test.secrets",
            metadata={"api_key": "sk_live_abc123"},
        )
        logger.append(event)
        recent = logger.recent()
        assert recent[0]["metadata"]["api_key"] == "[REDACTED]"

    def test_write_failure_non_fatal(self, tmp_path: Path) -> None:
        logger = McpAuditLogger(log_path="/nonexistent/path/audit.jsonl", recent_limit=50)
        event = McpAuditEvent(event_type="test.writefail")
        # Must not raise
        logger.append(event)
        # Buffer should still have the event
        assert len(logger._buffer) == 1
        assert logger._buffer[0]["event_type"] == "test.writefail"

    def test_forbidden_keys_stripped(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="test.forbidden",
            metadata={
                "output": "should be stripped",
                "stdout": "also stripped",
                "stderr": "stripped too",
                "content": "nope",
                "patch": "nope",
                "old_string": "nope",
                "new_string": "nope",
                "prompt": "nope",
                "task": "nope",
                "safe_key": "should remain",
            },
        )
        logger.append(event)
        log_file = tmp_path / "audit.jsonl"
        record = json.loads(log_file.read_text().strip())
        for key in FORBIDDEN_KEYS:
            assert key not in record["metadata"], f"forbidden key {key!r} should be stripped"
        assert record["metadata"]["safe_key"] == "should remain"

    def test_event_schema_stable(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(
            event_type="mcp.tool_blocked",
            actor_type="mcp_client",
            tool="execute_restricted",
            action="run_command",
            decision="deny",
            reason="command denied by policy",
            error_code="POLICY_VIOLATION",
            request_id="req-abc-123",
            metadata={"extra": "data"},
        )
        logger.append(event)
        record = json.loads((tmp_path / "audit.jsonl").read_text().strip())
        expected_fields = {
            "event_id", "timestamp", "event_type", "actor_type",
            "tool", "action", "decision", "reason", "error_code",
            "request_id", "metadata",
        }
        assert expected_fields == set(record.keys())

    def test_request_id_preserved(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(request_id="req-42")
        logger.append(event)
        record = json.loads((tmp_path / "audit.jsonl").read_text().strip())
        assert record["request_id"] == "req-42"

    def test_empty_metadata(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        event = McpAuditEvent(event_type="test.empty")
        logger.append(event)
        record = json.loads((tmp_path / "audit.jsonl").read_text().strip())
        assert record["metadata"] == {}

    def test_recent_limit_none(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        result = logger.recent(limit=None)
        assert result == []

    def test_recent_limit_specified(self, tmp_path: Path) -> None:
        logger = _tmp_logger(tmp_path)
        for i in range(10):
            logger.append(McpAuditEvent(event_type=f"event.{i}"))
        result = logger.recent(limit=3)
        assert len(result) == 3
        assert result[0]["event_type"] == "event.7"

    def test_append_multiple_batches(self, tmp_path: Path) -> None:
        logger = McpAuditLogger(log_path=str(tmp_path / "audit.jsonl"), recent_limit=100)
        for _ in range(3):
            for i in range(20):
                logger.append(McpAuditEvent(event_type=f"event.{i}"))
        lines = (tmp_path / "audit.jsonl").read_text().strip().split("\n")
        assert len(lines) == 60
        assert len(logger._buffer) == 60


# ---------------------------------------------------------------------------
# Tests: redact_secrets string patterns
# ---------------------------------------------------------------------------

class TestRedactPatterns:
    def test_password_equals(self) -> None:
        assert "secret" not in redact_secrets("password=secret")

    def test_password_colon(self) -> None:
        assert "secret" not in redact_secrets("passwd: secret")

    def test_password_assign(self) -> None:
        assert "secret" not in redact_secrets("pwd = secret123")

    def test_token_assign(self) -> None:
        assert "abc" not in redact_secrets("token = abc123")

    def test_api_key_underscore(self) -> None:
        assert "xyz" not in redact_secrets("api_key = xyz789")

    def test_api_key_hyphen(self) -> None:
        assert "xyz" not in redact_secrets("api-key: xyz789")

    def test_secret_key(self) -> None:
        assert "val" not in redact_secrets("secret = val")

    def test_bearer_token(self) -> None:
        assert "mytoken" not in redact_secrets("Authorization: Bearer mytoken")

    def test_sshpass(self) -> None:
        result = redact_secrets("sshpass -p mysecretpass")
        assert "mysecretpass" not in result
        assert "[REDACTED]" in result

    def test_no_false_positive(self) -> None:
        original = "this is a normal string with no secrets"
        assert redact_secrets(original) == original
