"""Regression tests for the jobs.py IDOR fix.

Before this fix, GET /api/jobs, GET/POST /api/jobs/{id}/{status,result,
cancel,stream,events}, POST /api/jobs/run and POST /api/bulk/execute had
no per-owner authorization at all (only jobs_wait did) — any caller with
jobs:read/jobs:run scope could read, cancel, or live-stream any other
tenant's job output, and jobs_run/bulk_execute could execute commands
against a session_id belonging to a different owner entirely.

These tests call the router coroutines directly (bypassing FastAPI's DI
layer) with hand-built AuthIdentity objects and a mocked job_manager/
manager, so they exercise exactly the new ownership-check code added to
app/routers/jobs.py without the overhead of a full HTTP+JWT round trip.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import state as _state
from app.auth_middleware import AuthIdentity, token_fingerprint
from app.job_manager import JobRecord
from app.routers import jobs as jobs_router

OWNER_TOKEN = "agent-token-owner"
OTHER_TOKEN = "agent-token-other"


def _identity(token: str, role: str | None = None, token_type: str = "agent") -> AuthIdentity:
    return AuthIdentity(token_type=token_type, token=token, name="agent", role=role)


def _fake_request(path: str = "/api/jobs/run") -> Request:
    """Minimal real Request — slowapi's rate_limit decorator requires an
    actual starlette.requests.Request instance, not a MagicMock."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "query_string": b"",
    }
    return Request(scope)


def _owned_job(job_id: str = "j-1") -> JobRecord:
    job = JobRecord(job_id=job_id, session_id="s-1", command="echo hi")
    job.owner_id = token_fingerprint(OWNER_TOKEN)
    job.status = "completed"
    job.exit_code = 0
    return job


@pytest.fixture(autouse=True)
def _mock_job_manager(monkeypatch):
    manager = AsyncMock()
    monkeypatch.setattr(_state, "job_manager", manager)
    return manager


class TestJobsStatusResultOwnership:
    @pytest.mark.asyncio
    async def test_owner_can_read_status(self, _mock_job_manager):
        job = _owned_job()
        _mock_job_manager.get_job.return_value = job
        _mock_job_manager.get_job_status.return_value = {
            "job_id": job.job_id, "status": "completed", "progress": {}, "duration": 1.0,
        }
        result = await jobs_router.jobs_status(job.job_id, _identity(OWNER_TOKEN))
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_non_owner_gets_403_on_status(self, _mock_job_manager):
        job = _owned_job()
        _mock_job_manager.get_job.return_value = job
        with pytest.raises(HTTPException) as exc:
            await jobs_router.jobs_status(job.job_id, _identity(OTHER_TOKEN))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_non_owner_gets_403_on_result(self, _mock_job_manager):
        job = _owned_job()
        _mock_job_manager.get_job.return_value = job
        with pytest.raises(HTTPException) as exc:
            await jobs_router.jobs_result(job.job_id, None, _identity(OTHER_TOKEN))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_role_can_read_foreign_job_result(self, _mock_job_manager):
        job = _owned_job()
        _mock_job_manager.get_job.return_value = job
        _mock_job_manager.get_job_result.return_value = job.to_dict()
        result = await jobs_router.jobs_result(job.job_id, None, _identity(OTHER_TOKEN, role="admin"))
        assert result.job_id == job.job_id

    @pytest.mark.asyncio
    async def test_missing_job_is_404_not_403(self, _mock_job_manager):
        _mock_job_manager.get_job.return_value = None
        with pytest.raises(HTTPException) as exc:
            await jobs_router.jobs_status("nope", _identity(OWNER_TOKEN))
        assert exc.value.status_code == 404


class TestJobsCancelOwnership:
    @pytest.mark.asyncio
    async def test_non_owner_cannot_cancel(self, _mock_job_manager):
        job = _owned_job()
        _mock_job_manager.get_job.return_value = job
        with pytest.raises(HTTPException) as exc:
            await jobs_router.jobs_cancel(job.job_id, _identity(OTHER_TOKEN))
        assert exc.value.status_code == 403
        _mock_job_manager.cancel_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_can_cancel(self, _mock_job_manager):
        job = _owned_job()
        _mock_job_manager.get_job.return_value = job
        result = await jobs_router.jobs_cancel(job.job_id, _identity(OWNER_TOKEN))
        assert result["status"] == "cancelled"
        _mock_job_manager.cancel_job.assert_called_once_with(job.job_id)


class TestJobsListFiltering:
    @pytest.mark.asyncio
    async def test_non_admin_only_sees_own_jobs(self, _mock_job_manager):
        mine = _owned_job("j-mine")
        foreign = JobRecord(job_id="j-foreign", session_id="s-2", command="whoami")
        foreign.owner_id = token_fingerprint(OTHER_TOKEN)
        _mock_job_manager.list_jobs.return_value = [mine, foreign]

        result = await jobs_router.jobs_list(None, None, _identity(OWNER_TOKEN))
        assert result.count == 1
        assert result.jobs[0].job_id == "j-mine"

    @pytest.mark.asyncio
    async def test_admin_sees_all_jobs(self, _mock_job_manager):
        mine = _owned_job("j-mine")
        foreign = JobRecord(job_id="j-foreign", session_id="s-2", command="whoami")
        foreign.owner_id = token_fingerprint(OTHER_TOKEN)
        _mock_job_manager.list_jobs.return_value = [mine, foreign]

        result = await jobs_router.jobs_list(None, None, _identity(OWNER_TOKEN, role="admin"))
        assert result.count == 2


class TestJobsRunAndBulkExecuteSessionOwnership:
    @pytest.fixture(autouse=True)
    def _mock_manager(self, monkeypatch):
        manager = AsyncMock()
        monkeypatch.setattr(_state, "manager", manager)
        return manager

    def _foreign_session(self):
        sess = MagicMock()
        sess.owner_type = "agent"
        sess.owner_token_fingerprint = token_fingerprint(OTHER_TOKEN)
        sess.tenant_labels = ()
        return sess

    @pytest.mark.asyncio
    async def test_jobs_run_rejects_foreign_session(self, _mock_manager):
        from app.models import JobRunRequest

        _mock_manager.get_session.return_value = self._foreign_session()
        req = JobRunRequest(session_id="s-foreign", command="echo hi")
        request = _fake_request()
        with pytest.raises(HTTPException) as exc:
            await jobs_router.jobs_run(req, request, _identity(OWNER_TOKEN))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_bulk_execute_rejects_foreign_session(self, _mock_manager):
        from app.models import BulkExecuteRequest

        _mock_manager.get_session.return_value = self._foreign_session()
        req = BulkExecuteRequest(session_id="s-foreign", commands=["echo hi"])
        request = _fake_request()
        with pytest.raises(HTTPException) as exc:
            await jobs_router.bulk_execute(req, request, _identity(OWNER_TOKEN))
        assert exc.value.status_code == 403
