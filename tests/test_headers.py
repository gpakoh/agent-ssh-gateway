"""Tests for response headers: CORS, security headers, rate-limit headers."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_module
import app.state as state_module
from app.security import SECURITY_HEADERS
from app.config import settings


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "test-key")
    yield


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


# ---------------------------------------------------------------------------
# Security Headers — Present On Every Response
# ---------------------------------------------------------------------------

ENDPOINTS = [
    "/health",
    "/api/capabilities",
]


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.asyncio
async def test_security_headers_present(path):
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        r = await client.get(path)
        for header, value in SECURITY_HEADERS.items():
            assert r.headers.get(header) == value, f"Missing {header} on {path}"


@pytest.mark.asyncio
async def test_security_headers_on_error():
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        r = await client.get("/api/servers")
        for header, value in SECURITY_HEADERS.items():
            assert r.headers.get(header) == value, f"Missing {header} on 401 response"


# ---------------------------------------------------------------------------
# CORS Headers On Non-auth Endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cors_origin_on_health():
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        r = await client.options("/health", headers={
            "Origin": "https://gateway.example.com",
            "Access-Control-Request-Method": "GET",
        })
        assert r.headers.get("access-control-allow-origin") == "https://gateway.example.com"


@pytest.mark.asyncio
async def test_cors_allow_headers():
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        r = await client.options("/health", headers={
            "Origin": "https://gateway.example.com",
            "Access-Control-Request-Method": "GET",
        })
        ac_headers = r.headers.get("access-control-allow-headers", "")
        assert "Content-Type" in ac_headers
        assert "Authorization" in ac_headers
        assert "X-API-Key" in ac_headers


@pytest.mark.asyncio
async def test_cors_allow_methods():
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        r = await client.options("/health", headers={
            "Origin": "https://gateway.example.com",
            "Access-Control-Request-Method": "GET",
        })
        ac_methods = r.headers.get("access-control-allow-methods", "")
        assert "POST" in ac_methods
        assert "GET" in ac_methods
        assert "PATCH" in ac_methods


# ---------------------------------------------------------------------------
# Retry-after On Rate-limit 429
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_throttle_returns_429():
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        await client.post("/api/ssh/connect", json={
            "host": "127.0.0.1", "port": 22, "username": "test", "password": "test",
        })
        for _ in range(12):
            r = await client.post("/api/ssh/connect", json={
                "host": "127.0.0.1", "port": 22, "username": "test", "password": "test",
            })
            if r.status_code == 429:
                return
        pytest.fail("Never got 429")


# ---------------------------------------------------------------------------
# Content-type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_content_type():
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        r = await client.get("/health")
        assert r.headers.get("content-type", "").startswith("application/json")
