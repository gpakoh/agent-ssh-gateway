"""Tests for /health, /openapi.json version, and /api/sdk/download consistency."""

from starlette.testclient import TestClient

from app.config import settings
from app.main import app

FALLBACK_VERSION = "0.1.0a0"


def test_openapi_version(monkeypatch):
    """OpenAPI info.version must match the pyproject version."""
    monkeypatch.setattr(settings, "api_auth_enabled", False)
    monkeypatch.setattr(settings, "api_key", "test-key")
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["info"]["version"] == FALLBACK_VERSION


def test_health_ready_is_true():
    """The /health endpoint must report ready=True."""
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True
    assert "status" in data
    assert "redis" in data
    assert "postgres" in data


def test_sdk_download_returns_200_with_master_key(monkeypatch):
    """SDK download must return the file with an identifiable marker."""
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "secret-42")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )
    with TestClient(app) as client:
        resp = client.get(
            "/api/sdk/download",
            headers={"X-API-Key": "secret-42"},
        )
    assert resp.status_code == 200
    assert "class SSHGatewayClient" in resp.text
