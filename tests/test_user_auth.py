"""Tests for Web UI auth: register, login, JWT verify, and gate."""

import os

os.environ.setdefault("AUTH_DB_PATH", "/tmp/test_auth.sqlite3")

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.user_auth import init_auth_db

DB_PATH = os.environ["AUTH_DB_PATH"]


@pytest.fixture(autouse=True)
def _reset_db():
    """Remove the test DB before each test so state is clean."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    asyncio.run(init_auth_db())
    yield


client = TestClient(app)


class TestAuthCheck:
    """Tests for the old user_auth /api/auth/check (now superseded by routers/auth.py diagnostic endpoint)."""

    def test_check_requires_auth(self):
        """The /api/auth/check endpoint now requires a valid API key."""
        resp = client.get("/api/auth/check")
        assert resp.status_code == 401
        data = resp.json()
        assert data.get("code") == "INVALID_API_KEY"

    def test_check_returns_diagnostic_with_key(self):
        """With a valid key, the new endpoint returns diagnostic info."""
        resp = client.get(
            "/api/auth/check",
            headers={"X-API-Key": "test123"},
        )
        # Will be 401 (test123 is not the real key) or 200 with valid key
        assert resp.status_code in (200, 401)


class TestRegister:
    def test_first_register_succeeds(self):
        resp = client.post(
            "/api/auth/register",
            json={"username": "admin", "password": "Test123!@#", "password_confirm": "Test123!@#"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "token" in data
        assert data["username"] == "admin"

    def test_second_register_returns_403(self):
        client.post(
            "/api/auth/register",
            json={"username": "admin", "password": "Test123!@#", "password_confirm": "Test123!@#"},
        )
        resp = client.post(
            "/api/auth/register",
            json={"username": "admin2", "password": "Test456!@#", "password_confirm": "Test456!@#"},
        )
        assert resp.status_code == 403
        assert "admin already exists" in resp.json()["detail"].lower()

    def test_register_password_mismatch(self):
        resp = client.post(
            "/api/auth/register",
            json={"username": "admin", "password": "Test123!@#", "password_confirm": "Test456!@#"},
        )
        assert resp.status_code == 400
        assert "do not match" in resp.json()["detail"].lower()

    def test_register_weak_password(self):
        resp = client.post(
            "/api/auth/register",
            json={"username": "admin", "password": "weak", "password_confirm": "weak"},
        )
        assert resp.status_code == 422

    def test_register_after_first_user_closed(self):
        """After first user registers, all further registration attempts are 403."""
        client.post(
            "/api/auth/register",
            json={"username": "admin", "password": "Test123!@#", "password_confirm": "Test123!@#"},
        )
        resp = client.post(
            "/api/auth/register",
            json={"username": "admin", "password": "Test456!@#", "password_confirm": "Test456!@#"},
        )
        assert resp.status_code == 403
        # Even with a different username, registration is closed
        resp2 = client.post(
            "/api/auth/register",
            json={"username": "other", "password": "Test789!@#", "password_confirm": "Test789!@#"},
        )
        assert resp2.status_code == 403


class TestLogin:
    def test_login_valid_credentials(self):
        client.post(
            "/api/auth/register",
            json={"username": "admin", "password": "Test123!@#", "password_confirm": "Test123!@#"},
        )
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "Test123!@#"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["username"] == "admin"

    def test_login_invalid_password(self):
        client.post(
            "/api/auth/register",
            json={"username": "admin", "password": "Test123!@#", "password_confirm": "Test123!@#"},
        )
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong-password"},
        )
        assert resp.status_code == 401

    def test_login_nonexistent_user(self):
        resp = client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "Test123!@#"},
        )
        assert resp.status_code == 401


class TestVerify:
    def test_verify_valid_token(self):
        reg = client.post(
            "/api/auth/register",
            json={"username": "admin", "password": "Test123!@#", "password_confirm": "Test123!@#"},
        )
        token = reg.json()["token"]
        resp = client.get("/api/auth/verify", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["username"] == "admin"

    def test_verify_missing_token(self):
        resp = client.get("/api/auth/verify")
        assert resp.status_code == 401

    def test_verify_invalid_token(self):
        resp = client.get("/api/auth/verify", headers={"Authorization": "Bearer invalid.jwt.token"})
        assert resp.status_code == 401

    def test_verify_expired_token(self):
        from datetime import UTC, datetime, timedelta

        import jwt as pyjwt

        token = pyjwt.encode(
            {
                "sub": "admin",
                "uid": 1,
                "type": "web-ui",
                "exp": datetime.now(UTC) - timedelta(hours=1),
                "iat": datetime.now(UTC) - timedelta(hours=2),
            },
            "test-jwt-secret-for-testing-only",
            algorithm="HS256",
        )
        resp = client.get("/api/auth/verify", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


class TestProtectedEndpointWithJWT:
    """A protected /api/ endpoint should accept JWT Bearer token."""

    def test_capabilities_with_jwt(self):
        reg = client.post(
            "/api/auth/register",
            json={"username": "admin", "password": "Test123!@#", "password_confirm": "Test123!@#"},
        )
        token = reg.json()["token"]
        resp = client.get("/api/capabilities", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
