"""Tests for audit event query endpoint."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.audit import AuditEvent, AuditEventLogger, Decision
from app.config import settings
from app.main import app


@pytest.fixture
def client():
    with patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1"):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


@pytest.fixture
def audit_logger():
    """Create a fresh audit logger for testing."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        path = f.name
    logger = AuditEventLogger(log_path=path, recent_limit=100)
    yield logger


class TestAuditRecentRequiresAuth:
    def test_no_auth_returns_401(self, client):
        resp = client.get("/api/admin/audit/recent")
        assert resp.status_code == 401


class TestAuditRecentLimit:
    def test_limit_capped_at_1000(self, client):
        resp = client.get(
            "/api/admin/audit/recent",
            params={"limit": 5000},
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 422  # validation error


class TestAuditRecentFilterEventType:
    def test_filter_by_event_type(self, client):
        with patch("app.state.event_audit_logger") as mock_logger:
            mock_logger.recent.return_value = [
                AuditEvent(event_type="command.execute", decision=Decision.ALLOWED),
                AuditEvent(event_type="command.deny", decision=Decision.DENIED),
                AuditEvent(event_type="file.read", decision=Decision.ALLOWED),
            ]
            mock_logger.recent_count = 3

            resp = client.get(
                "/api/admin/audit/recent",
                params={"event_type": "command.execute"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["events"]) == 1
            assert data["events"][0]["event_type"] == "command.execute"


class TestAuditRecentFilterDecision:
    def test_filter_by_decision(self, client):
        with patch("app.state.event_audit_logger") as mock_logger:
            mock_logger.recent.return_value = [
                AuditEvent(event_type="command.execute", decision=Decision.ALLOWED),
                AuditEvent(event_type="command.deny", decision=Decision.DENIED),
            ]
            mock_logger.recent_count = 2

            resp = client.get(
                "/api/admin/audit/recent",
                params={"decision": "denied"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["events"]) == 1
            assert data["events"][0]["decision"] == "denied"


class TestAuditRecentNoSecretLeakage:
    def test_secrets_redacted_in_response(self, client):
        """Secrets are redacted when events go through AuditEventLogger.append()."""
        with patch("app.state.event_audit_logger") as mock_logger:
            # Create event with secrets — the logger's _redact_event will strip them
            raw_event = AuditEvent(
                event_type="command.execute",
                action="password=SECRET123 api_key=KEY456",
                target_id="/etc/passwd",
            )
            # Simulate what append() does: redact then store
            from app.audit import AuditEventLogger
            redacted = AuditEventLogger._redact_event(raw_event)
            mock_logger.recent.return_value = [redacted]
            mock_logger.recent_count = 1

            resp = client.get(
                "/api/admin/audit/recent",
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200
            data = resp.json()
            event = data["events"][0]
            assert "SECRET123" not in event.get("action", "")
            assert "KEY456" not in event.get("action", "")


class TestAuditRecentEmptyLog:
    def test_empty_log_returns_empty_list(self, client):
        with patch("app.state.event_audit_logger") as mock_logger:
            mock_logger.recent.return_value = []
            mock_logger.recent_count = 0

            resp = client.get(
                "/api/admin/audit/recent",
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["events"] == []
            assert data["total"] == 0


class TestAuditRecentSort:
    def _make_events(self, n: int) -> list[AuditEvent]:
        """Create n events with incrementing timestamps."""
        return [
            AuditEvent(
                event_type="command.execute",
                decision=Decision.ALLOWED,
                action=f"cmd_{i}",
                timestamp=float(i),
            )
            for i in range(n)
        ]

    def test_default_returns_newest_first(self, client):
        events = self._make_events(5)
        with patch("app.state.event_audit_logger") as mock_logger:
            mock_logger.recent.return_value = events  # oldest-first
            mock_logger.recent_count = 5

            resp = client.get(
                "/api/admin/audit/recent",
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200
            data = resp.json()
            actions = [e["action"] for e in data["events"]]
            assert actions == ["cmd_4", "cmd_3", "cmd_2", "cmd_1", "cmd_0"]

    def test_sort_newest_returns_newest_first(self, client):
        events = self._make_events(5)
        with patch("app.state.event_audit_logger") as mock_logger:
            mock_logger.recent.return_value = events
            mock_logger.recent_count = 5

            resp = client.get(
                "/api/admin/audit/recent",
                params={"sort": "newest"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200
            actions = [e["action"] for e in resp.json()["events"]]
            assert actions == ["cmd_4", "cmd_3", "cmd_2", "cmd_1", "cmd_0"]

    def test_sort_oldest_returns_oldest_first(self, client):
        events = self._make_events(5)
        with patch("app.state.event_audit_logger") as mock_logger:
            mock_logger.recent.return_value = events
            mock_logger.recent_count = 5

            resp = client.get(
                "/api/admin/audit/recent",
                params={"sort": "oldest"},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200
            actions = [e["action"] for e in resp.json()["events"]]
            assert actions == ["cmd_0", "cmd_1", "cmd_2", "cmd_3", "cmd_4"]

    def test_limit_applied_after_sort(self, client):
        events = self._make_events(5)
        with patch("app.state.event_audit_logger") as mock_logger:
            mock_logger.recent.return_value = events
            mock_logger.recent_count = 5

            resp = client.get(
                "/api/admin/audit/recent",
                params={"sort": "newest", "limit": 3},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["events"]) == 3
            actions = [e["action"] for e in data["events"]]
            assert actions == ["cmd_4", "cmd_3", "cmd_2"]

    def test_filter_then_sort(self, client):
        events = [
            AuditEvent(event_type="command.execute", decision=Decision.ALLOWED, action="exec_0", timestamp=0.0),
            AuditEvent(event_type="command.deny", decision=Decision.DENIED, action="deny_1", timestamp=1.0),
            AuditEvent(event_type="command.execute", decision=Decision.ALLOWED, action="exec_2", timestamp=2.0),
            AuditEvent(event_type="command.deny", decision=Decision.DENIED, action="deny_3", timestamp=3.0),
        ]
        with patch("app.state.event_audit_logger") as mock_logger:
            mock_logger.recent.return_value = events
            mock_logger.recent_count = 4

            resp = client.get(
                "/api/admin/audit/recent",
                params={"event_type": "command.deny", "sort": "newest", "limit": 1},
                headers={"X-API-Key": settings.api_key},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["events"]) == 1
            assert data["events"][0]["action"] == "deny_3"
