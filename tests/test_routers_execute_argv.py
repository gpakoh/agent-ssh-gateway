"""Tests for POST /api/ssh/execute-argv endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _auth_headers():
    return {"X-API-Key": settings.api_key}


def _setup_test(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "secret-42")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
    from app import state as _app_state

    _app_state.audit_logger = MagicMock()
    _app_state.manager = MagicMock()
    return _app_state


def test_execute_argv_requires_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
    resp = client.post(
        "/api/ssh/execute-argv",
        json={"session_id": "x", "argv": ["ls"]},
    )
    assert resp.status_code == 401


def test_execute_argv_empty_argv_rejected(client, monkeypatch):
    _setup_test(monkeypatch)
    resp = client.post(
        "/api/ssh/execute-argv",
        json={"session_id": "x", "argv": []},
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


def test_execute_argv_arg_too_long_rejected(client, monkeypatch):
    _setup_test(monkeypatch)
    resp = client.post(
        "/api/ssh/execute-argv",
        json={"session_id": "x", "argv": ["x" * 256]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


def test_execute_argv_timeout_bounds_rejected(client, monkeypatch):
    _setup_test(monkeypatch)
    resp = client.post(
        "/api/ssh/execute-argv",
        json={"session_id": "x", "argv": ["ls"], "timeout_s": 0},
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


def test_execute_argv_session_not_found(client, monkeypatch):
    _setup_test(monkeypatch)
    _app_state = _setup_test(monkeypatch)
    _app_state.manager.get_session = AsyncMock(return_value=None)
    resp = client.post(
        "/api/ssh/execute-argv",
        json={"session_id": "nonexistent", "argv": ["ls"]},
        headers=_auth_headers(),
    )
    assert resp.status_code == 404


def test_execute_argv_command_policy_denied(client, monkeypatch):
    _setup_test(monkeypatch)
    _app_state = _setup_test(monkeypatch)

    mock_session = MagicMock()
    mock_session.owner_type = "master"
    mock_session.owner_token_fingerprint = None
    _app_state.manager.get_session = AsyncMock(return_value=mock_session)

    with patch("app.routers.ssh.evaluate_command_policy") as mock_policy:
        mock_policy.return_value = MagicMock(
            allowed=False, reason="denied", profile="default", mode="enforce", command_root="rm"
        )
        resp = client.post(
            "/api/ssh/execute-argv",
            json={"session_id": "sid", "argv": ["rm", "-rf", "/"]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 403
