"""Tests that command policy is enforced through jobs.run, bulk.execute, batch.execute.

Verifies:
- echo x>file is blocked (403) through all three paths
- harmless commands proceed (mocked execution)
- audit events are logged for both allow and deny
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_module
import app.state as state_module
from app.config import settings


@pytest.fixture(autouse=True)
def _setup_globals(monkeypatch):
    """Set up mocked state globals for all tests in this module."""
    monkeypatch.setattr(settings, "command_policy_mode", "enforce")
    monkeypatch.setattr(settings, "command_policy_profile", "default")

    state_module.manager = AsyncMock()
    state_module.manager.get_session = AsyncMock(return_value=MagicMock(session_id="s1"))
    state_module.manager.execute = AsyncMock(
        return_value={"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}
    )
    state_module.audit_logger = MagicMock()
    state_module.file_editor = AsyncMock()
    state_module.job_manager = AsyncMock()
    state_module.job_manager.create_job = AsyncMock(return_value="mock-job-id")
    state_module.bulk_ops = AsyncMock()
    state_module.bulk_ops.execute_batch_commands = AsyncMock(
        return_value=[
            {"success": True, "item": "echo hi", "result": {"stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}}
        ]
    )
    state_module.context_manager = AsyncMock()
    state_module.context_manager.get_context = AsyncMock(
        return_value=MagicMock(session_id="mock-session", path="/tmp")
    )
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

    for attr in [
        "manager", "audit_logger", "file_editor", "job_manager",
        "bulk_ops", "context_manager", "batch_manager", "server_manager",
    ]:
        try:
            delattr(state_module, attr)
        except AttributeError:
            pass


def _auth_headers():
    return {"X-API-Key": "test-key"}


# ---------------------------------------------------------------------------
# /api/jobs/run — redirect blocked
# ---------------------------------------------------------------------------


class TestJobsRunPolicyEnforcement:
    """Command policy enforcement on /api/jobs/run."""

    @pytest.mark.asyncio
    async def test_redirect_blocked(self, monkeypatch):
        """echo x>file must be denied in enforce mode."""
        monkeypatch.setattr(settings, "api_auth_enabled", False)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/jobs/run",
                json={"session_id": "s1", "command": "echo x>file"},
            )
            assert r.status_code == 403
            body = r.json()
            assert "detail" in body
            detail = body["detail"]
            assert "denied by policy" in detail.get("message", "").lower() or "command denied" in detail.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_harmless_command_allowed(self, monkeypatch):
        """A safe command must be accepted and create a job."""
        monkeypatch.setattr(settings, "api_auth_enabled", False)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/jobs/run",
                json={"session_id": "s1", "command": "ls -la"},
            )
            assert r.status_code == 200
            body = r.json()
            assert "job_id" in body
            state_module.job_manager.create_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_audit_event_logged_on_deny(self, monkeypatch):
        """Deny must produce a COMMAND_POLICY_DECISION audit event."""
        monkeypatch.setattr(settings, "api_auth_enabled", False)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/jobs/run",
                json={"session_id": "s1", "command": "echo x>file"},
            )
            calls = state_module.audit_logger.log_security_event.call_args_list
            policy_calls = [c for c in calls if "COMMAND_POLICY_DECISION" in str(c)]
            assert len(policy_calls) >= 1
            detail = policy_calls[0][0][1]
            assert "allowed=False" in detail

    @pytest.mark.asyncio
    async def test_audit_event_logged_on_allow(self, monkeypatch):
        """Allow must produce a COMMAND_POLICY_DECISION audit event with allowed=True."""
        monkeypatch.setattr(settings, "api_auth_enabled", False)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/jobs/run",
                json={"session_id": "s1", "command": "ls -la"},
            )
            calls = state_module.audit_logger.log_security_event.call_args_list
            policy_calls = [c for c in calls if "COMMAND_POLICY_DECISION" in str(c)]
            assert len(policy_calls) >= 1
            detail = policy_calls[0][0][1]
            assert "allowed=True" in detail


# ---------------------------------------------------------------------------
# /api/bulk/execute — redirect blocked
# ---------------------------------------------------------------------------


class TestBulkExecutePolicyEnforcement:
    """Command policy enforcement on /api/bulk/execute."""

    @pytest.mark.asyncio
    async def test_redirect_blocked(self, monkeypatch):
        """A list containing a redirect command must be denied."""
        monkeypatch.setattr(settings, "api_auth_enabled", False)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/bulk/execute",
                json={"session_id": "s1", "commands": ["echo x>file"]},
            )
            assert r.status_code == 403
            body = r.json()
            assert "detail" in body

    @pytest.mark.asyncio
    async def test_harmless_commands_allowed(self, monkeypatch):
        """Safe commands must be accepted and executed."""
        monkeypatch.setattr(settings, "api_auth_enabled", False)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/bulk/execute",
                json={"session_id": "s1", "commands": ["ls -la", "pwd"]},
            )
            assert r.status_code == 200
            body = r.json()
            assert "results" in body

    @pytest.mark.asyncio
    async def test_mixed_list_first_bad_denied(self, monkeypatch):
        """If any command in the list is denied, the whole request is denied."""
        monkeypatch.setattr(settings, "api_auth_enabled", False)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/bulk/execute",
                json={"session_id": "s1", "commands": ["ls", "echo x>file"]},
            )
            assert r.status_code == 403


# ---------------------------------------------------------------------------
# /api/batch/execute — execute op redirect blocked
# ---------------------------------------------------------------------------


class TestBatchExecutePolicyEnforcement:
    """Command policy enforcement on /api/batch/execute for execute-type operations."""

    @pytest.mark.asyncio
    async def test_execute_op_redirect_blocked(self, monkeypatch):
        """An execute-type operation with a redirect must be denied."""
        monkeypatch.setattr(settings, "api_auth_enabled", False)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/batch/execute",
                json={
                    "context_id": "ctx1",
                    "operations": [
                        {"type": "execute", "command": "echo x>file"}
                    ],
                },
            )
            assert r.status_code == 403
            body = r.json()
            assert "detail" in body

    @pytest.mark.asyncio
    async def test_execute_op_harmless_allowed(self, monkeypatch):
        """A safe execute-type operation must be accepted."""
        monkeypatch.setattr(settings, "api_auth_enabled", False)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/batch/execute",
                json={
                    "context_id": "ctx1",
                    "operations": [
                        {"type": "execute", "command": "ls -la"}
                    ],
                },
            )
            assert r.status_code == 200
            state_module.batch_manager.execute_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_execute_op_not_checked(self, monkeypatch):
        """Non-execute operations (read, edit) must not trigger command policy."""
        monkeypatch.setattr(settings, "api_auth_enabled", False)

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/batch/execute",
                json={
                    "context_id": "ctx1",
                    "operations": [
                        {"type": "read", "path": "/etc/hostname"}
                    ],
                },
            )
            # Should not be 403 — read ops don't go through command policy
            assert r.status_code != 403


# ---------------------------------------------------------------------------
# Audit mode — allowed but logged
# ---------------------------------------------------------------------------


class TestAuditModeLogging:
    """In audit mode, redirect commands are allowed but logged."""

    @pytest.mark.asyncio
    async def test_jobs_run_audit_mode_allows(self, monkeypatch):
        """Audit mode must allow the command but log would_allow=False."""
        monkeypatch.setattr(settings, "api_auth_enabled", False)
        monkeypatch.setattr(settings, "command_policy_mode", "audit")

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/jobs/run",
                json={"session_id": "s1", "command": "echo x>file"},
            )
            assert r.status_code == 200
            calls = state_module.audit_logger.log_security_event.call_args_list
            policy_calls = [c for c in calls if "COMMAND_POLICY_DECISION" in str(c)]
            assert len(policy_calls) >= 1
            detail = policy_calls[0][0][1]
            assert "would_allow=False" in detail
