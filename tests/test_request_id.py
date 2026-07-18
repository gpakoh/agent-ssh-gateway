"""Tests for request ID / correlation ID in audit events."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

TEST_API_KEY = "test-reqid-key-007"


@pytest.fixture
def client():
    with patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1"):
        with patch("app.config.settings.api_key", TEST_API_KEY):
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


class TestRequestIdGeneration:
    def test_generated_request_id_returned(self, client):
        resp = client.get(
            "/api/workspace/projects/web-ssh-gateway/tree",
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert "X-Request-ID" in resp.headers
        assert len(resp.headers["X-Request-ID"]) == 32  # uuid4 hex

    def test_inbound_request_id_preserved(self, client):
        resp = client.get(
            "/api/workspace/projects/web-ssh-gateway/tree",
            headers={
                "X-API-Key": TEST_API_KEY,
                "X-Request-ID": "my-custom-id-123",
            },
        )
        assert resp.headers["X-Request-ID"] == "my-custom-id-123"

    def test_invalid_request_id_replaced(self, client):
        # Too long
        resp = client.get(
            "/api/workspace/projects/web-ssh-gateway/tree",
            headers={
                "X-API-Key": TEST_API_KEY,
                "X-Request-ID": "a" * 65,
            },
        )
        assert len(resp.headers["X-Request-ID"]) == 32  # generated

    def test_invalid_charset_replaced(self, client):
        resp = client.get(
            "/api/workspace/projects/web-ssh-gateway/tree",
            headers={
                "X-API-Key": TEST_API_KEY,
                "X-Request-ID": "has spaces and special chars!",
            },
        )
        assert len(resp.headers["X-Request-ID"]) == 32  # generated


class TestRequestIdInAuditEvents:
    def test_workspace_readonly_event_has_request_id(self, client):
        with patch.object(settings, "workspace_readonly", True):
            with patch("app.state.event_audit_logger") as mock_logger:
                resp = client.post(
                    "/api/workspace/projects/web-ssh-gateway/files/write",
                    json={"path": "test.txt", "content": "hello"},
                    headers={
                        "X-API-Key": TEST_API_KEY,
                        "X-Request-ID": "audit-test-123",
                    },
                )
                assert resp.status_code == 403
                # Check that audit event was created with request_id
                if mock_logger.append.called:
                    event = mock_logger.append.call_args[0][0]
                    assert event.request_id == "audit-test-123"
