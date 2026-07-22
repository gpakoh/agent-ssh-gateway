import pytest

from app.notifier.formatting import format_audit_event


def test_format_command_deny_uses_safe_metadata_only():
    text = format_audit_event(
        {
            "event_type": "command.deny",
            "decision": "denied",
            "profile": "default",
            "request_id": "req-1",
            "actor_fingerprint": "abc123",
            "reason": "Root command denied",
            "metadata": {
                "command_root": "tee",
                "command": "cat secret | tee /tmp/out",
                "host": "203.0.113.10",
            },
            "source_ip": "203.0.113.11",
            "target_id": "session-secret",
        }
    )

    assert text is not None
    assert "command_root" in text
    assert "tee" in text
    assert "cat secret" not in text
    assert "203.0.113" not in text
    assert "session-secret" not in text


def test_format_workspace_readonly_includes_route_and_error_code():
    text = format_audit_event(
        {
            "event_type": "workspace.readonly_block",
            "decision": "denied",
            "route": "POST /api/workspace/projects/*/files/write",
            "error_code": "WORKSPACE_READONLY",
        }
    )

    assert text is not None
    assert "Workspace write blocked" in text
    assert "WORKSPACE_READONLY" in text


def test_unknown_event_returns_none():
    assert format_audit_event({"event_type": "file.read"}) is None


def test_formatter_clips_long_reason():
    text = format_audit_event(
        {
            "event_type": "system.error",
            "reason": "x" * 500,
        }
    )

    assert text is not None
    assert len(text) < 320
    assert "..." in text


@pytest.mark.parametrize(
    ("event_type", "title"),
    [
        ("command.deny", "Command blocked"),
        ("workspace.readonly_block", "Workspace write blocked"),
        ("session.connect", "SSH session connected"),
        ("session.disconnect", "SSH session disconnected"),
        ("system.error", "Gateway system error"),
    ],
)
def test_alert_matrix_formats_supported_events_safely(event_type: str, title: str):
    text = format_audit_event(
        {
            "event_type": event_type,
            "decision": "denied",
            "route": "POST /api/example/{id}",
            "profile": "default",
            "error_code": "EXAMPLE_DENIED",
            "request_id": "req-safe-1",
            "actor_fingerprint": "abc123def456",
            "actor_name": "raw-actor-name",
            "source_ip": "raw-source-ip",
            "target_id": "raw-session-id",
            "reason": "token=raw-secret-value sshpass -p raw-password-value",
            "metadata": {
                "command_root": "tee",
                "command": "cat private-file | tee output-file",
                "host": "raw-host-value",
                "path": "/raw/private/path",
            },
        }
    )

    assert text is not None
    assert title in text
    assert event_type in text
    assert "req-safe-1" in text
    assert "abc123def456" in text
    assert "command_root" in text
    assert "tee" in text
    assert "[REDACTED]" in text
    assert "raw-secret-value" not in text
    assert "raw-password-value" not in text
    assert "cat private-file" not in text
    assert "raw-host-value" not in text
    assert "/raw/private/path" not in text
    assert "raw-source-ip" not in text
    assert "raw-session-id" not in text
    assert "raw-actor-name" not in text
