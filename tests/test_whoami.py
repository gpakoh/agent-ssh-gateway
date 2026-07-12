"""Tests for GET /api/auth/whoami endpoint."""

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def _auth_settings(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "master-test-key-42")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(settings, "agent_token", "")
    monkeypatch.setattr(settings, "agent_token_scopes", [])
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


class TestWhoamiMasterKey:
    def test_master_key_returns_identity(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/whoami", headers=_headers("master-test-key-42"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["identity"] == "master"
        assert data["auth_method"] == "api_key"
        assert data["credential_id"].startswith("ak_")
        assert len(data["credential_id"]) == 11  # "ak_" + 8 hex chars
        assert "*" in data["scopes"]

    def test_master_key_no_session_id(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/whoami", headers=_headers("master-test-key-42"))
        data = resp.json()
        assert "session_id" not in data


class TestWhoamiUnauthorized:
    def test_no_key_returns_401(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/whoami")
        assert resp.status_code == 401


class TestWhoamiInvalidKey:
    def test_invalid_key_returns_401(self):
        with TestClient(app) as client:
            resp = client.get("/api/auth/whoami", headers=_headers("wrong-key"))
        assert resp.status_code == 401
