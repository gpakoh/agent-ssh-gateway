"""Unit tests for AgentEventStore and AgentEventEmitter."""

from app.agent_events import AgentEventEmitter, AgentEventStore


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
