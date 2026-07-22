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
