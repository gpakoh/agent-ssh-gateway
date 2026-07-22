"""Tests for audit event wiring — proves structured audit events are emitted
for workspace readonly deny, command policy decisions, and MCP tool blocks."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.audit import (
    AuditEvent,
    AuditEventLogger,
    AuditEventType,
    Decision,
    emit_command_policy_decision,
    emit_session_lifecycle_event,
)
from app.config import settings
from app.main import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app)


def _auth_headers():
    return {"X-API-Key": settings.api_key}


def _setup_test(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "secret-audit")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )
    from app import state as _app_state

    _app_state.audit_logger = MagicMock()
    _app_state.manager = MagicMock()
    return _app_state


# ---------------------------------------------------------------------------
# AuditEventLogger — emit_command_policy_decision helper
# ---------------------------------------------------------------------------


class TestEmitCommandPolicyDecision:
    """emit_command_policy_decision creates correct AuditEvent."""

    def test_emit_denied(self):
        logger = MagicMock(spec=AuditEventLogger)
        emit_command_policy_decision(
            event_logger=logger,
            command="rm -rf /",
            session_id="s1",
            effective_profile="readonly",
            decision_allowed=False,
            decision_reason="Root command 'rm' denied",
            command_root="rm",
            source_ip="10.0.0.1",
            route="POST /api/ssh/execute",
            actor_fingerprint="abc123",
        )
        logger.append.assert_called_once()
        event = logger.append.call_args[0][0]
        assert event.event_type == AuditEventType.COMMAND_DENY
        assert event.decision == Decision.DENIED
        assert event.profile == "readonly"
        assert event.target_id == "s1"
        assert event.source_ip == "10.0.0.1"
        assert event.actor_fingerprint == "abc123"
        assert event.metadata == {"command_root": "rm"}

    def test_emit_allowed(self):
        logger = MagicMock(spec=AuditEventLogger)
        emit_command_policy_decision(
            event_logger=logger,
            command="ls -la",
            session_id="s2",
            effective_profile="default",
            decision_allowed=True,
            decision_reason="Allowed by default profile",
            source_ip="10.0.0.2",
            route="POST /api/ssh/execute",
        )
        event = logger.append.call_args[0][0]
        assert event.event_type == AuditEventType.COMMAND_EXECUTE
        assert event.decision == Decision.ALLOWED
        assert event.profile == "default"

    def test_noop_when_logger_none(self):
        # Should not raise
        emit_command_policy_decision(
            event_logger=None,
            command="ls",
            session_id="s1",
            effective_profile="default",
            decision_allowed=True,
            decision_reason="ok",
        )


# ---------------------------------------------------------------------------
# Workspace readonly deny — audit event
# ---------------------------------------------------------------------------


class TestWorkspaceReadonlyAudit:
    """WORKSPACE_READONLY deny emits audit event."""

    def test_workspace_readonly_deny_creates_event(self, client, monkeypatch):
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "workspace_readonly", True)

        from app import state as _app_state

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "audit.jsonl")
            _app_state.event_audit_logger = AuditEventLogger(
                log_path=log_path, recent_limit=100
            )

            resp = client.post(
                "/api/workspace/projects/proj1/files/write",
                json={"path": "test.txt", "content": "hello"},
                headers=_auth_headers(),
            )
            assert resp.status_code == 403

            events = _app_state.event_audit_logger.recent()
            readonly_events = [
                e for e in events
                if e.event_type == AuditEventType.WORKSPACE_READONLY_BLOCK
            ]
            assert len(readonly_events) >= 1
            evt = readonly_events[0]
            assert evt.decision == Decision.DENIED
            assert evt.reason == "WORKSPACE_READONLY=true"
            assert evt.error_code == "WORKSPACE_READONLY"
            # Actor attribution from API key identity
            assert evt.actor_type == "master"
            assert evt.actor_fingerprint  # non-empty fingerprint
            assert len(evt.actor_fingerprint) == 12  # truncated to 12 chars

    def test_workspace_readonly_deny_no_raw_api_key(self, client, monkeypatch):
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "workspace_readonly", True)

        from app import state as _app_state

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "audit.jsonl")
            _app_state.event_audit_logger = AuditEventLogger(
                log_path=log_path, recent_limit=100
            )

            resp = client.post(
                "/api/workspace/projects/proj1/files/write",
                json={"path": "test.txt", "content": "hello"},
                headers=_auth_headers(),
            )
            assert resp.status_code == 403

            events = _app_state.event_audit_logger.recent()
            readonly_events = [
                e for e in events
                if e.event_type == AuditEventType.WORKSPACE_READONLY_BLOCK
            ]
            assert len(readonly_events) >= 1
            evt = readonly_events[0]
            # Fingerprint must be hash-truncated, not raw key
            assert "-" not in evt.actor_fingerprint
            assert settings.api_key not in evt.actor_fingerprint

    def test_workspace_readonly_deny_has_route(self, client, monkeypatch):
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "workspace_readonly", True)

        from app import state as _app_state

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "audit.jsonl")
            _app_state.event_audit_logger = AuditEventLogger(
                log_path=log_path, recent_limit=100
            )

            resp = client.post(
                "/api/workspace/projects/proj1/files/write",
                json={"path": "test.txt", "content": "hello"},
                headers=_auth_headers(),
            )
            assert resp.status_code == 403

            events = _app_state.event_audit_logger.recent()
            readonly_events = [
                e for e in events
                if e.event_type == AuditEventType.WORKSPACE_READONLY_BLOCK
            ]
            assert len(readonly_events) >= 1
            assert readonly_events[0].route == "POST /api/workspace/projects/*/files/write"


# ---------------------------------------------------------------------------
# Command policy — audit event with effective profile
# ---------------------------------------------------------------------------


class TestCommandPolicyAuditEvent:
    """Command denied creates audit event with effective profile."""

    def test_command_denied_creates_audit_event(self, client, monkeypatch):
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        from app import state as _app_state

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "audit.jsonl")
            _app_state.event_audit_logger = AuditEventLogger(
                log_path=log_path, recent_limit=100
            )

            resp = client.post(
                "/api/ssh/execute",
                json={"session_id": "s1", "command": "systemctl restart nginx"},
                headers=_auth_headers(),
            )
            assert resp.status_code == 403

            events = _app_state.event_audit_logger.recent()
            deny_events = [
                e for e in events
                if e.event_type == AuditEventType.COMMAND_DENY
            ]
            assert len(deny_events) >= 1
            evt = deny_events[0]
            assert evt.profile == "readonly"
            assert evt.decision == Decision.DENIED
            assert evt.target_id == "s1"
            assert "systemctl" in evt.reason or "not in" in evt.reason

    def test_command_allowed_creates_audit_event(self, client, monkeypatch):
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        mock_session = MagicMock()
        mock_session.owner_type = "master"
        mock_session.owner_token_fingerprint = None
        from app import state as _app_state

        _app_state.manager.get_session = AsyncMock(return_value=mock_session)
        _app_state.manager.execute = AsyncMock(
            return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "audit.jsonl")
            _app_state.event_audit_logger = AuditEventLogger(
                log_path=log_path, recent_limit=100
            )

            resp = client.post(
                "/api/ssh/execute",
                json={"session_id": "s1", "command": "ls -la"},
                headers=_auth_headers(),
            )
            assert resp.status_code == 200

            events = _app_state.event_audit_logger.recent()
            exec_events = [
                e for e in events
                if e.event_type == AuditEventType.COMMAND_EXECUTE
            ]
            assert len(exec_events) >= 1
            assert exec_events[0].profile == "readonly"
            assert exec_events[0].decision == Decision.ALLOWED


# ---------------------------------------------------------------------------
# Audit event contains no raw secrets / command output
# ---------------------------------------------------------------------------


class TestAuditNoSecrets:
    """Audit events must not contain raw command output, secrets, or sensitive data."""

    def test_audit_event_no_output_field(self, client, monkeypatch):
        _setup_test(monkeypatch)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "readonly")

        mock_session = MagicMock()
        mock_session.owner_type = "master"
        mock_session.owner_token_fingerprint = None
        from app import state as _app_state

        _app_state.manager.get_session = AsyncMock(return_value=mock_session)
        _app_state.manager.execute = AsyncMock(
            return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "audit.jsonl")
            _app_state.event_audit_logger = AuditEventLogger(
                log_path=log_path, recent_limit=100
            )

            client.post(
                "/api/ssh/execute",
                json={"session_id": "s1", "command": "cat /etc/passwd"},
                headers=_auth_headers(),
            )

            events = _app_state.event_audit_logger.recent()
            for evt in events:
                d = evt.to_dict()
                # Must not contain output, content, patch, or raw command output
                assert "output" not in d
                assert "content" not in d
                assert "patch" not in d
                assert "stdout" not in d
                assert "stderr" not in d

    def test_audit_event_no_api_key_in_action(self):
        event = AuditEvent(
            event_type=AuditEventType.COMMAND_DENY,
            actor_type="api_key",
            actor_name="user",
            actor_fingerprint="abc123def456",
            action="command denied by policy",
            decision=Decision.DENIED,
        )
        d = event.to_dict()
        # action should not contain raw API key
        assert "secret" not in d.get("action", "").lower()
        assert "api_key" not in d.get("action", "").lower()


# ---------------------------------------------------------------------------
# Audit event has request_id and actor_fingerprint
# ---------------------------------------------------------------------------


class TestAuditEventFields:
    """Audit events must have event_id, request_id, actor_fingerprint, no raw key."""

    def test_event_has_event_id(self):
        event = AuditEvent(
            event_type=AuditEventType.COMMAND_DENY,
            decision=Decision.DENIED,
        )
        assert event.event_id  # auto-generated UUID
        assert len(event.event_id) == 32  # hex UUID

    def test_event_has_timestamp(self):
        event = AuditEvent(
            event_type=AuditEventType.COMMAND_DENY,
            decision=Decision.DENIED,
        )
        assert event.timestamp  # auto-generated ISO timestamp

    def test_actor_fingerprint_is_hash_not_raw_key(self):
        event = AuditEvent(
            event_type=AuditEventType.COMMAND_DENY,
            actor_fingerprint="sha256:abc123def456",
            decision=Decision.DENIED,
        )
        d = event.to_dict()
        assert d["actor_fingerprint"] == "sha256:abc123def456"
        # Must not be a raw API key (no hyphens, reasonable length)
        assert "-" not in d["actor_fingerprint"]

    def test_request_id_can_be_set(self):
        event = AuditEvent(
            event_type=AuditEventType.COMMAND_DENY,
            request_id="req-abc-123",
            decision=Decision.DENIED,
        )
        assert event.request_id == "req-abc-123"


# ---------------------------------------------------------------------------
# Audit JSONL write — events are persisted
# ---------------------------------------------------------------------------


class TestAuditJsonlWrite:
    """Audit events are written to JSONL file."""

    def test_events_written_to_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "audit.jsonl")
            logger = AuditEventLogger(log_path=log_path, recent_limit=10)

            logger.append(AuditEvent(
                event_type=AuditEventType.COMMAND_DENY,
                action="test event",
                decision=Decision.DENIED,
            ))

            content = Path(log_path).read_text()
            lines = content.strip().split("\n")
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["event_type"] == "command.deny"
            assert data["decision"] == "denied"



# ---------------------------------------------------------------------------
# Session lifecycle ? structured audit event
# ---------------------------------------------------------------------------


class TestEmitSessionLifecycleEvent:
    """emit_session_lifecycle_event creates metadata-only session events."""

    def test_emit_session_connect(self):
        logger = MagicMock(spec=AuditEventLogger)
        emit_session_lifecycle_event(
            event_logger=logger,
            connected=True,
            session_id="s-new",
            actor_type="master",
            actor_name="api-key",
            actor_fingerprint="abc123",
            source_ip="127.0.0.1",
            route="POST /api/ssh/connect",
            request_id="req-connect",
        )

        logger.append.assert_called_once()
        event = logger.append.call_args[0][0]
        assert event.event_type == AuditEventType.SESSION_CONNECT
        assert event.decision == Decision.ALLOWED
        assert event.target_type == "session"
        assert event.target_id == "s-new"
        assert event.actor_type == "master"
        assert event.actor_fingerprint == "abc123"
        assert event.request_id == "req-connect"
        assert event.route == "POST /api/ssh/connect"
        assert event.metadata == {}

    def test_emit_session_disconnect(self):
        logger = MagicMock(spec=AuditEventLogger)
        emit_session_lifecycle_event(
            event_logger=logger,
            connected=False,
            session_id="s-old",
            actor_type="agent",
            actor_fingerprint="def456",
            route="POST /api/ssh/disconnect",
            reason="manual",
        )

        event = logger.append.call_args[0][0]
        assert event.event_type == AuditEventType.SESSION_DISCONNECT
        assert event.action == "session disconnected"
        assert event.reason == "manual"

    def test_noop_when_logger_none(self):
        emit_session_lifecycle_event(
            event_logger=None,
            connected=True,
            session_id="s1",
        )


class TestSessionLifecycleAuditRoutes:
    """SSH connect/disconnect routes emit lifecycle audit events."""

    def test_ssh_connect_emits_session_connect_event(self, client, monkeypatch):
        _setup_test(monkeypatch)
        from app import state as _app_state

        _app_state.manager.create_session = AsyncMock(return_value="s-new")
        _app_state.session_store = None

        with tempfile.TemporaryDirectory() as tmpdir:
            _app_state.event_audit_logger = AuditEventLogger(
                log_path=str(Path(tmpdir) / "audit.jsonl"), recent_limit=100
            )

            resp = client.post(
                "/api/ssh/connect",
                json={"host": "10.0.0.10", "username": "root", "password": "pw", "port": 22},
                headers={**_auth_headers(), "X-Request-ID": "req-connect-1"},
            )

            assert resp.status_code == 200
            events = _app_state.event_audit_logger.recent()
            lifecycle = [e for e in events if e.event_type == AuditEventType.SESSION_CONNECT]
            assert len(lifecycle) == 1
            event = lifecycle[0]
            assert event.target_id == "s-new"
            assert event.actor_type == "master"
            assert event.actor_fingerprint
            assert settings.api_key not in event.actor_fingerprint
            assert event.request_id == "req-connect-1"
            assert event.route == "POST /api/ssh/connect"
            assert "10.0.0.10" not in event.to_dict().get("action", "")
            assert event.metadata == {}

    def test_ssh_disconnect_emits_session_disconnect_event(self, client, monkeypatch):
        _setup_test(monkeypatch)
        from app import state as _app_state

        session = MagicMock()
        session.owner_type = "master"
        session.owner_token_fingerprint = None
        _app_state.manager.get_session = AsyncMock(return_value=session)
        _app_state.manager.disconnect = AsyncMock()
        _app_state.session_store = None

        with tempfile.TemporaryDirectory() as tmpdir:
            _app_state.event_audit_logger = AuditEventLogger(
                log_path=str(Path(tmpdir) / "audit.jsonl"), recent_limit=100
            )

            resp = client.post(
                "/api/ssh/disconnect",
                json={"session_id": "s-old"},
                headers={**_auth_headers(), "X-Request-ID": "req-disconnect-1"},
            )

            assert resp.status_code == 200
            events = _app_state.event_audit_logger.recent()
            lifecycle = [e for e in events if e.event_type == AuditEventType.SESSION_DISCONNECT]
            assert len(lifecycle) == 1
            event = lifecycle[0]
            assert event.target_id == "s-old"
            assert event.actor_type == "master"
            assert event.request_id == "req-disconnect-1"
            assert event.route == "POST /api/ssh/disconnect"
            assert event.reason == "manual"
            assert event.metadata == {}
