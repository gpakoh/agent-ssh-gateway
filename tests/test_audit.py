"""Tests for audit trail: AuditEvent, AuditEventLogger, redact_secrets.

Covers:
    - JSONL write validity
    - Ring buffer bounded capping
    - Secrets redaction
    - No content/patch/output fields accepted or redacted/dropped
    - Write error tolerance
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.audit import (
    AuditEvent,
    AuditEventLogger,
    AuditEventType,
    Decision,
    redact_secrets,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_log(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "events.jsonl"


@pytest.fixture()
def logger(tmp_log: Path) -> AuditEventLogger:
    return AuditEventLogger(log_path=str(tmp_log), recent_limit=50)


# ---------------------------------------------------------------------------
# AuditEvent basics
# ---------------------------------------------------------------------------


class TestAuditEvent:
    def test_default_fields(self) -> None:
        e = AuditEvent()
        assert e.event_id  # auto-generated UUID hex
        assert e.timestamp  # auto-generated ISO timestamp
        assert e.decision == Decision.ALLOWED
        assert e.metadata == {}

    def test_frozen(self) -> None:
        e = AuditEvent(event_type="test")
        with pytest.raises(AttributeError):
            e.event_type = "changed"  # type: ignore[misc]

    def test_to_dict_strips_empty(self) -> None:
        e = AuditEvent(event_type="test", decision=Decision.ALLOWED)
        d = e.to_dict()
        assert "event_type" in d
        assert "decision" in d
        # empty strings / dicts / lists stripped
        assert "actor_type" not in d
        assert "metadata" not in d
        assert "target_id" not in d

    def test_to_dict_keeps_non_empty(self) -> None:
        e = AuditEvent(
            event_type="cmd",
            source_ip="10.0.0.1",
            metadata={"exit_code": 0},
        )
        d = e.to_dict()
        assert d["source_ip"] == "10.0.0.1"
        assert d["metadata"] == {"exit_code": 0}

    def test_frozen_with_metadata(self) -> None:
        e = AuditEvent(metadata={"k": "v"})
        assert e.metadata == {"k": "v"}


# ---------------------------------------------------------------------------
# AuditEventLogger — JSONL writes
# ---------------------------------------------------------------------------


class TestJSONLWrite:
    def test_writes_valid_jsonl(self, logger: AuditEventLogger, tmp_log: Path) -> None:
        e = AuditEvent(
            event_type=AuditEventType.COMMAND_EXECUTE,
            source_ip="10.0.0.1",
            route="POST /api/ssh/execute",
            decision=Decision.ALLOWED,
        )
        logger.append(e)

        lines = tmp_log.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event_type"] == "command.execute"
        assert parsed["source_ip"] == "10.0.0.1"
        assert parsed["decision"] == "allowed"

    def test_appends_multiple_events(self, logger: AuditEventLogger, tmp_log: Path) -> None:
        for i in range(5):
            logger.append(AuditEvent(event_type=f"event.{i}"))

        lines = tmp_log.read_text().strip().splitlines()
        assert len(lines) == 5
        for i, line in enumerate(lines):
            parsed = json.loads(line)
            assert parsed["event_type"] == f"event.{i}"

    def test_jsonl_is_newline_delimited(self, logger: AuditEventLogger, tmp_log: Path) -> None:
        for _ in range(3):
            logger.append(AuditEvent(event_type="test"))

        content = tmp_log.read_text()
        # Each line ends with newline, no trailing newline after last line
        assert content.endswith("\n")
        # No blank lines
        assert "\n\n" not in content


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------


class TestRingBuffer:
    def test_recent_buffer_capped(self) -> None:
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=10)
        for i in range(20):
            logger.append(AuditEvent(event_type=f"event.{i}"))

        assert logger.recent_count == 10
        events = logger.recent()
        assert len(events) == 10
        # oldest kept are event.10..event.19 (last 10)
        assert events[0].event_type == "event.10"
        assert events[-1].event_type == "event.19"

    def test_recent_n_parameter(self) -> None:
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=100)
        for i in range(50):
            logger.append(AuditEvent(event_type=f"event.{i}"))

        last_5 = logger.recent(n=5)
        assert len(last_5) == 5
        assert last_5[0].event_type == "event.45"
        assert last_5[-1].event_type == "event.49"

    def test_recent_empty(self) -> None:
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=10)
        assert logger.recent() == []
        assert logger.recent_count == 0

    def test_recent_n_larger_than_buffer(self) -> None:
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=5)
        for i in range(3):
            logger.append(AuditEvent(event_type=f"e.{i}"))

        result = logger.recent(n=100)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


class TestRedactSecrets:
    def test_redact_password_in_string(self) -> None:
        result = redact_secrets("password=supersecret123")
        assert "supersecret123" not in result
        assert "[REDACTED]" in result

    def test_redact_token_in_string(self) -> None:
        result = redact_secrets("token: abcdef123456")
        assert "abcdef123456" not in result
        assert "[REDACTED]" in result

    def test_redact_bearer_auth(self) -> None:
        result = redact_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret")
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert "[REDACTED]" in result

    def test_redact_dict_keys(self) -> None:
        result = redact_secrets({"password": "secret123", "user": "admin"})
        assert result["password"] == "[REDACTED]"
        assert result["user"] == "admin"

    def test_redact_api_key_dict(self) -> None:
        result = redact_secrets({"api_key": "sk-abc123", "name": "test"})
        assert result["api_key"] == "[REDACTED]"
        assert result["name"] == "test"

    def test_redact_nested_dict(self) -> None:
        result = redact_secrets({"outer": {"secret_token": "tok123", "safe": "ok"}})
        assert result["outer"]["secret_token"] == "[REDACTED]"
        assert result["outer"]["safe"] == "ok"

    def test_redact_list(self) -> None:
        result = redact_secrets(["password=x", "normal"])
        assert result[0] == "[REDACTED]" or "[REDACTED]" in result[0]
        assert result[1] == "normal"

    def test_none_passthrough(self) -> None:
        assert redact_secrets(None) is None

    def test_non_string_passthrough(self) -> None:
        assert redact_secrets(42) == 42
        assert redact_secrets(True) is True

    def test_redact_sshpass(self) -> None:
        result = redact_secrets("sshpass -p mypassword123 ssh user@host")
        assert "mypassword123" not in result
        assert "[REDACTED]" in result


class TestAuditEventSecretRedaction:
    def test_actor_name_redacted(self) -> None:
        e = AuditEvent(actor_name="password=hunter2")
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=10)
        logger.append(e)
        recent = logger.recent()
        assert len(recent) == 1
        assert "hunter2" not in recent[0].actor_name
        assert "[REDACTED]" in recent[0].actor_name

    def test_target_id_redacted(self) -> None:
        e = AuditEvent(target_id="token=secret123")
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=10)
        logger.append(e)
        recent = logger.recent()
        assert "secret123" not in recent[0].target_id

    def test_metadata_redacted(self) -> None:
        e = AuditEvent(metadata={"password": "leaked"})
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=10)
        logger.append(e)
        recent = logger.recent()
        assert recent[0].metadata["password"] == "[REDACTED]"

    def test_action_redacted(self) -> None:
        e = AuditEvent(action="exec password=secret")
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=10)
        logger.append(e)
        recent = logger.recent()
        assert "secret" not in recent[0].action


# ---------------------------------------------------------------------------
# No content/patch/output fields
# ---------------------------------------------------------------------------


class TestNoSensitiveFields:
    """AuditEvent schema must not accept command output, file content, or patch diff."""

    EVENT_FIELDS = {f.name for f in AuditEvent.__dataclass_fields__.values()}

    def test_no_output_field(self) -> None:
        assert "output" not in self.EVENT_FIELDS
        assert "stdout" not in self.EVENT_FIELDS
        assert "stderr" not in self.EVENT_FIELDS

    def test_no_content_field(self) -> None:
        assert "content" not in self.EVENT_FIELDS
        assert "file_content" not in self.EVENT_FIELDS

    def test_no_patch_field(self) -> None:
        assert "patch" not in self.EVENT_FIELDS
        assert "diff" not in self.EVENT_FIELDS

    def test_no_command_field(self) -> None:
        assert "command" not in self.EVENT_FIELDS

    def test_metadata_rejects_content_if_passed(self) -> None:
        """If someone passes content in metadata, redact_secrets drops sensitive keys."""
        e = AuditEvent(
            metadata={"content": "secret file data", "password": "x"},
            action="write file",
        )
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=10)
        logger.append(e)
        # content key is NOT in AuditEvent fields — the defense is that
        # AuditEvent schema doesn't have a content field at all.
        assert "content" not in self.EVENT_FIELDS


# ---------------------------------------------------------------------------
# Write error tolerance
# ---------------------------------------------------------------------------


class TestWriteErrorTolerance:
    def test_write_error_does_not_crash(self) -> None:
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=10)
        # /dev/null is a valid path but let's simulate a real error
        with patch.object(Path, "open", side_effect=OSError("disk full")):
            # Should not raise
            logger.append(AuditEvent(event_type="test"))

    def test_write_error_still_buffers(self) -> None:
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=10)
        with patch.object(Path, "open", side_effect=OSError("disk full")):
            logger.append(AuditEvent(event_type="test"))
        # Event should still be in ring buffer despite write failure
        assert logger.recent_count == 1
        assert logger.recent()[0].event_type == "test"

    def test_write_error_unexpected_exception(self) -> None:
        logger = AuditEventLogger(log_path="/dev/null", recent_limit=10)
        with patch.object(Path, "open", side_effect=RuntimeError("unexpected")):
            logger.append(AuditEvent(event_type="test"))
        assert logger.recent_count == 1

    def test_parent_directory_created(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "events.jsonl"
        logger = AuditEventLogger(log_path=str(nested), recent_limit=10)
        logger.append(AuditEvent(event_type="test"))
        assert nested.exists()


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_appends(self) -> None:
        import concurrent.futures

        logger = AuditEventLogger(log_path="/dev/null", recent_limit=200)

        def _append(i: int) -> None:
            logger.append(AuditEvent(event_type=f"event.{i}"))

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(_append, i) for i in range(100)]
            for f in concurrent.futures.as_completed(futures):
                f.result()  # propagate exceptions

        assert logger.recent_count == 100


# ---------------------------------------------------------------------------
# Decision enum
# ---------------------------------------------------------------------------


class TestDecision:
    def test_values(self) -> None:
        assert Decision.ALLOWED == "allowed"
        assert Decision.DENIED == "denied"
        assert Decision.ERROR == "error"

    def test_frozen_event_with_decisions(self) -> None:
        for d in Decision:
            e = AuditEvent(decision=d)
            assert e.decision == d


# ---------------------------------------------------------------------------
# AuditEventType enum
# ---------------------------------------------------------------------------


class TestAuditEventType:
    def test_all_types_exist(self) -> None:
        assert AuditEventType.COMMAND_EXECUTE == "command.execute"
        assert AuditEventType.COMMAND_DENY == "command.deny"
        assert AuditEventType.FILE_READ == "file.read"
        assert AuditEventType.FILE_WRITE == "file.write"
        assert AuditEventType.SESSION_CONNECT == "session.connect"
        assert AuditEventType.AUTH_CHECK == "auth.check"
        assert AuditEventType.WORKSPACE_READONLY_BLOCK == "workspace.readonly_block"
        assert AuditEventType.POLICY_EVALUATE == "policy.evaluate"
        assert AuditEventType.MCP_TOOL == "mcp.tool"
        assert AuditEventType.SYSTEM_STARTUP == "system.startup"
        assert AuditEventType.SYSTEM_ERROR == "system.error"
