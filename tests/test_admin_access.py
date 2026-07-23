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
            assert entry.decision == "allow"

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
            assert entry.decision == "allow"
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
