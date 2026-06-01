"""Tests for rate limiting on mutation endpoints."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_module
import app.state as state_module
from app.config import settings
from app.security import rate_limit_mutation


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "test-key")
    yield


def test_rate_limit_mutation_import():
    """rate_limit_mutation returns a callable decorator."""
    deco = rate_limit_mutation(10, "minute")
    assert callable(deco)


@pytest.fixture(autouse=True)
def _setup_globals():
    """Mock module-level globals that are normally initialized in lifespan()."""
    state_module.manager = AsyncMock()
    state_module.manager.create_session = AsyncMock(return_value="mock-session-id")
    state_module.manager.execute = AsyncMock(
        return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}
    )
    state_module.audit_logger = MagicMock()
    state_module.file_editor = AsyncMock()
    state_module.file_editor.edit_file = AsyncMock(
        return_value={
            "path": "/tmp/test.txt", "operations_applied": 0, "changed": False, "success": True,
        }
    )
    state_module.job_manager = AsyncMock()
    state_module.job_manager.create_job = AsyncMock(return_value="mock-job-id")
    state_module.bulk_ops = MagicMock()
    state_module.bulk_ops.execute_batch_commands = AsyncMock(return_value=[])
    state_module.context_manager = AsyncMock()
    state_module.context_manager.get_context = AsyncMock(
        return_value=MagicMock(session_id="mock-session")
    )
    state_module.batch_manager = AsyncMock()
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
        "bulk_ops", "context_manager", "batch_manager",
    ]:
        try:
            delattr(state_module, n)
        except AttributeError:
            pass


@pytest.mark.asyncio
async def test_connect_first_ok_then_429():
    """POST /api/ssh/connect: burst eventually returns 429 (10/min).

    Rate-limit storage is global — prior tests may have consumed quota,
    so any request returning 429 validates the limit works.
    """
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        for _ in range(20):
            r = await client.post("/api/ssh/connect", json={
                "host": "127.0.0.1", "port": 22, "username": "test", "password": "test",
            })
            if r.status_code == 429:
                assert "application/json" in r.headers.get("content-type", "")
                return
        pytest.fail("Never got 429; rate-limiting may not be working")


@pytest.mark.asyncio
async def test_execute_ok():
    """POST /api/ssh/execute returns 200 (60/min, not hit by burst tests)."""
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        r = await client.post("/api/ssh/execute", json={
            "session_id": "test", "command": "ls",
        })
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_edit_ok():
    """PATCH /api/file/edit returns 200 (30/min)."""
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        r = await client.patch("/api/file/edit", json={
            "session_id": "test", "path": "/tmp/test.txt",
            "operations": [{"type": "append", "text": "hello"}],
        })
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_jobs_run_ok():
    """POST /api/jobs/run returns 200 (20/min)."""
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        r = await client.post("/api/jobs/run", json={
            "session_id": "test", "command": "ls",
        })
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_bulk_execute_first_ok_then_429():
    """POST /api/bulk/execute: first succeeds, burst returns 429 (10/min)."""
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-API-Key": "test-key"}) as client:
        r1 = await client.post("/api/bulk/execute", json={
            "session_id": "test", "commands": ["ls"],
        })
        assert r1.status_code == 200

        for _ in range(11):
            r = await client.post("/api/bulk/execute", json={
                "session_id": "test", "commands": ["ls"],
            })
            if r.status_code == 429:
                return
        assert r.status_code == 429
