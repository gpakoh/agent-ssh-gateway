"""Tests for alert policy primitives (severity, dedup keys, classification)."""

import pytest

from app.notifier.policy import (
    build_dedup_key,
    classify_event_delivery,
    severity_for_event,
)

# ---------------------------------------------------------------------------
# severity_for_event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("command.deny", "warning"),
        ("workspace.readonly_block", "warning"),
        ("system.error", "critical"),
        ("session.connect", "info"),
        ("session.disconnect", "info"),
        ("health.degraded", "warning"),
        ("health.unreachable", "critical"),
        ("health.recovered", "info"),
    ],
)
def test_severity_mapping(event_type: str, expected: str):
    assert severity_for_event(event_type) == expected


def test_severity_unknown_defaults_to_info():
    assert severity_for_event("file.read") == "info"
    assert severity_for_event("") == "info"


# ---------------------------------------------------------------------------
# build_dedup_key
# ---------------------------------------------------------------------------


def test_dedup_key_stable_across_event_id():
    """Different event_id / request_id / timestamp → same key."""
    base = {"event_type": "command.deny", "decision": "denied", "route": "POST /api/ssh/execute"}
    a = build_dedup_key({**base, "event_id": "evt-1", "request_id": "req-1", "timestamp": 1000})
    b = build_dedup_key({**base, "event_id": "evt-2", "request_id": "req-2", "timestamp": 2000})
    assert a == b
    assert len(a) == 16


def test_dedup_key_changes_for_command_root():
    """Different command_root → different key."""
    a = build_dedup_key({"event_type": "command.deny", "metadata": {"command_root": "rm"}})
    b = build_dedup_key({"event_type": "command.deny", "metadata": {"command_root": "curl"}})
    assert a != b


def test_dedup_key_changes_for_profile():
    """Different profile → different key."""
    a = build_dedup_key({"event_type": "command.deny", "profile": "readonly"})
    b = build_dedup_key({"event_type": "command.deny", "profile": "default"})
    assert a != b


def test_dedup_key_changes_for_route():
    """Different route → different key."""
    a = build_dedup_key({"event_type": "workspace.readonly_block", "route": "POST /a"})
    b = build_dedup_key({"event_type": "workspace.readonly_block", "route": "POST /b"})
    assert a != b


def test_dedup_key_changes_for_error_code():
    """Different error_code → different key."""
    a = build_dedup_key({"event_type": "system.error", "error_code": "E001"})
    b = build_dedup_key({"event_type": "system.error", "error_code": "E002"})
    assert a != b


def test_dedup_key_changes_for_decision():
    """Different decision → different key."""
    a = build_dedup_key({"event_type": "command.deny", "decision": "denied"})
    b = build_dedup_key({"event_type": "command.deny", "decision": "allowed"})
    assert a != b


def test_dedup_key_empty_for_no_fields():
    """Empty event → empty key."""
    assert build_dedup_key({}) == ""


def test_dedup_key_never_contains_sensitive_fields():
    """Key must NOT contain raw command, path, IP, host, token, target_id."""
    event = {
        "event_type": "command.deny",
        "decision": "denied",
        "request_id": "req-secret-123",
        "event_id": "evt-secret-456",
        "timestamp": 999999,
        "source_ip": "203.0.113.10",
        "target_id": "session-abc",
        "host": "prod-server-01",
        "path": "/etc/shadow",
        "reason": "token=sk_live_abcdef1234567890",
        "metadata": {
            "command": "cat /etc/shadow | tee /tmp/exfil",
            "host": "raw-host",
        },
    }
    key = build_dedup_key(event)
    assert key
    assert "req-secret" not in key
    assert "evt-secret" not in key
    assert "999999" not in key
    assert "203.0.113" not in key
    assert "session-abc" not in key
    assert "prod-server" not in key
    assert "/etc/shadow" not in key
    assert "sk_live" not in key
    assert "cat /etc" not in key


def test_dedup_key_includes_command_root():
    """command_root IS included in the key (safe bounded field)."""
    a = build_dedup_key({"event_type": "command.deny", "metadata": {"command_root": "tee"}})
    b = build_dedup_key({"event_type": "command.deny", "metadata": {}})
    assert a != b


# ---------------------------------------------------------------------------
# classify_event_delivery
# ---------------------------------------------------------------------------


def test_classify_realtime():
    realtime = ("command.deny", "system.error")
    digest = ("session.connect", "session.disconnect")
    assert classify_event_delivery("command.deny", realtime, digest) == "realtime"
    assert classify_event_delivery("system.error", realtime, digest) == "realtime"


def test_classify_digest():
    realtime = ("command.deny", "system.error")
    digest = ("session.connect", "session.disconnect")
    assert classify_event_delivery("session.connect", realtime, digest) == "digest"
    assert classify_event_delivery("session.disconnect", realtime, digest) == "digest"


def test_classify_skip():
    realtime = ("command.deny", "system.error")
    digest = ("session.connect", "session.disconnect")
    assert classify_event_delivery("file.read", realtime, digest) == "skip"
    assert classify_event_delivery("", realtime, digest) == "skip"
