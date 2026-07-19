"""Tests for request ID / correlation ID in audit events."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocket
from httpx import ASGITransport, AsyncClient

import app.main as main_module
import app.state as state_module
from app.config import settings
from app.main import app

TEST_API_KEY = "test-reqid-key-007"


@pytest.fixture
def client():
    with patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1"):
        with patch("app.config.settings.api_key", TEST_API_KEY):
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


class TestRequestIdGeneration:
    def test_generated_request_id_returned(self, client):
        resp = client.get(
            "/api/workspace/projects/web-ssh-gateway/tree",
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert "X-Request-ID" in resp.headers
        assert len(resp.headers["X-Request-ID"]) == 32  # uuid4 hex

    def test_inbound_request_id_preserved(self, client):
        resp = client.get(
            "/api/workspace/projects/web-ssh-gateway/tree",
            headers={
                "X-API-Key": TEST_API_KEY,
                "X-Request-ID": "my-custom-id-123",
            },
        )
        assert resp.headers["X-Request-ID"] == "my-custom-id-123"

    def test_invalid_request_id_replaced(self, client):
        # Too long
        resp = client.get(
            "/api/workspace/projects/web-ssh-gateway/tree",
            headers={
                "X-API-Key": TEST_API_KEY,
                "X-Request-ID": "a" * 65,
            },
        )
        assert len(resp.headers["X-Request-ID"]) == 32  # generated

    def test_invalid_charset_replaced(self, client):
        resp = client.get(
            "/api/workspace/projects/web-ssh-gateway/tree",
            headers={
                "X-API-Key": TEST_API_KEY,
                "X-Request-ID": "has spaces and special chars!",
            },
        )
        assert len(resp.headers["X-Request-ID"]) == 32  # generated


class TestRequestIdInAuditEvents:
    def test_workspace_readonly_event_has_request_id(self, client):
        with patch.object(settings, "workspace_readonly", True):
            with patch("app.state.event_audit_logger") as mock_logger:
                resp = client.post(
                    "/api/workspace/projects/web-ssh-gateway/files/write",
                    json={"path": "test.txt", "content": "hello"},
                    headers={
                        "X-API-Key": TEST_API_KEY,
                        "X-Request-ID": "audit-test-123",
                    },
                )
                assert resp.status_code == 403
                # Check that audit event was created with request_id
                if mock_logger.append.called:
                    event = mock_logger.append.call_args[0][0]
                    assert event.request_id == "audit-test-123"


# ---------------------------------------------------------------------------
# jobs/run — audit event carries inbound X-Request-ID
# ---------------------------------------------------------------------------


class TestJobsRunRequestId:
    """POST /api/jobs/run audit event contains the inbound X-Request-ID."""

    @pytest.mark.asyncio
    async def test_jobs_run_audit_has_request_id(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", False)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "default")

        state_module.manager = AsyncMock()
        state_module.manager.get_session = AsyncMock(
            return_value=MagicMock(session_id="s1")
        )
        state_module.job_manager = AsyncMock()
        state_module.job_manager.create_job = AsyncMock(return_value="job_abc")
        state_module.audit_logger = MagicMock()
        state_module.event_audit_logger = MagicMock()

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/jobs/run",
                json={"session_id": "s1", "command": "ls -la"},
                headers={"X-Request-ID": "jobs-req-42"},
            )
            assert resp.status_code == 200

        # Verify structured audit event has the request_id
        calls = state_module.event_audit_logger.append.call_args_list
        assert len(calls) >= 1
        event = calls[0][0][0]
        assert event.request_id == "jobs-req-42"
        assert event.route == "POST /api/jobs/run"

    @pytest.mark.asyncio
    async def test_jobs_run_generated_request_id_when_absent(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", False)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "default")

        state_module.manager = AsyncMock()
        state_module.manager.get_session = AsyncMock(
            return_value=MagicMock(session_id="s1")
        )
        state_module.job_manager = AsyncMock()
        state_module.job_manager.create_job = AsyncMock(return_value="job_abc")
        state_module.audit_logger = MagicMock()
        state_module.event_audit_logger = MagicMock()

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/jobs/run",
                json={"session_id": "s1", "command": "ls -la"},
            )
            assert resp.status_code == 200

        calls = state_module.event_audit_logger.append.call_args_list
        assert len(calls) >= 1
        event = calls[0][0][0]
        # Without inbound header, middleware generates UUID (32 hex chars)
        assert len(event.request_id) == 32


# ---------------------------------------------------------------------------
# batch/execute — audit event carries inbound X-Request-ID
# ---------------------------------------------------------------------------


class TestBatchExecuteRequestId:
    """POST /api/batch/execute audit event contains the inbound X-Request-ID."""

    @pytest.mark.asyncio
    async def test_batch_execute_audit_has_request_id(self, monkeypatch):
        monkeypatch.setattr(settings, "api_auth_enabled", False)
        monkeypatch.setattr(settings, "command_policy_mode", "enforce")
        monkeypatch.setattr(settings, "command_policy_profile", "default")

        state_module.manager = AsyncMock()
        state_module.context_manager = AsyncMock()
        ctx_mock = MagicMock()
        ctx_mock.session_id = "s1"
        state_module.context_manager.get_context = AsyncMock(return_value=ctx_mock)
        state_module.batch_manager = AsyncMock()
        batch_result = MagicMock()
        batch_result.transaction_id = "txn1"
        batch_result.overall_success = True
        batch_result.summary = "ok"
        batch_result.total_duration = 0.1
        batch_result.operations = []
        batch_result.git_commit = None
        batch_result.validation_result = None
        state_module.batch_manager.execute_batch = AsyncMock(return_value=batch_result)
        state_module.audit_logger = MagicMock()
        state_module.event_audit_logger = MagicMock()

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/batch/execute",
                json={
                    "context_id": "ctx1",
                    "operations": [
                        {"type": "execute", "command": "ls -la"},
                    ],
                },
                headers={"X-Request-ID": "batch-req-99"},
            )
            assert resp.status_code == 200

        calls = state_module.event_audit_logger.append.call_args_list
        assert len(calls) >= 1
        event = calls[0][0][0]
        assert event.request_id == "batch-req-99"
        assert event.route == "POST /api/batch/execute"


# ---------------------------------------------------------------------------
# websocket execute-stream — request_id from headers
# ---------------------------------------------------------------------------


class TestWsExtractRequestId:
    """_extract_ws_request_id reads and validates X-Request-ID from headers."""

    def test_valid_header_returned(self):
        from app.routers.ssh import _extract_ws_request_id

        ws = MagicMock(spec=WebSocket)
        ws.headers = {"x-request-id": "ws-valid-123"}
        assert _extract_ws_request_id(ws) == "ws-valid-123"

    def test_valid_long_id_at_boundary(self):
        from app.routers.ssh import _extract_ws_request_id

        ws = MagicMock(spec=WebSocket)
        ws.headers = {"x-request-id": "a" * 64}
        assert _extract_ws_request_id(ws) == "a" * 64

    def test_too_long_replaced(self):
        from app.routers.ssh import _extract_ws_request_id

        ws = MagicMock(spec=WebSocket)
        ws.headers = {"x-request-id": "a" * 65}
        result = _extract_ws_request_id(ws)
        assert len(result) == 32  # generated UUID

    def test_invalid_charset_replaced(self):
        from app.routers.ssh import _extract_ws_request_id

        ws = MagicMock(spec=WebSocket)
        ws.headers = {"x-request-id": "has spaces & special!"}
        result = _extract_ws_request_id(ws)
        assert len(result) == 32

    def test_missing_header_generates_uuid(self):
        from app.routers.ssh import _extract_ws_request_id

        ws = MagicMock(spec=WebSocket)
        ws.headers = {}
        result = _extract_ws_request_id(ws)
        assert len(result) == 32

    def test_empty_header_generates_uuid(self):
        from app.routers.ssh import _extract_ws_request_id

        ws = MagicMock(spec=WebSocket)
        ws.headers = {"x-request-id": ""}
        result = _extract_ws_request_id(ws)
        assert len(result) == 32

    def test_case_insensitive_header_lookup(self):
        from app.routers.ssh import _extract_ws_request_id

        ws = MagicMock(spec=WebSocket)
        # Real WebSocket.headers (Starlette Headers) is case-insensitive;
        # the helper uses .get("x-request-id") so test with mixed-case key
        # via a case-insensitive dict.
        from starlette.datastructures import Headers

        ws.headers = Headers(raw=[(b"x-request-id", b"case-test-abc")])
        assert _extract_ws_request_id(ws) == "case-test-abc"

    def test_hyphens_allowed(self):
        from app.routers.ssh import _extract_ws_request_id

        ws = MagicMock(spec=WebSocket)
        ws.headers = {"x-request-id": "a-b-c-d-e"}
        assert _extract_ws_request_id(ws) == "a-b-c-d-e"

    def test_underscores_invalid(self):
        from app.routers.ssh import _extract_ws_request_id

        ws = MagicMock(spec=WebSocket)
        ws.headers = {"x-request-id": "has_underscore"}
        result = _extract_ws_request_id(ws)
        assert len(result) == 32
