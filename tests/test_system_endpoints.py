"""Tests for /health, /openapi.json version, /api/sdk/download, and known-hosts endpoints."""

from starlette.testclient import TestClient

from app.config import settings
from app.main import app
from app.version import APP_VERSION


def test_openapi_version(monkeypatch):
    """OpenAPI info.version must match the pyproject version."""
    monkeypatch.setattr(settings, "api_auth_enabled", False)
    monkeypatch.setattr(settings, "api_key", "test-key")
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["info"]["version"] == APP_VERSION


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
    monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
    with TestClient(app) as client:
        resp = client.get(
            "/api/sdk/download",
            headers={"X-API-Key": "secret-42"},
        )
    assert resp.status_code == 200
    assert "class SSHGatewayClient" in resp.text


def test_known_hosts_check_unknown(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", False)
    with TestClient(app) as client:
        resp = client.get("/api/known-hosts/check?host=unknown.test&port=22")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unknown"
    assert data["host"] == "unknown.test"
    assert data["port"] == 22


def test_known_hosts_lookup_404(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", False)
    with TestClient(app) as client:
        resp = client.get("/api/known-hosts/unknown.test?port=22")
    assert resp.status_code == 404


def test_known_hosts_delete_with_port_404(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", False)
    with TestClient(app) as client:
        resp = client.delete("/api/known-hosts/unknown.test?port=22")
    assert resp.status_code == 404


def test_known_hosts_clear_all(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", False)
    with TestClient(app) as client:
        resp = client.delete("/api/known-hosts")
    assert resp.status_code == 200
    data = resp.json()
    assert "deleted" in data
