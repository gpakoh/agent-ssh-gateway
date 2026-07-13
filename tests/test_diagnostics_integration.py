"""Integration tests verifying all P1 diagnostics components work together."""

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def _auth_and_ip(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "integ-test-key")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


def test_health_and_whoami_independent():
    """Both /health and /api/auth/whoami must work without interfering."""
    with TestClient(app) as client:
        health_resp = client.get("/health")
        whoami_resp = client.get(
            "/api/auth/whoami",
            headers={"X-API-Key": "integ-test-key"},
        )

    assert health_resp.status_code == 200
    h = health_resp.json()
    assert "build_sha" in h
    assert "version" in h

    assert whoami_resp.status_code == 200
    w = whoami_resp.json()
    assert w["identity"] == "master"
    assert w["credential_id"].startswith("ak_")
    assert "session_id" not in w


def test_whoami_scope_in_openapi():
    """The /api/auth/whoami endpoint must appear in OpenAPI schema."""
    with TestClient(app) as client:
        resp = client.get(
            "/openapi.json",
            headers={"X-API-Key": "integ-test-key"},
        )
    schema = resp.json()
    whoami_path = schema.get("paths", {}).get("/api/auth/whoami", {})
    assert "get" in whoami_path, "/api/auth/whoami not found in OpenAPI"


def test_auth_read_scope_in_valid_scopes():
    """auth:read should be in VALID_AGENT_SCOPES."""
    from app.auth_middleware import VALID_AGENT_SCOPES

    assert "auth:read" in VALID_AGENT_SCOPES
