"""Tests for error handlers: ssh_exception_handler, validation_exception_handler, _err helper."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_module
import app.state as state_module
from app.config import settings
from app.ssh_manager import (
    AuthenticationError,
    ExecutionError,
    SessionNotFoundError,
    SSHManagerError,
)
from app.ssh_manager import (
    ConnectionError as SSHConnectionError,
)
from app.ssh_manager import (
    TimeoutError as SSHTimeoutError,
)
from app.state import _err

# ---------------------------------------------------------------------------
# _err Helper
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _setup_globals():
    state_module.manager = AsyncMock()
    state_module.manager.create_session = AsyncMock(return_value="mock-session-id")
    state_module.manager.execute = AsyncMock(
        return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}
    )
    state_module.audit_logger = MagicMock()
    state_module.file_editor = AsyncMock()
    state_module.job_manager = AsyncMock()
    state_module.job_manager.create_job = AsyncMock(return_value="mock-job-id")
    state_module.bulk_ops = MagicMock()
    state_module.bulk_ops.execute_batch_commands = AsyncMock(return_value=[])
    state_module.context_manager = AsyncMock()
    state_module.context_manager.get_context = AsyncMock(return_value=MagicMock(session_id="mock-session"))
    state_module.batch_manager = AsyncMock()
    state_module.server_manager = MagicMock()
    mock_result = MagicMock()
    mock_result.transaction_id = "txn"
    mock_result.overall_success = True
    mock_result.summary = "ok"
    mock_result.total_duration = 0.0
    mock_result.operations = []
    mock_result.git_commit = ""
    mock_result.validation_result = {}
    state_module.batch_manager.execute_batch = AsyncMock(return_value=mock_result)

    yield

    for n in [
        "manager", "audit_logger", "file_editor", "job_manager",
        "bulk_ops", "context_manager", "batch_manager", "server_manager",
    ]:
        try:
            delattr(state_module, n)
        except AttributeError:
            pass


@pytest.mark.parametrize("status,keyword", [
    (502, "connection"),
    (504, "timeout"),
    (404, "not found"),
    (401, "authentication"),
    (500, "internal"),
])
def test_err_auto_code(status, keyword):
    result = _err(status, f"some {keyword} error")
    assert result["http_status"] == status
    assert isinstance(result["message"], str)
    assert isinstance(result["code"], str)
    assert isinstance(result["retryable"], bool)
    assert isinstance(result["hint"], str)


def test_err_custom_code():
    result = _err(418, "custom error", code="CUSTOM_CODE")
    assert result["code"] == "CUSTOM_CODE"
    assert result["http_status"] == 418


def test_err_all_keys_present():
    result = _err(400, "bad request")
    assert set(result.keys()) == {"message", "code", "retryable", "hint", "http_status"}


# ---------------------------------------------------------------------------
# Ssh_exception_handler — Status Map
# ---------------------------------------------------------------------------

SSH_ERROR_CASES = [
    (SSHConnectionError("connection refused"), 502),
    (AuthenticationError("auth failed"), 401),
    (SessionNotFoundError("session gone"), 404),
    (SSHTimeoutError("timed out"), 504),
    (ExecutionError("command failed"), 500),
    (SSHManagerError("generic"), 500),
]


@pytest.mark.parametrize("exc,expected_status", SSH_ERROR_CASES)
@pytest.mark.asyncio
async def test_ssh_exception_handler(exc, expected_status):
    resp = await main_module.ssh_exception_handler(None, exc)
    assert resp.status_code == expected_status
    data = json.loads(resp.body)
    assert "message" in data
    assert "code" in data
    assert "hint" in data
    assert "retryable" in data
    assert "http_status" in data


# ---------------------------------------------------------------------------
# Validation_exception_handler
# ---------------------------------------------------------------------------


@pytest.fixture
def _validation_auth(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "test-key")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


@pytest.mark.asyncio
async def test_validation_exception_handler_empty_body(_validation_auth):
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/ssh/connect",
            json={},
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert body["message"] == "Request validation failed"
        assert "detail" not in body


@pytest.mark.asyncio
async def test_validation_exception_handler_missing_field(_validation_auth):
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/ssh/connect",
            json={"host": "127.0.0.1"},
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 422
        body = r.json()
        assert "errors" in body
        assert any("username" in str(e) or "port" in str(e) for e in body["errors"])


@pytest.mark.asyncio
async def test_validation_exception_handler_invalid_type(_validation_auth):
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/ssh/connect",
            json={
                "host": "127.0.0.1", "port": "not-a-number",
                "username": "test", "password": "test",
            },
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 422
        body = r.json()
        assert "errors" in body
        assert any("port" in str(e) for e in body["errors"])
