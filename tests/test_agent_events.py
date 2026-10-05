"""Unit tests for AgentEventStore and AgentEventEmitter."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.agent_events import AgentEventEmitter, AgentEventStore
from app.auth_middleware import AuthIdentity, token_fingerprint
from app.job_manager import JobManager


class TestAgentEventStore:
    def test_emit_returns_event(self):
        store = AgentEventStore()
        ev = store.emit("j1", "agent1", "started", {"command": "ls"})
        assert ev.event_type == "started"
        assert ev.job_id == "j1"
        assert ev.agent_id == "agent1"
        assert ev.payload == {"command": "ls"}
        assert ev.id

    def test_get_events_returns_oldest_first(self):
        store = AgentEventStore()
        store.emit("j1", "a", "started", {})
        store.emit("j1", "a", "heartbeat", {})
        store.emit("j1", "a", "completed", {})
        events = store.get_events("j1")
        assert [e.event_type for e in events] == ["started", "heartbeat", "completed"]

    def test_get_events_empty_for_unknown_job(self):
        store = AgentEventStore()
        assert store.get_events("nonexistent") == []

    def test_get_latest(self):
        store = AgentEventStore()
        store.emit("j1", "a", "started", {})
        latest = store.emit("j1", "a", "completed", {})
        assert store.get_latest("j1") is latest

    def test_get_latest_none_for_unknown(self):
        store = AgentEventStore()
        assert store.get_latest("nonexistent") is None

    def test_bounded_eviction(self):
        store = AgentEventStore(max_events_per_job=3)
        for i in range(5):
            store.emit("j1", "a", "progress", {"i": i})
        events = store.get_events("j1")
        assert len(events) == 3
        assert [e.payload["i"] for e in events] == [2, 3, 4]

    def test_isolation_between_jobs(self):
        store = AgentEventStore()
        store.emit("j1", "a", "started", {})
        store.emit("j2", "b", "started", {})
        assert len(store.get_events("j1")) == 1
        assert len(store.get_events("j2")) == 1
        assert store.get_events("j1")[0].agent_id == "a"
        assert store.get_events("j2")[0].agent_id == "b"

    def test_to_dict(self):
        store = AgentEventStore()
        ev = store.emit("j1", "a", "started", {"k": "v"})
        d = ev.to_dict()
        assert d["type"] == "started"
        assert d["job_id"] == "j1"
        assert d["payload"] == {"k": "v"}
        assert "id" in d
        assert "timestamp" in d


class TestAgentEventEmitter:
    def test_emit_wraps_store(self):
        emitter = AgentEventEmitter()
        ev = emitter.emit("j1", "a", "started", {})
        assert ev.event_type == "started"
        assert emitter.store.get_latest("j1") is ev

    def test_emitter_uses_injected_store(self):
        store = AgentEventStore(max_events_per_job=5)
        emitter = AgentEventEmitter(store=store)
        emitter.emit("j1", "a", "started", {})
        assert len(store.get_events("j1")) == 1


def _stream(events, delay: float = 0.0):
    async def execute_stream(*args, **kwargs):
        if delay:
            await asyncio.sleep(delay)
        for event in events:
            yield event

    return execute_stream


def _failing_stream():
    async def execute_stream(*args, **kwargs):
        raise RuntimeError("boom")
        yield ("exit", "1")  # pragma: no cover

    return execute_stream


def _durable_queue():
    queue = AsyncMock()
    queue._redis = MagicMock()

    async def _reserve(submission_key, *, job_id, **kwargs):
        return job_id, True

    queue.find_submission = AsyncMock(return_value=None)
    queue.reserve_submission_with_job = AsyncMock(side_effect=_reserve)
    queue.claim_durable_execution = AsyncMock(return_value=True)
    queue.heartbeat_durable_execution = AsyncMock(return_value=True)
    queue.is_durable_cancellation_requested = AsyncMock(return_value=False)
    queue.finish_durable_execution = AsyncMock(return_value=True)
    return queue


def _job_manager(execute_stream, emitter, redis_queue=None):
    ssh = AsyncMock()
    ssh.execute_stream = execute_stream
    return JobManager(
        ssh_manager=ssh,
        max_jobs=10,
        redis_queue=redis_queue,
        event_emitter=emitter,
    )


class TestJobManagerEventLifecycle:
    @pytest.mark.asyncio
    async def test_success_lifecycle_started_heartbeat_completed(self, monkeypatch):
        """create job -> started -> heartbeat(s) while running -> completed."""
        monkeypatch.setattr("app.state.agent_event_emitter", None)
        monkeypatch.setattr("app.job_manager.DURABLE_LEASE_TTL_SECONDS", 0.3)
        emitter = AgentEventEmitter()
        jm = _job_manager(
            _stream([("stdout", "hi\n"), ("exit", "0")], delay=0.25),
            emitter,
            redis_queue=_durable_queue(),
        )

        job_id = await jm.create_job("s1", "echo hi", owner_id="fp-a", submission_key="task:t:1")
        job_task = jm._job_tasks[job_id]
        # completed_event is set before terminal observability emission. This
        # lifecycle test asserts the terminal event itself, so synchronize on
        # the actual job task, which finishes only after lifecycle emission.
        await asyncio.wait_for(job_task, timeout=30)

        events = emitter.store.get_events(job_id)
        types = [e.event_type for e in events]
        assert types[0] == "started"
        assert "heartbeat" in types
        assert types[-1] == "completed"
        assert all(e.agent_id == "fp-a" for e in events)
        started = events[0]
        assert started.payload["session_id"] == "s1"
        completed = events[-1]
        assert completed.payload["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_unkeyed_job_emits_started_and_completed_without_heartbeat(self):
        emitter = AgentEventEmitter()
        jm = _job_manager(_stream([("stdout", "hi\n"), ("exit", "0")]), emitter)

        job_id = await jm.create_job("s1", "echo hi", owner_id="fp-a")
        job = await jm.get_job(job_id)
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)

        types = [e.event_type for e in emitter.store.get_events(job_id)]
        assert types == ["started", "completed"]

    @pytest.mark.asyncio
    async def test_execution_error_emits_failed(self):
        emitter = AgentEventEmitter()
        jm = _job_manager(_failing_stream(), emitter)

        job_id = await jm.create_job("s1", "echo hi", owner_id="fp-a")
        job = await jm.get_job(job_id)
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)

        events = emitter.store.get_events(job_id)
        types = [e.event_type for e in events]
        assert types[0] == "started"
        assert types[-1] == "failed"
        failed = events[-1]
        assert failed.payload["status"] == "failed"
        assert "boom" in str(failed.payload["error"])

    @pytest.mark.asyncio
    async def test_nonzero_exit_emits_failed_with_exit_code(self):
        emitter = AgentEventEmitter()
        jm = _job_manager(_stream([("stderr", "err\n"), ("exit", "2")]), emitter)

        job_id = await jm.create_job("s1", "false", owner_id="fp-a")
        job = await jm.get_job(job_id)
        await asyncio.wait_for(job.completed_event.wait(), timeout=5)

        events = emitter.store.get_events(job_id)
        assert events[-1].event_type == "failed"
        assert events[-1].payload["exit_code"] == 2


class TestAgentEventsEndpoint:
    OWNER = "owner-token-1"

    def _client(self, monkeypatch):
        from starlette.testclient import TestClient

        from app import state as _app_state
        from app.config import settings
        from app.main import app

        _app_state.job_manager = AsyncMock()
        _app_state.redis_queue = None
        _app_state.audit_logger = MagicMock()
        _app_state.manager = AsyncMock()
        monkeypatch.setattr(settings, "api_auth_enabled", True)
        monkeypatch.setattr(settings, "api_key", "secret-42")
        monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0,::1/128")
        monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
        monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")
        return TestClient(app, raise_server_exceptions=False)

    @pytest.fixture(autouse=True)
    def _fresh_singleton(self):
        from app import agent_events as agent_events_module

        agent_events_module.agent_events = AgentEventEmitter()
        yield
        agent_events_module.agent_events = AgentEventEmitter()

    def test_returns_event_history_for_known_job(self, monkeypatch):
        from app import agent_events as agent_events_module
        from app import state as _app_state

        client = self._client(monkeypatch)
        job = MagicMock(owner_id="user:admin")
        job.status = "completed"
        _app_state.job_manager.get_job = AsyncMock(return_value=job)
        agent_events_module.agent_events.emit("job-ev-1", "fp-a", "started", {"session_id": "s1"})
        agent_events_module.agent_events.emit("job-ev-1", "fp-a", "completed", {"exit_code": 0})

        resp = client.get(
            "/api/agents/job-ev-1/events",
            headers={"X-API-Key": "secret-42"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "job-ev-1"
        assert data["count"] == 2
        assert [e["type"] for e in data["events"]] == ["started", "completed"]

    def test_limit_returns_last_n_events(self, monkeypatch):
        from app import agent_events as agent_events_module
        from app import state as _app_state

        client = self._client(monkeypatch)
        job = MagicMock(owner_id="user:admin")
        job.status = "running"
        _app_state.job_manager.get_job = AsyncMock(return_value=job)
        for i in range(5):
            agent_events_module.agent_events.emit("job-lim", "fp-a", "heartbeat", {"i": i})

        resp = client.get(
            "/api/agents/job-lim/events?limit=2",
            headers={"X-API-Key": "secret-42"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert [e["payload"]["i"] for e in data["events"]] == [3, 4]

    def test_unknown_job_returns_404(self, monkeypatch):
        from app import state as _app_state

        client = self._client(monkeypatch)
        _app_state.job_manager.get_job = AsyncMock(return_value=None)

        resp = client.get(
            "/api/agents/job-missing/events",
            headers={"X-API-Key": "secret-42"},
        )

        assert resp.status_code == 404

    def test_no_auth_returns_401(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/api/agents/job-ev-1/events")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_owner_gets_403(self, monkeypatch):
        from unittest.mock import patch

        from app import state as _app_state
        from app.agent_events import AgentEventEmitter
        from app.routers.agents import agent_events_history

        emitter = AgentEventEmitter()
        monkeypatch.setattr("app.routers.agents.list_agent_events", emitter.store.get_events)
        job = MagicMock()
        job.owner_id = token_fingerprint(self.OWNER)
        _app_state.job_manager = AsyncMock()
        _app_state.job_manager.get_job = AsyncMock(return_value=job)

        other = AuthIdentity(
            token_type="agent",
            token="other-token",
            name="other",
            scopes=["jobs:read"],
        )
        with patch.object(_app_state, "job_manager", _app_state.job_manager):
            with pytest.raises(HTTPException) as exc_info:
                await agent_events_history("job-x", other)

        assert exc_info.value.status_code == 403
