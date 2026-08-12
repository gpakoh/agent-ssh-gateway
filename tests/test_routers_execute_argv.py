"""Tests for POST /api/ssh/execute-argv endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.command_gate import CommandGateDecision


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


def test_execute_argv_total_limit_rejected(client, monkeypatch):
    # Per-arg 255 cap removed (compileall fallback passes a large -c script);
    # the 65536-byte total limit still applies.
    _setup_test(monkeypatch)
    resp = client.post(
        "/api/ssh/execute-argv",
        json={"session_id": "x", "argv": ["a" * 40000, "b" * 40000]},
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

    with patch("app.routers.ssh.evaluate_with_access_gate") as mock_gate:
        mock_gate.return_value = CommandGateDecision(
            allowed=False,
            reason="denied",
            command_root="rm",
            effective_profile="default",
            policy_mode="enforce",
        )
        resp = client.post(
            "/api/ssh/execute-argv",
            json={"session_id": "sid", "argv": ["rm", "-rf", "/"]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 403


def test_execute_argv_safe_command_passes_literal_argv_without_shell_expansion(client, monkeypatch):
    _app_state = _setup_test(monkeypatch)

    mock_session = MagicMock()
    mock_session.owner_type = "master"
    mock_session.owner_token_fingerprint = None
    _app_state.manager.get_session = AsyncMock(return_value=mock_session)
    _app_state.manager.execute_argv = AsyncMock(
        return_value={"stdout": "$(id)", "stderr": "", "exit_code": 0, "duration": 0.01}
    )

    with patch("app.routers.ssh.evaluate_with_access_gate") as mock_gate:
        mock_gate.return_value = CommandGateDecision(
            allowed=True,
            reason="allowed",
            command_root="printf",
            effective_profile="default",
            policy_mode="enforce",
        )
        resp = client.post(
            "/api/ssh/execute-argv",
            json={"session_id": "sid", "argv": ["printf", "%s", "$(id)"]},
            headers=_auth_headers(),
        )

    assert resp.status_code == 200
    mock_gate.assert_called_once()
    assert mock_gate.call_args.args[2] == "printf %s '$(id)'"
    _app_state.manager.execute_argv.assert_awaited_once_with(
        session_id="sid",
        command_str="printf %s '$(id)'",
        stdin_data=b"",
        timeout=30,
    )
    assert resp.json()["stdout"] == "$(id)"
