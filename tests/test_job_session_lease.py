from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.job_manager import JobManager
from app.ssh_manager import SessionRecord, SSHSessionManager


def _record(*, sid: str = "sid") -> SessionRecord:
    client = MagicMock()
    client.get_transport.return_value.is_active.return_value = True

    stdin = MagicMock()
    stdin.channel = MagicMock()
    stdout = MagicMock()
    stdout.channel = MagicMock()
    stderr = MagicMock()
    stderr.channel = MagicMock()
    stdout.channel.exit_status_ready.return_value = True
    stdout.channel.recv_ready.return_value = False
    stdout.channel.recv_exit_status.return_value = 0
    stderr.channel.recv_stderr_ready.return_value = False
    client.exec_command.return_value = (stdin, stdout, stderr)

    return SessionRecord(
        session_id=sid,
        client=client,
        host="target.invalid",
        port=22,
        username="tester",
        source_ip="172.19.0.17",
        effective_idle_timeout=300,
        ephemeral=True,
    )


def _ssh_manager(record: SessionRecord) -> SSHSessionManager:
    manager = SSHSessionManager.__new__(SSHSessionManager)
    manager._sessions = {record.session_id: record}
    manager._pending_sessions_by_ip = {}
    manager._lock = asyncio.Lock()
    manager._session_timeout = 3600
    manager._pool = None
    manager._circuit_breakers = None
    manager._secret_manager = None
    manager._host_key_store = None
    manager._strict_host_key = False
    return manager


@pytest.mark.asyncio
async def test_09_async_job_ack_pins_session_through_prestart_reaper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TEST-09: successful job acceptance protects the SID before execute_stream."""
    record = _record()
    ssh = _ssh_manager(record)
    jobs = JobManager(ssh_manager=ssh, max_jobs=10)
    prestart_entered = asyncio.Event()
    allow_start = asyncio.Event()

    async def blocked_emit(
        _job: Any,
        event_type: str,
        _payload: dict[str, Any] | None = None,
        *,
        budget: float,
    ) -> None:
        assert budget > 0
        if event_type == "started":
            prestart_entered.set()
            await allow_start.wait()

    monkeypatch.setattr(jobs, "_emit_bounded", blocked_emit)

    job_id = await jobs.create_job(record.session_id, "true", owner_id="owner-a", timeout=5)
    task = jobs._job_tasks[job_id]
    await asyncio.wait_for(prestart_entered.wait(), timeout=1)

    # The HTTP handler could already have returned job_id here. The worker has
    # not entered execute_stream yet, but the accepted-job lease must pin SID.
    assert record.active_operations == 1
    record.last_activity = time.time() - 600
    assert await ssh.cleanup_stale_sessions() == 0
    assert ssh._sessions[record.session_id] is record
    record.client.close.assert_not_called()

    allow_start.set()
    await asyncio.wait_for(task, timeout=1)
    job = await jobs.get_job(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.exit_code == 0
    assert record.active_operations == 0

    # Completion released the acceptance lease; ordinary idle reaping resumes.
    record.last_activity = time.time() - 600
    assert await ssh.cleanup_stale_sessions() == 1
    assert record.session_id not in ssh._sessions


@pytest.mark.asyncio
async def test_async_job_prestart_exception_releases_session_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    ssh = _ssh_manager(record)
    jobs = JobManager(ssh_manager=ssh, max_jobs=10)

    async def fail_started_emit(
        _job: Any,
        event_type: str,
        _payload: dict[str, Any] | None = None,
        *,
        budget: float,
    ) -> None:
        assert budget > 0
        if event_type == "started":
            raise RuntimeError("prestart boom")

    monkeypatch.setattr(jobs, "_emit_bounded", fail_started_emit)
    job_id = await jobs.create_job(record.session_id, "true", owner_id="owner-a")
    task = jobs._job_tasks[job_id]

    with pytest.raises(RuntimeError, match="prestart boom"):
        await task
    assert record.active_operations == 0
    assert job_id not in jobs._session_leases


@pytest.mark.asyncio
async def test_async_job_task_cancellation_before_remote_start_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    ssh = _ssh_manager(record)
    jobs = JobManager(ssh_manager=ssh, max_jobs=10)
    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocked_impl(_job_id: str) -> None:
        entered.set()
        await never.wait()

    monkeypatch.setattr(jobs, "_run_job_impl", blocked_impl)
    job_id = await jobs.create_job(record.session_id, "true", owner_id="owner-a")
    task = jobs._job_tasks[job_id]
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert record.active_operations == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert record.active_operations == 0
    assert job_id not in jobs._session_leases
    record.client.exec_command.assert_not_called()


@pytest.mark.asyncio
async def test_async_job_claim_failure_releases_lease_without_remote_start() -> None:
    record = _record()
    ssh = _ssh_manager(record)
    redis = AsyncMock()
    redis._redis = object()
    redis.find_submission.return_value = None

    async def reserve(_submission_key: str, *, job_id: str, **_kwargs: Any) -> tuple[str, bool]:
        return job_id, True

    redis.reserve_submission_with_job.side_effect = reserve
    redis.claim_durable_execution.return_value = False
    jobs = JobManager(ssh_manager=ssh, max_jobs=10, redis_queue=redis)

    job_id = await jobs.create_job(
        record.session_id,
        "true",
        owner_id="owner-a",
        submission_key="stable-key",
    )
    task = jobs._job_tasks[job_id]
    await asyncio.wait_for(task, timeout=1)

    assert record.active_operations == 0
    assert job_id not in jobs._session_leases
    record.client.exec_command.assert_not_called()
    redis.claim_durable_execution.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_durable_submission_does_not_add_second_session_lease() -> None:
    record = _record()
    ssh = _ssh_manager(record)
    # Model the original accepted execution's pin. The duplicate request may
    # provisionally acquire a lease after its first lookup races, but must give
    # it back when the atomic reservation reports the existing job.
    record.active_operations = 1
    redis = AsyncMock()
    redis.find_submission.return_value = None
    redis._redis = object()
    redis.reserve_submission_with_job.return_value = ("existing-job", False)
    jobs = JobManager(ssh_manager=ssh, max_jobs=10, redis_queue=redis)

    result = await jobs.create_job(
        record.session_id,
        "true",
        owner_id="owner-a",
        submission_key="stable-key",
    )

    assert result == "existing-job"
    assert record.active_operations == 1
    assert jobs._session_leases == {}
    assert jobs._job_tasks == {}


@pytest.mark.asyncio
async def test_force_cleanup_releases_accepted_job_lease_even_if_task_never_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    ssh = _ssh_manager(record)
    jobs = JobManager(ssh_manager=ssh, max_jobs=10)
    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocked_impl(_job_id: str) -> None:
        entered.set()
        await never.wait()

    monkeypatch.setattr(jobs, "_run_job_impl", blocked_impl)
    job_id = await jobs.create_job(record.session_id, "true", owner_id="owner-a")
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert record.active_operations == 1

    assert await jobs.force_cleanup() == 1
    assert record.active_operations == 0
    assert job_id not in jobs._session_leases
