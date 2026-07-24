"""Tests for admin access-control decision endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.state as _state
from app.config import settings
from app.ssh_manager import SSHSessionManager


def _headers(api_key: str = "test-api-key-12345") -> dict[str, str]:
    return {"X-API-Key": api_key}


def _get_app():
    from app.main import app
    return app


def _patch_auth(monkeypatch):
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "")
    monkeypatch.setattr("app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1")


def _client(monkeypatch):
    """TestClient with IP allowlist bypassed and get_client_ip patched."""
    _patch_auth(monkeypatch)
    return TestClient(_get_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAdminEndpointAuth:
    def test_rejects_unauthenticated(self, monkeypatch):
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/access-control/decision",
                json={
                    "actor_fingerprint": "abc123",
                    "source_ip": "10.0.0.1",
                    "decision": "allow",
                },
            )
        assert resp.status_code == 401

    def test_rejects_agent_token(self, monkeypatch):
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/access-control/decision",
                json={
                    "actor_fingerprint": "abc123",
                    "source_ip": "10.0.0.1",
                    "decision": "allow",
                },
                headers={"X-API-Key": "test-agent-token-12345"},
            )
        assert resp.status_code == 401

    def test_master_can_set_allow(self, monkeypatch):
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/access-control/decision",
                json={
                    "actor_fingerprint": "fp_abc",
                    "source_ip": "10.0.0.99",
                    "decision": "allow",
                    "reason": "approved by admin",
                },
                headers=_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "allow"
        assert body["effective_now"] is True
        assert body["decision_id"].startswith("dec_")
        assert "key_hash" in body
        assert body["expires_at"] > 0

    def test_deny_response_structure(self, monkeypatch):
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/access-control/decision",
                json={
                    "actor_fingerprint": "fp_bad",
                    "source_ip": "10.0.0.50",
                    "decision": "deny",
                    "reason": "violation",
                },
                headers=_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "deny"
        assert body["effective_now"] is True
        assert body["decision_id"].startswith("dec_")

    def test_invalid_decision_value(self, monkeypatch):
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/access-control/decision",
                json={
                    "actor_fingerprint": "fp",
                    "source_ip": "10.0.0.1",
                    "decision": "invalid",
                },
                headers=_headers(),
            )
        assert resp.status_code == 422

    def test_custom_ttl(self, monkeypatch):
        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            resp = c.post(
                "/api/admin/access-control/decision",
                json={
                    "actor_fingerprint": "fp_ttl",
                    "source_ip": "10.0.0.1",
                    "decision": "allow",
                    "ttl_seconds": 120,
                },
                headers=_headers(),
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["expires_at"] > 0
            entry = _state.access_control_store.get("fp_ttl", "10.0.0.1")
            assert entry is not None
            assert entry.decision == "allowed"

    def test_decision_persists_in_store(self, monkeypatch):
        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            c.post(
                "/api/admin/access-control/decision",
                json={
                    "actor_fingerprint": "fp_persist",
                    "source_ip": "10.0.0.1",
                    "decision": "allow",
                },
                headers=_headers(),
            )
            entry = _state.access_control_store.get("fp_persist", "10.0.0.1")
            assert entry is not None
            assert entry.decision == "allowed"
            assert entry.decided_by == "operator"

    def test_deny_kills_matching_sessions(self, monkeypatch):
        """Deny decision should disconnect sessions matching actor+IP."""
        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            manager = _state.manager
            mock_client = MagicMock()
            manager._sessions["sess-1"] = MagicMock(
                session_id="sess-1",
                client=mock_client,
                host="10.0.0.5",
                port=22,
                username="root",
                owner_token_fingerprint="fp_kill",
                source_ip="10.0.0.5",
                owner_type="master",
                owner_name=None,
                connected_at=0,
                last_activity=0,
                reconnect_count=0,
                last_reconnect_reason=None,
            )

            resp = c.post(
                "/api/admin/access-control/decision",
                json={
                    "actor_fingerprint": "fp_kill",
                    "source_ip": "10.0.0.5",
                    "decision": "deny",
                },
                headers=_headers(),
            )
            assert resp.status_code == 200
            assert "sess-1" not in manager._sessions
            mock_client.close.assert_called_once()

    def test_deny_only_kills_matching_actor(self, monkeypatch):
        """Deny should not disconnect sessions with different actor or IP."""
        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            manager = _state.manager
            manager._sessions["sess-other"] = MagicMock(
                session_id="sess-other",
                client=MagicMock(),
                host="10.0.0.6",
                port=22,
                username="root",
                owner_token_fingerprint="fp_other",
                source_ip="10.0.0.6",
                owner_type="master",
                owner_name=None,
                connected_at=0,
                last_activity=0,
                reconnect_count=0,
                last_reconnect_reason=None,
            )

            resp = c.post(
                "/api/admin/access-control/decision",
                json={
                    "actor_fingerprint": "fp_kill",
                    "source_ip": "10.0.0.5",
                    "decision": "deny",
                },
                headers=_headers(),
            )
            assert resp.status_code == 200
            assert "sess-other" in manager._sessions

    def test_deny_stores_internal_denied_and_blocks(self, monkeypatch):
        """POST deny stores 'denied' internally; subsequent access check blocks."""
        from app.access_control import AccessDeniedError

        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            resp = c.post(
                "/api/admin/access-control/decision",
                json={
                    "actor_fingerprint": "fp_block",
                    "source_ip": "10.0.0.77",
                    "decision": "deny",
                    "reason": "regression test",
                },
                headers=_headers(),
            )
            assert resp.status_code == 200
            assert resp.json()["decision"] == "deny"

        entry = _state.access_control_store.get("fp_block", "10.0.0.77")
        assert entry is not None
        assert entry.decision == "denied"

        with pytest.raises(AccessDeniedError):
            _state.access_control_store.resolve_access_policy(
                actor_fingerprint="fp_block",
                token_type="agent",
                source_ip="10.0.0.77",
                requested_profile="default",
            )


# ---------------------------------------------------------------------------
# Unit test for disconnect_sessions_for_actor_source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_sessions_for_actor_source_direct():
    """Direct unit test for the helper function."""
    from app.ssh_manager import disconnect_sessions_for_actor_source

    manager = SSHSessionManager(cleanup_interval=3600)
    try:
        mock_client = MagicMock()
        manager._sessions["match-1"] = MagicMock(
            session_id="match-1",
            client=mock_client,
            host="10.0.0.5",
            port=22,
            username="root",
            owner_token_fingerprint="fp_target",
            source_ip="10.0.0.5",
            owner_type="agent",
            owner_name=None,
            connected_at=0,
            last_activity=0,
            reconnect_count=0,
            last_reconnect_reason=None,
        )
        manager._sessions["no-match"] = MagicMock(
            session_id="no-match",
            client=MagicMock(),
            host="10.0.0.6",
            port=22,
            username="root",
            owner_token_fingerprint="fp_other",
            source_ip="10.0.0.6",
            owner_type="agent",
            owner_name=None,
            connected_at=0,
            last_activity=0,
            reconnect_count=0,
            last_reconnect_reason=None,
        )

        count = await disconnect_sessions_for_actor_source(manager, "fp_target", "10.0.0.5")
        assert count == 1
        assert "match-1" not in manager._sessions
        assert "no-match" in manager._sessions
        mock_client.close.assert_called_once()
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_create_session_stores_source_ip():
    """create_session propagates source_ip to SessionRecord."""
    manager = SSHSessionManager(cleanup_interval=3600)
    try:
        mock_client = MagicMock()
        mock_transport = MagicMock()
        mock_client.get_transport.return_value = mock_transport

        import paramiko

        original_connect = paramiko.SSHClient.connect

        def fake_connect(self, *a, **kw):
            pass

        paramiko.SSHClient.connect = fake_connect
        try:
            session_id = await manager.create_session(
                host="10.0.0.5",
                port=22,
                username="root",
                owner_type="agent",
                owner_token_fingerprint="fp_src_test",
                source_ip="192.168.1.99",
            )
            record = manager._sessions[session_id]
            assert record.source_ip == "192.168.1.99"
            assert record.owner_token_fingerprint == "fp_src_test"
        finally:
            paramiko.SSHClient.connect = original_connect
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_disconnect_sessions_for_actor_source_with_real_source_ip():
    """deny kills sessions created via create_session with source_ip."""
    from app.ssh_manager import disconnect_sessions_for_actor_source

    manager = SSHSessionManager(cleanup_interval=3600)
    try:
        mock_client = MagicMock()
        manager._sessions["real-sess"] = MagicMock(
            session_id="real-sess",
            client=mock_client,
            host="10.0.0.5",
            port=22,
            username="root",
            owner_token_fingerprint="fp_real",
            source_ip="10.0.0.99",
            owner_type="agent",
            owner_name=None,
            connected_at=0,
            last_activity=0,
            reconnect_count=0,
            last_reconnect_reason=None,
        )

        count = await disconnect_sessions_for_actor_source(manager, "fp_real", "10.0.0.99")
        assert count == 1
        assert "real-sess" not in manager._sessions
        mock_client.close.assert_called_once()
    finally:
        await manager.close_all()


# ---------------------------------------------------------------------------
# GET /api/admin/access-control/recent
# ---------------------------------------------------------------------------


class TestAdminRecent:
    def test_recent_requires_master(self, monkeypatch):
        with _client(monkeypatch) as c:
            resp = c.get("/api/admin/access-control/recent")
        assert resp.status_code == 401

    def test_recent_returns_decisions(self, monkeypatch):
        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            # Seed a decision
            c.post(
                "/api/admin/access-control/decision",
                json={
                    "actor_fingerprint": "fp_recent",
                    "source_ip": "10.0.0.88",
                    "decision": "deny",
                    "reason": "recent test",
                },
                headers=_headers(),
            )
            resp = c.get(
                "/api/admin/access-control/recent",
                headers=_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        decisions = body["decisions"]
        assert any(d["actor_fingerprint"] == "fp_recent" for d in decisions)
        first = decisions[0]
        assert first["decision"] == "denied"
        assert first["ttl_seconds_remaining"] > 0
        assert first["source_ip"] == "10.0.0.88"

    def test_recent_limit_cap(self, monkeypatch):
        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            resp = c.get(
                "/api/admin/access-control/recent",
                params={"limit": 1},
                headers=_headers(),
            )
        assert resp.status_code == 200
        assert len(resp.json()["decisions"]) <= 1

    def test_recent_decision_filter(self, monkeypatch):
        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            c.post(
                "/api/admin/access-control/decision",
                json={"actor_fingerprint": "fp_allow", "source_ip": "10.0.0.1", "decision": "allow"},
                headers=_headers(),
            )
            resp = c.get(
                "/api/admin/access-control/recent",
                params={"decision": "denied"},
                headers=_headers(),
            )
        body = resp.json()
        assert not any(d["actor_fingerprint"] == "fp_allow" for d in body["decisions"])

    def test_recent_sort_oldest(self, monkeypatch):
        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            c.post(
                "/api/admin/access-control/decision",
                json={"actor_fingerprint": "fp_first", "source_ip": "10.0.0.1", "decision": "deny"},
                headers=_headers(),
            )
            import time as _time
            _time.sleep(0.01)
            c.post(
                "/api/admin/access-control/decision",
                json={"actor_fingerprint": "fp_second", "source_ip": "10.0.0.2", "decision": "deny"},
                headers=_headers(),
            )
            resp = c.get(
                "/api/admin/access-control/recent",
                params={"sort": "oldest"},
                headers=_headers(),
            )
        body = resp.json()
        fps = [d["actor_fingerprint"] for d in body["decisions"]]
        assert "fp_first" in fps
        assert "fp_second" in fps
        assert fps.index("fp_first") < fps.index("fp_second")


# ---------------------------------------------------------------------------
# POST /api/admin/access-control/clear
# ---------------------------------------------------------------------------


class TestAdminClear:
    def test_clear_requires_master(self, monkeypatch):
        with _client(monkeypatch) as c:
            resp = c.post(
                "/api/admin/access-control/clear",
                json={"actor_fingerprint": "fp", "source_ip": "10.0.0.1"},
            )
        assert resp.status_code == 401

    def test_clear_removes_decision(self, monkeypatch):
        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            # Set deny first
            c.post(
                "/api/admin/access-control/decision",
                json={"actor_fingerprint": "fp_clear", "source_ip": "10.0.0.77", "decision": "deny"},
                headers=_headers(),
            )
            # Clear it
            resp = c.post(
                "/api/admin/access-control/clear",
                json={"actor_fingerprint": "fp_clear", "source_ip": "10.0.0.77", "reason": "smoke"},
                headers=_headers(),
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["cleared"] is True

            # Verify gone from recent
            resp2 = c.get(
                "/api/admin/access-control/recent",
                params={"decision": "denied"},
                headers=_headers(),
            )
            found = any(
                d["actor_fingerprint"] == "fp_clear"
                for d in resp2.json().get("decisions", [])
            )
            assert not found

    def test_clear_returns_false_when_missing(self, monkeypatch):
        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            resp = c.post(
                "/api/admin/access-control/clear",
                json={"actor_fingerprint": "fp_never", "source_ip": "10.0.0.1"},
                headers=_headers(),
            )
            assert resp.status_code == 200
            assert resp.json()["cleared"] is False

    def test_clear_tuple_returns_pending(self, monkeypatch):
        _patch_auth(monkeypatch)
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            # Deny, then clear
            c.post(
                "/api/admin/access-control/decision",
                json={"actor_fingerprint": "fp_pend", "source_ip": "10.0.0.55", "decision": "deny"},
                headers=_headers(),
            )
            c.post(
                "/api/admin/access-control/clear",
                json={"actor_fingerprint": "fp_pend", "source_ip": "10.0.0.55"},
                headers=_headers(),
            )
        # Verify pending semantics
        entry = _state.access_control_store.get("fp_pend", "10.0.0.55")
        assert entry is None
        result = _state.access_control_store.resolve_access_policy(
            actor_fingerprint="fp_pend",
            token_type="agent",
            source_ip="10.0.0.55",
            requested_profile="ops",
        )
        assert result.state == "pending"


# ---------------------------------------------------------------------------
# Structured audit event types
# ---------------------------------------------------------------------------


class TestAuditEventTypes:
    def test_decision_event_type(self, monkeypatch):
        from unittest.mock import MagicMock as _MagicMock
        _patch_auth(monkeypatch)
        event_logger = _MagicMock()
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            import app.state as __state
            original = getattr(__state, "event_audit_logger", None)
            __state.event_audit_logger = event_logger
            try:
                c.post(
                    "/api/admin/access-control/decision",
                    json={"actor_fingerprint": "fp_audit", "source_ip": "10.0.0.99", "decision": "allow"},
                    headers=_headers(),
                )
            finally:
                __state.event_audit_logger = original
        event_logger.append.assert_called_once()
        event = event_logger.append.call_args[0][0]
        assert event.event_type == "access_control.decision"
        assert event.event_type != "system.error"
        assert event.decision == "allowed"

    def test_clear_event_type(self, monkeypatch):
        from unittest.mock import MagicMock as _MagicMock
        _patch_auth(monkeypatch)
        event_logger = _MagicMock()
        with TestClient(_get_app(), raise_server_exceptions=False) as c:
            import app.state as __state
            original = getattr(__state, "event_audit_logger", None)
            __state.event_audit_logger = event_logger
            try:
                c.post(
                    "/api/admin/access-control/clear",
                    json={"actor_fingerprint": "fp_clear_audit", "source_ip": "10.0.0.77", "reason": "test"},
                    headers=_headers(),
                )
            finally:
                __state.event_audit_logger = original
        event_logger.append.assert_called_once()
        event = event_logger.append.call_args[0][0]
        assert event.event_type == "access_control.clear"
        assert event.event_type != "system.error"
