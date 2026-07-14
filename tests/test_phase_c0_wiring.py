"""Agent 4 — integration smoke: verify routing, help, no secret leaks."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def _bypass(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "smoke-key")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


HEADERS = {"X-API-Key": "smoke-key"}


class TestRoutingWiring:
    """Endpoints are reachable through the app router stack."""

    def test_auth_check_reachable(self):
        with TestClient(app) as c:
            resp = c.get("/api/auth/check", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["valid"] is True

    def test_session_check_reachable(self):
        with TestClient(app) as c:
            resp = c.post(
                "/api/session/check",
                headers=HEADERS,
                json={"session_id": "nonexistent"},
            )
        assert resp.status_code == 200
        assert resp.json()["valid"] is False

    def test_auth_check_in_openapi(self):
        with TestClient(app) as c:
            schema = c.get("/openapi.json", headers=HEADERS).json()
        assert "/api/auth/check" in schema["paths"]
        assert "get" in schema["paths"]["/api/auth/check"]

    def test_session_check_in_openapi(self):
        with TestClient(app) as c:
            schema = c.get("/openapi.json", headers=HEADERS).json()
        assert "/api/session/check" in schema["paths"]
        assert "post" in schema["paths"]["/api/session/check"]


class TestHelpDiscoverability:
    """Endpoints appear in /api/help response."""

    def _all_help_paths(self, help_data: dict) -> list[str]:
        paths: list[str] = []
        for _key, val in help_data.items():
            if isinstance(val, list):
                paths.extend(ep.get("path", "") for ep in val if isinstance(ep, dict))
            elif isinstance(val, dict):
                for sub_val in val.values():
                    if isinstance(sub_val, list):
                        paths.extend(
                            ep.get("path", "") for ep in sub_val if isinstance(ep, dict)
                        )
        return paths

    def test_auth_check_in_help(self):
        with TestClient(app) as c:
            resp = c.get("/api/help", headers=HEADERS)
        assert "/api/auth/check" in self._all_help_paths(resp.json())

    def test_session_check_in_help(self):
        with TestClient(app) as c:
            resp = c.get("/api/help", headers=HEADERS)
        assert "/api/session/check" in self._all_help_paths(resp.json())


class TestSecurityNoLeaks:
    """Invalid auth returns 401, no secret material in responses."""

    def test_invalid_key_401(self):
        with TestClient(app) as c:
            resp = c.get("/api/auth/check", headers={"X-API-Key": "garbage"})
        assert resp.status_code == 401
        body = resp.json()
        assert body.get("code") == "INVALID_API_KEY"
        assert "smoke-key" not in resp.text

    def test_no_key_401(self):
        with TestClient(app) as c:
            resp = c.get("/api/auth/check")
        assert resp.status_code == 401

    def test_valid_key_no_secret_in_response(self):
        with TestClient(app) as c:
            resp = c.get("/api/auth/check", headers=HEADERS)
        body = resp.json()
        assert "smoke-key" not in str(body)
        assert body["valid"] is True
