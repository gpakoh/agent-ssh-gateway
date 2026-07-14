"""Tests for auth/session diagnostic endpoints (Phase C0)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def _auth_and_ip(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "test-key-007")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


# ---------------------------------------------------------------------------
# GET /api/auth/check
# ---------------------------------------------------------------------------


class TestAuthCheck:
    def test_valid_key_returns_200(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/check", headers={"X-API-Key": "test-key-007"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["auth_mode"] == "api_key"
        assert data["key_name"] == "master"

    def test_invalid_key_returns_401(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/check", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401
        data = resp.json()
        assert data.get("code") == "INVALID_API_KEY"
        assert "hint" in data
        assert "X-API-Key" in data["hint"]

    def test_no_key_returns_401(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/check")
        assert resp.status_code == 401
        data = resp.json()
        assert data.get("code") == "INVALID_API_KEY"
        assert "hint" in data

    def test_public_no_auth_required(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/check")
        assert resp.status_code == 401
        data = resp.json()
        assert data["code"] == "INVALID_API_KEY"

    def test_accepts_bearer_token(self):
        with TestClient(app) as client:
            resp = client.get(
                "/api/auth/check",
                headers={"Authorization": "Bearer test-key-007"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True

    def test_openapi_contains_endpoint(self):
        with TestClient(app) as client:
            resp = client.get(
                "/openapi.json",
                headers={"X-API-Key": "test-key-007"},
            )
        schema = resp.json()
        paths = schema.get("paths", {})
        assert "/api/auth/check" in paths
        assert "get" in paths["/api/auth/check"]


# ---------------------------------------------------------------------------
# POST /api/session/check
# ---------------------------------------------------------------------------


class TestSessionCheck:
    def test_missing_session_returns_not_found(self):
        with TestClient(app) as client:
            resp = client.post(
                "/api/session/check",
                headers={"X-API-Key": "test-key-007"},
                json={"session_id": "nonexistent-session-id"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert data["code"] == "SESSION_NOT_FOUND"
        assert "POST /api/ssh/connect" in data["hint"]

    def test_requires_auth(self):
        with TestClient(app) as client:
            resp = client.post(
                "/api/session/check",
                json={"session_id": "any-id"},
            )
        assert resp.status_code == 401

    def _make_mock_mgr(self, record):
        mock_mgr = MagicMock()
        mock_mgr.get_session = AsyncMock(return_value=record)
        mock_mgr.stop_cleanup_task = AsyncMock()
        mock_mgr.start_cleanup_task = AsyncMock()
        mock_mgr.list_sessions = AsyncMock(return_value=[])
        mock_mgr.disconnect = AsyncMock()
        return mock_mgr

    def _make_mock_record(self, connected=True):
        mock = MagicMock()
        mock.is_connected.return_value = connected
        return mock

    def test_live_session_returns_connected(self, monkeypatch):
        mock_mgr = self._make_mock_mgr(self._make_mock_record(connected=True))
        monkeypatch.setattr("app.main.SSHSessionManager", lambda **kw: mock_mgr)
        with TestClient(app) as client:
            resp = client.post(
                "/api/session/check",
                headers={"X-API-Key": "test-key-007"},
                json={"session_id": "live-session-42"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["session_id"] == "live-session-42"
        assert data["status"] == "connected"

    def test_disconnected_session(self, monkeypatch):
        mock_mgr = self._make_mock_mgr(self._make_mock_record(connected=False))
        monkeypatch.setattr("app.main.SSHSessionManager", lambda **kw: mock_mgr)
        with TestClient(app) as client:
            resp = client.post(
                "/api/session/check",
                headers={"X-API-Key": "test-key-007"},
                json={"session_id": "dead-session-99"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["session_id"] == "dead-session-99"
        assert data["status"] == "disconnected"

    def test_validation_error_on_empty_session_id(self):
        with TestClient(app) as client:
            resp = client.post(
                "/api/session/check",
                headers={"X-API-Key": "test-key-007"},
                json={"session_id": ""},
            )
        assert resp.status_code == 422

    def test_validation_error_on_missing_session_id(self):
        with TestClient(app) as client:
            resp = client.post(
                "/api/session/check",
                headers={"X-API-Key": "test-key-007"},
                json={},
            )
        assert resp.status_code == 422
        data = resp.json()
        assert "code" in data
        assert data["code"] == "VALIDATION_ERROR"
